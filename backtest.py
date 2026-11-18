import argparse
import csv
import os
import warnings

import backtrader as bt
import joblib  # Required to load the ML model

from smc_indicator import SmartMoneyConcepts

# Suppress scikit-learn warnings about feature names during prediction
warnings.filterwarnings("ignore", category=UserWarning)

# Resolve files relative to this script so it works from any directory.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FEATURES_FILE = os.path.join(BASE_DIR, 'smc_features_dataset.csv')
MODEL_FILE = os.path.join(BASE_DIR, 'smc_svm_model.pkl')
DATA_FILE = os.path.join(BASE_DIR, 'BTC_USDT_1h.csv')
CSV_COLUMNS = ['ob_height', 'volume_ratio', 'impulse_size', 'has_fvg', 'outcome']

HOLD_BARS = 5          # arbitrary time exit: close N bars after entry
PENDING_EXPIRY = 100   # drop an un-mitigated OB after this many bars


class SMC_ML_Strategy(bt.Strategy):
    """
    Trades Order Blocks found by SmartMoneyConcepts, in both directions:
      * bullish OB  -> BUY  when price returns to the OB (stop: close below the OB)
      * bearish OB  -> SELL when price returns to the OB (stop: close above the OB)
    `sides` = 'both' | 'long' | 'short' picks which directions are traded.

    Two modes:
      * collect (no model, or --collect): every OB is traded and each
        closed trade's features + outcome are written to the dataset CSV
        (which is rewritten from scratch, so reruns never duplicate rows).
      * filtered (model present): the SVM approves/rejects each OB.
        Nothing is logged, so the training set is never polluted with
        model-selected trades.
    """
    params = (('collect', False), ('write_dataset', True), ('verbose', True), ('sides', 'both'))

    def __init__(self):
        self.smc = SmartMoneyConcepts(lookback=2, impulse_threshold=1.5)
        self.pending_trade = False
        self.pending_features = None
        self.pending_bar = 0
        self.pending_dir = 'long'
        self.mitigation_level = 0.0
        self.entry_dir = None       # direction of the order about to fill / the open trade
        self._entry_order_open = False
        self.invalidation_level = 0.0
        self.active_features = None
        self.bar_executed = 0

        self.model = None
        if not self.p.collect:
            try:
                self.model = joblib.load(MODEL_FILE)
                self._say("[+] ML Model loaded successfully into trading engine.")
            except FileNotFoundError:
                self._say("[!] No smc_svm_model.pkl found yet — running unfiltered "
                      "to collect training data.")

        self.collecting = self.model is None
        if self.collecting and self.p.write_dataset:
            with open(FEATURES_FILE, 'w', newline='') as f:
                csv.writer(f).writerow(CSV_COLUMNS)

        # Records for the web dashboard (harmless for the CLI).
        self.trade_log = []
        self.equity_curve = []
        self.signal_log = []
        self._entry_time = None
        self._exit_price = None

    def _say(self, msg):
        if self.p.verbose:
            print(msg)

    def next(self):
        self.equity_curve.append((self.data.datetime.datetime(0), self.broker.getvalue()))
        # 1. A new Order Block is confirmed on this bar (bullish -> long, bearish -> short).
        new_dir = ('long' if self.smc.is_bullish_ob[0] == 1 else
                   'short' if self.smc.is_bearish_ob[0] == 1 else None)
        if new_dir and self.p.sides in ('both', new_dir) and not self.position:
            offset = int(self.smc.ob_offset[0])      # bars back to the OB candle
            ob_top = self.smc.ob_top[0]
            ob_bottom = self.smc.ob_bottom[0]

            n = min(20, len(self))
            avg_volume = sum(self.data.volume.get(size=n)) / n
            volume_ratio = self.data.volume[-offset] / avg_volume if avg_volume > 0 else 1.0

            # Features are magnitudes, so shorts are the mirror image of longs.
            impulse = (self.data.close[0] - ob_top) if new_dir == 'long' else (ob_bottom - self.data.close[0])
            features = [
                ob_top - ob_bottom,                  # ob_height
                volume_ratio,                        # volume_ratio
                impulse,                             # impulse_size
                int(self.smc.ob_has_fvg[0]),         # has_fvg
            ]

            take_trade = True
            if self.model is not None:
                take_trade = self.model.predict([features])[0] == 1
                tag = "AI Approved" if take_trade else "AI Rejected"
                self._say(f"{self.data.datetime.date(0)}: {new_dir} OB detected -> {tag}")
            else:
                self._say(f"{self.data.datetime.date(0)}: {new_dir} OB detected -> "
                          f"collecting data (no model loaded)")

            self.signal_log.append({
                'time': self.data.datetime.datetime(0), 'features': features, 'side': new_dir,
                'approved': bool(take_trade), 'ai': self.model is not None})

            if take_trade:
                self.pending_trade = True
                self.pending_dir = new_dir
                self.pending_features = features
                self.pending_bar = len(self)
                self.mitigation_level = ob_top if new_dir == 'long' else ob_bottom
                self.invalidation_level = ob_bottom if new_dir == 'long' else ob_top

        # 2. Enter once price returns to mitigate the OB. Drop the setup
        #    if price closes through the OB (invalidated) or it goes stale.
        if not self.position and self.pending_trade and len(self) > self.pending_bar:
            long_ = self.pending_dir == 'long'
            invalidated = (self.data.close[0] < self.invalidation_level) if long_ \
                else (self.data.close[0] > self.invalidation_level)
            if invalidated or len(self) - self.pending_bar > PENDING_EXPIRY:
                self.pending_trade = False
            elif (self.data.low[0] <= self.mitigation_level) if long_ else (self.data.high[0] >= self.mitigation_level):
                self.entry_dir = self.pending_dir
                self._entry_order_open = True
                self.buy() if long_ else self.sell()
                self.active_features = self.pending_features
                self.pending_trade = False

        # 3. Basic exit logic: close HOLD_BARS bars after the entry fill.
        if self.position and len(self) >= self.bar_executed + HOLD_BARS:
            self.close()

    def notify_order(self, order):
        if order.status == order.Completed:
            if self._entry_order_open:       # the order that opens the trade (BUY for longs, SELL for shorts)
                self._entry_order_open = False
                self.bar_executed = len(self)
                self._entry_time = self.data.datetime.datetime(0)
                self._say(f"--> {self.data.datetime.date(0)}: {'BUY' if order.isbuy() else 'SELL'} FILLED at "
                          f"{order.executed.price:.2f}")
            else:
                self._exit_price = order.executed.price
        elif order.status in (order.Canceled, order.Margin, order.Rejected):
            self._say(f"[!] Order failed: {order.getstatusname()}")
            self._entry_order_open = False
            self.active_features = None

    def notify_trade(self, trade):
        # Label from the real result (net of commission), not a signal-bar guess.
        if trade.isclosed:
            self.trade_log.append({
                'entry_time': self._entry_time, 'exit_time': self.data.datetime.datetime(0),
                'entry_price': trade.price, 'exit_price': self._exit_price,
                'side': 'long' if trade.long else 'short',
                'size': trade.size if trade.size else None,
                'pnl': trade.pnlcomm, 'features': self.active_features,
            })
        if trade.isclosed and self.collecting and self.active_features and trade.long:
            outcome = 1 if trade.pnlcomm > 0 else 0
            if self.p.write_dataset:
                with open(FEATURES_FILE, 'a', newline='') as f:
                    csv.writer(f).writerow(self.active_features + [outcome])
            self.active_features = None


def run_backtest(df, use_ai=True, cash=10000.0, commission=0.001, percent=90, sides='both'):
    """
    Run the strategy on a pandas DataFrame (columns: datetime index or column,
    open/high/low/close/volume) WITHOUT touching the dataset CSV. Used by the
    web dashboard. Returns trades, equity curve and signals.
    """
    import pandas as pd
    d = df.copy()
    if 'datetime' in d.columns:
        d = d.set_index('datetime')
    d.index = pd.to_datetime(d.index)

    cerebro = bt.Cerebro(stdstats=False)
    # collect=True ignores the model (trades every OB); use_ai False -> that.
    cerebro.addstrategy(SMC_ML_Strategy, collect=not use_ai,
                        write_dataset=False, verbose=False, sides=sides)
    cerebro.broker.setcash(cash)
    cerebro.broker.setcommission(commission=commission)
    cerebro.addsizer(bt.sizers.PercentSizer, percents=percent)
    cerebro.adddata(bt.feeds.PandasData(dataname=d, openinterest=None))
    strat = cerebro.run()[0]
    return {
        'trades': strat.trade_log,
        'equity': strat.equity_curve,
        'signals': strat.signal_log,
        'ai_active': strat.model is not None,
        'start_cash': cash,
        'final_value': cerebro.broker.getvalue(),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--collect', action='store_true',
                        help='ignore any saved model; trade every OB and rebuild the dataset')
    parser.add_argument('--no-plot', action='store_true')
    parser.add_argument('--sides', choices=['both', 'long', 'short'], default='both',
                        help='trade buys, sells, or both (the training dataset only ever records buy trades)')
    args = parser.parse_args()

    cerebro = bt.Cerebro()
    cerebro.addstrategy(SMC_ML_Strategy, collect=args.collect, sides=args.sides)

    cerebro.broker.setcash(10000.0)
    cerebro.broker.setcommission(commission=0.001)  # 0.1% per trade
    # Default sizer buys 1 whole unit (1 BTC) which a $10k account can't afford.
    cerebro.addsizer(bt.sizers.PercentSizer, percents=90)

    if not os.path.exists(DATA_FILE):
        print(f"Data file '{DATA_FILE}' not found. Run fetch_data.py first.")
        return
    cerebro.adddata(bt.feeds.GenericCSVData(
        dataname=DATA_FILE,
        dtformat='%Y-%m-%d %H:%M:%S',
        timeframe=bt.TimeFrame.Minutes,
        compression=60,                 # hourly candles
        open=1, high=2, low=3, close=4, volume=5,
        openinterest=-1,
    ))

    print("--- Starting ML-Powered Backtest ---")
    start_value = cerebro.broker.getvalue()
    print(f"Starting Portfolio Value: ${start_value:,.2f}")

    cerebro.run()

    final_value = cerebro.broker.getvalue()
    print(f"Final Portfolio Value: ${final_value:,.2f}")
    print(f"Total Return on Investment (ROI): "
          f"{(final_value - start_value) / start_value * 100:.2f}%")

    if not (args.no_plot or os.environ.get('SMC_NO_PLOT') == '1'):
        try:
            print("Generating chart...")
            cerebro.plot(style='candlestick', barup='green', bardown='red', volume=False)
        except Exception as e:
            print(f"[i] Skipping plot (no display available): {e}")


if __name__ == '__main__':
    main()
