"""
screener/engine.py
──────────────────────────────────────────────────
VWAP Weekly Screener — enhanced with:
  • FVG (Fair Value Gap) entry zones
  • Minimum RR 1:2  (risk = entry→SL, reward = entry→TP ≥ 2×risk)
  • Immediate Telegram alert when criteria met on 15m candle

Signal criteria
───────────────
🟢 LONG  valid when ALL of:
  1. 15m close ABOVE Weekly VWAP mid-line
  2. RSI < 60
  3. Current price is INSIDE a Bullish FVG (between FVG.bottom and FVG.top)
     OR price bounced off FVG (close just exited above FVG.top within last 2 candles)
  4. SL = FVG.bottom - small_buffer
  5. TP1 = entry + 1× risk  (mid target)
  6. TP2 = entry + 2× risk  (minimum RR 1:2)
  7. Risk/Reward ≥ 2.0

🔴 SHORT valid when ALL of:
  1. 15m close BELOW Weekly VWAP mid-line
  2. RSI > 40
  3. Current price is INSIDE a Bearish FVG
     OR price rejected from FVG within last 2 candles
  4. SL = FVG.top + small_buffer
  5. TP1 = entry - 1× risk
  6. TP2 = entry - 2× risk
  7. Risk/Reward ≥ 2.0
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
SL_BUFFER_PCT  = 0.002   # 0.2% buffer below/above FVG for SL
MIN_RR         = 2.0     # minimum risk:reward
RSI_PERIOD     = 14
VWAP_STD_MULT  = 1.0     # band = VWAP ± N×stddev
FVG_LOOKBACK   = 60      # candles to look back for FVG

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
            entry = close
            sl    = bull_fvg.bottom * (1 - SL_BUFFER_PCT)
            risk  = entry - sl

            if risk <= 0:
                return None

            tp1 = entry + risk          # 1:1
            tp2 = entry + 2 * risk      # 1:2

            rr  = (tp2 - entry) / risk

            if rr < MIN_RR:
                return None

            strong = bounced_from and (rsi < 50)
            conviction = _conviction(rr, rsi, dist_pct, direction="long")

            return {
                "symbol"    : symbol.split("/")[0].replace(":USDT", ""),
                "direction" : "LONG",
                "strong"    : strong,
                "entry"     : round(entry, 6),
                "sl"        : round(sl, 6),
                "tp1"       : round(tp1, 6),
                "tp2"       : round(tp2, 6),
                "rr"        : round(rr, 2),
                "rsi"       : round(rsi, 1),
                "vwap"      : round(vwap, 6),
                "dist_pct"  : round(dist_pct, 3),
                "fvg_top"   : round(bull_fvg.top, 6),
                "fvg_bot"   : round(bull_fvg.bottom, 6),
                "fvg_type"  : "bullish",
                "conviction": conviction,
                "timeframe" : timeframe,
            }

    # ──────────────────────────────────────────────────────────────────
    # SHORT setup
    # ──────────────────────────────────────────────────────────────────
    elif close < vwap and rsi > 40:
        bear_fvg = nearest_fvg(fvgs, "bearish", close)

        in_fvg       = bear_fvg and (bear_fvg.bottom <= close <= bear_fvg.top)
        rejected_at  = bear_fvg and (prev_high >= bear_fvg.bottom and close < bear_fvg.bottom)

        if bear_fvg and (in_fvg or rejected_at):
            entry = close
            sl    = bear_fvg.top * (1 + SL_BUFFER_PCT)
            risk  = sl - entry

            if risk <= 0:
                return None

            tp1 = entry - risk
            tp2 = entry - 2 * risk

            rr  = (entry - tp2) / risk

            if rr < MIN_RR:
                return None

            strong = rejected_at and (rsi > 55)
            conviction = _conviction(rr, rsi, dist_pct, direction="short")

            return {
                "symbol"    : symbol.split("/")[0].replace(":USDT", ""),
                "direction" : "SHORT",
                "strong"    : strong,
                "entry"     : round(entry, 6),
                "sl"        : round(sl, 6),
                "tp1"       : round(tp1, 6),
                "tp2"       : round(tp2, 6),
                "rr"        : round(rr, 2),
                "rsi"       : round(rsi, 1),
                "vwap"      : round(vwap, 6),
                "dist_pct"  : round(dist_pct, 3),
                "fvg_top"   : round(bear_fvg.top, 6),
                "fvg_bot"   : round(bear_fvg.bottom, 6),
                "fvg_type"  : "bearish",
                "conviction": conviction,
                "timeframe" : timeframe,
            }

    return None


def _conviction(rr: float, rsi: float, dist_pct: float, direction: str) -> str:
    score = 0
    if rr >= 3.0:
        score += 2
    elif rr >= 2.0:
        score += 1

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

    if abs(dist_pct) > 0.5:
        score += 1

    if score >= 4:
        return "🟢 High"
    elif score >= 2:
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
