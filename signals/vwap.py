"""
signals/vwap.py
───────────────
Weekly-anchored VWAP + FVG detection + Entry/SL/TP + Reason generator.

Signal Logic (30m candles):
  LONG  : close > VWAP weekly mid
  SHORT : close < VWAP weekly mid

  STRONG variant: bounce/rejection confirmed at mid-line

Entry  : close of signal candle
SL     : entry ± 1.5 × ATR(14)
TP     : VWAP upper band (long) / VWAP lower band (short)

FVG    : 3-candle Fair Value Gap pattern
  Bullish FVG: candle[i-2].high < candle[i].low   → gap not filled
  Bearish FVG: candle[i-2].low  > candle[i].high  → gap not filled
"""

import numpy as np
import pandas as pd
from typing import Optional


# ── Indicators ────────────────────────────────────────────────────────────────
def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain  = delta.clip(lower=0).ewm(com=period - 1, min_periods=period).mean()
    loss  = (-delta.clip(upper=0)).ewm(com=period - 1, min_periods=period).mean()
    rs    = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, prev_close = df['high'], df['low'], df['close'].shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(com=period - 1, min_periods=period).mean()


# ── Weekly-anchored VWAP ──────────────────────────────────────────────────────
def compute_vwap_weekly(df: pd.DataFrame) -> pd.DataFrame:
    """
    Weekly-anchored VWAP + ±1 StdDev bands.
    Resets every Monday 00:00 UTC.
    Adds: vwap_mid, vwap_upper, vwap_lower
    """
    d = df.copy().sort_index()
    tp = (d['high'] + d['low'] + d['close']) / 3.0
    d['_tp']    = tp
    d['_tpvol'] = tp * d['volume']

    idx      = d.index
    week_key = (idx.isocalendar().year.astype(str) + '_' +
                idx.isocalendar().week.astype(str).str.zfill(2))
    d['_week'] = week_key.values

    d['_cum_tpvol'] = d.groupby('_week', sort=False)['_tpvol'].cumsum()
    d['_cum_vol']   = d.groupby('_week', sort=False)['volume'].cumsum()
    d['vwap_mid']   = d['_cum_tpvol'] / d['_cum_vol'].replace(0, np.nan)

    def _week_std(grp):
        return ((grp['_tp'] - grp['vwap_mid']) ** 2).expanding().mean() ** 0.5

    d['_std']       = d.groupby('_week', group_keys=False).apply(_week_std)
    d['vwap_upper'] = d['vwap_mid'] + d['_std']
    d['vwap_lower'] = d['vwap_mid'] - d['_std']

    drop = ['_tp','_tpvol','_cum_tpvol','_cum_vol','_week','_std']
    d.drop(columns=[c for c in drop if c in d.columns], inplace=True)
    return d


# ── FVG Detection ─────────────────────────────────────────────────────────────
def detect_fvg(df: pd.DataFrame) -> pd.DataFrame:
    """
    Fair Value Gap (3-candle pattern):
      Bullish FVG : high[i-2] < low[i]   → bullish gap between candle i-2 and i
      Bearish FVG : low[i-2]  > high[i]  → bearish gap between candle i-2 and i

    Adds columns:
      fvg_bullish  — bool: bullish FVG present on this candle
      fvg_bearish  — bool: bearish FVG present
      fvg_top      — upper edge of FVG zone
      fvg_bottom   — lower edge of FVG zone
    """
    h2 = df['high'].shift(2)
    l2 = df['low'].shift(2)
    h0 = df['high']
    l0 = df['low']

    df['fvg_bullish'] = l0 > h2          # gap above candle[i-2] high
    df['fvg_bearish'] = h0 < l2          # gap below candle[i-2] low

    # FVG zone edges
    df['fvg_top']    = np.where(df['fvg_bullish'], l0,
                       np.where(df['fvg_bearish'], l2, np.nan))
    df['fvg_bottom'] = np.where(df['fvg_bullish'], h2,
                       np.where(df['fvg_bearish'], h0, np.nan))
    return df


# ── Entry / SL / TP ───────────────────────────────────────────────────────────
def compute_trade_levels(df: pd.DataFrame,
                          atr_mult: float = 1.5) -> pd.DataFrame:
    """
    Entry : close of signal candle
    SL    : entry ± atr_mult × ATR(14)
    TP    : VWAP upper band (long) / lower band (short)
    RR    : (TP - entry) / (entry - SL)  — computed per signal
    """
    df['atr'] = _atr(df)

    # These will be set per-signal in generate_signals
    df['entry'] = df['close']
    df['sl_long']  = df['close'] - atr_mult * df['atr']
    df['sl_short'] = df['close'] + atr_mult * df['atr']
    df['tp_long']  = df['vwap_upper']
    df['tp_short'] = df['vwap_lower']

    # RR ratio
    long_risk   = (df['entry'] - df['sl_long']).clip(lower=1e-10)
    long_reward = (df['tp_long'] - df['entry']).clip(lower=0)
    df['rr_long'] = (long_reward / long_risk).round(2)

    short_risk   = (df['sl_short'] - df['entry']).clip(lower=1e-10)
    short_reward = (df['entry'] - df['tp_short']).clip(lower=0)
    df['rr_short'] = (short_reward / short_risk).round(2)

    return df


# ── Reason generator ──────────────────────────────────────────────────────────
def build_reason(row: pd.Series, direction: str) -> str:
    """
    Build a human-readable reason string for a signal.
    Checks: VWAP position, bounce/rejection, FVG, RSI zone.
    """
    parts = []

    is_long  = 'LONG'  in direction
    is_short = 'SHORT' in direction

    # 1. VWAP position
    dist = row.get('dist_pct', 0)
    if is_long:
        parts.append(f"Close di atas VWAP weekly mid ({dist:+.2f}%)")
    else:
        parts.append(f"Close di bawah VWAP weekly mid ({dist:+.2f}%)")

    # 2. Bounce / rejection
    if 'STRONG' in direction:
        if is_long:
            parts.append("Bounce terkonfirmasi di mid-line (low prev candle menyentuh VWAP)")
        else:
            parts.append("Rejection terkonfirmasi di mid-line (high prev candle menyentuh VWAP)")

    # 3. FVG
    if row.get('fvg_bullish') and is_long:
        top    = row.get('fvg_top', 0)
        bottom = row.get('fvg_bottom', 0)
        parts.append(f"FVG Bullish terdeteksi 30m (zona {bottom:.4f}–{top:.4f})")
    elif row.get('fvg_bearish') and is_short:
        top    = row.get('fvg_top', 0)
        bottom = row.get('fvg_bottom', 0)
        parts.append(f"FVG Bearish terdeteksi 30m (zona {bottom:.4f}–{top:.4f})")

    # 4. RSI context
    rsi = row.get('rsi', 50)
    if rsi < 30:
        parts.append(f"RSI oversold ({rsi:.0f}) — momentum reversal tinggi")
    elif rsi < 40:
        parts.append(f"RSI {rsi:.0f} — area oversold")
    elif rsi > 70:
        parts.append(f"RSI overbought ({rsi:.0f}) — momentum reversal tinggi")
    elif rsi > 60:
        parts.append(f"RSI {rsi:.0f} — area overbought")
    else:
        parts.append(f"RSI netral ({rsi:.0f})")

    # 5. Band position
    band_pos = row.get('dist_from_low', 0.5)
    if is_long and band_pos < 0.3:
        parts.append("Harga di lower band — potensi reversal kuat")
    elif is_short and band_pos > 0.7:
        parts.append("Harga di upper band — potensi reversal kuat")

    return " | ".join(parts)


# ── Main signal generator ─────────────────────────────────────────────────────
def generate_signals(df: pd.DataFrame) -> pd.DataFrame:
    d = compute_vwap_weekly(df)
    d = detect_fvg(d)
    d = compute_trade_levels(d)
    d['rsi'] = _rsi(d['close'])

    close = d['close']
    high  = d['high']
    low   = d['low']
    mid   = d['vwap_mid']
    upper = d['vwap_upper']
    lower = d['vwap_lower']
    rsi   = d['rsi']

    prev_low  = low.shift(1)
    prev_high = high.shift(1)
    prev_mid  = mid.shift(1)

    band_width      = (upper - lower).replace(0, np.nan)
    dist_from_low   = (close - lower) / band_width
    d['dist_from_low'] = dist_from_low
    long_near_low   = dist_from_low < 0.5
    short_near_high = dist_from_low >= 0.5

    # Signal classification
    long_close_above = close > mid
    long_bounce      = (prev_low <= prev_mid) & long_close_above
    strong_long      = long_bounce & long_near_low

    short_close_below = close < mid
    short_rejection   = (prev_high >= prev_mid) & short_close_below
    strong_short      = short_rejection & short_near_high

    base_long  = long_close_above  & ~strong_long
    base_short = short_close_below & ~strong_short

    signal = pd.Series('NEUTRAL', index=d.index)
    signal[base_long]    = 'LONG'
    signal[base_short]   = 'SHORT'
    signal[strong_long]  = 'LONG_STRONG'
    signal[strong_short] = 'SHORT_STRONG'
    d['signal'] = signal

    # Conviction
    conviction = pd.Series(0.0, index=d.index)
    conviction[base_long]    = 3
    conviction[base_short]   = 3
    conviction[strong_long]  = 6
    conviction[strong_short] = 6
    conviction[(rsi < 40) & signal.isin(['LONG','LONG_STRONG'])]   += 2
    conviction[(rsi > 60) & signal.isin(['SHORT','SHORT_STRONG'])]  += 2
    conviction[(rsi < 30) & signal.isin(['LONG','LONG_STRONG'])]   += 1
    conviction[(rsi > 70) & signal.isin(['SHORT','SHORT_STRONG'])]  += 1
    conviction[long_near_low   & signal.isin(['LONG','LONG_STRONG'])]   += 1
    conviction[short_near_high & signal.isin(['SHORT','SHORT_STRONG'])]  += 1

    # FVG conviction boost
    conviction[d['fvg_bullish'] & signal.isin(['LONG','LONG_STRONG'])]   += 1
    conviction[d['fvg_bearish'] & signal.isin(['SHORT','SHORT_STRONG'])]  += 1

    d['conviction'] = conviction.clip(0, 10).round(0).astype(int)

    # dist_pct
    d['dist_pct'] = (close - mid) / mid.replace(0, np.nan) * 100

    return d


# ── Latest signal extractor ───────────────────────────────────────────────────
def get_latest_signal(df: pd.DataFrame, symbol: str) -> Optional[dict]:
    if df is None or len(df) == 0:
        return None

    last = df.iloc[-1]
    sig  = str(last.get('signal', 'NEUTRAL'))
    if sig == 'NEUTRAL':
        return None

    is_long = 'LONG' in sig
    entry   = float(last['close'])
    sl      = float(last['sl_long']  if is_long else last['sl_short'])
    tp      = float(last['tp_long']  if is_long else last['tp_short'])
    rr      = float(last['rr_long']  if is_long else last['rr_short'])

    row    = last.copy()
    row['dist_pct']     = float(last.get('dist_pct', 0))
    row['dist_from_low']= float(last.get('dist_from_low', 0.5))
    reason = build_reason(row, sig)

    return {
        'symbol'     : symbol,
        'signal'     : sig,
        'conviction' : int(last.get('conviction', 0)),
        'close'      : entry,
        'entry'      : entry,
        'sl'         : round(sl, 6),
        'tp'         : round(tp, 6),
        'rr'         : round(rr, 2),
        'atr'        : round(float(last.get('atr', 0)), 6),
        'vwap_mid'   : round(float(last.get('vwap_mid', 0)), 6),
        'vwap_upper' : round(float(last.get('vwap_upper', 0)), 6),
        'vwap_lower' : round(float(last.get('vwap_lower', 0)), 6),
        'rsi'        : round(float(last.get('rsi', 50)), 1),
        'dist_pct'   : round(float(last.get('dist_pct', 0)), 2),
        'fvg_bullish': bool(last.get('fvg_bullish', False)),
        'fvg_bearish': bool(last.get('fvg_bearish', False)),
        'fvg_top'    : round(float(last.get('fvg_top', 0) or 0), 6),
        'fvg_bottom' : round(float(last.get('fvg_bottom', 0) or 0), 6),
        'reason'     : reason,
        'timestamp'  : last.name,
    }
