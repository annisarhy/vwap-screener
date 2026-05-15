"""
backtest/tracker.py
────────────────────
Tracks all signals emitted, evaluates outcomes (TP hit / SL hit / open),
and computes win rate statistics.

Storage: JSON file (signals_log.json) — simple, no DB needed.

Each signal logged:
  {
    id, symbol, signal, entry, sl, tp, rr,
    timestamp_entry, timestamp_closed,
    outcome: 'TP' | 'SL' | 'OPEN',
    pnl_pct, reason, conviction, ...
  }
"""

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Optional
import pandas as pd

LOG_PATH = os.getenv('SIGNAL_LOG_PATH', '/app/data/signals_log.json')


def _load() -> list[dict]:
    if not os.path.exists(LOG_PATH):
        return []
    try:
        with open(LOG_PATH, 'r') as f:
            return json.load(f)
    except Exception:
        return []


def _save(signals: list[dict]):
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, 'w') as f:
        json.dump(signals, f, indent=2, default=str)


# ── Log a new signal ──────────────────────────────────────────────────────────
def log_signal(signal: dict) -> str:
    """Add a new signal to the log. Returns signal ID."""
    signals = _load()

    # Avoid duplicate — same symbol+direction within 30 min
    now = datetime.now(timezone.utc)
    cutoff = 30 * 60  # 30 minutes in seconds
    for s in signals:
        if (s['symbol'] == signal['symbol'] and
            s['signal'] == signal['signal'] and
            s['outcome'] == 'OPEN'):
            try:
                t = datetime.fromisoformat(s['timestamp_entry'])
                if (now - t).total_seconds() < cutoff:
                    return s['id']   # already logged recently
            except Exception:
                pass

    sig_id = str(uuid.uuid4())[:8]
    record = {
        'id'              : sig_id,
        'symbol'          : signal['symbol'],
        'signal'          : signal['signal'],
        'entry'           : signal['entry'],
        'sl'              : signal['sl'],
        'tp'              : signal['tp'],
        'rr'              : signal['rr'],
        'atr'             : signal.get('atr', 0),
        'rsi'             : signal.get('rsi', 50),
        'conviction'      : signal.get('conviction', 0),
        'vwap_mid'        : signal.get('vwap_mid', 0),
        'fvg_bullish'     : signal.get('fvg_bullish', False),
        'fvg_bearish'     : signal.get('fvg_bearish', False),
        'reason'          : signal.get('reason', ''),
        'timestamp_entry' : now.isoformat(),
        'timestamp_closed': None,
        'outcome'         : 'OPEN',
        'pnl_pct'         : None,
        'exchange_count'  : signal.get('exchange_count', 1),
        'exchanges'       : signal.get('exchanges', ''),
    }
    signals.append(record)
    _save(signals)
    return sig_id


# ── Evaluate open signals ─────────────────────────────────────────────────────
def evaluate_open_signals(all_data: dict[str, pd.DataFrame]):
    """
    Check all OPEN signals against latest price data.
    Mark TP/SL if price has touched the level.
    """
    signals = _load()
    now     = datetime.now(timezone.utc)
    changed = False

    for s in signals:
        if s['outcome'] != 'OPEN':
            continue

        sym = s['symbol']
        df  = all_data.get(sym)
        if df is None or len(df) == 0:
            continue

        # Get candles after entry time
        try:
            entry_time = datetime.fromisoformat(s['timestamp_entry'])
            # Make entry_time timezone-aware
            if entry_time.tzinfo is None:
                entry_time = entry_time.replace(tzinfo=timezone.utc)
            future = df[df.index > entry_time]
        except Exception:
            continue

        if len(future) == 0:
            continue

        entry     = s['entry']
        sl        = s['sl']
        tp        = s['tp']
        is_long   = 'LONG' in s['signal']

        # Check each candle for TP/SL hit
        outcome = None
        close_time = None
        close_price = None

        for ts, row in future.iterrows():
            if is_long:
                if row['low'] <= sl:
                    outcome     = 'SL'
                    close_time  = ts
                    close_price = sl
                    break
                if row['high'] >= tp:
                    outcome     = 'TP'
                    close_time  = ts
                    close_price = tp
                    break
            else:
                if row['high'] >= sl:
                    outcome     = 'SL'
                    close_time  = ts
                    close_price = sl
                    break
                if row['low'] <= tp:
                    outcome     = 'TP'
                    close_time  = ts
                    close_price = tp
                    break

        # Auto-close signals older than 48h as EXPIRED
        age_hours = (now - entry_time).total_seconds() / 3600
        if outcome is None and age_hours > 48:
            outcome     = 'EXPIRED'
            close_time  = now
            close_price = float(future.iloc[-1]['close'])

        if outcome:
            pnl_pct = ((close_price - entry) / entry * 100) if is_long \
                      else ((entry - close_price) / entry * 100)
            s['outcome']          = outcome
            s['timestamp_closed'] = str(close_time)
            s['pnl_pct']          = round(pnl_pct, 3)
            changed = True

    if changed:
        _save(signals)

    return signals


# ── Statistics ────────────────────────────────────────────────────────────────
def compute_stats(days: int = 7) -> dict:
    """Compute backtest statistics for last N days."""
    signals = _load()
    now     = datetime.now(timezone.utc)
    cutoff  = now.timestamp() - days * 86400

    recent = []
    for s in signals:
        try:
            t = datetime.fromisoformat(s['timestamp_entry'])
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            if t.timestamp() >= cutoff:
                recent.append(s)
        except Exception:
            continue

    closed = [s for s in recent if s['outcome'] in ('TP', 'SL', 'EXPIRED')]
    open_  = [s for s in recent if s['outcome'] == 'OPEN']
    tp_    = [s for s in closed if s['outcome'] == 'TP']
    sl_    = [s for s in closed if s['outcome'] == 'SL']
    exp_   = [s for s in closed if s['outcome'] == 'EXPIRED']

    win_rate = len(tp_) / len(closed) * 100 if closed else 0
    pnl_list = [s['pnl_pct'] for s in closed if s['pnl_pct'] is not None]
    avg_pnl  = sum(pnl_list) / len(pnl_list) if pnl_list else 0
    total_pnl = sum(pnl_list)

    # By direction
    longs  = [s for s in closed if 'LONG'  in s['signal']]
    shorts = [s for s in closed if 'SHORT' in s['signal']]
    long_wr  = sum(1 for s in longs  if s['outcome']=='TP') / len(longs)  * 100 if longs  else 0
    short_wr = sum(1 for s in shorts if s['outcome']=='TP') / len(shorts) * 100 if shorts else 0

    # Best / worst
    best  = max(pnl_list) if pnl_list else 0
    worst = min(pnl_list) if pnl_list else 0

    # Streak
    streak_win = streak_loss = cur = 0
    for s in sorted(closed, key=lambda x: x['timestamp_entry']):
        if s['outcome'] == 'TP':
            cur = max(0, cur) + 1
            streak_win = max(streak_win, cur)
        elif s['outcome'] == 'SL':
            cur = min(0, cur) - 1
            streak_loss = max(streak_loss, abs(cur))

    return {
        'days'       : days,
        'total'      : len(recent),
        'closed'     : len(closed),
        'open'       : len(open_),
        'tp'         : len(tp_),
        'sl'         : len(sl_),
        'expired'    : len(exp_),
        'win_rate'   : round(win_rate, 1),
        'avg_pnl'    : round(avg_pnl, 2),
        'total_pnl'  : round(total_pnl, 2),
        'best_trade' : round(best, 2),
        'worst_trade': round(worst, 2),
        'long_wr'    : round(long_wr, 1),
        'short_wr'   : round(short_wr, 1),
        'streak_win' : streak_win,
        'streak_loss': streak_loss,
        'recent_closed': sorted(closed, key=lambda x: x.get('timestamp_closed',''),
                                reverse=True)[:10],
    }
