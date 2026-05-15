"""
main.py
────────
Entry point. Two threads:
  1. Screener scheduler — auto-run every N min, log signals, evaluate outcomes
  2. Telegram bot       — handle /run /status /summary /help

Daily summary sent at 00:00 UTC automatically.

Env vars:
  TELEGRAM_BOT_TOKEN       — from @BotFather
  TELEGRAM_CHAT_ID         — your chat ID
  SCREENER_INTERVAL        — minutes between auto-runs (default: 60)
  SCREENER_TIMEFRAME       — default timeframe (default: 30m)
  SCREENER_TOP_N           — coins to screen (default: 50)
  SCREENER_MIN_CONVICTION  — min conviction to show (default: 1)
  SCREENER_TOP_DISPLAY     — max coins per section in TG (default: 5)
  SIGNAL_LOG_PATH          — path for signal log JSON (default: /app/data/signals_log.json)
"""

import os
import time
import threading
from datetime import datetime, timezone

from screener.engine import run_screener
from notify.telegram import send_result, TelegramBot
from backtest.tracker import log_signal, evaluate_open_signals, compute_stats
from backtest.summary import build_summary_message, send_daily_summary

# ── Config ────────────────────────────────────────────────────────────────────
CONFIG = {
    'telegram_token'   : os.getenv('TELEGRAM_BOT_TOKEN', ''),
    'telegram_chat_id' : os.getenv('TELEGRAM_CHAT_ID', ''),
    'interval_min'     : int(os.getenv('SCREENER_INTERVAL', '60')),
    'timeframe'        : os.getenv('SCREENER_TIMEFRAME', '30m'),
    'top_n'            : int(os.getenv('SCREENER_TOP_N', '50')),
    'min_conviction'   : int(os.getenv('SCREENER_MIN_CONVICTION', '1')),
    'top_display'      : int(os.getenv('SCREENER_TOP_DISPLAY', '5')),
}

_last_result: dict = {}
_last_run_time: str = 'Never'
_last_all_data: dict = {}   # store latest OHLCV for outcome evaluation


# ── Run screener ──────────────────────────────────────────────────────────────
def do_run(timeframe: str = None) -> dict:
    global _last_result, _last_run_time, _last_all_data
    tf     = timeframe or CONFIG['timeframe']
    result = run_screener(
        timeframe=tf,
        top_n=CONFIG['top_n'],
        min_conviction=CONFIG['min_conviction'],
    )
    _last_result   = result
    _last_run_time = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')

    # Log all new signals to backtest tracker
    for sig in result['longs'] + result['shorts']:
        log_signal(sig)

    # Evaluate open signals against new price data
    if hasattr(result, '_all_data'):
        _last_all_data = result['_all_data']

    return result


def get_status() -> str:
    if not _last_result:
        return '📭 Belum ada run. Kirim /run untuk mulai.'
    s = _last_result.get('stats', {})
    stats = compute_stats(days=7)
    return (
        f'📊 <b>Status Run Terakhir</b>\n'
        f'🕐 {_last_run_time}\n'
        f'⏱ Timeframe : {_last_result.get("timeframe","?")}\n'
        f'🔍 Scanned  : {s.get("total_scanned",0)} coins\n'
        f'🟢 Long     : {s.get("long_count",0)}  '
        f'(🔥 strong: {s.get("strong_long",0)})\n'
        f'🔴 Short    : {s.get("short_count",0)}  '
        f'(🔥 strong: {s.get("strong_short",0)})\n\n'
        f'📈 <b>Backtest 7 Hari</b>\n'
        f'Win rate    : {stats["win_rate"]:.1f}%\n'
        f'Total PnL   : {stats["total_pnl"]:+.2f}%\n'
        f'TP/SL/Open  : {stats["tp"]}/{stats["sl"]}/{stats["open"]}'
    )


def get_summary(days: int = 7) -> str:
    return build_summary_message(days)


# ── Scheduler ─────────────────────────────────────────────────────────────────
def scheduler_loop():
    interval_sec = CONFIG['interval_min'] * 60
    last_summary_date = None
    print(f'[scheduler] Auto-run every {CONFIG["interval_min"]} min')

    while True:
        try:
            # Run screener
            print('[scheduler] Running screener...')
            result = do_run()
            send_result(result, CONFIG['telegram_chat_id'],
                        top_n=CONFIG['top_display'])

            # Daily summary at 00:00 UTC
            now = datetime.now(timezone.utc)
            today = now.date()
            if now.hour == 0 and last_summary_date != today:
                print('[scheduler] Sending daily backtest summary...')
                send_daily_summary(CONFIG['telegram_chat_id'], days=7)
                last_summary_date = today

            print(f'[scheduler] Done. Next in {CONFIG["interval_min"]} min.')
        except Exception as e:
            print(f'[scheduler] Error: {e}')

        time.sleep(interval_sec)


# ── Validate ──────────────────────────────────────────────────────────────────
def validate():
    missing = []
    if not CONFIG['telegram_token']:
        missing.append('TELEGRAM_BOT_TOKEN')
    if not CONFIG['telegram_chat_id']:
        missing.append('TELEGRAM_CHAT_ID')
    if missing:
        raise ValueError(f'Missing env vars: {", ".join(missing)}')


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    validate()

    print('═' * 60)
    print('  VWAP WEEKLY SCREENER — starting')
    print(f'  Timeframe : {CONFIG["timeframe"]}')
    print(f'  Top N     : {CONFIG["top_n"]} coins')
    print(f'  Interval  : every {CONFIG["interval_min"]} min')
    print(f'  Sources   : Bybit + OKX + Gate.io (averaged)')
    print('═' * 60)

    # Initial run
    print('\n[startup] Initial run...')
    try:
        result = do_run()
        send_result(result, CONFIG['telegram_chat_id'],
                    top_n=CONFIG['top_display'])
    except Exception as e:
        print(f'[startup] Error: {e}')

    # Scheduler thread
    t = threading.Thread(target=scheduler_loop, daemon=True)
    t.start()

    # Bot (main thread, blocking)
    bot = TelegramBot(
        chat_id=CONFIG['telegram_chat_id'],
        on_run_cmd=do_run,
        on_status_cmd=get_status,
        on_summary_cmd=get_summary,
    )
    bot.start_polling()


if __name__ == '__main__':
    main()
