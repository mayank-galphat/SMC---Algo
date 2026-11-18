import os

import ccxt
import pandas as pd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def fetch_binance_data(symbol='BTC/USDT', timeframe='1h', limit=1000):
    """Fetches historical OHLCV data from Binance and saves it to a CSV."""
    print(f"Fetching {limit} bars of {timeframe} {symbol} data...")

    exchange = ccxt.binance({'enableRateLimit': True})

    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
    except ccxt.BaseError as e:
        print(f"[!] Could not fetch data from Binance: {e}")
        return None
    if not ohlcv:
        print("[!] Binance returned no candles.")
        return None

    df = pd.DataFrame(ohlcv, columns=['datetime', 'open', 'high', 'low', 'close', 'volume'])
    df['datetime'] = pd.to_datetime(df['datetime'], unit='ms')

    # Binance's last candle is still forming — drop it so the backtest
    # never trains/tests on a partial bar.
    df = df.iloc[:-1]

    filename = os.path.join(BASE_DIR, f"{symbol.replace('/', '_')}_{timeframe}.csv")
    df.to_csv(filename, index=False, date_format='%Y-%m-%d %H:%M:%S')
    print(f"Data saved successfully to {filename}")
    return filename


if __name__ == '__main__':
    fetch_binance_data()
