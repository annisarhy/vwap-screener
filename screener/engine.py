"""
screener/engine.py
──────────────────────────────────────────────────
VWAP Weekly Screener — enhanced with:
  • FVG (Fair Value Gap) entry zones
  • Minimum RR 1:2  (risk = entry→SL, reward = entry→TP ≥ 2×risk)
  • Immediate Telegram alert when criteria met on 15m candle

  🆕 v2 Enhancements:
  • Volume Spike Filter — bounce from FVG must have ≥ 130% avg volume
  • Market Structure Shift (MSS) — break of recent swing high/low
  • Multi-TF Confluence — 1H VWAP alignment check
  • Dynamic SL — based on FVG trigger candle + 0.2% buffer

Signal criteria
───────────────
🟢 LONG  valid when ALL of:
  1. 15m close ABOVE Weekly VWAP mid-line
  2. RSI < 60
  3. Current price is INSIDE a Bullish FVG (between FVG.bottom and FVG.top)
     OR price bounced off FVG (close just exited above FVG.top within last 2 candles)
  4. 🆕 Volume spike ≥ 30% above 20-candle average
  5. 🆕 MSS: candle broke above recent swing high (5-candle lookback)
  6. SL = max(FVG.bottom, trigger_candle_low) × (1 - 0.002)  🆕 Dynamic
  7. TP1 = entry + 1× risk  (mid target)
  8. TP2 = entry + 2× risk  (minimum RR 1:2)
  9. Risk/Reward ≥ 2.0
  10. 🆕 HTF check: 1H VWAP alignment (soft filter — affects conviction)

🔴 SHORT valid when ALL of:
  1. 15m close BELOW Weekly VWAP mid-line
  2. RSI > 40
  3. Current price is INSIDE a Bearish FVG
     OR price rejected from FVG within last 2 candles
  4. 🆕 Volume spike ≥ 30% above 20-candle average
  5. 🆕 MSS: candle broke below recent swing low (5-candle lookback)
  6. SL = min(FVG.top, trigger_candle_high) × (1 + 0.002)  🆕 Dynamic
  7. TP1 = entry - 1× risk
  8. TP2 = entry - 2× risk
  9. Risk/Reward ≥ 2.0
  10. 🆕 HTF check: 1H VWAP alignment (soft filter — affects conviction)
"""

from __future__ import annotations

import os
import time
import numpy as np
import pandas as pd
import ccxt
from datetime import datetime, timezone, timedelta
from typing import Optional

from screener.fvg import detect_fvgs, nearest_fvg, FVG

# ── Constants ─────────────────────────────────────────────────────────────────
SL_BUFFER_PCT       = 0.002    # 0.2% buffer below/above FVG for SL
MIN_RR              = 2.0      # minimum risk:reward
RSI_PERIOD          = 14
VWAP_STD_MULT       = 1.0      # band = VWAP ± N×stddev
FVG_LOOKBACK        = 60       # candles to look back for FVG
VOL_SPIKE_THRESHOLD = 1.15     # 115% of avg = 15% spike (soft filter)
VOL_AVG_PERIOD      = 20       # rolling average window for volume
MSS_LOOKBACK        = 3        # candles to check for swing high/low break (relaxed)

# ── Exchange helpers ──────────────────────────────────────────────────────────
_exchanges: dict[str, ccxt.Exchange] = {}

def _get_exchange(name: str) -> ccxt.Exchange:
    if name not in _exchanges:
        cls = getattr(ccxt, name)
        _exchanges[name] = cls({"enableRateLimit": True})
    return _exchanges[name]


def _fetch_ohlcv(symbol: str, timeframe: str, limit: int = 300) -> Optional[pd.DataFrame]:
    """Try Bybit → OKX → Gate in order. Return first success."""
    sources = [
        ("bybit",  symbol),
        ("okx",    symbol),
        ("gateio", symbol.replace("/", "_")),
    ]
    for exname, sym in sources:
        try:
            ex = _get_exchange(exname)
            raw = ex.fetch_ohlcv(sym, timeframe=timeframe, limit=limit)
            if not raw or len(raw) < 50:
                continue
            df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
            df["timestamp"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
            df = df.drop(columns=["ts"]).reset_index(drop=True)
            return df
        except Exception:
            continue
    return None


def _top_symbols(top_n: int = 50) -> list[str]:
    """Get top-N USDT perpetual symbols by volume from Bybit."""
    try:
        ex = _get_exchange("bybit")
        markets = ex.load_markets()
        tickers = ex.fetch_tickers()
        swap_syms = [
            s for s, m in markets.items()
            if m.get("type") == "swap" and s.endswith("/USDT:USDT")
        ]
        scored = []
        for s in swap_syms:
            t = tickers.get(s, {})
            vol = float(t.get("quoteVolume") or 0)
            scored.append((s, vol))
        scored.sort(key=lambda x: x[1], reverse=True)
        return [s for s, _ in scored[:top_n]]
    except Exception:
        # fallback: well-known pairs
        return [
            "BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT",
            "BNB/USDT:USDT", "XRP/USDT:USDT", "DOGE/USDT:USDT",
            "ADA/USDT:USDT", "AVAX/USDT:USDT", "DOT/USDT:USDT",
            "LINK/USDT:USDT", "MATIC/USDT:USDT", "UNI/USDT:USDT",
            "INJ/USDT:USDT", "ARB/USDT:USDT", "OP/USDT:USDT",
            "SUI/USDT:USDT", "APT/USDT:USDT", "TON/USDT:USDT",
            "TIA/USDT:USDT", "JTO/USDT:USDT",
        ]


# ── Indicators ────────────────────────────────────────────────────────────────
def _calc_rsi(closes: pd.Series, period: int = 14) -> float:
    delta = closes.diff().dropna()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss.replace(0, np.nan)
    rsi   = 100 - 100 / (1 + rs)
    return float(rsi.iloc[-1])


def _calc_weekly_vwap(df: pd.DataFrame) -> tuple[float, float, float]:
    """
    Weekly-anchored VWAP (resets every Monday 00:00 UTC).
    Returns (vwap_mid, vwap_upper, vwap_lower).
    """
    # Find start of current week (Monday)
    now  = datetime.now(timezone.utc)
    week_start = now - timedelta(
        days=now.weekday(),
        hours=now.hour,
        minutes=now.minute,
        seconds=now.second,
        microseconds=now.microsecond,
    )
    week_df = df[df["timestamp"] >= week_start].copy()
    if len(week_df) < 2:
        week_df = df.copy()

    tp = (week_df["high"] + week_df["low"] + week_df["close"]) / 3
    week_df["tp"] = tp
    week_df["tp_vol"] = tp * week_df["volume"]

    cum_tpvol = week_df["tp_vol"].cumsum()
    cum_vol   = week_df["volume"].cumsum()
    vwap_series = cum_tpvol / cum_vol

    vwap_mid = float(vwap_series.iloc[-1])

    # Standard deviation band
    deviation_sq = ((week_df["tp"] - vwap_series) ** 2 * week_df["volume"]).cumsum()
    variance     = deviation_sq / cum_vol
    stddev       = float(np.sqrt(variance.iloc[-1]))

    upper = vwap_mid + VWAP_STD_MULT * stddev
    lower = vwap_mid - VWAP_STD_MULT * stddev
    return vwap_mid, upper, lower


# ── Volume Spike Detection ───────────────────────────────────────────────────
def _check_volume_spike(df: pd.DataFrame) -> tuple[bool, float]:
    """
    Check if the latest candle has a volume spike ≥ 30% above average.

    Returns:
      (has_spike: bool, vol_ratio: float)
      vol_ratio = current_vol / avg_vol  (e.g. 1.50 = 50% above avg)
    """
    if len(df) < VOL_AVG_PERIOD + 1:
        return True, 1.0   # not enough data — assume OK

    avg_vol = df["volume"].iloc[-(VOL_AVG_PERIOD + 1):-1].mean()
    cur_vol = float(df["volume"].iloc[-1])

    if avg_vol <= 0:
        return True, 1.0

    ratio = cur_vol / avg_vol
    return ratio >= VOL_SPIKE_THRESHOLD, round(ratio, 2)


def _check_volume_divergence(df: pd.DataFrame, direction: str) -> bool:
    """
    Check for bearish/bullish volume divergence (price up + volume down = weak).

    Returns True if volume confirms price direction (healthy).
    Returns False if divergence detected (weak signal).
    """
    if len(df) < 3:
        return True

    c1, c2 = df["close"].iloc[-2], df["close"].iloc[-1]
    v1, v2 = df["volume"].iloc[-2], df["volume"].iloc[-1]

    if direction == "LONG":
        # Price going up but volume dropping = bearish divergence
        if c2 > c1 and v2 < v1 * 0.7:   # volume dropped > 30%
            return False
    else:
        # Price going down but volume dropping = bullish divergence
        if c2 < c1 and v2 < v1 * 0.7:
            return False

    return True


# ── Market Structure Shift (MSS) Detection ───────────────────────────────────
def _check_mss(df: pd.DataFrame, direction: str) -> bool:
    """
    Market Structure Shift check on the current timeframe.

    LONG MSS:  current candle high > highest high of last N candles
               (break of recent swing high = bullish structure shift)

    SHORT MSS: current candle low < lowest low of last N candles
               (break of recent swing low = bearish structure shift)

    This ensures we don't enter Long in a Lower-Low trend or
    Short in a Higher-High trend.
    """
    if len(df) < MSS_LOOKBACK + 1:
        return True   # not enough data, assume OK

    curr_high  = float(df["high"].iloc[-1])
    curr_low   = float(df["low"].iloc[-1])
    curr_close = float(df["close"].iloc[-1])

    # Look at the N candles BEFORE the current one
    lookback = df.iloc[-(MSS_LOOKBACK + 1):-1]

    if direction == "LONG":
        # Current close must break above the highest high of last N candles
        swing_high = float(lookback["high"].max())
        return curr_close > swing_high or curr_high > swing_high
    else:
        # Current close must break below the lowest low of last N candles
        swing_low = float(lookback["low"].min())
        return curr_close < swing_low or curr_low < swing_low


# ── Multi-Timeframe Confluence ────────────────────────────────────────────────
def _check_htf_alignment(symbol: str, direction: str) -> tuple[bool, Optional[float]]:
    """
    Check if 1H VWAP aligns with the signal direction.

    LONG  signal: bullish if 1H price > 1H VWAP (trending up on HTF)
    SHORT signal: bearish if 1H price < 1H VWAP (trending down on HTF)

    Returns:
      (aligned: bool, htf_vwap: float or None)
    """
    try:
        df_1h = _fetch_ohlcv(symbol, "1h", limit=200)
        if df_1h is None or len(df_1h) < 20:
            return True, None   # can't check — assume aligned

        vwap_1h, _, _ = _calc_weekly_vwap(df_1h)
        close_1h = float(df_1h["close"].iloc[-1])

        if direction == "LONG":
            aligned = close_1h > vwap_1h
        else:
            aligned = close_1h < vwap_1h

        return aligned, round(vwap_1h, 6)

    except Exception:
        return True, None   # fail-safe: assume aligned


# ── Core signal logic ─────────────────────────────────────────────────────────
def _analyse_symbol(symbol: str, timeframe: str) -> Optional[dict]:
    """
    Full analysis pipeline for one symbol.
    Returns signal dict or None if no valid setup.
    """
    df = _fetch_ohlcv(symbol, timeframe, limit=300)
    if df is None or len(df) < 50:
        return None

    close   = float(df["close"].iloc[-1])
    high    = float(df["high"].iloc[-1])
    low     = float(df["low"].iloc[-1])
    prev_low  = float(df["low"].iloc[-2])
    prev_high = float(df["high"].iloc[-2])

    rsi              = _calc_rsi(df["close"])
    vwap, vwap_u, vwap_l = _calc_weekly_vwap(df)
    fvgs             = detect_fvgs(df, lookback=FVG_LOOKBACK)

    dist_pct = (close - vwap) / vwap * 100

    # ── Volume Spike check (shared for both directions) ──────────────
    vol_spike, vol_ratio = _check_volume_spike(df)

    # ──────────────────────────────────────────────────────────────────
    # LONG setup
    # ──────────────────────────────────────────────────────────────────
    if close > vwap and rsi < 60:
        bull_fvg = nearest_fvg(fvgs, "bullish", close)

        # Entry condition: price bounced FROM bullish FVG
        # (current close above FVG.top, or price just exited FVG upward)
        in_fvg        = bull_fvg and (bull_fvg.bottom <= close <= bull_fvg.top)
        bounced_from  = bull_fvg and (prev_low <= bull_fvg.top and close > bull_fvg.top)

        if bull_fvg and (in_fvg or bounced_from):

            # ── Soft Filter 1: Volume Spike (conviction modifier) ─────
            vol_healthy = _check_volume_divergence(df, "LONG")

            # ── Soft Filter 2: Market Structure Shift ─────────────────
            mss_ok = _check_mss(df, "LONG")

            # ── Dynamic SL ────────────────────────────────────────────
            # Use the LOWER of FVG bottom and trigger candle low
            trigger_low = bull_fvg.trigger_low if bull_fvg.trigger_low > 0 else bull_fvg.bottom
            sl_base = min(bull_fvg.bottom, trigger_low)
            entry = close
            sl    = sl_base * (1 - SL_BUFFER_PCT)
            risk  = entry - sl

            if risk <= 0:
                return None

            tp1 = entry + risk          # 1:1
            tp2 = entry + 2 * risk      # 1:2

            rr  = (tp2 - entry) / risk

            if rr < MIN_RR:
                return None

            # ── Soft Filter 3: HTF alignment ──────────────────────────
            htf_aligned, htf_vwap = _check_htf_alignment(symbol, "LONG")

            strong = bounced_from and (rsi < 50) and vol_spike and mss_ok
            conviction = _conviction(
                rr, rsi, dist_pct, direction="long",
                vol_spike=vol_spike, vol_healthy=vol_healthy,
                mss_ok=mss_ok, htf_aligned=htf_aligned,
            )

            return {
                "symbol"        : symbol.split("/")[0].replace(":USDT", ""),
                "direction"     : "LONG",
                "strong"        : strong,
                "entry"         : round(entry, 6),
                "sl"            : round(sl, 6),
                "tp1"           : round(tp1, 6),
                "tp2"           : round(tp2, 6),
                "rr"            : round(rr, 2),
                "rsi"           : round(rsi, 1),
                "vwap"          : round(vwap, 6),
                "dist_pct"      : round(dist_pct, 3),
                "fvg_top"       : round(bull_fvg.top, 6),
                "fvg_bot"       : round(bull_fvg.bottom, 6),
                "fvg_type"      : "bullish",
                "conviction"    : conviction,
                "timeframe"     : timeframe,
                # 🆕 New fields
                "vol_spike"     : vol_spike,
                "vol_ratio"     : vol_ratio,
                "vol_healthy"   : vol_healthy,
                "mss_confirmed" : mss_ok,
                "htf_aligned"   : htf_aligned,
                "htf_vwap"      : htf_vwap,
                "sl_type"       : "dynamic",
            }

    # ──────────────────────────────────────────────────────────────────
    # SHORT setup
    # ──────────────────────────────────────────────────────────────────
    elif close < vwap and rsi > 40:
        bear_fvg = nearest_fvg(fvgs, "bearish", close)

        in_fvg       = bear_fvg and (bear_fvg.bottom <= close <= bear_fvg.top)
        rejected_at  = bear_fvg and (prev_high >= bear_fvg.bottom and close < bear_fvg.bottom)

        if bear_fvg and (in_fvg or rejected_at):

            # ── Soft Filter 1: Volume Spike (conviction modifier) ─────
            vol_healthy = _check_volume_divergence(df, "SHORT")

            # ── Soft Filter 2: Market Structure Shift ─────────────────
            mss_ok = _check_mss(df, "SHORT")

            # ── Dynamic SL ────────────────────────────────────────────
            # Use the HIGHER of FVG top and trigger candle high
            trigger_high = bear_fvg.trigger_high if bear_fvg.trigger_high > 0 else bear_fvg.top
            sl_base = max(bear_fvg.top, trigger_high)
            entry = close
            sl    = sl_base * (1 + SL_BUFFER_PCT)
            risk  = sl - entry

            if risk <= 0:
                return None

            tp1 = entry - risk
            tp2 = entry - 2 * risk

            rr  = (entry - tp2) / risk

            if rr < MIN_RR:
                return None

            # ── Filter 3: HTF alignment (soft) ────────────────────────
            htf_aligned, htf_vwap = _check_htf_alignment(symbol, "SHORT")

            strong = rejected_at and (rsi > 55) and vol_spike and mss_ok
            conviction = _conviction(
                rr, rsi, dist_pct, direction="short",
                vol_spike=vol_spike, vol_healthy=vol_healthy,
                mss_ok=mss_ok, htf_aligned=htf_aligned,
            )

            return {
                "symbol"        : symbol.split("/")[0].replace(":USDT", ""),
                "direction"     : "SHORT",
                "strong"        : strong,
                "entry"         : round(entry, 6),
                "sl"            : round(sl, 6),
                "tp1"           : round(tp1, 6),
                "tp2"           : round(tp2, 6),
                "rr"            : round(rr, 2),
                "rsi"           : round(rsi, 1),
                "vwap"          : round(vwap, 6),
                "dist_pct"      : round(dist_pct, 3),
                "fvg_top"       : round(bear_fvg.top, 6),
                "fvg_bot"       : round(bear_fvg.bottom, 6),
                "fvg_type"      : "bearish",
                "conviction"    : conviction,
                "timeframe"     : timeframe,
                # 🆕 New fields
                "vol_spike"     : vol_spike,
                "vol_ratio"     : vol_ratio,
                "vol_healthy"   : vol_healthy,
                "mss_confirmed" : mss_ok,
                "htf_aligned"   : htf_aligned,
                "htf_vwap"      : htf_vwap,
                "sl_type"       : "dynamic",
            }

    return None


def _conviction(rr: float, rsi: float, dist_pct: float, direction: str, *,
                vol_spike: bool = True, vol_healthy: bool = True,
                mss_ok: bool = True, htf_aligned: bool = True) -> str:
    """
    Enhanced conviction scoring with new filters.

    Scoring breakdown (max 10):
      RR ≥ 3.0    → +2    |  RR ≥ 2.0    → +1
      RSI extreme  → +2    |  RSI moderate → +1
      VWAP dist    → +1
      Vol spike    → +1    🆕
      Vol healthy  → +1    🆕
      MSS confirm  → +1    🆕
      HTF aligned  → +1    🆕
    """
    score = 0

    # RR scoring
    if rr >= 3.0:
        score += 2
    elif rr >= 2.0:
        score += 1

    # RSI scoring
    if direction == "long":
        if rsi < 45:
            score += 2
        elif rsi < 55:
            score += 1
    else:
        if rsi > 60:
            score += 2
        elif rsi > 50:
            score += 1

    # VWAP distance
    if abs(dist_pct) > 0.5:
        score += 1

    # 🆕 Volume spike bonus
    if vol_spike:
        score += 1

    # 🆕 Volume health (no divergence)
    if vol_healthy:
        score += 1

    # 🆕 MSS confirmed
    if mss_ok:
        score += 1

    # 🆕 HTF alignment bonus
    if htf_aligned:
        score += 1

    if score >= 6:
        return "🟢 High"
    elif score >= 3:
        return "🟡 Medium"
    else:
        return "🔴 Low"


# ── Public API ────────────────────────────────────────────────────────────────
def run_screener(
    timeframe: str = "15m",
    top_n: int = 50,
    min_conviction: int = 1,
) -> dict:
    """
    Scan top_n symbols and return all valid setups.
    `min_conviction`: 1=Low+, 2=Medium+, 3=High only
    """
    symbols = _top_symbols(top_n)
    longs, shorts = [], []

    print(f"[engine] Scanning {len(symbols)} symbols on {timeframe}...")
    print(f"[engine] Soft filters: Vol≥{VOL_SPIKE_THRESHOLD:.0%} | MSS(swing{MSS_LOOKBACK}) | HTF(1H) | DynSL")

    for sym in symbols:
        try:
            sig = _analyse_symbol(sym, timeframe)
            if sig is None:
                continue

            conv_rank = {"🔴 Low": 1, "🟡 Medium": 2, "🟢 High": 3}
            if conv_rank.get(sig["conviction"], 0) < min_conviction:
                continue

            if sig["direction"] == "LONG":
                longs.append(sig)
            else:
                shorts.append(sig)

        except Exception as e:
            print(f"[engine] {sym}: {e}")
        time.sleep(0.05)   # gentle rate-limit

    # Sort by conviction desc, then RR desc
    def sort_key(s):
        rank = {"🟢 High": 3, "🟡 Medium": 2, "🔴 Low": 1}
        return (rank.get(s["conviction"], 0), s["rr"])

    longs.sort(key=sort_key, reverse=True)
    shorts.sort(key=sort_key, reverse=True)

    return {
        "timeframe" : timeframe,
        "scanned_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "longs"     : longs,
        "shorts"    : shorts,
        "stats": {
            "total_scanned": len(symbols),
            "long_count"   : len(longs),
            "short_count"  : len(shorts),
            "strong_long"  : sum(1 for s in longs  if s["strong"]),
            "strong_short" : sum(1 for s in shorts if s["strong"]),
        },
    }
