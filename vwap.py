"""
signals/vwap.py
───────────────
Weekly-anchored VWAP with ±1 StdDev bands.

Signal Logic (on 15m candles):
───────────────────────────────
  LONG  : candle close > VWAP weekly mid-line
           AND previous candle low touched / crossed mid-line (bounce)
           AND RSI(14) < 60  (not overbought)

  SHORT : candle close < VWAP weekly mid-line
           AND previous candle high touched / crossed mid-line (rejection)
           AND RSI(14) > 40  (not oversold)

VWAP weekly resets every Monday 00:00 UTC.
Bands = VWAP ± 1 * rolling StdDev of (typical_price - VWAP).
"""

import numpy as np
import pandas as pd
from typing import Optional


# ── RSI ───────────────────────────────────────────────────────────────────────
def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain  = delta.clip(lower=0).ewm(com=period - 1, min_periods=period).mean()
    loss  = (-delta.clip(upper=0)).ewm(com=period - 1, min_periods=period).mean()
    rs    = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


# ── Weekly-anchored VWAP ──────────────────────────────────────────────────────
def compute_vwap_weekly(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute weekly-anchored VWAP + ±1 StdDev bands.
    Anchor resets every Monday 00:00 UTC.

    Returns df with added columns:
      vwap_mid   — weekly VWAP (middle line)
      vwap_upper — VWAP + 1 StdDev
      vwap_lower — VWAP - 1 StdDev
    """
    d = df.copy().sort_index()

    # Typical price
    tp = (d['high'] + d['low'] + d['close']) / 3.0
    d['_tp']    = tp
    d['_tpvol'] = tp * d['volume']

    # Week key: ISO year + week number → resets Monday 00:00 UTC
    idx = d.index  # DatetimeIndex in UTC
    week_key = idx.isocalendar().year.astype(str) + '_' + \
               idx.isocalendar().week.astype(str).str.zfill(2)
    d['_week'] = week_key.values

    # Cumulative sums within each week
    d['_cum_tpvol'] = d.groupby('_week', sort=False)['_tpvol'].cumsum()
    d['_cum_vol']   = d.groupby('_week', sort=False)['volume'].cumsum()
    d['vwap_mid']   = d['_cum_tpvol'] / d['_cum_vol'].replace(0, np.nan)

    # Rolling StdDev of (tp - vwap) within each week → bands
    def _week_std(grp):
        dev = (grp['_tp'] - grp['vwap_mid']) ** 2
        return dev.expanding().mean() ** 0.5

    d['_std'] = d.groupby('_week', group_keys=False).apply(_week_std)

    d['vwap_upper'] = d['vwap_mid'] + d['_std']
    d['vwap_lower'] = d['vwap_mid'] - d['_std']

    # Cleanup temp cols
    drop = ['_tp', '_tpvol', '_cum_tpvol', '_cum_vol', '_week', '_std']
    d.drop(columns=[c for c in drop if c in d.columns], inplace=True)

    return d


# ── Signal generation ─────────────────────────────────────────────────────────
def generate_signals(df: pd.DataFrame) -> pd.DataFrame:
    """
    Generate LONG / SHORT / NEUTRAL signals on 15m candles.

    Columns added:
      rsi           — RSI(14)
      signal        — 'LONG' | 'SHORT' | 'NEUTRAL'
      signal_reason — human-readable explanation
      vwap_mid/upper/lower
    """
    d = compute_vwap_weekly(df)
    d['rsi'] = _rsi(d['close'])

    close  = d['close']
    high   = d['high']
    low    = d['low']
    mid    = d['vwap_mid']
    upper  = d['vwap_upper']
    lower  = d['vwap_lower']
    rsi    = d['rsi']

    prev_low  = low.shift(1)
    prev_high = high.shift(1)
    prev_mid  = mid.shift(1)

    # ── LONG conditions ───────────────────────────────────────────────────────
    # Primary: close above mid-line
    long_close_above = close > mid

    # Bounce confirmation: prev candle low touched or went below mid,
    # current close recovered above mid
    long_bounce = (prev_low <= prev_mid) & long_close_above

    # RSI filter: not already overbought
    long_rsi = rsi < 60

    # Band context: price near lower band (stronger bounce signal)
    band_width    = (upper - lower).replace(0, np.nan)
    dist_from_low = (close - lower) / band_width
    long_near_low = dist_from_low < 0.5   # in lower half of band

    # ── SHORT conditions ──────────────────────────────────────────────────────
    # Primary: close below mid-line
    short_close_below = close < mid

    # Rejection confirmation: prev candle high touched or exceeded mid,
    # current close fell back below mid
    short_rejection = (prev_high >= prev_mid) & short_close_below

    # RSI filter: not already oversold
    short_rsi = rsi > 40

    # Band context: price near upper band
    short_near_high = dist_from_low > 0.5   # in upper half of band

    # ── Combine ───────────────────────────────────────────────────────────────
    # Strong signals (bounce + RSI + band position)
    strong_long  = long_bounce  & long_rsi  & long_near_low
    strong_short = short_rejection & short_rsi & short_near_high

    # Base signals (close above/below only — always shown with lower conviction)
    base_long  = long_close_above  & long_rsi  & ~strong_long
    base_short = short_close_below & short_rsi & ~strong_short

    # Assign
    signal = pd.Series('NEUTRAL', index=d.index)
    signal[base_long]   = 'LONG'
    signal[base_short]  = 'SHORT'
    signal[strong_long] = 'LONG_STRONG'
    signal[strong_short]= 'SHORT_STRONG'
    d['signal'] = signal

    # Conviction score 1–10
    conviction = pd.Series(0, index=d.index, dtype=float)

    # Base: close above/below mid
    conviction += (close > mid).astype(float) * -1 + \
                  (close < mid).astype(float) * 1
    # Absolute: just use sign
    conviction = pd.Series(0.0, index=d.index)
    conviction[base_long]   = 4
    conviction[base_short]  = 4
    conviction[strong_long] = 8
    conviction[strong_short]= 8

    # Boost for RSI extremes
    conviction += (rsi < 35).astype(float) * 2   # oversold → boost long
    conviction += (rsi > 65).astype(float) * 2   # overbought → boost short

    # Boost for band position
    conviction[long_near_low  & (signal.isin(['LONG','LONG_STRONG']))]  += 1
    conviction[short_near_high & (signal.isin(['SHORT','SHORT_STRONG']))] += 1

    d['conviction'] = conviction.clip(0, 10).round(0).astype(int)

    # Human-readable reason
    def _reason(row):
        if 'LONG' in str(row['signal']):
            bounce = ' + bounce' if 'STRONG' in str(row['signal']) else ''
            return f"Close above VWAP mid{bounce} | RSI {row['rsi']:.0f}"
        elif 'SHORT' in str(row['signal']):
            rej = ' + rejection' if 'STRONG' in str(row['signal']) else ''
            return f"Close below VWAP mid{rej} | RSI {row['rsi']:.0f}"
        return 'No signal'

    d['signal_reason'] = d.apply(_reason, axis=1)

    return d


# ── Summary for one symbol ────────────────────────────────────────────────────
def get_latest_signal(df_with_signals: pd.DataFrame,
                      symbol: str) -> Optional[dict]:
    """Extract the latest candle's signal info."""
    if df_with_signals is None or len(df_with_signals) == 0:
        return None

    last = df_with_signals.iloc[-1]
    sig  = str(last.get('signal', 'NEUTRAL'))

    if sig == 'NEUTRAL':
        return None

    return {
        'symbol'     : symbol,
        'signal'     : sig,
        'conviction' : int(last.get('conviction', 0)),
        'close'      : float(last['close']),
        'vwap_mid'   : float(last.get('vwap_mid', 0)),
        'vwap_upper' : float(last.get('vwap_upper', 0)),
        'vwap_lower' : float(last.get('vwap_lower', 0)),
        'rsi'        : float(last.get('rsi', 50)),
        'reason'     : str(last.get('signal_reason', '')),
        'timestamp'  : last.name,
        # Distance from mid as % — useful for context
        'dist_pct'   : (float(last['close']) - float(last.get('vwap_mid', last['close'])))
                        / float(last.get('vwap_mid', last['close']) or 1) * 100,
    }
