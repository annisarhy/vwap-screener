"""
backtest/summary.py
──────────────────────────────────────────────────
Format summary backtest untuk Telegram.
Data diambil dari Google Sheets via backtest/tracker.py
"""

from __future__ import annotations
from datetime import datetime, timezone, timedelta

from backtest.tracker import compute_stats


def build_summary_message(days: int = 7) -> str:
    """
    Buat pesan ringkasan performa untuk command /summary.
    Contoh output:

    📊 Ringkasan 7 Hari Terakhir
    ─────────────────────────────
    Total sinyal : 38
    Closed       : 25  (OPEN: 13)
    TP2 ✅        : 14  (56.0%)
    TP1          : 3   (12.0%)
    SL ❌         : 8   (32.0%)

    📈 Win rate  : 68.0%
    💰 PnL total : +18.40%
    📅 Periode   : 2026-05-08 → 2026-05-15
    """
    stats = compute_stats(days=days)

    closed   = stats["tp"] + stats["sl"]
    tp_rate  = stats["tp"] / closed * 100 if closed > 0 else 0
    sl_rate  = stats["sl"] / closed * 100 if closed > 0 else 0
    tp3_rate = stats.get("tp3", 0) / closed * 100 if closed > 0 else 0
    tp2_rate = stats["tp2"] / closed * 100 if closed > 0 else 0
    tp1_rate = stats["tp1"] / closed * 100 if closed > 0 else 0
    tsl_rate = stats.get("tsl", 0) / closed * 100 if closed > 0 else 0

    now      = datetime.now(timezone.utc)
    date_end = now.strftime("%Y-%m-%d")
    date_start = (now - timedelta(days=days)).strftime("%Y-%m-%d")

    divider = "─" * 34

    pnl_sign = "+" if stats["total_pnl"] >= 0 else ""
    win_emoji = "🔥" if stats["win_rate"] >= 60 else ("⚠️" if stats["win_rate"] >= 40 else "❌")

    lines = [
        f"📊 <b>Ringkasan {days} Hari Terakhir</b>",
        divider,
        f"Total sinyal : {stats['total']}",
        f"Closed       : {closed}  (OPEN: {stats['open']})",
        f"🚀 TP3 ✅     : {stats.get('tp3', 0)}  ({tp3_rate:.1f}%)  ← extended",
        f"🏆 TP2        : {stats['tp2']}  ({tp2_rate:.1f}%)",
        f"🎯 TP1        : {stats['tp1']}  ({tp1_rate:.1f}%)",
        f"💡 TSL ✅      : {stats.get('tsl', 0)}  ({tsl_rate:.1f}%)  trailing",
        f"🛑 SL  ❌      : {stats['sl']}  ({sl_rate:.1f}%)",
        "",
        f"{win_emoji} Win rate  : <b>{stats['win_rate']:.1f}%</b>",
        f"💰 PnL total : <b>{pnl_sign}{stats['total_pnl']:.2f}%</b>",
        f"📅 Periode   : {date_start} → {date_end}",
    ]

    if closed == 0:
        lines.append("")
        lines.append("⏳ Belum ada sinyal closed dalam periode ini.")
        lines.append("Backtest akan muncul setelah sinyal hit TP/SL.")

    return "\n".join(lines)


def send_daily_summary(chat_id: str, days: int = 7) -> None:
    """Kirim ringkasan harian ke Telegram (dipanggil scheduler jam 00:00 UTC)."""
    try:
        from notify.telegram import _send, _get_token
        msg   = build_summary_message(days)
        token = _get_token()
        if token and chat_id:
            _send(token, chat_id, msg)
            print(f"[backtest] Daily summary sent to {chat_id}")
    except Exception as e:
        print(f"[backtest] send_daily_summary error: {e}")
