"""
main.py
──────────────────────────────────────────────────
Entry point.  Threads:
  1. Screener 15m  — auto-run setiap candle 15m
  2. Telegram bot  — handle /run /status /summary /help
  3. Sheets price  — update harga + backtest status di Google Sheets

Rules pengiriman sinyal:
  • Hanya 15m (5m dihapus — terlalu noise)
  • Coin yang sudah ada posisi OPEN / TP1 di sheet TIDAK dikirim ulang
    sampai posisinya close (hit SL / TP2)
  • Dedup per run: key = DIRECTION:SYMBOL agar tidak kirim 2x dalam 1 scan

Env vars:
  TELEGRAM_BOT_TOKEN       — dari @BotFather
  TELEGRAM_CHAT_ID         — chat ID kamu
  SCREENER_INTERVAL        — interval scan dalam detik (default: 900 = 15m)
  SCREENER_TOP_N           — jumlah coin yang di-scan (default: 50)
  SCREENER_MIN_CONVICTION  — min conviction 1=Low 2=Med 3=High (default: 1)
  SCREENER_TOP_DISPLAY     — max coin per summary di TG (default: 5)

  GOOGLE_SHEET_ID               — spreadsheet ID dari URL
  GOOGLE_SERVICE_ACCOUNT_JSON   — isi service account JSON (satu baris)
  GOOGLE_SERVICE_ACCOUNT_FILE   — atau path file JSON (default: /app/sa.json)
  GOOGLE_SHEET_TAB              — nama tab (default: Signals)
  PRICE_REFRESH_SEC             — interval update harga (default: 60)
"""

import os
import time
import threading
from datetime import datetime, timezone

from screener.engine import run_screener
from notify.telegram import send_result, send_signal, TelegramBot
from notify.sheets import (
    init_sheets,
    log_signal        as sheets_log,
    price_updater_loop,
    get_sheet_stats,
    has_open_position,
)

# Optional backtest
try:
    from backtest.tracker import log_signal as bt_log, compute_stats
    from backtest.summary import build_summary_message, send_daily_summary
    HAS_BACKTEST = True
except ImportError:
    HAS_BACKTEST = False
    def bt_log(sig): pass
    def compute_stats(days=7): return {"win_rate": 0, "total_pnl": 0, "tp": 0, "sl": 0, "open": 0}
    def build_summary_message(days=7): return "📊 Backtest module not available."
    def send_daily_summary(chat_id, days=7): pass


# ── Config ────────────────────────────────────────────────────────────────────
CONFIG = {
    "telegram_token"  : os.getenv("TELEGRAM_BOT_TOKEN", ""),
    "telegram_chat_id": os.getenv("TELEGRAM_CHAT_ID", ""),
    "interval_sec"    : int(os.getenv("SCREENER_INTERVAL", "900")),
    "top_n"           : int(os.getenv("SCREENER_TOP_N", "50")),
    "min_conviction"  : int(os.getenv("SCREENER_MIN_CONVICTION", "1")),
    "top_display"     : int(os.getenv("SCREENER_TOP_DISPLAY", "5")),
    "sheets_enabled"  : False,
}

_last_result: dict  = {}
_last_run_time: str = "Never"

# Dedup dalam satu run: hindari kirim sinyal yang sama 2x di scan yang sama
_alerted_this_run: set[str] = set()


# ── Core run ──────────────────────────────────────────────────────────────────
def do_run(timeframe: str = "15m") -> dict:
    result = run_screener(
        timeframe      = timeframe,
        top_n          = CONFIG["top_n"],
        min_conviction = CONFIG["min_conviction"],
    )
    global _last_result, _last_run_time
    _last_result   = result
    _last_run_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    for sig in result["longs"] + result["shorts"]:
        bt_log(sig)
    return result


def _dispatch_signals(result: dict) -> None:
    """
    Kirim sinyal ke Telegram + catat ke Sheets.

    Filter berlapis:
      1. Dedup dalam run ini (key = DIRECTION:SYMBOL)
      2. Skip jika coin masih OPEN/TP1 di sheet (posisi belum close)
    """
    sent = 0
    skipped_dedup   = 0
    skipped_open    = 0

    for sig in result["longs"] + result["shorts"]:
        sym  = sig["symbol"]
        dirn = sig["direction"]
        key  = f"{dirn}:{sym}"

        # ── Filter 1: dedup dalam run ini ────────────────────────────
        if key in _alerted_this_run:
            skipped_dedup += 1
            continue

        # ── Filter 2: posisi masih OPEN di sheet ─────────────────────
        if CONFIG["sheets_enabled"] and has_open_position(sym, dirn):
            print(f"[dispatch] Skip {dirn} {sym} — masih OPEN di sheet")
            skipped_open += 1
            continue

        # ── Kirim ────────────────────────────────────────────────────
        send_signal(sig, CONFIG["telegram_chat_id"])
        _alerted_this_run.add(key)
        sent += 1
        time.sleep(0.3)

        if CONFIG["sheets_enabled"]:
            sheets_log(sig)
            time.sleep(0.1)

    # Reset dedup setelah satu run selesai
    _alerted_this_run.clear()

    print(f"[dispatch] Sent={sent}  skip_dedup={skipped_dedup}  skip_open={skipped_open}")


# ── Scheduler ─────────────────────────────────────────────────────────────────
def scheduler_loop():
    interval = CONFIG["interval_sec"]
    print(f"[scheduler] 15m only, every {interval}s")
    last_daily = None

    while True:
        try:
            print("[scheduler] Running 15m screener...")
            result = do_run("15m")

            if result["longs"] or result["shorts"]:
                _dispatch_signals(result)
            else:
                print("[scheduler] No signals.")

            # Daily summary @ 00:00 UTC
            now   = datetime.now(timezone.utc)
            today = now.date()
            if now.hour == 0 and last_daily != today:
                send_daily_summary(CONFIG["telegram_chat_id"], days=7)
                last_daily = today

        except Exception as e:
            print(f"[scheduler] Error: {e}")

        time.sleep(interval)


# ── Status / summary ──────────────────────────────────────────────────────────
def get_status() -> str:
    lines = [f"📊 <b>Status</b>  •  {_last_run_time}"]

    r = _last_result
    if r:
        s = r.get("stats", {})
        lines.append(
            f"\n⏱ <b>15m</b>  scanned {s.get('total_scanned', 0)} coins\n"
            f"🟢 Long  {s.get('long_count', 0)}  (🔥 {s.get('strong_long', 0)})\n"
            f"🔴 Short {s.get('short_count', 0)}  (🔥 {s.get('strong_short', 0)})"
        )

    stats = compute_stats(days=7)
    lines.append(
        f"\n📈 <b>Backtest 7 hari</b>\n"
        f"Win rate  : {stats['win_rate']:.1f}%\n"
        f"PnL total : {stats['total_pnl']:+.2f}%\n"
        f"TP / SL   : {stats['tp']} / {stats['sl']}"
    )

    if CONFIG["sheets_enabled"]:
        ss = get_sheet_stats()
        if ss:
            lines.append(
                f"\n📋 <b>Google Sheets</b>\n"
                f"Total logged : {ss.get('total', 0)}\n"
                f"Open         : {ss.get('open', 0)}\n"
                f"TP2 ✅       : {ss.get('tp2', 0)}\n"
                f"SL ❌        : {ss.get('sl', 0)}\n"
                f"Win rate     : {ss.get('win_rate', 0):.1f}%"
            )

    return "\n".join(lines)


def get_summary(days: int = 7) -> str:
    return build_summary_message(days)


# ── Validate ──────────────────────────────────────────────────────────────────
def validate():
    missing = []
    if not CONFIG["telegram_token"]:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not CONFIG["telegram_chat_id"]:
        missing.append("TELEGRAM_CHAT_ID")
    if missing:
        raise ValueError(f"Missing env vars: {', '.join(missing)}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    validate()
    CONFIG["sheets_enabled"] = init_sheets()

    print("═" * 60)
    print(" VWAP WEEKLY SCREENER")
    print(f" Timeframe   : 15m only  (5m disabled — too noisy)")
    print(f" Interval    : every {CONFIG['interval_sec']//60} min")
    print(f" Top N       : {CONFIG['top_n']} coins")
    print(f" Entry zone  : FVG Bullish / Bearish")
    print(f" Min RR      : 1:2")
    print(f" Dedup       : skip coin yg masih OPEN di sheet")
    print(f" Telegram    : ✅")
    print(f" Google Sheets: {'✅ connected' if CONFIG['sheets_enabled'] else '❌ disabled'}")
    print("═" * 60)

    # ── Initial run ──────────────────────────────────────────────────
    try:
        result = do_run("15m")
        send_result(result, CONFIG["telegram_chat_id"], top_n=CONFIG["top_display"])
        if CONFIG["sheets_enabled"]:
            for sig in result["longs"] + result["shorts"]:
                if not has_open_position(sig["symbol"], sig["direction"]):
                    sheets_log(sig)
                    time.sleep(0.1)
    except Exception as e:
        print(f"[startup] {e}")

    # ── Background threads ───────────────────────────────────────────
    threads = [threading.Thread(target=scheduler_loop, daemon=True)]
    if CONFIG["sheets_enabled"]:
        threads.append(threading.Thread(target=price_updater_loop, daemon=True))
    for t in threads:
        t.start()

    # ── Telegram bot (main thread, blocking) ─────────────────────────
    bot = TelegramBot(
        chat_id        = CONFIG["telegram_chat_id"],
        on_run_cmd     = do_run,
        on_status_cmd  = get_status,
        on_summary_cmd = get_summary,
    )
    bot.start_polling()


if __name__ == "__main__":
    main()
