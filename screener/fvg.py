"""
screener/fvg.py
──────────────────────────────────────────────────
Fair Value Gap (FVG) detector.

Bullish FVG  : candle[i-2].high  <  candle[i].low
               (gap between two candles, bullish imbalance)

Bearish FVG  : candle[i-2].low   >  candle[i].high
               (gap between two candles, bearish imbalance)

Returns the most recent unmitigated FVG of each type.
A FVG is "mitigated" when price trades through the gap's midpoint.

Enhanced: Now stores trigger candle data for Dynamic SL calculation.
"""

from dataclasses import dataclass
from typing import Optional
import pandas as pd


@dataclass
class FVG:
    kind: str           # 'bullish' | 'bearish'
    top: float          # upper edge of gap
    bottom: float       # lower edge of gap
    mid: float          # midpoint of gap
    idx: int            # candle index where FVG formed (candle[i])
    timestamp: pd.Timestamp
    # Trigger candle data for Dynamic SL
    trigger_high: float = 0.0   # high of the FVG trigger candle (candle[i-1])
    trigger_low: float  = 0.0   # low of the FVG trigger candle (candle[i-1])


def detect_fvgs(df: pd.DataFrame, lookback: int = 50) -> list[FVG]:
    """
    Scan the last `lookback` candles for all unmitigated FVGs.

    df must have columns: open, high, low, close, timestamp (index or column).
    Returns list of FVG objects sorted newest-first.
    """
    if len(df) < 3:
        return []

    sub = df.iloc[-lookback:].reset_index(drop=True)
    fvgs: list[FVG] = []

    for i in range(2, len(sub)):
        prev2_high = sub.loc[i - 2, "high"]
        prev2_low  = sub.loc[i - 2, "low"]
        curr_low   = sub.loc[i, "low"]
        curr_high  = sub.loc[i, "high"]

        # Trigger candle = candle[i-1] (the middle candle of the 3-candle pattern)
        trigger_high = sub.loc[i - 1, "high"]
        trigger_low  = sub.loc[i - 1, "low"]

        ts = sub.loc[i, "timestamp"] if "timestamp" in sub.columns else sub.index[i]

        # ── Bullish FVG ───────────────────────────────────────────────
        if prev2_high < curr_low:
            bottom = prev2_high
            top    = curr_low
            mid    = (top + bottom) / 2

            # Check if gap has been mitigated by subsequent candles
            mitigated = _is_mitigated_bull(sub, i, mid)
            if not mitigated:
                fvgs.append(FVG(
                    "bullish", top=top, bottom=bottom, mid=mid,
                    idx=i, timestamp=ts,
                    trigger_high=trigger_high, trigger_low=trigger_low,
                ))

        # ── Bearish FVG ───────────────────────────────────────────────
        elif prev2_low > curr_high:
            top    = prev2_low
            bottom = curr_high
            mid    = (top + bottom) / 2

            mitigated = _is_mitigated_bear(sub, i, mid)
            if not mitigated:
                fvgs.append(FVG(
                    "bearish", top=top, bottom=bottom, mid=mid,
                    idx=i, timestamp=ts,
                    trigger_high=trigger_high, trigger_low=trigger_low,
                ))

    # Sort newest first
    fvgs.sort(key=lambda f: f.idx, reverse=True)
    return fvgs


def _is_mitigated_bull(df: pd.DataFrame, from_idx: int, mid: float) -> bool:
    """Bullish FVG is mitigated when a candle close goes below its midpoint."""
    for j in range(from_idx + 1, len(df)):
        if df.loc[j, "close"] < mid:
            return True
    return False


def _is_mitigated_bear(df: pd.DataFrame, from_idx: int, mid: float) -> bool:
    """Bearish FVG is mitigated when a candle close goes above its midpoint."""
    for j in range(from_idx + 1, len(df)):
        if df.loc[j, "close"] > mid:
            return True
    return False


def nearest_fvg(fvgs: list[FVG], kind: str, price: float) -> Optional[FVG]:
    """
    Return the nearest unmitigated FVG of `kind` relevant to current `price`.

    Bullish FVG entry:
      - Price inside FVG  (bottom ≤ price ≤ top)  → "filling" the gap → valid entry
      - Price just above FVG (top < price ≤ top×1.003)  → bounce confirmed → valid entry
      - FVG top < price (more distant)  → still valid as support

    Bearish FVG entry:
      - Price inside FVG  → valid short entry
      - Price just below FVG (bottom×0.997 ≤ price < bottom)  → rejection confirmed
      - FVG bottom > price  → still valid as resistance

    Returns the FVG closest (by midpoint distance) to current price.
    """
    candidates = [f for f in fvgs if f.kind == kind]
    if not candidates:
        return None

    if kind == "bullish":
        # Accept FVGs at or below price (support), within 2% above also (recent bounce)
        relevant = [
            f for f in candidates
            if f.bottom <= price * 1.02   # FVG bottom not too far above
        ]
        if not relevant:
            return None
        return min(relevant, key=lambda f: abs(price - f.mid))

    else:  # bearish
        # Accept FVGs at or above price (resistance), within 2% below also (recent rejection)
        relevant = [
            f for f in candidates
            if f.top >= price * 0.98
        ]
        if not relevant:
            return None
        return min(relevant, key=lambda f: abs(price - f.mid))
