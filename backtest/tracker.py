"""
backtest/tracker.py
──────────────────────────────────────────────────
Backtest tracker yang baca langsung dari Google Sheets.
Tidak perlu database terpisah — sheet IS the database.

compute_stats(days)  → win rate, PnL, TP/SL count dari N hari terakhir
"""

from __future__ import annotations
from datetime import datetime, timezone, timedelta


def log_signal(sig: dict) -> None:
    """
    No-op: logging sudah ditangani oleh notify/sheets.py (sheets_log).
    Fungsi ini ada supaya import di main.py tidak error.
    """
    pass


def compute_stats(days: int = 7) -> dict:
    """
    Hitung statistik backtest dari data Google Sheets.
    Ambil semua sinyal dalam N hari terakhir yang sudah closed (TP/SL).
    """
    try:
        from notify.sheets import _ws, _lock, _enabled, COL
        if not _enabled or _ws is None:
            return _empty_stats()

        with _lock:
            rows = _ws.get_all_values()[1:]   # skip header

        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        tp1_n = tp2_n = sl_n = open_n = 0
        pnl_total = 0.0

        for row in rows:
            if len(row) < 17:
                continue

            # Parse timestamp
            try:
                ts_str = row[COL["Timestamp (UTC)"] - 1]
                ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                if ts < cutoff:
                    continue
            except Exception:
                continue

            status = row[COL["Status"] - 1]
            pnl_str = row[COL["PnL %"] - 1].replace("%", "").replace("+", "").strip()

            try:
                pnl_val = float(pnl_str) if pnl_str and pnl_str != "–" else 0.0
            except ValueError:
                pnl_val = 0.0

            if "TP2" in status:
                tp2_n    += 1
                pnl_total += pnl_val
            elif status == "TP1":
                tp1_n    += 1
                pnl_total += pnl_val
            elif "SL" in status:
                sl_n     += 1
                pnl_total += pnl_val
            else:
                open_n += 1

        closed  = tp2_n + tp1_n + sl_n
        win_rate = (tp2_n + tp1_n) / closed * 100 if closed > 0 else 0.0

        return {
            "win_rate" : win_rate,
            "total_pnl": pnl_total,
            "tp"       : tp2_n + tp1_n,   # TP1 + TP2
            "tp1"      : tp1_n,
            "tp2"      : tp2_n,
            "sl"       : sl_n,
            "open"     : open_n,
            "total"    : len(rows),
            "days"     : days,
        }

    except Exception as e:
        print(f"[backtest] compute_stats error: {e}")
        return _empty_stats()


def _empty_stats() -> dict:
    return {
        "win_rate": 0.0, "total_pnl": 0.0,
        "tp": 0, "tp1": 0, "tp2": 0,
        "sl": 0, "open": 0, "total": 0, "days": 7,
    }
