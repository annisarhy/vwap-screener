"""
screener/engine.py
──────────────────
Full pipeline:
  1. Fetch top N symbols (common across Bybit+OKX+Gate.io)
  2. Average OHLCV across exchanges
  3. Compute VWAP weekly signals
  4. Rank by conviction
"""

from datetime import datetime, timezone
from data.fetcher import MultiExchangeFetcher
from signals.vwap import generate_signals, get_latest_signal


def run_screener(timeframe: str = '15m',
                 top_n: int = 50,
                 min_conviction: int = 3) -> dict:
    now = datetime.now(timezone.utc)
    print(f'\n{"="*60}')
    print(f'  VWAP WEEKLY SCREENER  ·  {timeframe}  ·  {now.strftime("%Y-%m-%d %H:%M UTC")}')
    print(f'  Sources: Bybit + OKX + Gate.io  (averaged)')
    print(f'{"="*60}')

    # 1. Init fetcher + discover symbols
    print('\n[1/3] Discovering common symbols across 3 exchanges...')
    fetcher = MultiExchangeFetcher(top_n=top_n)
    symbols = fetcher.get_common_symbols()
    if not symbols:
        return _empty(now, timeframe)

    # 2. Fetch averaged OHLCV
    print(f'\n[2/3] Fetching {timeframe} OHLCV (averaged across exchanges)...')
    all_data = fetcher.fetch_all(symbols, timeframe)

    # 3. Generate signals
    print('\n[3/3] Computing VWAP weekly signals...')
    longs, shorts = [], []

    for base, df in all_data.items():
        try:
            df_sig = generate_signals(df)
            result = get_latest_signal(df_sig, base)
            if result is None or result['conviction'] < min_conviction:
                continue
            # Attach exchange info
            result['exchanges']      = str(df['exchanges'].iloc[-1])
            result['exchange_count'] = int(df['exchange_count'].iloc[-1])

            if 'LONG'  in result['signal']:
                longs.append(result)
            elif 'SHORT' in result['signal']:
                shorts.append(result)
        except Exception as e:
            print(f'  [engine] {base} error: {e}')

    # Sort: conviction desc, then distance from mid asc (fresher bounce)
    longs  = sorted(longs,  key=lambda x: (x['conviction'], -abs(x['dist_pct'])), reverse=True)
    shorts = sorted(shorts, key=lambda x: (x['conviction'], -abs(x['dist_pct'])), reverse=True)

    stats = {
        'total_scanned': len(all_data),
        'long_count'   : len(longs),
        'short_count'  : len(shorts),
        'strong_long'  : sum(1 for x in longs  if 'STRONG' in x['signal']),
        'strong_short' : sum(1 for x in shorts if 'STRONG' in x['signal']),
        'full_3ex'     : sum(1 for x in longs + shorts if x.get('exchange_count', 0) == 3),
    }

    print(f'\n  Signals: {stats["long_count"]} LONG  |  {stats["short_count"]} SHORT')
    print(f'  Full 3-exchange coverage: {stats["full_3ex"]} signals')

    return {
        'timestamp': now,
        'timeframe': timeframe,
        'longs'    : longs,
        'shorts'   : shorts,
        'stats'    : stats,
    }


def _empty(now, timeframe):
    return {
        'timestamp': now, 'timeframe': timeframe,
        'longs': [], 'shorts': [],
        'stats': {'total_scanned':0,'long_count':0,'short_count':0,
                  'strong_long':0,'strong_short':0,'full_3ex':0},
    }
