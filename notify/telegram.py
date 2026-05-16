"""
notify/telegram.py
──────────────────────────────────────────────────
Telegram notifier + bot polling.

send_signal(sig, chat_id)       → send ONE signal immediately
send_result(result, chat_id)    → send full screener run summary
TelegramBot                     → polling bot for /run /status /help

🆕 v2: Enhanced signal format with Volume Spike, MSS, HTF alignment,
       Dynamic SL, and Trailing Stop info.
"""

from __future__ import annotations

import os
import time
import requests
import threading
from datetime import datetime, timezone
from typing import Callable, Optional


TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"


# ── Low-level helpers ─────────────────────────────────────────────────────────
def _api(token: str, method: str, payload: dict) -> dict:
    url = TELEGRAM_API.format(token=token, method=method)
    try:
        r = requests.post(url, json=payload, timeout=10)
        return r.json()
    except Exception as e:
        print(f"[telegram] {method} error: {e}")
        return {}


def _send(token: str, chat_id: str, text: str, parse_mode: str = "HTML") -> bool:
    res = _api(token, "sendMessage", {
        "chat_id"   : chat_id,
        "text"      : text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    })
    return res.get("ok", False)


def _get_token() -> str:
    return os.getenv("TELEGRAM_BOT_TOKEN", "")


# ── Signal formatting ─────────────────────────────────────────────────────────
def _fmt_price(p: float) -> str:
    """Format price intelligently based on magnitude."""
    if p >= 1000:
        return f"{p:,.2f}"
    elif p >= 1:
        return f"{p:.4f}"
    elif p >= 0.01:
        return f"{p:.5f}"
    else:
        return f"{p:.8f}"


def _fmt_signal(sig: dict) -> str:
    """
    Render a single signal as a Telegram message.

    Example output (v2):
    ─────────────────────────────────
    🟢🔥 LONG STRONG — BTC  •  15m
    ─────────────────────────────────
    📍 Entry   : 68,420.00
    🛑 SL      : 67,900.00   (-0.76%)  🔒 Dynamic
    🎯 TP1     : 68,940.00   (+0.76%)
    🏆 TP2     : 69,460.00   (+1.52%)  ← RR 1:2

    📊 RSI : 44.2   |   Dist VWAP : +0.18%
    🔲 FVG : 68,100 – 68,350  (bullish)
    📈 RR  : 1 : 2.00

    📦 Vol Spike : ✅ 1.45x avg
    🔄 MSS      : ✅ Swing break confirmed
    🕐 HTF 1H   : ✅ Aligned

    💡 Exit: Trailing Stop setelah TP1
    ─────────────────────────────────
    """
    d    = sig["direction"]
    sym  = sig["symbol"]
    tf   = sig["timeframe"]
    strong = sig.get("strong", False)
    conv = sig.get("conviction", "")

    entry = sig["entry"]
    sl    = sig["sl"]
    tp1   = sig["tp1"]
    tp2   = sig["tp2"]
    tp3   = sig.get("tp3", 0)
    rr    = sig["rr"]
    rsi   = sig["rsi"]
    dist  = sig["dist_pct"]
    fvg_b = sig["fvg_bot"]
    fvg_t = sig["fvg_top"]
    fvg_k = sig.get("fvg_type", "")

    # New fields
    vol_spike     = sig.get("vol_spike", True)
    vol_ratio     = sig.get("vol_ratio", 1.0)
    vol_healthy   = sig.get("vol_healthy", True)
    mss_confirmed = sig.get("mss_confirmed", True)
    htf_aligned   = sig.get("htf_aligned", True)
    sl_type       = sig.get("sl_type", "static")

    sl_pct  = (sl  - entry) / entry * 100
    tp1_pct = (tp1 - entry) / entry * 100
    tp2_pct = (tp2 - entry) / entry * 100
    tp3_pct = (tp3 - entry) / entry * 100 if tp3 else 0

    if d == "LONG":
        icon = "🟢🔥" if strong else "🟢"
        label = "LONG STRONG" if strong else "LONG"
    else:
        icon = "🔴🔥" if strong else "🔴"
        label = "SHORT STRONG" if strong else "SHORT"

    sl_label = "🔒 Dynamic" if sl_type == "dynamic" else ""

    divider = "─" * 36
    lines = [
        divider,
        f"{icon} <b>{label}</b>  —  <b>{sym}</b>  •  {tf}",
        divider,
        f"📍 Entry   :  <code>{_fmt_price(entry)}</code>",
        f"🛑 SL      :  <code>{_fmt_price(sl)}</code>   ({sl_pct:+.2f}%)  {sl_label}",
        f"🎯 TP1     :  <code>{_fmt_price(tp1)}</code>   ({tp1_pct:+.2f}%)",
        f"🏆 TP2     :  <code>{_fmt_price(tp2)}</code>   ({tp2_pct:+.2f}%)  ← RR 1:{rr:.1f}",
    ]
    if tp3:
        lines.append(f"🚀 TP3     :  <code>{_fmt_price(tp3)}</code>   ({tp3_pct:+.2f}%)  ← extended")
    lines += [
        "",
        f"📊 RSI : <b>{rsi}</b>   |   Dist VWAP : {dist:+.3f}%",
        f"🔲 FVG : <code>{_fmt_price(fvg_b)}</code> – <code>{_fmt_price(fvg_t)}</code>  ({fvg_k})",
        f"📈 RR  : 1 : {rr:.2f}   {conv}",
        "",
    ]

    # ── New filter info ───────────────────────────────────────────────
    vol_icon = "✅" if vol_spike else "❌"
    vol_h_icon = "" if vol_healthy else "  ⚠️ Vol↓"
    lines.append(f"📦 Vol   : {vol_icon} {vol_ratio:.2f}x avg{vol_h_icon}")

    mss_icon = "✅" if mss_confirmed else "❌"
    lines.append(f"🔄 MSS   : {mss_icon} {'Swing break confirmed' if mss_confirmed else 'No break'}")

    htf_icon = "✅" if htf_aligned else "⚠️"
    lines.append(f"🕐 HTF   : {htf_icon} {'Aligned' if htf_aligned else 'Berlawanan'}")

    lines.append("")
    lines.append("💡 <i>Exit: TP1→trail(50%) → TP2→trail(30%) → TP3</i>")
    lines.append(divider)

    return "\n".join(lines)


def _fmt_summary(result: dict, top_n: int = 5) -> str:
    """Render full screener run as Telegram message."""
    tf       = result.get("timeframe", "?")
    scanned  = result.get("scanned_at", "")
    stats    = result.get("stats", {})
    longs    = result["longs"][:top_n]
    shorts   = result["shorts"][:top_n]

    total_l = stats.get("long_count", 0)
    total_s = stats.get("short_count", 0)

    lines = [
        f"📊 <b>VWAP WEEKLY SCREENER</b>  •  {tf}  •  {scanned}",
        f"🔍 Scanned: {stats.get('total_scanned',0)} coins  |  "
        f"Signals: {total_l}L  {total_s}S",
        f"🔧 <i>Filters: VolSpike≥30% | MSS | HTF(1H) | DynSL</i>",
        "",
    ]

    if longs:
        lines.append("🟢 <b>LONG</b>  —  FVG bounce + above VWAP weekly mid")
        lines.append(f"{'Symbol':<8}  {'RSI':>5}  {'Dist':>7}  {'RR':>5}  {'Vol':>5}  {'Conviction'}")
        for s in longs:
            flag = "🔥" if s["strong"] else "  "
            vol_r = s.get("vol_ratio", 1.0)
            htf   = "✓" if s.get("htf_aligned", True) else "✗"
            lines.append(
                f"🟢{flag} {s['symbol']:<7}  {s['rsi']:>5}  "
                f"{s['dist_pct']:>+6.2f}%  {s['rr']:>4.1f}  {vol_r:>4.1f}x  {s['conviction']} {htf}"
            )
        lines.append("")

    if shorts:
        lines.append("🔴 <b>SHORT</b>  —  FVG rejection + below VWAP weekly mid")
        lines.append(f"{'Symbol':<8}  {'RSI':>5}  {'Dist':>7}  {'RR':>5}  {'Vol':>5}  {'Conviction'}")
        for s in shorts:
            flag = "🔥" if s["strong"] else "  "
            vol_r = s.get("vol_ratio", 1.0)
            htf   = "✓" if s.get("htf_aligned", True) else "✗"
            lines.append(
                f"🔴{flag} {s['symbol']:<7}  {s['rsi']:>5}  "
                f"{s['dist_pct']:>+6.2f}%  {s['rr']:>4.1f}  {vol_r:>4.1f}x  {s['conviction']} {htf}"
            )
        lines.append("")

    if not longs and not shorts:
        lines.append("⏳ Belum ada sinyal yang memenuhi kriteria.")
        lines.append("<i>Filter aktif: FVG + RR 1:2 + VolSpike + MSS + HTF</i>")
        lines.append("Screener tetap jalan otomatis setiap candle 15m.")

    return "\n".join(lines)


# ── Public send functions ─────────────────────────────────────────────────────
def send_signal(sig: dict, chat_id: str) -> bool:
    """Send a single signal alert immediately."""
    token = _get_token()
    if not token or not chat_id:
        return False
    text = _fmt_signal(sig)
    ok = _send(token, chat_id, text)
    if ok:
        print(f"[telegram] ✅ Sent signal: {sig['direction']} {sig['symbol']}")
    else:
        print(f"[telegram] ❌ Failed signal: {sig['direction']} {sig['symbol']}")
    return ok


def send_result(result: dict, chat_id: str, top_n: int = 5) -> None:
    """Send screener summary + individual signal alerts."""
    token = _get_token()
    if not token or not chat_id:
        return

    all_sigs = result.get("longs", []) + result.get("shorts", [])

    # Always send individual alerts first (1 per coin, max top_n)
    sent = 0
    for sig in all_sigs:
        if sent >= top_n:
            break
        send_signal(sig, chat_id)
        time.sleep(0.3)
        sent += 1

    # Then send the summary table
    if all_sigs:
        summary = _fmt_summary(result, top_n=top_n)
        _send(token, chat_id, summary)
    else:
        # Even if no signals, send a brief status
        msg = (
            f"📭 <b>VWAP Screener</b>  •  {result.get('timeframe','?')}\n"
            f"🕐 {result.get('scanned_at','')}\n"
            f"🔍 Scanned {result.get('stats',{}).get('total_scanned',0)} coins\n"
            f"⏳ Tidak ada setup yang memenuhi semua filter.\n"
            f"<i>Filter: FVG + RR 1:2 + VolSpike≥30% + MSS + HTF(1H)</i>"
        )
        _send(token, chat_id, msg)


# ── Telegram Bot (polling) ───────────────────────────────────────────────────
class TelegramBot:
    def __init__(
        self,
        chat_id: str,
        on_run_cmd:     Callable,
        on_status_cmd:  Callable,
        on_summary_cmd: Callable,
    ):
        self.token    = _get_token()
        self.chat_id  = chat_id
        self.on_run   = on_run_cmd
        self.on_status = on_status_cmd
        self.on_summary = on_summary_cmd
        self._offset   = 0

    def _get_updates(self) -> list[dict]:
        res = _api(self.token, "getUpdates", {
            "offset": self._offset,
            "timeout": 30,
            "allowed_updates": ["message"],
        })
        return res.get("result", [])

    def _handle(self, update: dict) -> None:
        msg  = update.get("message", {})
        text = msg.get("text", "").strip()
        cid  = str(msg.get("chat", {}).get("id", ""))

        if not text.startswith("/"):
            return

        parts = text.split()
        cmd   = parts[0].lower()

        if cmd == "/run":
            tf = parts[1] if len(parts) > 1 else None
            _send(self.token, cid, "⏳ Running screener...")
            try:
                result = self.on_run(tf)
                send_result(result, cid)
            except Exception as e:
                _send(self.token, cid, f"❌ Error: {e}")

        elif cmd == "/status":
            _send(self.token, cid, self.on_status(), "HTML")

        elif cmd in ("/summary",):
            days = int(parts[1]) if len(parts) > 1 else 7
            _send(self.token, cid, self.on_summary(days), "HTML")

        elif cmd == "/help":
            help_text = (
                "📋 <b>VWAP Screener v2 — Commands</b>\n\n"
                "/run          — Jalankan screener sekarang (15m)\n"
                "/run 1h       — Jalankan dengan timeframe lain\n"
                "/status       — Info run terakhir + backtest 7 hari\n"
                "/summary [N]  — Ringkasan N hari terakhir (default 7)\n"
                "/help         — Daftar command\n\n"
                "<b>Kriteria sinyal v2:</b>\n"
                "• Close di atas/bawah VWAP Weekly mid\n"
                "• Entry di zona FVG Bullish/Bearish\n"
                "• Minimum RR 1:2\n"
                "• 📦 Volume Spike ≥ 30% dari avg 20 candle\n"
                "• 🔄 Market Structure Shift (MSS) terkonfirmasi\n"
                "• 🕐 HTF 1H VWAP alignment check\n"
                "• 🛑 Dynamic SL (trigger candle + 0.2% buffer)\n"
                "• 💡 Trailing Stop setelah TP1 hit\n"
                "• Auto-alert setiap candle 15m"
            )
            _send(self.token, cid, help_text, "HTML")

    def start_polling(self) -> None:
        print("[bot] Polling started...")
        while True:
            try:
                updates = self._get_updates()
                for u in updates:
                    self._offset = u["update_id"] + 1
                    self._handle(u)
            except Exception as e:
                print(f"[bot] Poll error: {e}")
                time.sleep(5)
