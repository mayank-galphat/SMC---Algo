"""
Build symbols.csv: the local catalogue of listed securities the dashboard searches and browses.

    python build_symbols.py            # downloads official lists, writes symbols.csv

Sources (free, official, no API key):
  Nasdaq Trader symbol directory  -> every NASDAQ / NYSE / NYSE American / NYSE Arca / Cboe security
  NSE India EQUITY_L.csv          -> every NSE-listed equity
Other exchanges (LSE, TSX, HKEX, TSE, BSE...) are still reachable through the live Yahoo search box.
Re-run now and then (new listings / delistings). Needs internet access.
"""
from __future__ import annotations

import csv
import io
import os
import sys

import requests

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "symbols.csv")
UA = {"User-Agent": "Mozilla/5.0 (compatible; SMC-Terminal)"}
NASDAQ_URLS = ["https://www.nasdaqtrader.com/dynamic/symdir/nasdaqlisted.txt",
               "https://www.nasdaqtrader.com/dynamic/symdir/otherlisted.txt"]
NSE_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
OTHER_EXCH = {"A": ("NYSE American", "AMEX"), "N": ("NYSE", "NYSE"), "P": ("NYSE Arca", "NYSEARCA"),
              "Z": ("Cboe BZX", "BATS"), "V": ("IEX", "IEX")}
NSE_LISTS = {  # official NSE index constituent files -> tag stored in the `lists` column
    "NIFTY50": "https://nsearchives.nseindia.com/content/indices/ind_nifty50list.csv",
    "NIFTYNEXT50": "https://nsearchives.nseindia.com/content/indices/ind_niftynext50list.csv",
    "NIFTYBANK": "https://nsearchives.nseindia.com/content/indices/ind_niftybanklist.csv",
    "NIFTY100": "https://nsearchives.nseindia.com/content/indices/ind_nifty100list.csv",
}
FIELDS = ["symbol", "name", "exchange", "market", "group", "tv", "lists"]


def _rows(text: str, delim: str):
    return list(csv.DictReader(io.StringIO(text), delimiter=delim))


def parse_nasdaq(text: str):
    for r in _rows(text, "|"):
        sym = (r.get("Symbol") or "").strip()
        if not sym or sym.startswith("File Creation") or r.get("Test Issue") == "Y" or "$" in sym:
            continue
        yield {"symbol": "YF:" + sym.replace(".", "-"), "name": r["Security Name"].split(" - ")[0].strip(),
               "exchange": "NASDAQ", "market": "US", "group": "US ETFs" if r.get("ETF") == "Y" else "US stocks",
               "tv": f"NASDAQ:{sym}"}


def parse_other(text: str):
    for r in _rows(text, "|"):
        sym = (r.get("ACT Symbol") or "").strip()
        ex = OTHER_EXCH.get(r.get("Exchange", ""))
        if not sym or not ex or r.get("Test Issue") == "Y" or "$" in sym:
            continue
        yield {"symbol": "YF:" + sym.replace(".", "-"), "name": r["Security Name"].split(" - ")[0].strip(),
               "exchange": ex[0], "market": "US", "group": "US ETFs" if r.get("ETF") == "Y" else "US stocks",
               "tv": f"{ex[1]}:{sym.replace('.', '')}"}


def parse_nse(text: str):
    for r in _rows(text, ","):
        r = {k.strip(): (v or "").strip() for k, v in r.items() if k}
        sym = r.get("SYMBOL")
        if sym:
            yield {"symbol": f"YF:{sym}.NS", "name": r.get("NAME OF COMPANY", sym), "exchange": "NSE",
                   "market": "IN", "group": "India", "tv": f"NSE:{sym}"}


def get(url: str) -> str | None:
    try:
        r = requests.get(url, headers=UA, timeout=30)
        r.raise_for_status()
        return r.text
    except requests.RequestException as exc:
        print(f"[!] {url}: {exc}")
        return None


def main() -> int:
    rows, seen = [], set()
    for url, parser in [(NASDAQ_URLS[0], parse_nasdaq), (NASDAQ_URLS[1], parse_other), (NSE_URL, parse_nse)]:
        text = get(url)
        n = 0
        for row in (parser(text) if text else []):
            if row["symbol"] not in seen:
                seen.add(row["symbol"]); rows.append(row); n += 1
        print(f"{n:6d}  {url}")
    if not rows:
        print("Nothing downloaded; keeping any existing symbols.csv. Check your internet connection.")
        return 1
    by_sym = {r["symbol"]: r for r in rows}
    for tag, url in NSE_LISTS.items():
        text = get(url)
        got = 0
        for r in (_rows(text, ",") if text else []):
            r = {k.strip(): (v or "").strip() for k, v in r.items() if k}
            row = by_sym.get(f"YF:{r.get('Symbol', '')}.NS")
            if row:
                row["lists"] = ",".join(filter(None, [row.get("lists", ""), tag])); got += 1
        print(f"{got:6d}  {tag}")
    for r in rows:
        r.setdefault("lists", "")
    rows.sort(key=lambda r: (r["market"], r["name"].lower()))
    with open(OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS); w.writeheader(); w.writerows(rows)
    print(f"Wrote {len(rows)} securities to {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
