"""
main.py
──────────────────────────────────────────────────
Entry point.  Threads:
  1. Screener 5m   — auto-run setiap candle 5m
  2. Screener 15m  — auto-run setiap candle 15m
  3. Telegram bot  — handle /run /status /summary /help
  4. Sheets price  — update harga realtime di Google Sheets

Env vars:
  TELEGRAM_BOT_TOKEN       — dari @BotFather
  TELEGRAM_CHAT_ID         — chat ID kamu
  SCREENER_INTERVAL_5M     — override interval 5m (detik, default: 300)
  SCREENER_INTERVAL_15M    — override interval 15m (detik, default: 900)
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
    log_signal       as sheets_log,
    price_updater_loop,
    get_sheet_stats,
)

# Optional backtest
try:
    from backtest.tracker import log_signal as bt_log, compute_stats
    from backtest.summary import build_summary_message, send_daily_summary
    HAS_BACKTEST = True
except ImportError:
    HAS_BACKTEST = False
    def bt_log(sig): pass
    def compute_stats(days=7): return {"win_rate":0,"total_pnl":0,"tp":0,"sl":0,"open":0}
    def build_summary_message(days=7): return "📊 Backtest module not available."
    def send_daily_summary(chat_id, days=7): pass


# ── Config ────────────────────────────────────────────────────────────────────
CONFIG = {
    "telegram_token"    : os.getenv("TELEGRAM_BOT_TOKEN", ""),
    "telegram_chat_id"  : os.getenv("TELEGRAM_CHAT_ID", ""),
    "interval_5m_sec"   : int(os.getenv("SCREENER_INTERVAL_5M",  "300")),
    "interval_15m_sec"  : int(os.getenv("SCREENER_INTERVAL_15M", "900")),
    "top_n"             : int(os.getenv("SCREENER_TOP_N", "50")),
    "min_conviction"    : int(os.getenv("SCREENER_MIN_CONVICTION", "1")),
    "top_display"       : int(os.getenv("SCREENER_TOP_DISPLAY", "5")),
    "sheets_enabled"    : False,
}

_last_results: dict[str, dict] = {}   # tf → last result
_last_run_time: str = "Never"

# Dedup: key = "DIRECTION:SYMBOL:TF"  → set per scheduler tick
_alerted_5m:  set[str] = set()
_alerted_15m: set[str] = set()


# ── Core run ──────────────────────────────────────────────────────────────────
def do_run(timeframe: str = "15m") -> dict:
    result = run_screener(
        timeframe      = timeframe,
        top_n          = CONFIG["top_n"],
        min_conviction = CONFIG["min_conviction"],
    )
    _last_results[timeframe] = result
    global _last_run_time
    _last_run_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    for sig in result["longs"] + result["shorts"]:
        bt_log(sig)
    return result


def _dispatch_signals(result: dict, alerted: set[str]) -> None:
    """Send Telegram + log to Sheets for new signals only."""
    for sig in result["longs"] + result["shorts"]:
        key = f"{sig['direction']}:{sig['symbol']}:{sig['timeframe']}"
        if key in alerted:
            continue

        # ── Telegram ────────────────────────────────────────────────
        send_signal(sig, CONFIG["telegram_chat_id"])
        alerted.add(key)
        time.sleep(0.3)

        # ── Google Sheets ────────────────────────────────────────────
        if CONFIG["sheets_enabled"]:
            sheets_log(sig)
            time.sleep(0.1)

    alerted.clear()   # reset dedup after full dispatch


# ── Scheduler loops ───────────────────────────────────────────────────────────
def scheduler_5m():
    print(f"[5m] Scheduler started (every {CONFIG['interval_5m_sec']}s)")
    while True:
        try:
            result = do_run("5m")
            if result["longs"] or result["shorts"]:
                _dispatch_signals(result, _alerted_5m)
            else:
                print("[5m] No signals.")
        except Exception as e:
            print(f"[5m] Error: {e}")
        time.sleep(CONFIG["interval_5m_sec"])


def scheduler_15m():
    print(f"[15m] Scheduler started (every {CONFIG['interval_15m_sec']}s)")
    last_daily = None
    while True:
        try:
            result = do_run("15m")
            if result["longs"] or result["shorts"]:
                _dispatch_signals(result, _alerted_15m)
            else:
                print("[15m] No signals.")

            # Daily summary @ 00:00 UTC
            now   = datetime.now(timezone.utc)
            today = now.date()
            if now.hour == 0 and last_daily != today:
                send_daily_summary(CONFIG["telegram_chat_id"], days=7)
                last_daily = today

        except Exception as e:
            print(f"[15m] Error: {e}")
        time.sleep(CONFIG["interval_15m_sec"])


# ── Status / summary ──────────────────────────────────────────────────────────
def get_status() -> str:
    lines = [f"📊 <b>Status</b>  •  {_last_run_time}"]

    for tf in ("5m", "15m"):
        r = _last_results.get(tf)
        if not r:
            continue
        s = r.get("stats", {})
        lines.append(
            f"\n⏱ <b>{tf}</b>  scanned {s.get('total_scanned',0)} coins\n"
            f"🟢 Long  {s.get('long_count',0)}  (🔥 {s.get('strong_long',0)})\n"
            f"🔴 Short {s.get('short_count',0)}  (🔥 {s.get('strong_short',0)})"
        )

    # Backtest
    stats = compute_stats(days=7)
    lines.append(
        f"\n📈 <b>Backtest 7 hari</b>\n"
        f"Win rate  : {stats['win_rate']:.1f}%\n"
        f"PnL total : {stats['total_pnl']:+.2f}%\n"
        f"TP / SL   : {stats['tp']} / {stats['sl']}"
    )

    # Sheets stats
    if CONFIG["sheets_enabled"]:
        ss = get_sheet_stats()
        if ss:
            lines.append(
                f"\n📋 <b>Google Sheets</b>\n"
                f"Total logged : {ss.get('total',0)}\n"
                f"Open         : {ss.get('open',0)}\n"
                f"TP2 ✅       : {ss.get('tp2',0)}\n"
                f"SL ❌        : {ss.get('sl',0)}\n"
                f"Win rate     : {ss.get('win_rate',0):.1f}%"
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

    # Google Sheets
    CONFIG["sheets_enabled"] = init_sheets()

    print("═" * 60)
    print(" VWAP WEEKLY SCREENER — multi-TF + Sheets edition")
    print(f" Timeframes  : 5m  (every {CONFIG['interval_5m_sec']//60} min)")
    print(f"               15m (every {CONFIG['interval_15m_sec']//60} min)")
    print(f" Top N       : {CONFIG['top_n']} coins")
    print(f" Entry zone  : FVG Bullish / Bearish")
    print(f" Min RR      : 1:2")
    print(f" Telegram    : ✅")
    print(f" Google Sheets: {'✅ connected' if CONFIG['sheets_enabled'] else '❌ disabled (set GOOGLE_SHEET_ID)'}")
    print("═" * 60)

    # ── Initial run ──────────────────────────────────────────────────
    for tf in ("5m", "15m"):
        try:
            result = do_run(tf)
            send_result(result, CONFIG["telegram_chat_id"],
                        top_n=CONFIG["top_display"])
            if CONFIG["sheets_enabled"]:
                for sig in result["longs"] + result["shorts"]:
                    sheets_log(sig)
                    time.sleep(0.1)
        except Exception as e:
            print(f"[startup:{tf}] {e}")

    # ── Background threads ───────────────────────────────────────────
    threads = [
        threading.Thread(target=scheduler_5m,   daemon=True),
        threading.Thread(target=scheduler_15m,  daemon=True),
    ]
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
