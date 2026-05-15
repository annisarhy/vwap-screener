"""
main.py
──────────────────────────────────────────────────
Entry point. Two threads:
  1. Screener scheduler — auto-run setiap candle 15m (configurable)
  2. Telegram bot       — handle /run /status /summary /help

Perubahan dari versi sebelumnya:
  • Entry harus berada di FVG Bullish (long) atau FVG Bearish (short)
  • Minimum RR 1:2 (TP2 = entry ± 2× risk)
  • Setiap sinyal valid langsung dikirim ke Telegram (1 coin pun langsung alert)
  • Default interval = 15 menit (setiap candle 15m)

Env vars:
  TELEGRAM_BOT_TOKEN    — dari @BotFather
  TELEGRAM_CHAT_ID      — chat ID kamu
  SCREENER_INTERVAL     — menit antar auto-run (default: 15)
  SCREENER_TIMEFRAME    — timeframe default (default: 15m)
  SCREENER_TOP_N        — jumlah coin yang di-scan (default: 50)
  SCREENER_MIN_CONVICTION — min conviction 1=Low 2=Med 3=High (default: 1)
  SCREENER_TOP_DISPLAY  — max coin per section di TG (default: 5)
  SIGNAL_LOG_PATH       — path log JSON (default: /app/data/signals_log.json)
"""

import os
import time
import threading
from datetime import datetime, timezone

from screener.engine import run_screener
from notify.telegram import send_result, send_signal, TelegramBot

# Optional backtest — import defensively
try:
    from backtest.tracker import log_signal, compute_stats
    from backtest.summary import build_summary_message, send_daily_summary
    HAS_BACKTEST = True
except ImportError:
    HAS_BACKTEST = False
    def log_signal(sig): pass
    def compute_stats(days=7): return {"win_rate":0,"total_pnl":0,"tp":0,"sl":0,"open":0}
    def build_summary_message(days=7): return "📊 Backtest module not available."
    def send_daily_summary(chat_id, days=7): pass


# ── Config ────────────────────────────────────────────────────────────────────
CONFIG = {
    "telegram_token"  : os.getenv("TELEGRAM_BOT_TOKEN", ""),
    "telegram_chat_id": os.getenv("TELEGRAM_CHAT_ID", ""),
    "interval_min"    : int(os.getenv("SCREENER_INTERVAL", "15")),
    "timeframe"       : os.getenv("SCREENER_TIMEFRAME", "15m"),
    "top_n"           : int(os.getenv("SCREENER_TOP_N", "50")),
    "min_conviction"  : int(os.getenv("SCREENER_MIN_CONVICTION", "1")),
    "top_display"     : int(os.getenv("SCREENER_TOP_DISPLAY", "5")),
}

_last_result   : dict = {}
_last_run_time : str  = "Never"

# De-duplicate: track symbols already alerted in last N candles
_alerted_this_run: set[str] = set()


# ── Run screener ──────────────────────────────────────────────────────────────
def do_run(timeframe: str = None) -> dict:
    global _last_result, _last_run_time, _alerted_this_run

    tf     = timeframe or CONFIG["timeframe"]
    result = run_screener(
        timeframe      = tf,
        top_n          = CONFIG["top_n"],
        min_conviction = CONFIG["min_conviction"],
    )

    _last_result   = result
    _last_run_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Log signals for backtest
    for sig in result["longs"] + result["shorts"]:
        log_signal(sig)

    return result


def get_status() -> str:
    if not _last_result:
        return "📭 Belum ada run. Kirim /run untuk mulai."
    s     = _last_result.get("stats", {})
    stats = compute_stats(days=7)
    return (
        f"📊 <b>Status Run Terakhir</b>\n"
        f"🕐 {_last_run_time}\n"
        f"⏱ Timeframe  : {_last_result.get('timeframe','?')}\n"
        f"🔍 Scanned   : {s.get('total_scanned',0)} coins\n"
        f"🟢 Long      : {s.get('long_count',0)} "
        f"(🔥 strong: {s.get('strong_long',0)})\n"
        f"🔴 Short     : {s.get('short_count',0)} "
        f"(🔥 strong: {s.get('strong_short',0)})\n\n"
        f"📈 <b>Backtest 7 Hari</b>\n"
        f"Win rate  : {stats['win_rate']:.1f}%\n"
        f"Total PnL : {stats['total_pnl']:+.2f}%\n"
        f"TP/SL/Open: {stats['tp']}/{stats['sl']}/{stats['open']}"
    )


def get_summary(days: int = 7) -> str:
    return build_summary_message(days)


# ── Scheduler loop ────────────────────────────────────────────────────────────
def scheduler_loop():
    interval_sec      = CONFIG["interval_min"] * 60
    last_summary_date = None

    print(f"[scheduler] Auto-run every {CONFIG['interval_min']} min "
          f"(timeframe: {CONFIG['timeframe']})")

    while True:
        try:
            print("[scheduler] Running screener...")
            result = do_run()

            all_sigs = result["longs"] + result["shorts"]

            if all_sigs:
                # ─── Kirim alert individual per sinyal baru ──────────
                for sig in all_sigs:
                    key = f"{sig['direction']}:{sig['symbol']}"
                    if key not in _alerted_this_run:
                        send_signal(sig, CONFIG["telegram_chat_id"])
                        _alerted_this_run.add(key)
                        time.sleep(0.4)

                # Reset dedup set setiap run (biar tidak skip sinyal baru)
                # tapi hanya reset setelah semua sinyal terkirim
                _alerted_this_run.clear()
            else:
                print("[scheduler] Tidak ada sinyal baru.")

            # Daily summary 00:00 UTC
            now   = datetime.now(timezone.utc)
            today = now.date()
            if now.hour == 0 and last_summary_date != today:
                print("[scheduler] Sending daily summary...")
                send_daily_summary(CONFIG["telegram_chat_id"], days=7)
                last_summary_date = today

            print(f"[scheduler] Done. Next in {CONFIG['interval_min']} min.")

        except Exception as e:
            print(f"[scheduler] Error: {e}")

        time.sleep(interval_sec)


# ── Validate env ──────────────────────────────────────────────────────────────
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
    print("═" * 60)
    print(" VWAP WEEKLY SCREENER — enhanced edition")
    print(f" Timeframe  : {CONFIG['timeframe']}")
    print(f" Top N      : {CONFIG['top_n']} coins")
    print(f" Interval   : every {CONFIG['interval_min']} min")
    print(f" Entry zone : FVG Bullish / Bearish")
    print(f" Min RR     : 1:2")
    print(f" Alert      : Telegram (setiap sinyal valid)")
    print(f" Sources    : Bybit + OKX + Gate.io")
    print("═" * 60)

    # ── Initial run ──────────────────────────────────────────────────
    print("\n[startup] Initial run...")
    try:
        result = do_run()
        send_result(result, CONFIG["telegram_chat_id"],
                    top_n=CONFIG["top_display"])
    except Exception as e:
        print(f"[startup] Error: {e}")

    # ── Scheduler thread ─────────────────────────────────────────────
    t = threading.Thread(target=scheduler_loop, daemon=True)
    t.start()

    # ── Bot (main thread, blocking) ──────────────────────────────────
    bot = TelegramBot(
        chat_id       = CONFIG["telegram_chat_id"],
        on_run_cmd    = do_run,
        on_status_cmd = get_status,
        on_summary_cmd= get_summary,
    )
    bot.start_polling()


if __name__ == "__main__":
    main()
