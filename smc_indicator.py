import backtrader as bt


class SmartMoneyConcepts(bt.Indicator):
    """
    Custom Smart Money Concepts (SMC) indicator for Backtrader.

    Detects:
      - Swing highs / swing lows (fractal-style pivots)
      - Bullish / bearish Order Blocks (OB) formed after an impulsive
        Break of Structure (BOS)
      - Bullish / bearish Fair Value Gaps (FVG) — 3-candle imbalance
        gaps that often sit inside/near an Order Block
    """

    # NOTE: a bt.Indicator can only declare ONE `lines` tuple. The original
    # file declared it twice, so the first assignment (swing_high,
    # swing_low, ob_top, ob_bottom, is_bullish_ob, is_bearish_ob) was
    # silently thrown away and code elsewhere that read those lines would
    # have crashed with a LineSeries error. All lines now live together.
    lines = (
        'swing_high',
        'swing_low',
        'ob_top',
        'ob_bottom',
        'is_bullish_ob',
        'is_bearish_ob',
        'bullish_fvg',
        'bearish_fvg',
        'ob_offset',      # bars back (as a positive int) from the confirmation bar to the OB candle
        'ob_has_fvg',     # 1 if a same-direction FVG formed between the OB candle and now
    )

    params = (
        ('lookback', 2),
        ('impulse_threshold', 1.5),  # how big a move must be to count as "impulsive"
    )

    OB_SEARCH_BARS = 10

    def __init__(self):
        # addminperiod needs enough bars for the swing lookback window
        # AND for the 3-candle FVG check.
        # The OB search looks back up to OB_SEARCH_BARS candles and the
        # impulse check averages 5 candles, so wait for enough history —
        # otherwise negative indexes run off the start of the buffer.
        self.addminperiod(max(self.p.lookback * 2 + 1, self.OB_SEARCH_BARS + 2))

        # State variables used to track the most recent swing points so we
        # can detect a Break of Structure (BOS) later.
        self.last_swing_high = None
        self.last_swing_low = None
        self.last_swing_high_index = 0
        self.last_swing_low_index = 0

    def next(self):
        # Initialize default (flat) values for this bar.
        self.lines.swing_high[0] = 0
        self.lines.swing_low[0] = 0
        self.lines.ob_top[0] = 0
        self.lines.ob_bottom[0] = 0
        self.lines.is_bullish_ob[0] = 0
        self.lines.is_bearish_ob[0] = 0
        self.lines.bullish_fvg[0] = 0
        self.lines.bearish_fvg[0] = 0
        self.lines.ob_offset[0] = 0
        self.lines.ob_has_fvg[0] = 0

        check_index = -self.p.lookback

        # --- 1. DETECT SWING POINTS -----------------------------------
        check_high = self.data.high[check_index]
        is_sh = True
        for i in range(-self.p.lookback * 2, 1):
            if i != check_index and self.data.high[i] >= check_high:
                is_sh = False
                break
        if is_sh:
            self.lines.swing_high[check_index] = 1
            self.last_swing_high = check_high
            self.last_swing_high_index = len(self) + check_index

        check_low = self.data.low[check_index]
        is_sl = True
        for i in range(-self.p.lookback * 2, 1):
            if i != check_index and self.data.low[i] <= check_low:
                is_sl = False
                break
        if is_sl:
            self.lines.swing_low[check_index] = 1
            self.last_swing_low = check_low
            self.last_swing_low_index = len(self) + check_index

        # --- 2. DETECT FAIR VALUE GAPS (3-candle imbalance) ------------
        # Bullish FVG: low of the current candle is above the high of the
        # candle 2 bars ago (a gap between candle[-2] and candle[0]).
        if self.data.low[0] > self.data.high[-2]:
            self.lines.bullish_fvg[-1] = 1
        if self.data.high[0] < self.data.low[-2]:
            self.lines.bearish_fvg[-1] = 1

        # --- 3. DETECT ORDER BLOCKS -------------------------------------
        # An OB is flagged on the bar where the impulsive break is
        # CONFIRMED (bar 0), so the strategy sees it immediately and with
        # no look-ahead. ob_top / ob_bottom hold the OB candle's range and
        # ob_offset says how many bars back that candle is.
        #
        # Bullish OB: close breaks above the last swing high impulsively;
        # the OB is the last down (red) candle before that move.
        search = range(1, self.OB_SEARCH_BARS + 1)
        avg_candle_size = sum(
            self.data.high[-i] - self.data.low[-i] for i in range(1, 6)
        ) / 5

        if self.last_swing_high is not None and self.data.close[0] > self.last_swing_high:
            move_size = (self.data.close[0] - self.last_swing_low
                         if self.last_swing_low is not None else 0)
            if avg_candle_size > 0 and move_size > avg_candle_size * self.p.impulse_threshold:
                for i in search:
                    if self.data.close[-i] < self.data.open[-i]:  # down candle
                        self.lines.is_bullish_ob[0] = 1
                        self.lines.ob_top[0] = self.data.high[-i]
                        self.lines.ob_bottom[0] = self.data.low[-i]
                        self.lines.ob_offset[0] = i
                        self.lines.ob_has_fvg[0] = int(any(
                            self.lines.bullish_fvg[-j] == 1 for j in range(1, i + 1)))
                        self.last_swing_high = None
                        break

        # Bearish OB: close breaks below the last swing low impulsively;
        # the OB is the last up (green) candle before that move.
        if self.last_swing_low is not None and self.data.close[0] < self.last_swing_low:
            move_size = (self.last_swing_high - self.data.close[0]
                         if self.last_swing_high is not None else 0)
            if avg_candle_size > 0 and move_size > avg_candle_size * self.p.impulse_threshold:
                for i in search:
                    if self.data.close[-i] > self.data.open[-i]:  # up candle
                        self.lines.is_bearish_ob[0] = 1
                        # Bullish and bearish OBs on the same bar are
                        # essentially impossible, but never clobber one.
                        if not self.lines.is_bullish_ob[0]:
                            self.lines.ob_top[0] = self.data.high[-i]
                            self.lines.ob_bottom[0] = self.data.low[-i]
                            self.lines.ob_offset[0] = i
                            self.lines.ob_has_fvg[0] = int(any(
                                self.lines.bearish_fvg[-j] == 1 for j in range(1, i + 1)))
                        self.last_swing_low = None
                        break
