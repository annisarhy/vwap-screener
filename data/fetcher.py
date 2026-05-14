"""
data/fetcher.py
───────────────
Fetch OHLCV from Bybit + OKX + Gate.io via CCXT.
Averages OHLCV across exchanges for unbiased VWAP computation.
"""

import time
import ccxt
import numpy as np
import pandas as pd
from typing import Optional

# ── Exchange configs ──────────────────────────────────────────────────────────
EXCHANGES_CFG = {
    'bybit' : {'class': ccxt.bybit,  'options': {'defaultType': 'swap'}},
    'okx'   : {'class': ccxt.okx,    'options': {'defaultType': 'swap'}},
    'gateio': {'class': ccxt.gateio, 'options': {'defaultType': 'swap'}},
}

TIMEFRAME_MAP = {
    '1m':'1m','5m':'5m','15m':'15m','30m':'30m',
    '1h':'1h','4h':'4h','1d':'1d',
}

LIMIT_MAP = {
    '1m':1440,'5m':504,'15m':672,
    '30m':336,'1h':168,'4h':84,'1d':30,
}


class MultiExchangeFetcher:
    def __init__(self, top_n: int = 50):
        self.top_n = top_n
        self.exchanges: dict[str, ccxt.Exchange] = {}
        self._init_exchanges()

    def _init_exchanges(self):
        for name, cfg in EXCHANGES_CFG.items():
            try:
                ex = cfg['class']({'enableRateLimit': True, 'options': cfg['options']})
                self.exchanges[name] = ex
                print(f'  ✓ {name} connected')
            except Exception as e:
                print(f'  ✗ {name} error: {e}')

    # ── Symbol discovery ──────────────────────────────────────────────────────
    def get_common_symbols(self) -> list[str]:
        """Top N symbols by Bybit volume that exist on all 3 exchanges."""
        available: dict[str, set] = {}
        for name, ex in self.exchanges.items():
            try:
                markets = ex.load_markets()
                bases = {
                    v['base'] for v in markets.values()
                    if v.get('swap') and v.get('quote') == 'USDT' and v.get('active', True)
                }
                available[name] = bases
            except Exception as e:
                print(f'  [fetcher] {name} load_markets: {e}')
                available[name] = set()

        if not available:
            return []

        common = set.intersection(*available.values())

        # Rank by Bybit 24h quote volume
        try:
            tickers = self.exchanges['bybit'].fetch_tickers()
            ranked = sorted(
                [(k.split('/')[0], v.get('quoteVolume', 0))
                 for k, v in tickers.items()
                 if k.split('/')[0] in common and v.get('quoteVolume')],
                key=lambda x: x[1], reverse=True
            )
            top = [r[0] for r in ranked[:self.top_n]]
            print(f'  Common symbols (all 3 exchanges): {len(common)} → top {len(top)} by volume')
            return top
        except Exception as e:
            print(f'  [fetcher] ranking error: {e}')
            return list(common)[:self.top_n]

    # ── Single exchange fetch ─────────────────────────────────────────────────
    def _fetch_one(self, ex: ccxt.Exchange, base: str,
                   timeframe: str, limit: int) -> Optional[pd.DataFrame]:
        tf = TIMEFRAME_MAP.get(timeframe, '15m')
        for sym in [f'{base}/USDT:USDT', f'{base}/USDT']:
            try:
                if sym not in ex.markets:
                    continue
                raw = ex.fetch_ohlcv(sym, tf, limit=limit)
                if not raw:
                    continue
                df = pd.DataFrame(raw, columns=['timestamp','open','high','low','close','volume'])
                df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True)
                return df.set_index('timestamp').sort_index().astype(float)
            except Exception:
                continue
        return None

    # ── Average across exchanges ──────────────────────────────────────────────
    def fetch_averaged(self, base: str, timeframe: str, limit: int) -> Optional[pd.DataFrame]:
        """
        Fetch from all exchanges → align timestamps → average prices, sum volume.
        exchange_count column tells how many sources contributed.
        """
        frames, sources = [], []
        for name, ex in self.exchanges.items():
            df = self._fetch_one(ex, base, timeframe, limit)
            if df is not None and len(df) >= 20:
                frames.append(df)
                sources.append(name)
            time.sleep(0.05)

        if not frames:
            return None

        if len(frames) == 1:
            out = frames[0].copy()
            out['exchange_count'] = 1
            out['exchanges']      = sources[0]
            return out

        # Common timestamp index
        idx = frames[0].index
        for f in frames[1:]:
            idx = idx.intersection(f.index)

        # Fallback to union+ffill if intersection too small
        if len(idx) < 20:
            idx = frames[0].index
            for f in frames[1:]:
                idx = idx.union(f.index)
            frames = [f.reindex(idx).ffill() for f in frames]

        aligned = [f.reindex(idx) for f in frames]

        avg = pd.DataFrame(index=idx)
        for col in ['open','high','low','close']:
            avg[col] = np.nanmean(
                np.stack([f[col].values for f in aligned], axis=1), axis=1
            )
        avg['volume']         = np.nansum(
            np.stack([f['volume'].values for f in aligned], axis=1), axis=1
        )
        avg['exchange_count'] = len(frames)
        avg['exchanges']      = '+'.join(sources)
        return avg

    # ── Batch ─────────────────────────────────────────────────────────────────
    def fetch_all(self, symbols: list[str], timeframe: str) -> dict[str, pd.DataFrame]:
        limit   = LIMIT_MAP.get(timeframe, 300)
        results = {}
        for i, base in enumerate(symbols):
            df = self.fetch_averaged(base, timeframe, limit)
            if df is not None and len(df) >= 50:
                results[base] = df
            if i % 5 == 0 and i > 0:
                time.sleep(0.3)

        n3 = sum(1 for d in results.values() if int(d['exchange_count'].iloc[-1]) == 3)
        n2 = sum(1 for d in results.values() if int(d['exchange_count'].iloc[-1]) == 2)
        print(f'  Fetched {len(results)}/{len(symbols)}  (3-ex: {n3}  2-ex: {n2}  1-ex: {len(results)-n3-n2})')
        return results
