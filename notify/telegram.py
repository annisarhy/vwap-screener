"""
notify/telegram.py
──────────────────
Telegram notifier + bot command handler.

Commands:
  /run [tf]     — run screener now
  /status       — last run info
  /summary      — backtest summary (7 days)
  /summary 30   — backtest summary (30 days)
  /help         — commands
"""

import os
import time
import requests
from datetime import datetime, timezone
from typing import Optional

TELEGRAM_API = f"https://api.telegram.org/bot{os.getenv('TELEGRAM_BOT_TOKEN', '')}"


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


# ── Format one signal card ────────────────────────────────────────────────────
def fmt_signal_card(r: dict) -> str:
    sym    = r['symbol']
    emoji  = _signal_emoji(r['signal'])
    conv   = _conviction_label(r['conviction'])
    is_long = 'LONG' in r['signal']

    entry  = r.get('entry', r.get('close', 0))
    sl     = r.get('sl', 0)
    tp     = r.get('tp', 0)
    rr     = r.get('rr', 0)
    rsi    = r.get('rsi', 50)
    dist   = r.get('dist_pct', 0)
    n_ex   = r.get('exchange_count', 1)
    ex_badge = '🔵🔵🔵' if n_ex == 3 else ('🔵🔵⚪' if n_ex == 2 else '🔵⚪⚪')

    fvg_tag = ''
    if r.get('fvg_bullish') and is_long:
        fvg_tag = ' 〔FVG Bullish〕'
    elif r.get('fvg_bearish') and not is_long:
        fvg_tag = ' 〔FVG Bearish〕'

    reason = r.get('reason', '')

    lines = [
        f'{emoji} <b>{sym}</b>{fvg_tag}  {ex_badge}  {conv}',
        f'   Entry : <code>{entry:.6g}</code>',
        f'   SL    : <code>{sl:.6g}</code>  ({abs((sl-entry)/entry*100):.2f}%)',
        f'   TP    : <code>{tp:.6g}</code>  ({abs((tp-entry)/entry*100):.2f}%)',
        f'   RR    : 1:{rr:.2f}  |  RSI {rsi:.0f}  |  dist {dist:+.2f}%',
    ]
    if reason:
        # Truncate reason to keep TG message clean
        short_reason = reason if len(reason) < 200 else reason[:197] + '...'
        lines.append(f'   📝 {short_reason}')

    return '\n'.join(lines)


# ── Full screener result message ──────────────────────────────────────────────
def build_message(result: dict, top_n: int = 5) -> str:
    ts    = result['timestamp'].strftime('%Y-%m-%d %H:%M UTC')
    tf    = result['timeframe']
    stats = result['stats']
    longs  = result['longs'][:top_n]
    shorts = result['shorts'][:top_n]
    full3  = stats.get('full_3ex', 0)

    lines = [
        f'📊 <b>VWAP WEEKLY SCREENER</b>  •  {tf}  •  {ts}',
        f'📡 <b>Bybit + OKX + Gate.io</b>  (averaged)',
        f'🔍 Scanned: {stats["total_scanned"]}  |  '
        f'{stats["long_count"]}L  {stats["short_count"]}S  |  '
        f'3-ex: {full3}',
        '',
        '─── 🟢 LONG ───',
    ]
    if longs:
        for r in longs:
            lines.append(fmt_signal_card(r))
            lines.append('')
    else:
        lines.append('  — no candidates —')
        lines.append('')

    lines.append('─── 🔴 SHORT ───')
    if shorts:
        for r in shorts:
            lines.append(fmt_signal_card(r))
            lines.append('')
    else:
        lines.append('  — no candidates —')
        lines.append('')

    lines += [
        '─────────────────────────────',
        '<i>🔥=bounce confirmed  🔵=exchange coverage</i>',
        '<i>SL=ATR-based  TP=VWAP band  ⚠️Not financial advice</i>',
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


def send_result(result: dict, chat_id: str, top_n: int = 5) -> bool:
    msg = build_message(result, top_n=top_n)
    return send_message(chat_id, msg)


# ── Bot ───────────────────────────────────────────────────────────────────────
class TelegramBot:
    def __init__(self, chat_id: str, on_run_cmd, on_status_cmd, on_summary_cmd):
        self.chat_id         = chat_id
        self.on_run_cmd      = on_run_cmd
        self.on_status_cmd   = on_status_cmd
        self.on_summary_cmd  = on_summary_cmd
        self.offset          = 0

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

        if chat_id != self.chat_id:
            return

        if not text.startswith('/'):
            return

        parts = text.split()
        cmd   = parts[0].lower().split('@')[0]
        args  = parts[1:]

        print(f'[bot] @{user} → {cmd} {args}')

        if cmd == '/help':
            self._send(
                '🤖 <b>VWAP Screener Commands</b>\n\n'
                '/run           — screener 30m\n'
                '/run 1h        — screener timeframe lain\n'
                '/summary       — backtest 7 hari\n'
                '/summary 30    — backtest 30 hari\n'
                '/status        — info run terakhir\n'
                '/help          — daftar perintah\n\n'
                '<i>Sinyal include: Entry, SL (ATR-based), TP (VWAP band), RR, FVG, Reason</i>'
            )

        elif cmd == '/run':
            valid_tf = {'1m','5m','15m','30m','1h','4h','1d'}
            tf = args[0] if args and args[0] in valid_tf else '30m'
            self._send(f'⏳ Running screener ({tf})...')
            try:
                result = self.on_run_cmd(tf)
                send_result(result, self.chat_id)
            except Exception as e:
                self._send(f'❌ Error: {e}')

        elif cmd == '/summary':
            days = 7
            if args:
                try:
                    days = int(args[0])
                except ValueError:
                    pass
            self._send(f'📊 Generating backtest summary ({days} hari)...')
            try:
                msg = self.on_summary_cmd(days)
                self._send(msg)
            except Exception as e:
                self._send(f'❌ Error: {e}')

        elif cmd == '/status':
            try:
                self._send(self.on_status_cmd())
            except Exception as e:
                self._send(f'❌ Error: {e}')

        else:
            self._send(f'❓ Unknown: <code>{cmd}</code> — ketik /help')

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
                print(f'[bot] error: {e}')
                time.sleep(5)
