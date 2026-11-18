"""Checks smc_engine.detect() against the Backtrader SmartMoneyConcepts indicator."""
import os
import backtrader as bt
import pandas as pd
import smc_engine
from smc_indicator import SmartMoneyConcepts

BASE = os.path.dirname(os.path.abspath(__file__))


class Probe(bt.Strategy):
    def __init__(self):
        self.smc = SmartMoneyConcepts(lookback=2, impulse_threshold=1.5)
        self.rows = []

    def next(self):
        s = self.smc
        self.rows.append((len(self) - 1, int(s.is_bullish_ob[0]), int(s.is_bearish_ob[0]),
                          int(s.ob_offset[0]), float(s.ob_top[0]), float(s.ob_bottom[0]),
                          int(s.ob_has_fvg[0])))


def main():
    df = pd.read_csv(os.path.join(BASE, "BTC_USDT_1h.csv"), parse_dates=["datetime"]).set_index("datetime")
    cerebro = bt.Cerebro(stdstats=False)
    cerebro.addstrategy(Probe)
    cerebro.adddata(bt.feeds.PandasData(dataname=df, openinterest=None))
    probe = cerebro.run()[0]

    bt_obs = [(r[0], "bull" if r[1] else "bear", r[0] - r[3], round(r[4], 4), round(r[5], 4), bool(r[6]))
              for r in probe.rows if r[1] or r[2]]
    py_obs = [(o["confirm"], o["side"], o["candle"], round(o["top"], 4), round(o["bottom"], 4), o["has_fvg"])
              for o in smc_engine.detect(df.reset_index())["order_blocks"]]
    print(f"backtrader OBs: {len(bt_obs)}   engine OBs: {len(py_obs)}")
    assert bt_obs == py_obs, f"MISMATCH\nbt: {bt_obs[:5]}\npy: {py_obs[:5]}"
    print("PARITY OK")


if __name__ == "__main__":
    main()
