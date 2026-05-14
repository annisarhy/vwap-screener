"""
main.py
────────
Entry point. Runs two threads:
  1. Scheduler — runs screener every N minutes, sends result to Telegram
  2. Telegram bot — listens for manual /run /status /help commands

Config via environment variables (set in Railway dashboard):
  TELEGRAM_BOT_TOKEN   — from @BotFather
  TELEGRAM_CHAT_ID     — your chat/group ID
  SCREENER_INTERVAL    — minutes between auto-runs (default: 60)
  SCREENER_TIMEFRAME   — default timeframe (default: 15m)
  SCREENER_TOP_N       — coins to screen (default: 50)
  SCREENER_MIN_CONVICTION — min conviction to show (default: 3)
  SCREENER_TOP_DISPLAY — max coins shown per section in TG (default: 8)
"""

import os
import time
import threading
from datetime import datetime, timezone

from screener.engine import run_screener
from notify.telegram import send_result, TelegramBot

# ── Config ────────────────────────────────────────────────────────────────────
CONFIG = {
    'telegram_token'    : os.getenv('TELEGRAM_BOT_TOKEN', ''),
    'telegram_chat_id'  : os.getenv('TELEGRAM_CHAT_ID', ''),
    'interval_min'      : int(os.getenv('SCREENER_INTERVAL', '60')),
    'timeframe'         : os.getenv('SCREENER_TIMEFRAME', '15m'),
    'top_n'             : int(os.getenv('SCREENER_TOP_N', '50')),
    'min_conviction'    : int(os.getenv('SCREENER_MIN_CONVICTION', '3')),
    'top_display'       : int(os.getenv('SCREENER_TOP_DISPLAY', '8')),
}

# Global state for /status command
_last_result: dict = {}
_last_run_time: str = 'Never'


# ── Screener runner ───────────────────────────────────────────────────────────
def do_run(timeframe: str = None) -> dict:
    global _last_result, _last_run_time
    tf = timeframe or CONFIG['timeframe']
    result = run_screener(
        timeframe=tf,
        top_n=CONFIG['top_n'],
        min_conviction=CONFIG['min_conviction'],
    )
    _last_result   = result
    _last_run_time = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    return result


def get_status() -> str:
    if not _last_result:
        return '📭 No runs yet. Send /run to start.'
    s = _last_result.get('stats', {})
    return (
        f'📊 <b>Last Run Status</b>\n'
        f'🕐 {_last_run_time}\n'
        f'⏱ Timeframe: {_last_result.get("timeframe","?")}\n\n'
        f'🔍 Scanned: {s.get("total_scanned",0)} coins\n'
        f'🟢 Long signals : {s.get("long_count",0)} '
        f'(🔥 strong: {s.get("strong_long",0)})\n'
        f'🔴 Short signals: {s.get("short_count",0)} '
        f'(🔥 strong: {s.get("strong_short",0)})\n'
    )


# ── Scheduler thread ──────────────────────────────────────────────────────────
def scheduler_loop():
    """Run screener every N minutes and push to Telegram."""
    interval_sec = CONFIG['interval_min'] * 60
    print(f'[scheduler] Auto-run every {CONFIG["interval_min"]} min')

    while True:
        try:
            print(f'[scheduler] Running screener...')
            result = do_run()
            send_result(result, CONFIG['telegram_chat_id'],
                        top_n=CONFIG['top_display'])
            print(f'[scheduler] Done. Next run in {CONFIG["interval_min"]} min.')
        except Exception as e:
            print(f'[scheduler] Error: {e}')
        time.sleep(interval_sec)


# ── Validate config ───────────────────────────────────────────────────────────
def validate():
    missing = [k for k in ('telegram_token', 'telegram_chat_id')
               if not CONFIG[k]]
    if missing:
        raise ValueError(
            f'Missing env vars: '
            + ', '.join({'telegram_token': 'TELEGRAM_BOT_TOKEN',
                         'telegram_chat_id': 'TELEGRAM_CHAT_ID'}[k]
                        for k in missing)
        )


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    validate()

    print('═' * 60)
    print('  VWAP WEEKLY SCREENER — starting')
    print(f'  Timeframe : {CONFIG["timeframe"]}')
    print(f'  Top N     : {CONFIG["top_n"]} coins')
    print(f'  Interval  : every {CONFIG["interval_min"]} min')
    print(f'  Chat ID   : {CONFIG["telegram_chat_id"]}')
    print('═' * 60)

    # Run once immediately on startup
    print('\n[startup] Running initial screener...')
    try:
        result = do_run()
        send_result(result, CONFIG['telegram_chat_id'],
                    top_n=CONFIG['top_display'])
    except Exception as e:
        print(f'[startup] Initial run error: {e}')

    # Start scheduler in background thread
    sched = threading.Thread(target=scheduler_loop, daemon=True)
    sched.start()

    # Start Telegram bot in main thread (blocking)
    bot = TelegramBot(
        chat_id=CONFIG['telegram_chat_id'],
        on_run_cmd=do_run,
        on_status_cmd=get_status,
    )
    bot.start_polling()


if __name__ == '__main__':
    main()
