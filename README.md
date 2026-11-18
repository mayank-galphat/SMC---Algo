# SMC Algo Trading — Fixed & Working

## What this project does
An ML-filtered "Smart Money Concepts" (SMC) trading engine, built on
Backtrader:

1. `smc_indicator.py` — detects swing highs/lows, Order Blocks (OB), and
   Fair Value Gaps (FVG) on OHLCV candles.
2. `backtest.py` — runs a Backtrader strategy that trades detected Order
   Blocks, optionally filtered by a trained ML model, and logs every
   trade's features + outcome to `smc_features_dataset.csv`.
3. `train_model.py` — trains an SVM classifier on that logged data to
   predict whether an Order Block is likely to be a winning trade.
4. `fetch_data.py` — pulls fresh OHLCV candles from Binance via `ccxt`.

Typical loop: `fetch_data.py` → `python backtest.py --collect` (trade every OB,
rebuild dataset) → `train_model.py` → `python backtest.py` (AI-filtered) → repeat.

## Web dashboard (SMC Terminal)

```bash
pip install -r requirements.txt
python app.py            # then open http://127.0.0.1:5000
```

- **Live chart**: Binance candles stream over WebSocket into TradingView's
  open-source *Lightweight Charts* (vendored in `static/vendor/`, no CDN needed).
  Symbols BTC/ETH/SOL/BNB/XRP, timeframes 5m to 1d.
- **SMC overlays**: Order Blocks (bullish/bearish, active/mitigated/invalidated),
  unfilled Fair Value Gaps, swing points and backtest trade markers, each
  toggleable. The OB labels show whether the SVM would approve the setup.
- **TradingView tab**: embeds the full TradingView Advanced Chart widget
  (drawing tools, indicators) for the same market and timeframe.
- **Setups panel**: every detected Order Block with status, range, FVG flag and
  AI verdict; click one to jump the chart to it.
- **Backtest panel**: runs the real Backtrader strategy from `backtest.py`
  (SVM filter on/off) and shows return, win rate, profit factor, drawdown,
  equity curve and trades. It never writes to `smc_features_dataset.csv`.
- `smc_engine.py` is a pandas port of `smc_indicator.py` so the web UI can run
  fast; `python test_engine_parity.py` proves both find identical Order Blocks.
- If Binance can't be reached, the server falls back to the bundled
  `BTC_USDT_1h.csv` (BTC/USDT 1h only) and the UI shows "Replay".
- The SVM was trained on the same history it is backtested on, so the filtered
  results are in-sample. Treat them as a demo, not evidence of an edge.

## Live trading on YOUR Angel One account (new, off by default)

The **Live** tab sends real NSE cash-equity orders to **your own** Angel One account through SmartAPI (`angel.py`).
It is single-account on purpose: Angel One's retail API is for one person trading their own account (one API
profile = one trading account, orders only from a registered **static IP**). It cannot trade other people's accounts;
that needs a licensed algo-provider arrangement. Check SEBI/exchange rules before offering any of this to others.

Setup
1. Angel One: enable SmartAPI, create an app to get the **API key**, enable **TOTP** and keep its secret
   (the text code shown with the QR), and register your server's **static IPv4** in SmartAPI.
2. Set environment variables on the server (never in the browser or code):
```
SMC_ANGEL_API_KEY=...  SMC_ANGEL_CLIENT_CODE=A123456  SMC_ANGEL_PIN=....  SMC_ANGEL_TOTP_SECRET=BASE32SECRET
SMC_ANGEL_PUBLIC_IP=your.static.ip        SMC_LIVE_EMAILS=you@example.com   # only these logins see the Live tab
SMC_LIVE_MAX_ORDER_VALUE=10000            SMC_LIVE_MAX_ORDERS_PER_DAY=10    # optional limits (defaults shown)
```
3. Start the app. It runs in **DRY-RUN**: every order goes through all checks and the confirmation box but nothing is
   sent. Confirm that your cash and holdings load, and run a few dry-runs.
4. Only then set `SMC_ANGEL_LIVE=1` (real orders) and start with 1 share.

Safeguards: allow-listed e-mails only; NSE `-EQ` stocks, whole shares, MARKET/LIMIT, Delivery/Intraday; two-step
preview then confirm with a signed single-use 90-second token; real orders need a tick-box; per-order value and daily
order caps; limit price must be within 20% of the last price; **emergency stop** button (or `SMC_LIVE_KILL=1`);
every attempt is stored in the `live_orders` table. Use HTTPS, and treat the PIN/TOTP secret like a bank password.
Tests: `python test_angel.py` (fake broker). Not verified against a real Angel One account from the development
environment: do the dry-run first. Not investment advice; you are responsible for your orders.

## Practice trading: buy and sell with virtual money (new)

The **Trade** tab lets each user buy and sell the symbol on the chart with **virtual money** at live prices.
Nothing real is bought, sold or charged. Wallets: **INR** (NSE/BSE stocks, default 10,00,000) and **USD**
(US stocks, ETFs and crypto, default 10,000); change with `SMC_PAPER_INR` / `SMC_PAPER_USD`.
Rules: long-only (you can sell only what you hold), whole shares for stocks and fractions for crypto, a simulated
0.05% fee, no indices/forex/futures. The server chooses the fill price (latest 5-minute close; stocks may be delayed
or the market closed) and checks cash and holdings, so the browser cannot name its own price; orders are atomic, so a
double-click cannot overspend. Positions show live P&L; **Reset practice account** starts over.
Tests: `python test_paper.py`. Code: `paper.py`.

Real trading is deliberately not included. Placing real orders needs a broker (Zerodha Kite Connect, Upstox, Angel One,
Alpaca, ...) with each user connecting *their own* account, and holding customers' money or shares yourself is
regulated (SEBI in India). `paper.py` is built so a broker connector can replace the price/fill step later.

## Payments and Pro plan (new)

Free users get the SMC chart, TradingView and every market. **Pro** unlocks the **ICT Chart**, **Backtesting** and
**Supply & Demand zones**. This is enforced on the server (the API answers 402), not just hidden in the UI.
Edit `PRO_FEATURES` in `billing.py` and the `billing.gate(...)` calls in `app.py` to change what is paid.

Gateway: **Razorpay** (UPI, cards, netbanking, wallets; INR). Pro is a one-time pass, 1 month or 12 months, not
auto-renewing; buying again extends the date. Prices default to 499 / 4999 and are placeholders: set your own.

Setup
1. Create a Razorpay account (complete KYC for live mode). Start with the **test** keys (`rzp_test_...`).
2. Dashboard -> Settings -> Payment capture: set to **automatic** so paid orders are captured.
3. Dashboard -> Webhooks -> add `https://YOUR-DOMAIN/api/billing/webhook`, choose events `payment.captured` and
   `order.paid`, and set a secret. The webhook is the safety net that grants Pro even if the buyer closes the tab.
4. Set environment variables, then start the app:
```
SMC_RAZORPAY_KEY_ID=rzp_test_xxx        SMC_RAZORPAY_KEY_SECRET=xxx
SMC_RAZORPAY_WEBHOOK_SECRET=xxx
SMC_PRICE_MONTHLY=499  SMC_PRICE_YEARLY=4999  SMC_CURRENCY=INR     # optional
SMC_ADMIN_EMAILS=you@example.com                                    # optional: always Pro
SMC_HTTPS=1                                                         # when served over HTTPS
```
`SMC_BILLING_MODE=demo` simulates payments with no money (to try the screens); `off` unlocks everything.
With no keys and no mode set, billing is off, so existing installs keep working unchanged.

Security: prices are set on the server; Razorpay's HMAC signature is verified for both the checkout result and the
webhook; an order can only be claimed by the user who created it; grants are idempotent (replays never extend twice);
card data never touches this server. Run `python test_billing.py` (22 checks, fake gateway, no network).
Still to do before charging real customers: HTTPS, your refund/terms/privacy pages, and GST invoicing if you are registered.
Webhook/order state lives in `users.db` (tables `payments`, `entitlements`); back it up.

## Buy and Sell trades (new)

The strategy in `backtest.py` now trades Order Blocks in both directions: a **bullish OB -> BUY** and a
**bearish OB -> SELL (short)**, entering when price returns to the block and stopping out on a close through it,
exactly as the buy side always did. In the Backtest tab choose **Buy & Sell / Buy only / Sell only**; the chart
marks BUY (blue, below the bar) and SELL (orange, above the bar), the trade list tags each trade, and a Buy vs Sell
breakdown shows trades, win rate and P&L per direction. CLI: `python backtest.py --sides long|short|both`.
Notes: `smc_features_dataset.csv` still records buy trades only, so `train_model.py` is unchanged. The SVM was
trained on buys; sell trades reuse it on mirrored features, which is not validated. Shorting is simulated on the
spot data with the same 0.1% commission and no borrow or funding costs.

## Supply & Demand zones in the SMC chart (new)

`smc_engine.detect_zones()` finds classic base -> leg-out zones (1-4 small candles, then a strong move away):
**DBR / RBR** demand (teal) and **RBD / DBD** supply (orange). Drawn with a solid *distal* line (stop side) and a
dashed *proximal* line (entry side). Status is `fresh` (never retested), `tested` (retested, still holding) or
`broken` (a close beyond the distal; hidden unless **Invalidated** is on). Each zone gets a 0-100 strength score
(departure size, tight base, freshness, imbalance, reversal pattern). Toggle with **Supply/Demand** in the layer bar;
the **S/D** tab lists zones nearest to price first and clicking one jumps to it. These are separate from Order Blocks
and do not feed the SVM or the backtest. Thresholds are constants at the top of that section of `smc_engine.py`.

## Markets: search every symbol (new)

The market dropdown is now a search box (`markets.py`, `/api/search`).

- **Crypto**: every Binance spot pair quoted in USDT (several hundred), with the live WebSocket feed.
- **Global stocks, ETFs, indices, forex, futures**: any ticker Yahoo Finance lists, e.g. `RELIANCE.NS`,
  `TCS.NS` (NSE), `AAPL`, `NVDA` (US), `^NSEI` (NIFTY 50), `EURUSD=X`, `GC=F`. Stored as `YF:<ticker>`.
- Yahoo has no public stream, so the chart polls every 30 s. Data can be ~15 min delayed on some exchanges.
- Yahoo limits history: 5m/15m about 60 days, 1h about 2 years; 4h is built from 1h. Stock charts only
  contain exchange hours, and some indices/forex have no volume (volume-based SMC features then read 0).
- Unofficial source, rate-limited: fine for a dashboard, not for order execution.
- The SVM was trained on BTC/USDT 1h only, so its AI verdicts on other markets are not validated.
- **Full listings (run once):** `python build_symbols.py` downloads the official NASDAQ/NYSE/NYSE American/Arca
  directory and every NSE equity into `symbols.csv` (roughly 10,000+ US and 2,000+ Indian securities).
  The picker then searches it instantly and the *US / India / Crypto* tabs let you browse the whole list.
  Re-run occasionally for new listings. Without `symbols.csv` search still works through live Yahoo search.
- **Indices tab** (works without any download): NIFTY 50, Bank Nifty, Next 50, NIFTY IT/Auto/Pharma/FMCG/Metal/Energy,
  India VIX, SENSEX, S&P 500, Dow, NASDAQ 100, FTSE, DAX, CAC, Nikkei, Hang Seng and more. Typing "nifty" or "sensex" finds them.
- **Nifty 50 / Bank Nifty / Next 50 tabs** list the constituent stocks, taken from NSE's official files by
  `build_symbols.py` (re-run it after NSE's semi-annual rebalances). SENSEX constituents are not included
  (BSE offers no free official file); most are also in the Nifty 50 and each trades on NSE as `<TICKER>.NS`.
- Other exchanges (LSE, TSX, HKEX, TSE, BSE, ...) are found through the live Yahoo search box.
- TradingView tab follows the selected market (`NSE:RELIANCE`, `NASDAQ:AAPL`, ...).

## ICT Chart (new)

A third chart button, **ICT Chart**, sits next to *SMC Chart* and *TradingView*. It runs
`ict_engine.py` through `/api/ict` and has its own toolbar and an **ICT** side tab.

| ICT concept | What is drawn |
|---|---|
| Market structure | BOS and CHoCH / MSS lines |
| Order Blocks | last opposing candle before the structure-breaking move, with 50% line; status active / mitigated / invalidated |
| Breaker Blocks | an invalidated OB that flips polarity |
| Fair Value Gaps | BISI (bullish) / SIBI (bearish) with consequent encroachment, partial-fill state |
| Inversion FVG | a gap closed through becomes an opposite-side zone |
| Liquidity | BSL / SSL swing pools, EQH / EQL equal highs & lows, sweep markers |
| Displacement | impulse candles used to qualify MSS and setups |
| Premium / Discount | dealing range, 50% equilibrium, OTE 62-79% zone |
| Key levels | PDH/PDL, PWH/PWL, daily open, weekly open, Asian range (New York day) |
| Kill zones | Asia, London, NY AM, NY PM shading; Silver Bullet windows (intraday only) |
| Entry model | sweep -> MSS/CHoCH + displacement -> retrace into FVG/OB, with entry, SL, TP, R:R, confluence score and simulated win/loss in R |

`python app.py` as before; no new dependencies. Sessions use `America/New_York` (DST aware).
The ICT entry-model results are a simple in-sample simulation (stop hit before target on the same
bar counts as a loss), not a validated edge.

## Accounts (login and signup)

The dashboard and every `/api/*` route now require an account.

```bash
python app.py        # open http://127.0.0.1:5000 -> redirects to /login
```

- **/signup** creates an account (name, email, password with a strength meter) and signs you in.
- **/login** signs in, with an optional "Keep me signed in" (30 days; otherwise the session ends with the browser).
- The avatar menu in the dashboard header has **Sign out**. An expired session sends you back to the login page.
- Users are stored in `users.db` (SQLite, created automatically; set `SMC_DB` to move it). Passwords are
  stored only as salted hashes (werkzeug scrypt). The session signing key is saved to `.secret_key`
  on first run (or set `SMC_SECRET_KEY`). Set `SMC_HTTPS=1` when serving over HTTPS.
- Protections: HttpOnly + SameSite=Lax cookie, JSON-only auth endpoints with an Origin check (CSRF),
  login throttling (5 failures per 5 minutes per IP and email), sign-up throttling (10 per hour per IP),
  and identical error text for a wrong password and an unknown email.
- `python test_auth.py` runs 11 checks against a throw-away database.
- Not included yet: password reset by email and social login (both need an email service or OAuth keys).
- Delete `users.db` and `.secret_key` before sharing the folder; they belong to your machine.

## Bugs found and fixed

| File | Bug | Fix |
|---|---|---|
| `smc_indicator.py` | `lines = (...)` was declared **twice** — the second definition silently overwrote the first, so `swing_high`, `swing_low`, `ob_top`, `ob_bottom`, `is_bullish_ob`, `is_bearish_ob` didn't exist as indicator lines even though the rest of the code and `backtest.py` referenced them. | Merged into a single `lines` tuple with all 8 lines. |
| `smc_indicator.py` | `def __init__(self, lookback=2, impulse_threshold=1.5):` had no body (immediately followed by `params = (...)` at the same indent level) — this is a Python `IndentationError`, the file could not even be imported. | Removed the broken duplicate `__init__`; kept the correct params-based one. |
| `smc_indicator.py` | `bullish_fvg` / `bearish_fvg` lines were declared and *read* in `backtest.py`, but never actually computed anywhere in `next()`. | Added real 3-candle Fair Value Gap detection logic. |
| `backtest.py` | The file had a second, dead `next()` method appended *after* `if __name__ == '__main__':`, outside the class — unreachable code left over from an editing mistake. | Removed; consolidated into one clean `next()`. |
| `backtest.py` | `cerebro.broker.setcash(10000.0)` was set, but **no sizer was configured**. Backtrader's default sizer buys 1 whole unit of the traded instrument — 1 BTC at ~$60k+ — which a $10,000 account can never afford. Every single order was silently rejected with a `Margin` error, so the strategy never actually took a trade. | Added `cerebro.addsizer(bt.sizers.PercentSizer, percents=90)` so each entry is sized against available cash. |
| `backtest.py` | Nothing ever wrote to `smc_features_dataset.csv`, so the "ML feedback loop" was fake — `train_model.py` could only ever train on the 8 hand-typed dummy rows that shipped in the CSV. | Added `_log_trade()`, which appends `[features..., outcome]` to the CSV whenever a position is closed, so the model can be retrained on real backtest results. |
| `backtest.py` / `train_model.py` | `backtest.py` called `self.model.predict([[ob_height, volume_ratio, impulse_size, has_fvg]])` (4 features), but `train_model.py` trained on only `['ob_height', 'volume_ratio', 'impulse_size']` (3 features). This mismatch would crash `.predict()` with a "X has 4 features, but SVC is expecting 3 features" error the moment a model-filtered backtest ran. | `train_model.py` now trains on the same 4 columns, including `has_fvg`. |
| `smc_features_dataset.csv` | Only 8 rows of hand-typed placeholder data — too small to train anything meaningful, and disconnected from the actual indicator/strategy. | Regenerated from a real backtest run (29 trades over the sample data). |

I verified all of this by actually installing `backtrader`, `pandas`,
`scikit-learn`, and `joblib` and running the full pipeline end-to-end:
`backtest.py` (data-collection mode) → `train_model.py` → `backtest.py`
(AI-filtered mode) — trades now execute, outcomes get logged, the model
trains, and the AI-filtered run correctly approves/rejects Order Blocks.

## Second pass — additional bugs fixed

| File | Bug | Fix |
|---|---|---|
| `smc_indicator.py` | OBs were written retroactively onto a past bar (up to 10 back), while the strategy only looked at offset `[-2]` — so each OB was acted on after a variable delay and its features (impulse, volume, FVG) were measured from the wrong bar. | OBs are flagged on the bar they are confirmed; new lines `ob_offset` and `ob_has_fvg` carry the OB candle position and FVG flag. |
| `smc_indicator.py` | Look-back indexing (`range(-5,0)`, OB search up to 11 bars) could run past the start of the data; swing levels tested with truthiness (`if self.last_swing_high`). | Proper min-period; `is not None` checks. |
| `backtest.py` | `entry_price` / outcome used the signal-bar close, but the order fills at the next open; "BUY EXECUTED" printed before any fill. | Fill price printed in `notify_order`; label comes from `notify_trade` (net PnL after commission). |
| `backtest.py` | Dataset file was appended to on every run (67 rows vs the 39 the README claimed), and AI-filtered runs also appended, so the model retrained on its own selections. | Dataset is rebuilt only in collect mode (`--collect` or no model); filtered runs never write. |
| `backtest.py` | Pending OB setups never expired or got invalidated; hourly data was declared as 1-minute bars; file paths depended on the current directory. | Invalidated when price closes below the OB, expires after 100 bars; `compression=60`; paths relative to the script. |
| `train_model.py` | Features not scaled for an RBF SVM; random shuffle split on time-ordered trades; crashed on a single-class dataset; probability=True needlessly slow. | StandardScaler pipeline, chronological 80/20 split, guards for tiny/single-class data. |
| `fetch_data.py` | Kept Binance's still-forming last candle; no error handling; unused import. | Drops the partial candle, catches ccxt errors, saves next to the script. |

Note: with only ~29 trades the model accuracy is not meaningful (see limitations).

## Known limitations (worth calling out, not hidden)
- 39 trades on ~1000 hourly candles of one symbol is nowhere near enough
  data to train a trustworthy classifier — the test accuracy is not meaningful yet.
- The exit rule ("close after 5 bars") is arbitrary and not linked to
  stop-loss/take-profit or the structure of the trade at all.
- Validation is a single chronological 80/20 split, not rolling walk-forward.
- Only long trades on Order Blocks are handled (bearish OBs are detected
  by the indicator but never traded).

---

## Turning this into a university major project

This codebase is a solid, working core — plausible in scope for a final
year / major project once it's built out. A reasonable structure:

**1. Strengthen the strategy core**
- Trade bearish Order Blocks too (short side), not just bullish.
- Add liquidity-sweep detection (stop hunts before a BOS) — a core SMC
  concept not yet implemented.
- Replace the fixed "close after 5 bars" exit with ATR-based
  stop-loss/take-profit, or a structure-based exit (e.g., close at the
  next opposing OB).
- Multi-timeframe confirmation (e.g., only take a 1h OB if the 4h trend
  agrees).

**2. Make the ML component real**
- Collect trades across many symbols and a much longer history (multiple
  years, several pairs) to get a dataset large enough to trust.
- Proper **walk-forward validation**: train on data up to time T, test
  strictly on data after T, roll forward — not a random shuffle split.
- Compare multiple models (SVM, Random Forest, XGBoost, logistic
  regression) and report which generalizes best, with a confusion matrix
  and precision/recall discussion, not just accuracy.
- Add more engineered features: ATR at signal time, distance from key
  moving averages, time-of-day/session (Asian/London/NY), recent
  volatility regime.

**3. Evaluation rigor (this is what makes it a "major project" instead
of a script)**
- Benchmark against a naive baseline (e.g., buy every OB unfiltered, or
  buy-and-hold) so the ML filter's value is provable, not assumed.
- Standard backtest metrics: Sharpe ratio, max drawdown, win rate,
  profit factor, average R-multiple — not just ROI.
- Sensitivity analysis on the `lookback` and `impulse_threshold`
  parameters (avoid overfitting to one setting).

**4. Productize it**
- A small Streamlit or Flask dashboard: pick a symbol/timeframe, run the
  backtest, see the equity curve, trade list, and detected OB/FVG zones
  plotted on the candlestick chart.
- Optional: paper-trading mode against a live exchange (`ccxt`) that logs
  signals in real time without executing real orders — great for a demo.

**5. Write-up structure for the report/viva**
- Problem statement & motivation (why SMC, why ML filtering).
- Literature review (algorithmic trading, technical analysis, ML in
  finance — and an honest discussion of why most retail "SMC" trading
  claims lack rigorous backing).
- System design (architecture diagram: data → indicator → strategy →
  logger → trainer → filtered strategy).
- Implementation (this codebase, with the bug-fix history above as
  evidence of iterative engineering).
- Evaluation (metrics from point 3 above).
- Limitations & future work (the "Known limitations" section above is a
  good starting point).

This is a good scope for one or two semesters depending on how many of
the extensions above you take on. I'd suggest picking 2–3 from section 1,
doing section 2 and 3 properly (that's where the real engineering rigor
your evaluators will look for lives), and treating section 4 as a
stretch goal if time allows.
