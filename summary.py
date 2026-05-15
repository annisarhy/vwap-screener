"""
backtest/summary.py
────────────────────
Build and send daily backtest summary to Telegram.
"""

from backtest.tracker import compute_stats
from notify.telegram import send_message


def _bar(value: float, max_val: float = 100, width: int = 10) -> str:
    filled = int(min(value / max_val * width, width)) if max_val > 0 else 0
    return '█' * filled + '░' * (width - filled)


def build_summary_message(days: int = 7) -> str:
    s = compute_stats(days)

    win_rate   = s['win_rate']
    wr_bar     = _bar(win_rate)
    wr_emoji   = '🔥' if win_rate >= 70 else '🟢' if win_rate >= 55 else '🟡' if win_rate >= 45 else '🔴'

    pnl_emoji  = '📈' if s['total_pnl'] >= 0 else '📉'
    pnl_sign   = '+' if s['total_pnl'] >= 0 else ''

    lines = [
        f'📊 <b>BACKTEST SUMMARY</b>  •  {days} hari terakhir',
        f'─────────────────────────────────',
        '',
        f'📋 <b>Overview</b>',
        f'  Total sinyal : {s["total"]}  (open: {s["open"]}  closed: {s["closed"]})',
        f'  TP hit       : {s["tp"]}  |  SL hit: {s["sl"]}  |  Expired: {s["expired"]}',
        '',
        f'{wr_emoji} <b>Win Rate</b>  :  {win_rate:.1f}%',
        f'  {wr_bar}  {win_rate:.0f}/100',
        f'  🟢 Long WR   : {s["long_wr"]:.1f}%',
        f'  🔴 Short WR  : {s["short_wr"]:.1f}%',
        '',
        f'{pnl_emoji} <b>PnL</b>',
        f'  Total        : {pnl_sign}{s["total_pnl"]:.2f}%',
        f'  Avg per trade: {pnl_sign}{s["avg_pnl"]:.2f}%',
        f'  Best trade   : +{s["best_trade"]:.2f}%',
        f'  Worst trade  : {s["worst_trade"]:.2f}%',
        '',
        f'🔢 <b>Streak</b>',
        f'  Win streak   : {s["streak_win"]}',
        f'  Loss streak  : {s["streak_loss"]}',
    ]

    # Recent closed trades
    if s['recent_closed']:
        lines += ['', '📝 <b>10 Trade Terakhir</b>']
        for t in s['recent_closed']:
            sym    = t['symbol'][:8]
            out    = t['outcome']
            pnl    = t.get('pnl_pct', 0) or 0
            sig    = '🟢' if 'LONG' in t['signal'] else '🔴'
            out_em = '✅' if out == 'TP' else ('❌' if out == 'SL' else '⏰')
            pnl_s  = f'{pnl:+.2f}%'
            lines.append(f'  {sig} {out_em} <b>{sym:<10}</b> {pnl_s}')

    lines += [
        '',
        '─────────────────────────────────',
        '<i>⚠️ Past performance ≠ future results.</i>',
        '<i>Not financial advice.</i>',
    ]

    return '\n'.join(lines)


def send_daily_summary(chat_id: str, days: int = 7):
    msg = build_summary_message(days)
    return send_message(chat_id, msg)
