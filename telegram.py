"""
notify/telegram.py
──────────────────
Telegram notifier + bot command handler.

Commands:
  /run [timeframe]  — run screener now (default 15m)
  /status           — show last run summary
  /help             — show commands
"""

import os
import time
import requests
from datetime import datetime, timezone
from typing import Optional


TELEGRAM_API = f"https://api.telegram.org/bot{os.getenv('TELEGRAM_BOT_TOKEN', '')}"


# ── Conviction helpers ────────────────────────────────────────────────────────
def _conviction_label(score: int) -> str:
    if score >= 9:  return '🔥 Very High'
    if score >= 7:  return '🟢 High'
    if score >= 5:  return '🟠 Medium'
    if score >= 3:  return '🟡 Low'
    return '🔘 Very Low'


def _signal_emoji(sig: str) -> str:
    if sig == 'LONG_STRONG':  return '🟢🔥'
    if sig == 'LONG':         return '🟢'
    if sig == 'SHORT_STRONG': return '🔴🔥'
    if sig == 'SHORT':        return '🔴'
    return '⚪'


# ── Message builder ───────────────────────────────────────────────────────────
def build_message(result: dict, top_n: int = 8) -> str:
    ts    = result['timestamp'].strftime('%Y-%m-%d %H:%M UTC')
    tf    = result['timeframe']
    stats = result['stats']
    longs  = result['longs'][:top_n]
    shorts = result['shorts'][:top_n]

    def fmt_row(r: dict) -> str:
        sym    = r['symbol'].replace('/USDT:USDT', '').replace('/USDT', '')
        emoji  = _signal_emoji(r['signal'])
        conv   = _conviction_label(r['conviction'])
        dist   = r['dist_pct']
        dist_s = f'{dist:+.2f}%' if dist != 0 else '0.00%'
        rsi    = r['rsi']
        n_ex   = r.get('exchange_count', 1)
        ex_badge = '🔵🔵🔵' if n_ex == 3 else ('🔵🔵⚪' if n_ex == 2 else '🔵⚪⚪')
        return (f"{emoji} <b>{sym:<8}</b>  "
                f"RSI {rsi:>4.0f}  "
                f"dist {dist_s:>7}  "
                f"{ex_badge}  "
                f"{conv}")

    full3 = stats.get("full_3ex", 0)
    lines = [
        f'📊 <b>VWAP WEEKLY SCREENER</b>  •  {tf}  •  {ts}',
        f'📡 Sources: <b>Bybit + OKX + Gate.io</b>  (averaged)',
        f'🔍 Scanned: {stats["total_scanned"]} coins  |  '
        f'Signals: {stats["long_count"]}L  {stats["short_count"]}S  '
        f'|  3-ex confirmed: {full3}',
        '',
    ]

    # LONG section
    lines.append('🟢 <b>LONG</b>  —  close above VWAP weekly mid')
    lines.append('<code>Symbol    RSI   Dist    Exch  Conviction</code>')
    if longs:
        for r in longs:
            lines.append(fmt_row(r))
    else:
        lines.append('  — no candidates —')

    lines.append('')

    # SHORT section
    lines.append('🔴 <b>SHORT</b>  —  close below VWAP weekly mid')
    lines.append('<code>Symbol    RSI   Dist    Exch  Conviction</code>')
    if shorts:
        for r in shorts:
            lines.append(fmt_row(r))
    else:
        lines.append('  — no candidates —')

    lines += [
        '',
        '<i>🔥 = bounce/rejection confirmed  |  🔵 = exchange coverage (max 3)</i>',
        '<i>dist = % distance close from VWAP mid (averaged across exchanges)</i>',
        '<i>⚠️ Not financial advice.</i>',
    ]

    return '\n'.join(lines)


# ── Sender ────────────────────────────────────────────────────────────────────
def send_message(chat_id: str, text: str) -> bool:
    url = f'{TELEGRAM_API}/sendMessage'
    payload = {
        'chat_id'                 : chat_id,
        'text'                    : text,
        'parse_mode'              : 'HTML',
        'disable_web_page_preview': True,
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        return r.json().get('ok', False)
    except Exception as e:
        print(f'[telegram] send error: {e}')
        return False


def send_result(result: dict, chat_id: str, top_n: int = 8) -> bool:
    """Send screener result to Telegram."""
    msg = build_message(result, top_n=top_n)
    return send_message(chat_id, msg)


# ── Polling bot ───────────────────────────────────────────────────────────────
class TelegramBot:
    def __init__(self, chat_id: str, on_run_cmd, on_status_cmd):
        self.chat_id      = chat_id
        self.on_run_cmd   = on_run_cmd    # callable(timeframe) → result dict
        self.on_status_cmd = on_status_cmd  # callable() → str
        self.offset       = 0

    def _get_updates(self, timeout: int = 30) -> list:
        url    = f'{TELEGRAM_API}/getUpdates'
        params = {'offset': self.offset, 'timeout': timeout,
                  'allowed_updates': ['message']}
        try:
            r = requests.get(url, params=params, timeout=timeout + 5)
            return r.json().get('result', [])
        except Exception as e:
            print(f'[telegram] poll error: {e}')
            return []

    def _send(self, text: str):
        send_message(self.chat_id, text)

    def _handle(self, message: dict):
        chat_id = str(message.get('chat', {}).get('id', ''))
        text    = message.get('text', '').strip()
        user    = message.get('from', {}).get('username', '?')

        # Security: only respond to configured chat
        if chat_id != self.chat_id:
            print(f'[bot] ignored msg from {chat_id} (@{user})')
            return

        if not text.startswith('/'):
            return

        parts   = text.split()
        cmd     = parts[0].lower().split('@')[0]
        args    = parts[1:]

        print(f'[bot] @{user} → {cmd} {args}')

        if cmd == '/help':
            self._send(
                '🤖 <b>VWAP Screener Commands</b>\n\n'
                '/run          — run screener (15m)\n'
                '/run 1h       — run with timeframe\n'
                '/status       — last run info\n'
                '/help         — this menu\n\n'
                '<i>Valid timeframes: 1m 5m 15m 30m 1h 4h 1d</i>'
            )

        elif cmd == '/run':
            valid_tf = {'1m','5m','15m','30m','1h','4h','1d'}
            tf = args[0] if args and args[0] in valid_tf else '15m'
            self._send(f'⏳ Running VWAP screener ({tf})...')
            try:
                result = self.on_run_cmd(tf)
                send_result(result, self.chat_id)
            except Exception as e:
                self._send(f'❌ Error: {e}')

        elif cmd == '/status':
            try:
                status_text = self.on_status_cmd()
                self._send(status_text)
            except Exception as e:
                self._send(f'❌ Error: {e}')

        else:
            self._send(f'❓ Unknown command: <code>{cmd}</code>\nType /help')

    def poll_once(self):
        updates = self._get_updates(timeout=30)
        for upd in updates:
            self.offset = upd['update_id'] + 1
            msg = upd.get('message')
            if msg:
                self._handle(msg)

    def start_polling(self):
        print('[bot] Telegram polling started')
        while True:
            try:
                self.poll_once()
                time.sleep(1)
            except KeyboardInterrupt:
                print('[bot] stopped')
                break
            except Exception as e:
                print(f'[bot] loop error: {e}')
                time.sleep(5)
