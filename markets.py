"""
Market catalogue + data for every tradable market the dashboard can show.

  Crypto  : every Binance spot pair quoted in USDT (~400+), live WebSocket.   id = "BTCUSDT"
  Global  : stocks, ETFs, indices, forex, futures on every exchange Yahoo
            Finance covers (NSE/BSE, NYSE, NASDAQ, LSE, TSX, HKEX, TSE, ASX, ...). id = "YF:RELIANCE.NS"

No extra dependencies: Yahoo's public chart/search JSON is called with `requests`.
Yahoo is an unofficial, rate-limited, sometimes delayed source - fine for a dashboard, not for execution.
"""
from __future__ import annotations

import csv
import os
import re
import time

import pandas as pd
import requests

BINANCE_HOSTS = ["https://api.binance.com", "https://data-api.binance.vision", "https://api.binance.us"]
YF_HOSTS = ["https://query1.finance.yahoo.com", "https://query2.finance.yahoo.com"]
UA = {"User-Agent": "Mozilla/5.0 (compatible; SMC-Terminal)"}
INTERVAL_SECONDS = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
# our interval -> (yahoo interval, yahoo range, resample-to-4h?)
YF_MAP = {"5m": ("5m", "60d", False), "15m": ("15m", "60d", False), "1h": ("60m", "730d", False),
          "4h": ("60m", "730d", True), "1d": ("1d", "10y", False)}
ID_RE = re.compile(r"^(YF:[A-Z0-9.^=&\-]{1,24}|[A-Z0-9]{2,20})$")

POPULAR = [
    ("BTCUSDT", "Bitcoin", "Crypto"), ("ETHUSDT", "Ethereum", "Crypto"), ("SOLUSDT", "Solana", "Crypto"),
    ("BNBUSDT", "BNB", "Crypto"), ("XRPUSDT", "XRP", "Crypto"),
    ("YF:AAPL", "Apple", "US stocks"), ("YF:MSFT", "Microsoft", "US stocks"), ("YF:NVDA", "NVIDIA", "US stocks"),
    ("YF:TSLA", "Tesla", "US stocks"), ("YF:AMZN", "Amazon", "US stocks"), ("YF:GOOGL", "Alphabet", "US stocks"),
    ("YF:RELIANCE.NS", "Reliance Industries (NSE)", "India"), ("YF:TCS.NS", "Tata Consultancy (NSE)", "India"),
    ("YF:HDFCBANK.NS", "HDFC Bank (NSE)", "India"), ("YF:INFY.NS", "Infosys (NSE)", "India"),
    ("YF:^NSEI", "NIFTY 50", "Indices"), ("YF:^BSESN", "SENSEX", "Indices"), ("YF:^GSPC", "S&P 500", "Indices"),
    ("YF:^IXIC", "NASDAQ Composite", "Indices"), ("YF:^FTSE", "FTSE 100", "Indices"),
    ("YF:EURUSD=X", "EUR/USD", "Forex"), ("YF:GBPUSD=X", "GBP/USD", "Forex"), ("YF:USDINR=X", "USD/INR", "Forex"),
    ("YF:GC=F", "Gold futures", "Commodities"), ("YF:CL=F", "Crude oil futures", "Commodities"),
]
TV_EXCH = {"NMS": "NASDAQ", "NGM": "NASDAQ", "NCM": "NASDAQ", "NYQ": "NYSE", "ASE": "AMEX", "PCX": "NYSEARCA",
           "NSI": "NSE", "BSE": "BSE", "LSE": "LSE", "TOR": "TSX", "HKG": "HKEX", "JPX": "TSE", "ASX": "ASX",
           "PAR": "EURONEXT", "SES": "SGX", "KSC": "KRX", "SHH": "SSE", "SHZ": "SZSE", "GER": "XETR", "FRA": "FWB"}

# (yahoo ticker, name, region, TradingView symbol)
_INDICES = [
    ("^NSEI", "NIFTY 50", "India", "NSE:NIFTY"), ("^NSEBANK", "NIFTY Bank (Bank Nifty)", "India", "NSE:BANKNIFTY"),
    ("^NSMIDCP", "NIFTY Next 50", "India", "NSE:NIFTYJR"), ("^CNXIT", "NIFTY IT", "India", "NSE:CNXIT"),
    ("^CNXAUTO", "NIFTY Auto", "India", "NSE:CNXAUTO"), ("^CNXPHARMA", "NIFTY Pharma", "India", "NSE:CNXPHARMA"),
    ("^CNXFMCG", "NIFTY FMCG", "India", "NSE:CNXFMCG"), ("^CNXMETAL", "NIFTY Metal", "India", "NSE:CNXMETAL"),
    ("^CNXENERGY", "NIFTY Energy", "India", "NSE:CNXENERGY"), ("^INDIAVIX", "India VIX", "India", "NSE:INDIAVIX"),
    ("^BSESN", "SENSEX (BSE 30)", "India", "BSE:SENSEX"),
    ("^GSPC", "S&P 500", "US", "SP:SPX"), ("^DJI", "Dow Jones Industrial Average", "US", "DJ:DJI"),
    ("^IXIC", "NASDAQ Composite", "US", "NASDAQ:IXIC"), ("^NDX", "NASDAQ 100", "US", "NASDAQ:NDX"),
    ("^RUT", "Russell 2000", "US", "TVC:RUT"), ("^VIX", "CBOE Volatility Index", "US", "CBOE:VIX"),
    ("^FTSE", "FTSE 100", "Europe", "FTSE:UKX"), ("^GDAXI", "DAX 40", "Europe", "XETR:DAX"),
    ("^FCHI", "CAC 40", "Europe", "EURONEXT:PX1"), ("^STOXX50E", "EURO STOXX 50", "Europe", "TVC:SX5E"),
    ("^N225", "Nikkei 225", "Asia-Pacific", "TVC:NI225"), ("^HSI", "Hang Seng", "Asia-Pacific", "HSI:HSI"),
    ("000001.SS", "Shanghai Composite", "Asia-Pacific", "SSE:000001"), ("^KS11", "KOSPI", "Asia-Pacific", "KRX:KOSPI"),
    ("^AXJO", "S&P/ASX 200", "Asia-Pacific", "ASX:XJO"), ("^GSPTSE", "S&P/TSX Composite", "Americas", "TSX:TSX"),
    ("^BVSP", "Bovespa", "Americas", "BMFBOVESPA:IBOV"),
]
INDICES = [{"symbol": "YF:" + t, "name": n, "group": "Index · " + g, "exch": "Index", "market": "IDX", "tv": tv}
           for t, n, g, tv in _INDICES]
_TV_BY_ID = {i["symbol"]: i["tv"] for i in INDICES}
CATALOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "symbols.csv")
_catalog: dict = {"mtime": None, "rows": []}
_binance_list: dict = {"t": 0, "rows": []}


def is_yahoo(symbol: str) -> bool:
    return symbol.startswith("YF:")


def valid_id(symbol: str) -> bool:
    return bool(ID_RE.match(symbol))


def _get(hosts, path, **kw):
    for host in hosts:
        try:
            r = requests.get(host + path, headers=UA, timeout=6, **kw)
            if r.status_code == 200:
                return r.json()
        except (requests.RequestException, ValueError):
            continue
    return None


def tv_symbol(symbol: str, exch: str | None = None, qtype: str | None = None) -> str:
    if not is_yahoo(symbol):
        return f"BINANCE:{symbol}"
    if symbol in _TV_BY_ID:
        return _TV_BY_ID[symbol]
    t = symbol[3:]
    if t.endswith("=X"):
        return f"FX:{t[:-2] if len(t) > 3 else t}"
    if exch in TV_EXCH:
        return f"{TV_EXCH[exch]}:{t.split('.')[0]}"
    return t.split(".")[0].lstrip("^")


# ------------------------------------------------------------- catalogue ---
def binance_pairs() -> list[dict]:
    if time.time() - _binance_list["t"] < 6 * 3600 and _binance_list["rows"]:
        return _binance_list["rows"]
    j = _get(BINANCE_HOSTS, "/api/v3/exchangeInfo")
    if not j:
        return _binance_list["rows"]
    rows = [{"symbol": s["symbol"], "name": s["baseAsset"], "group": "Crypto", "exch": "Binance", "tv": f"BINANCE:{s['symbol']}"}
            for s in j.get("symbols", [])
            if s.get("quoteAsset") == "USDT" and s.get("status") == "TRADING"
            and not re.search(r"(UP|DOWN|BULL|BEAR)$", s["baseAsset"])]
    _binance_list.update(t=time.time(), rows=rows)
    return rows


# browse tabs backed by NSE index membership (column `lists` of symbols.csv, see build_symbols.py)
LISTS = {"N50": "NIFTY50", "NNEXT50": "NIFTYNEXT50", "NBANK": "NIFTYBANK", "N100": "NIFTY100"}


def catalog() -> list[dict]:
    """Local securities list from build_symbols.py (empty if it has not been built)."""
    try:
        mt = os.path.getmtime(CATALOG_FILE)
    except OSError:
        return []
    if _catalog["mtime"] != mt:
        with open(CATALOG_FILE, newline="", encoding="utf-8") as f:
            _catalog.update(mtime=mt, rows=[{**r, "exch": r["exchange"]} for r in csv.DictReader(f)])
    return _catalog["rows"]


def browse(market: str, offset: int = 0, limit: int = 60) -> dict:
    """Page through a market. market: US | IN | CRYPTO."""
    if market == "CRYPTO":
        rows = sorted(binance_pairs(), key=lambda r: r["symbol"])
    elif market == "IDX":
        rows = INDICES
    elif market in LISTS:
        rows = [r for r in catalog() if LISTS[market] in (r.get("lists") or "").split(",")]
    else:
        rows = [r for r in catalog() if r["market"] == market]
    return {"results": rows[offset:offset + limit], "total": len(rows), "offset": offset}


def _in_scope(r: dict, market: str) -> bool:
    if market in LISTS:
        return LISTS[market] in (r.get("lists") or "").split(",")
    return not market or market in ("CRYPTO", "IDX") or r.get("market") == market


def search(query: str, limit: int = 30, market: str = "") -> list[dict]:
    q = query.strip()
    if not q:
        return []
    qu = q.upper()
    ql = qu.replace("/", "").replace("-", "")
    out: list[dict] = []

    # indices (NIFTY 50, SENSEX, S&P 500, ...) - match on name or ticker
    if market in ("", "IDX"):
        out += [r for r in INDICES if qu in r["name"].upper() or ql in r["symbol"][3:].replace("^", "").replace("-", "")][:8]

    # crypto
    if market in ("", "CRYPTO"):
        hits = [(0 if r["symbol"].startswith(ql) or r["name"].upper() == ql else 1, r) for r in binance_pairs()
                if ql in r["symbol"] or ql in r["name"].upper()]
        out += [r for _, r in sorted(hits, key=lambda x: (x[0], len(x[1]["symbol"])))][:12]

    # local stock catalogue (build_symbols.py)
    if market not in ("CRYPTO", "IDX"):
        local = []
        for r in catalog():
            if not _in_scope(r, market):
                continue
            sym = r["symbol"][3:].replace("-", "").upper()
            nm = r["name"].upper()
            if sym == ql or sym.split(".")[0] == ql:
                local.append((0, r))
            elif sym.startswith(ql) or nm.startswith(qu):
                local.append((1, r))
            elif len(q) >= 3 and qu in nm:
                local.append((2, r))
        out += [r for _, r in sorted(local, key=lambda x: (x[0], len(x[1]["symbol"])))[:25]]

    # live Yahoo search fills the gaps (other exchanges, forex, futures) on the unfiltered tab
    if market == "":
        have = {r["symbol"] for r in out}
        j = _get(YF_HOSTS, "/v1/finance/search", params={"q": q, "quotesCount": 20, "newsCount": 0})
        for it in (j or {}).get("quotes", []):
            sym = it.get("symbol")
            if not sym or it.get("quoteType") not in ("EQUITY", "ETF", "INDEX", "CURRENCY", "FUTURE", "MUTUALFUND", "CRYPTOCURRENCY"):
                continue
            sid = "YF:" + sym.upper()
            if not valid_id(sid) or sid in have:
                continue
            out.append({"symbol": sid, "name": it.get("shortname") or it.get("longname") or sym,
                        "group": (it.get("quoteType") or "").title(), "exch": it.get("exchDisp") or it.get("exchange") or "",
                        "tv": tv_symbol(sid, it.get("exchange"), it.get("quoteType"))})
    return out[:limit]


def popular() -> list[dict]:
    return [{"symbol": s, "name": n, "group": g, "tv": tv_symbol(s)} for s, n, g in POPULAR]


# ------------------------------------------------------------------ data ---
def fetch_binance(symbol: str, interval: str, limit: int = 1000):
    j = _get(BINANCE_HOSTS, "/api/v3/klines", params={"symbol": symbol, "interval": interval, "limit": limit})
    if not j:
        return None
    df = pd.DataFrame(j).iloc[:, :6]
    df.columns = ["time", "open", "high", "low", "close", "volume"]
    df = df.astype(float)
    df["time"] = (df["time"] // 1000).astype(int)
    return df


def fetch_yahoo(symbol: str, interval: str, limit: int = 1000):
    yi, rng, to4h = YF_MAP[interval]
    j = _get(YF_HOSTS, "/v8/finance/chart/" + requests.utils.quote(symbol[3:], safe=""),
             params={"interval": yi, "range": rng, "includePrePost": "false"})
    try:
        res = j["chart"]["result"][0]
        q = res["indicators"]["quote"][0]
        df = pd.DataFrame({"time": res["timestamp"], "open": q["open"], "high": q["high"], "low": q["low"],
                           "close": q["close"], "volume": q["volume"]})
    except (TypeError, KeyError, IndexError):
        return None
    df = df.dropna(subset=["open", "high", "low", "close"]).copy()
    df["volume"] = df["volume"].fillna(0)
    df = df.astype(float)
    df["time"] = df["time"].astype(int)
    if to4h:
        g = df.groupby(df["time"] // 14400 * 14400)
        df = pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
                           "close": g["close"].last(), "volume": g["volume"].sum()}).reset_index(names="time")
    df = df.drop_duplicates("time").sort_values("time").tail(limit).reset_index(drop=True)
    return df if len(df) >= 60 else None


def drop_forming(df: pd.DataFrame, interval: str, source: str) -> pd.DataFrame:
    """Remove the still-forming last bar of a live source so signals never repaint."""
    if source in ("binance", "yahoo") and len(df) and df["time"].iloc[-1] + INTERVAL_SECONDS[interval] > time.time():
        return df.iloc[:-1].reset_index(drop=True)
    return df
