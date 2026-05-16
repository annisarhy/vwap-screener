"""
notify/sheets.py
──────────────────────────────────────────────────
Google Sheets integration.

Fitur:
  • log_signal(sig)        → append sinyal baru ke tab "Signals"
  • price_updater_loop()   → update kolom "Current Price" + "PnL %" + "Status"
                             setiap PRICE_REFRESH_SEC detik untuk sinyal OPEN

Layout sheet (tab: Signals)
──────────────────────────────────────────────────────────────────────────────
A           B         C          D   E       F      G     H     I    J
Timestamp   Symbol    Direction  TF  Entry   SL     TP1   TP2   RR   RSI

K           L           M        N         O              P
FVG Type    FVG Bottom  FVG Top  Conviction Current Price  PnL %

Q            R
Status       Notes
──────────────────────────────────────────────────────────────────────────────

Status values:
  OPEN    — sinyal baru, belum hit TP/SL
  TP1     — harga sudah sentuh TP1
  TP2 ✅  — harga sudah sentuh TP2 (target utama)
  SL  ❌  — harga sudah hit SL

Auth: Google Service Account JSON
  Simpan isi file JSON ke env var GOOGLE_SERVICE_ACCOUNT_JSON  (satu baris)
  atau letakkan file-nya di path GOOGLE_SERVICE_ACCOUNT_FILE (default: /app/sa.json)

Spreadsheet ID:
  GOOGLE_SHEET_ID  — ambil dari URL spreadsheet kamu
  https://docs.google.com/spreadsheets/d/<ID>/edit
"""

from __future__ import annotations

import os
import json
import time
import threading
from datetime import datetime, timezone
from typing import Optional

import gspread
from google.oauth2.service_account import Credentials
import ccxt


def _tofloat(val) -> float:
    """Parse float dari string yang mungkin pakai koma desimal (locale ID/EU)."""
    if isinstance(val, (int, float)):
        return float(val)
    return float(str(val).replace(",", ".").strip())


# ── Config ────────────────────────────────────────────────────────────────────
SHEET_ID            = os.getenv("GOOGLE_SHEET_ID", "")
SA_JSON_ENV         = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
SA_JSON_FILE        = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "/app/sa.json")
SIGNALS_TAB         = os.getenv("GOOGLE_SHEET_TAB", "Signals")
PRICE_REFRESH_SEC   = int(os.getenv("PRICE_REFRESH_SEC", "60"))   # update harga tiap N detik

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Header row
HEADERS = [
    "Timestamp (UTC)", "Symbol", "Direction", "Timeframe",
    "Entry", "SL", "TP1", "TP2", "TP3", "RR", "RSI",
    "FVG Type", "FVG Bottom", "FVG Top", "Conviction",
    "Current Price", "PnL %", "Status", "Notes",
    "Vol Ratio", "MSS", "HTF", "Trailing SL",
]

# Column indices (1-based for gspread)
COL = {h: i + 1 for i, h in enumerate(HEADERS)}

# ── Auth & client ─────────────────────────────────────────────────────────────
_gc:     Optional[gspread.Client]    = None
_ws:     Optional[gspread.Worksheet] = None
_lock   = threading.Lock()
_enabled = False


def _build_creds() -> Optional[Credentials]:
    """Load service account credentials from env JSON string or file."""
    info = None
    if SA_JSON_ENV:
        try:
            info = json.loads(SA_JSON_ENV)
        except json.JSONDecodeError as e:
            print(f"[sheets] Bad GOOGLE_SERVICE_ACCOUNT_JSON: {e}")
            return None
    elif os.path.exists(SA_JSON_FILE):
        with open(SA_JSON_FILE) as f:
            info = json.load(f)
    else:
        return None

    return Credentials.from_service_account_info(info, scopes=SCOPES)


def init_sheets() -> bool:
    """
    Initialise connection and ensure the Signals tab + header row exist.
    Returns True if ready, False if disabled / misconfigured.
    """
    global _gc, _ws, _enabled

    if not SHEET_ID:
        print("[sheets] GOOGLE_SHEET_ID not set — Google Sheets disabled.")
        return False

    creds = _build_creds()
    if creds is None:
        print("[sheets] No credentials found — Google Sheets disabled.")
        return False

    try:
        _gc = gspread.authorize(creds)
        sh  = _gc.open_by_key(SHEET_ID)

        # Get or create the Signals worksheet
        try:
            _ws = sh.worksheet(SIGNALS_TAB)
        except gspread.WorksheetNotFound:
            _ws = sh.add_worksheet(title=SIGNALS_TAB, rows=5000, cols=len(HEADERS))

        # Ensure header row
        existing = _ws.row_values(1)
        if existing != HEADERS:
            _ws.update("A1", [HEADERS])
            _fmt_header(_ws)

        _enabled = True
        print(f"[sheets] ✅ Connected → '{SIGNALS_TAB}' tab ready.")
        return True

    except Exception as e:
        print(f"[sheets] Init failed: {e}")
        return False


def _fmt_header(ws: gspread.Worksheet) -> None:
    """Bold + freeze the header row, add column widths."""
    try:
        ws.format("A1:W1", {
            "textFormat"      : {"bold": True},
            "backgroundColor" : {"red": 0.16, "green": 0.19, "blue": 0.28},
            "horizontalAlignment": "CENTER",
        })
        ws.freeze(rows=1)
    except Exception:
        pass   # formatting is cosmetic, don't crash


def _fmt_row(ws: gspread.Worksheet, row: int, direction: str) -> None:
    """Color row by direction."""
    try:
        if direction == "LONG":
            color = {"red": 0.85, "green": 0.96, "blue": 0.85}
        else:
            color = {"red": 0.98, "green": 0.87, "blue": 0.87}
        ws.format(f"A{row}:W{row}", {"backgroundColor": color})
    except Exception:
        pass


# ── Log signal ────────────────────────────────────────────────────────────────
def log_signal(sig: dict) -> bool:
    """
    Append one signal as a new row.
    Safe to call even when Sheets is disabled (returns False silently).
    """
    if not _enabled or _ws is None:
        return False

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    row = [
        ts,
        sig.get("symbol", ""),
        sig.get("direction", ""),
        sig.get("timeframe", ""),
        sig.get("entry", ""),
        sig.get("sl", ""),
        sig.get("tp1", ""),
        sig.get("tp2", ""),
        sig.get("tp3", ""),
        sig.get("rr", ""),
        sig.get("rsi", ""),
        sig.get("fvg_type", ""),
        sig.get("fvg_bot", ""),
        sig.get("fvg_top", ""),
        sig.get("conviction", ""),
        sig.get("entry", ""),   # Current Price = entry at log time
        "0.00%",                # PnL % starts at 0
        "OPEN",                 # Status
        "",                     # Notes
        sig.get("vol_ratio", ""),   # Vol Ratio
        "✅" if sig.get("mss_confirmed") else "❌",  # MSS
        "✅" if sig.get("htf_aligned") else "⚠️",    # HTF
        sig.get("sl", ""),         # Trailing SL = initial SL
    ]

    try:
        with _lock:
            _ws.append_row(row, value_input_option="USER_ENTERED")
            row_num = len(_ws.get_all_values())   # last row after append
            _fmt_row(_ws, row_num, sig.get("direction", ""))
        print(f"[sheets] ✅ Logged {sig['direction']} {sig['symbol']}")
        return True
    except Exception as e:
        print(f"[sheets] log_signal error: {e}")
        return False


# ── Live price fetcher ────────────────────────────────────────────────────────
_price_cache: dict[str, float] = {}
_bybit: Optional[ccxt.Exchange] = None


def _get_bybit() -> ccxt.Exchange:
    global _bybit
    if _bybit is None:
        _bybit = ccxt.bybit({"enableRateLimit": True})
    return _bybit


def _fetch_price(symbol_short: str) -> Optional[float]:
    """
    Fetch current price for a symbol like 'BTC', 'ETH', 'SOL'.
    Tries Bybit perpetual first, then spot.
    """
    # Check cache (staleness handled by caller's loop)
    sym_perp = f"{symbol_short}/USDT:USDT"
    sym_spot = f"{symbol_short}/USDT"
    ex = _get_bybit()
    for sym in [sym_perp, sym_spot]:
        try:
            ticker = ex.fetch_ticker(sym)
            price  = float(ticker.get("last") or ticker.get("close") or 0)
            if price > 0:
                _price_cache[symbol_short] = price
                return price
        except Exception:
            continue
    return _price_cache.get(symbol_short)   # return stale if fetch failed


def _calc_pnl(direction: str, entry: float, exit_price: float) -> str:
    if entry <= 0:
        return "–"
    pct = (exit_price - entry) / entry * 100
    if direction == "SHORT":
        pct = -pct
    sign = "+" if pct >= 0 else ""
    return f"{sign}{pct:.2f}%"


def _resolve_live(direction: str, entry: float, sl: float,
                  tp1: float, tp2: float, current: float,
                  trailing_sl: float = 0.0, tp3: float = 0.0) -> str:
    """
    Status resolver with full trailing stop flow:
      OPEN → TP1 🔒 → TP2 🔒 → TP3 ✅ or TSL ✅
    """
    active_sl = trailing_sl if trailing_sl > 0 else sl

    if direction == "LONG":
        if tp3 > 0 and current >= tp3: return "TP3 ✅"
        if current >= tp2: return "TP2 🔒"   # trailing continues to TP3
        if current >= tp1: return "TP1 🔒"   # trailing activated
        if current <= active_sl: return "SL ❌"
    else:
        if tp3 > 0 and current <= tp3: return "TP3 ✅"
        if current <= tp2: return "TP2 🔒"
        if current <= tp1: return "TP1 🔒"
        if current >= active_sl: return "SL ❌"
    return "OPEN"


def _calc_trailing_sl(direction: str, entry: float, sl: float,
                      current: float, old_trailing: float,
                      phase: str = "tp1") -> float:
    """
    Calculate trailing stop level based on phase:
      tp1 phase: trail at 50% risk, floor = entry (breakeven)
      tp2 phase: trail tighter at 30% risk, floor = tp1 level
    """
    risk = abs(entry - sl)

    if phase == "tp2":
        trail_dist = risk * 0.30   # tighter trail after TP2
        tp1_level  = entry + risk if direction == "LONG" else entry - risk
    else:
        trail_dist = risk * 0.50   # wider trail after TP1
        tp1_level  = entry         # breakeven

    if direction == "LONG":
        new_trail = current - trail_dist
        new_trail = max(new_trail, tp1_level)   # never below floor
        if old_trailing > 0:
            return max(new_trail, old_trailing)  # only move UP
        return new_trail
    else:
        new_trail = current + trail_dist
        new_trail = min(new_trail, tp1_level)   # never above floor
        if old_trailing > 0:
            return min(new_trail, old_trailing)  # only move DOWN
        return new_trail


def _backtest_resolve(
    symbol: str, direction: str, timeframe: str,
    entry: float, sl: float, tp1: float, tp2: float,
    signal_ts: datetime,
) -> tuple[str, float]:
    """
    Replay candles from signal_ts to find which level was hit FIRST.
    Checks wick high/low per candle — candle where SL and TP conflict,
    SL wins (conservative). Returns (status, exit_price).
    """
    ex = _get_bybit()
    since_ms = int(signal_ts.timestamp() * 1000)

    raw = None
    for sym in [f"{symbol}/USDT:USDT", f"{symbol}/USDT"]:
        try:
            raw = ex.fetch_ohlcv(sym, timeframe=timeframe, since=since_ms, limit=300)
            if raw and len(raw) >= 2:
                break
        except Exception:
            continue

    if not raw or len(raw) < 2:
        curr = _fetch_price(symbol) or entry
        return _resolve_live(direction, entry, sl, tp1, tp2, curr), curr

    # Skip first candle (entry candle itself)
    for candle in raw[1:]:
        _, _, high, low, close, _ = candle
        if direction == "LONG":
            if low <= sl:   return "SL ❌",  sl
            if high >= tp2: return "TP2 ✅", tp2
            if high >= tp1: return "TP1",    tp1
        else:
            if high >= sl:  return "SL ❌",  sl
            if low <= tp2:  return "TP2 ✅", tp2
            if low <= tp1:  return "TP1",    tp1

    # No level hit yet → OPEN, return current price
    curr = _fetch_price(symbol) or entry
    return "OPEN", curr


# Map timeframe string → candle duration in minutes
_TF_MINUTES = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
               "1h": 60, "2h": 120, "4h": 240, "1d": 1440}

# Interval backtest resolver (lebih jarang dari live price update)
BACKTEST_RESOLVE_SEC = int(os.getenv("BACKTEST_RESOLVE_SEC", "300"))  # default 5 menit


def price_updater_loop() -> None:
    """
    Dua tugas dipisah:
      Thread A (tiap PRICE_REFRESH_SEC = 60s) → update kolom O & P saja (harga live + PnL)
      Thread B (tiap BACKTEST_RESOLVE_SEC = 300s) → replay candle, update Status (TP/SL)
    Pemisahan ini memastikan harga live SELALU terupdate meski backtest resolver lambat.
    """
    if not _enabled:
        return
    print(f"[sheets] Live price updater: every {PRICE_REFRESH_SEC}s")
    print(f"[sheets] Backtest resolver : every {BACKTEST_RESOLVE_SEC}s")

    # Thread B — backtest resolver (background dari background)
    import threading
    t = threading.Thread(target=_backtest_resolve_loop, daemon=True)
    t.start()

    # Thread A — live price (main loop)
    while True:
        try:
            _update_live_prices()
        except Exception as e:
            print(f"[sheets] live price error: {e}")
        time.sleep(PRICE_REFRESH_SEC)


def _update_live_prices() -> None:
    """
    Ambil harga REALTIME semua coin OPEN/TP1.
    Update kolom O (Current Price) + P (PnL %) + Q (Status) sekaligus.
    Kalau harga live sudah lewat SL/TP → langsung close, tidak tunggu resolver.
    """
    if _ws is None:
        return

    with _lock:
        all_rows = _ws.get_all_values()

    if len(all_rows) <= 1:
        return

    open_rows = []
    for i, row in enumerate(all_rows[1:], start=2):
        if len(row) < 17:
            continue
        status = row[COL["Status"] - 1]
        if status in ("OPEN", "TP1", "TP1 🔒", "TP2 🔒", ""):
            open_rows.append((i, row))

    if not open_rows:
        return

    # Fetch harga per symbol unik
    symbols = {row[COL["Symbol"] - 1] for _, row in open_rows}
    prices: dict[str, float] = {}
    for sym in symbols:
        p = _fetch_price(sym)
        if p:
            prices[sym] = p
        time.sleep(0.08)

    if not prices:
        print("[sheets] live: semua fetch gagal")
        return

    price_col    = _col_letter(COL["Current Price"])
    pnl_col      = _col_letter(COL["PnL %"])
    status_col   = _col_letter(COL["Status"])
    trail_sl_col = _col_letter(COL["Trailing SL"])

    updates: list[dict] = []
    to_recolor: list[tuple[int, str]] = []

    for row_num, row in open_rows:
        sym  = row[COL["Symbol"]    - 1]
        dirn = row[COL["Direction"] - 1]
        curr = prices.get(sym)
        if curr is None:
            continue

        try:
            entry = _tofloat(row[COL["Entry"] - 1])
            sl    = _tofloat(row[COL["SL"]    - 1])
            tp1   = _tofloat(row[COL["TP1"]   - 1])
            tp2   = _tofloat(row[COL["TP2"]   - 1])
        except (ValueError, IndexError):
            continue

        # Read TP3
        tp3 = 0.0
        try:
            tp3_idx = COL["TP3"] - 1
            if len(row) > tp3_idx and row[tp3_idx]:
                tp3 = _tofloat(row[tp3_idx])
        except (ValueError, IndexError):
            pass

        # Read existing trailing SL
        old_trailing = 0.0
        try:
            trail_idx = COL["Trailing SL"] - 1
            if len(row) > trail_idx and row[trail_idx]:
                old_trailing = _tofloat(row[trail_idx])
        except (ValueError, IndexError):
            pass

        cur_status = row[COL["Status"] - 1]

        # ── Trailing Stop logic per phase ──────────────────────────────
        new_trailing = old_trailing

        if cur_status in ("TP2", "TP2 🔒"):
            # Phase 2: tighter trailing after TP2 → running toward TP3
            new_trailing = _calc_trailing_sl(dirn, entry, sl, curr, old_trailing, phase="tp2")
        elif cur_status in ("TP1", "TP1 🔒"):
            # Phase 1: trailing after TP1
            new_trailing = _calc_trailing_sl(dirn, entry, sl, curr, old_trailing, phase="tp1")

        pnl        = _calc_pnl(dirn, entry, curr)
        new_status = _resolve_live(dirn, entry, sl, tp1, tp2, curr, new_trailing, tp3)

        # Activate trailing on phase transitions
        if new_status == "TP1 🔒" and cur_status == "OPEN":
            new_trailing = _calc_trailing_sl(dirn, entry, sl, curr, 0.0, phase="tp1")
        elif new_status == "TP2 🔒" and cur_status in ("TP1", "TP1 🔒"):
            # Transitioning to TP2 phase → reset trailing with tighter params
            new_trailing = _calc_trailing_sl(dirn, entry, sl, curr, 0.0, phase="tp2")

        # Check trailing SL hit for active positions
        if cur_status in ("TP1", "TP1 🔒", "TP2", "TP2 🔒") and new_trailing > 0:
            if dirn == "LONG" and curr <= new_trailing:
                new_status = "TSL ✅"   # Trailing Stop hit (profit locked)
            elif dirn == "SHORT" and curr >= new_trailing:
                new_status = "TSL ✅"

        # Update price + PnL + status
        updates.append({
            "range" : f"{price_col}{row_num}:{status_col}{row_num}",
            "values": [[curr, pnl, new_status]],
        })

        # Update trailing SL column
        if new_trailing != old_trailing and new_trailing > 0:
            updates.append({
                "range" : f"{trail_sl_col}{row_num}",
                "values": [[round(new_trailing, 6)]],
            })

        if new_status in ("TP3 ✅", "SL ❌", "TSL ✅"):
            to_recolor.append((row_num, new_status))

    if updates:
        try:
            with _lock:
                _ws.batch_update(updates, value_input_option="RAW")
            closed = len(to_recolor)
            print(f"[sheets] live: updated {len(updates)} rows  ({closed} closed)")
        except Exception as e:
            print(f"[sheets] live batch_update error: {e}")
            return

    for row_num, status in to_recolor:
        try:
            if status == "TP3 ✅":
                color = {"red": 0.60, "green": 0.95, "blue": 0.65}   # bright green for max TP
            elif status == "TSL ✅":
                color = {"red": 0.78, "green": 0.92, "blue": 0.85}   # teal for trailing win
            else:
                color = {"red": 0.95, "green": 0.72, "blue": 0.72}
            _ws.format(f"A{row_num}:W{row_num}", {"backgroundColor": color})
        except Exception:
            pass

    # Refresh dashboard + daily tab setiap live update
    update_dashboard()
    update_daily_pnl()


def _backtest_resolve_loop() -> None:
    """
    Background thread (lebih jarang): replay candle untuk cek TP/SL.
    Update kolom Status (Q) + warna baris.
    """
    while True:
        time.sleep(BACKTEST_RESOLVE_SEC)
        try:
            _run_backtest_resolver()
        except Exception as e:
            print(f"[sheets] backtest resolver error: {e}")


def _run_backtest_resolver() -> None:
    """Replay candle historis untuk tiap OPEN/TP1 row, update Status."""
    if _ws is None:
        return

    with _lock:
        all_rows = _ws.get_all_values()

    if len(all_rows) <= 1:
        return

    now_utc = datetime.now(timezone.utc)
    status_col_letter = _col_letter(COL["Status"])
    updates = []
    to_recolor: list[tuple[int, str]] = []

    for i, row in enumerate(all_rows[1:], start=2):
        if len(row) < 17:
            continue
        cur_status = row[COL["Status"] - 1]
        if cur_status not in ("OPEN", "TP1", "TP1 🔒", "TP2 🔒", ""):
            continue

        sym  = row[COL["Symbol"]    - 1]
        dirn = row[COL["Direction"] - 1]
        tf   = row[COL["Timeframe"] - 1] or "15m"

        try:
            entry = _tofloat(row[COL["Entry"] - 1])
            sl    = _tofloat(row[COL["SL"]    - 1])
            tp1   = _tofloat(row[COL["TP1"]   - 1])
            tp2   = _tofloat(row[COL["TP2"]   - 1])
        except (ValueError, IndexError):
            continue

        try:
            ts_str    = row[COL["Timestamp (UTC)"] - 1]
            signal_ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except Exception:
            continue

        tf_min  = _TF_MINUTES.get(tf, 15)
        age_min = (now_utc - signal_ts).total_seconds() / 60

        # Hanya replay kalau sudah lewat minimal 1 candle
        if age_min < tf_min:
            continue

        new_status, _ = _backtest_resolve(sym, dirn, tf, entry, sl, tp1, tp2, signal_ts)
        time.sleep(0.2)

        if new_status != cur_status:
            updates.append({
                "range" : f"{status_col_letter}{i}",
                "values": [[new_status]],
            })
            if new_status in ("TP3 ✅", "SL ❌", "TSL ✅"):
                to_recolor.append((i, new_status))

    if updates:
        try:
            with _lock:
                _ws.batch_update(updates, value_input_option="RAW")
            print(f"[sheets] resolver: updated {len(updates)} statuses")
        except Exception as e:
            print(f"[sheets] resolver batch_update error: {e}")

    # Recolor closed rows AFTER batch update
    for row_num, status in to_recolor:
        try:
            if status == "TP3 ✅":
                color = {"red": 0.60, "green": 0.95, "blue": 0.65}
            elif status == "TSL ✅":
                color = {"red": 0.78, "green": 0.92, "blue": 0.85}
            else:
                color = {"red": 0.95, "green": 0.72, "blue": 0.72}
            _ws.format(f"A{row_num}:W{row_num}", {"backgroundColor": color})
        except Exception:
            pass
    update_dashboard()
    update_daily_pnl()
    # Refresh dashboard setelah resolver update



def _col_letter(n: int) -> str:
    """Convert 1-based column index to letter (A, B, ... Z, AA, ...)."""
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


# ── Summary stats helper ──────────────────────────────────────────────────────
def get_sheet_stats() -> dict:
    """Return quick stats from the Signals sheet (for /status command)."""
    if not _enabled or _ws is None:
        return {}
    try:
        rows = _ws.get_all_values()[1:]   # skip header
        total   = len(rows)
        stat_idx = COL["Status"] - 1
        open_n  = sum(1 for r in rows if len(r) > stat_idx and r[stat_idx] in ("OPEN",))
        tp3_n   = sum(1 for r in rows if len(r) > stat_idx and "TP3" in r[stat_idx])
        tp2_n   = sum(1 for r in rows if len(r) > stat_idx and "TP2" in r[stat_idx] and "🔒" not in r[stat_idx])
        tp1_n   = sum(1 for r in rows if len(r) > stat_idx and r[stat_idx] in ("TP1", "TP1 🔒"))
        tsl_n   = sum(1 for r in rows if len(r) > stat_idx and "TSL" in r[stat_idx])
        sl_n    = sum(1 for r in rows if len(r) > stat_idx and "SL" in r[stat_idx] and "TSL" not in r[stat_idx])
        wins    = tp3_n + tp2_n + tsl_n
        win_rate = wins / (wins + sl_n) * 100 if (wins + sl_n) else 0
        return {
            "total": total, "open": open_n,
            "tp3": tp3_n, "tp2": tp2_n, "tp1": tp1_n, "tsl": tsl_n, "sl": sl_n,
            "win_rate": win_rate,
        }
    except Exception:
        return {}


# ── Open position guard ───────────────────────────────────────────────────────
def has_open_position(symbol: str, direction: str) -> bool:
    """
    Return True jika coin ini masih punya sinyal OPEN atau TP1
    (belum hit SL/TP2) di sheet — sehingga sinyal baru tidak perlu dikirim.

    Kalau Sheets disabled, selalu return False (tidak ada throttling).
    """
    if not _enabled or _ws is None:
        return False
    try:
        with _lock:
            rows = _ws.get_all_values()[1:]   # skip header
        for row in rows:
            if len(row) < 17:
                continue
            row_sym   = row[COL["Symbol"]    - 1].upper()
            row_dir   = row[COL["Direction"] - 1].upper()
            row_stat  = row[COL["Status"]    - 1]
            if (row_sym == symbol.upper()
                    and row_dir == direction.upper()
                    and row_stat in ("OPEN", "TP1", "TP1 🔒", "TP2 🔒", "")):
                return True
        return False
    except Exception:
        return False   # fail-open: kalau error, izinkan sinyal lewat



# ── Daily PnL Tab ──────────────────────────────────────────────────────────────
DAILY_TAB = "Daily"
_daily_ws  = None

# ── Dashboard Tab ──────────────────────────────────────────────────────────────
DASHBOARD_TAB = "Dashboard"
_dash_ws = None


def _get_daily_ws():
    global _daily_ws
    if _daily_ws is not None:
        return _daily_ws
    try:
        sh = _gc.open_by_key(SHEET_ID)
        try:
            _daily_ws = sh.worksheet(DAILY_TAB)
        except gspread.WorksheetNotFound:
            _daily_ws = sh.add_worksheet(title=DAILY_TAB, rows=100, cols=16)
        return _daily_ws
    except Exception as e:
        print(f"[daily] init error: {e}")
        return None


def update_daily_pnl() -> None:
    """
    Writes a 'Daily PnL' worksheet with:
      - Row 1 : Header bar
      - Row 2 : Date filter input (B2) + computed stats for that date (C2-L2)
      - Row 4 : Column headers for daily table
      - Row 5+: One row per calendar day (newest first, last 60 days)
    """
    if not _enabled or _ws is None:
        return
    dws = _get_daily_ws()
    if dws is None:
        return

    try:
        with _lock:
            rows = _ws.get_all_values()[1:]
    except Exception as e:
        print(f"[daily] read error: {e}")
        return

    ts_idx   = COL["Timestamp (UTC)"] - 1
    dir_idx  = COL["Direction"] - 1
    stat_idx = COL["Status"] - 1
    pnl_idx  = COL["PnL %"] - 1
    sym_idx  = COL["Symbol"] - 1

    # ── Aggregate per date ─────────────────────────────────────────
    from collections import defaultdict
    daily: dict = defaultdict(lambda: {
        "total":0,"wins":0,"losses":0,"open":0,
        "pnl":0.0,"best":0.0,"worst":0.0,
        "best_sym":"","worst_sym":"",
        "long_w":0,"long_l":0,"short_w":0,"short_l":0,
    })

    for r in rows:
        if len(r) <= max(ts_idx, stat_idx, pnl_idx): continue
        ts_raw = r[ts_idx].strip()
        if not ts_raw: continue
        date_key = ts_raw[:10]   # "YYYY-MM-DD"
        status   = r[stat_idx].strip()
        dirn     = r[dir_idx].strip().upper() if len(r) > dir_idx else ""
        sym      = r[sym_idx].strip() if len(r) > sym_idx else ""
        raw_p    = r[pnl_idx].replace("%","").replace("+","").strip() if len(r) > pnl_idx else ""
        try:
            pv = float(raw_p.replace(",",".")) if raw_p and raw_p != "–" else 0.0
        except ValueError:
            pv = 0.0

        d = daily[date_key]
        d["total"] += 1

        is_win  = ("TP" in status and "🔒" not in status) or "TSL" in status
        is_loss = "SL" in status and "TSL" not in status

        if is_win:
            d["wins"] += 1; d["pnl"] += pv
            if dirn == "LONG":  d["long_w"]  += 1
            else:               d["short_w"] += 1
            if pv > d["best"]:
                d["best"] = pv; d["best_sym"] = sym
        elif is_loss:
            d["losses"] += 1; d["pnl"] += pv
            if dirn == "LONG":  d["long_l"]  += 1
            else:               d["short_l"] += 1
            if pv < d["worst"]:
                d["worst"] = pv; d["worst_sym"] = sym
        else:
            d["open"] += 1

    # Sort dates newest-first, limit 60 days
    sorted_dates = sorted(daily.keys(), reverse=True)[:60]

    # ── Read user filter date from B2 ─────────────────────────────
    try:
        filter_date = dws.acell("B2").value or ""
        filter_date = filter_date.strip()
    except Exception:
        filter_date = ""

    # ── Build header info for filtered date ───────────────────────
    def _day_stats_row(date_key):
        d = daily.get(date_key)
        if not d or d["total"] == 0:
            return ["—"] * 10
        closed = d["wins"] + d["losses"]
        wr     = d["wins"] / closed * 100 if closed > 0 else 0.0
        pnl_s  = f"{'+'if d['pnl']>=0 else''}{d['pnl']:.2f}%"
        best_s = f"+{d['best']:.2f}% ({d['best_sym']})" if d["best_sym"] else "—"
        worst_s= f"{d['worst']:.2f}% ({d['worst_sym']})" if d["worst_sym"] else "—"
        return [
            d["total"], closed, d["wins"], d["losses"],
            f"{wr:.1f}%", pnl_s,
            f"L {d['long_w']}W/{d['long_l']}L",
            f"S {d['short_w']}W/{d['short_l']}L",
            best_s, worst_s,
        ]

    fd_stats = _day_stats_row(filter_date) if filter_date else ["(ketik tanggal di B2)"] + [""] * 9

    # ── Build grid ────────────────────────────────────────────────
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    empty16 = [""] * 16

    def row16(*vals):
        r = list(vals); r += [""] * (16 - len(r)); return r[:16]

    grid = []
    # ROW 1: Header
    grid.append(row16("📅  DAILY PnL REVIEW","","","","","","","","","","","","","",f"🕐 {now_str}",""))
    # ROW 2: Filter row
    grid.append(row16("🔍 Filter Tanggal:", filter_date if filter_date else "YYYY-MM-DD",
        fd_stats[0], fd_stats[1], fd_stats[2], fd_stats[3],
        fd_stats[4], fd_stats[5], fd_stats[6], fd_stats[7],
        fd_stats[8], fd_stats[9], "", "", "", ""))
    # ROW 3: Filter labels
    grid.append(row16("", "Tanggal (edit B2)", "Total", "Closed", "Wins", "Losses",
        "Win Rate", "Net PnL", "LONG W/L", "SHORT W/L", "Best Trade", "Worst Trade",
        "", "", "", ""))
    # ROW 4: Spacer
    grid.append(empty16[:])
    # ROW 5: Table header
    grid.append(row16("Tanggal","Total","Closed","W","L","Win Rate","Net PnL",
        "LONG","SHORT","Best Trade","Worst Trade","","","","",""))

    # ROW 6+: Daily rows
    for dk in sorted_dates:
        d = daily[dk]
        closed  = d["wins"] + d["losses"]
        wr      = d["wins"] / closed * 100 if closed > 0 else 0.0
        pnl_s   = f"{'+'if d['pnl']>=0 else''}{d['pnl']:.2f}%"
        best_s  = f"+{d['best']:.2f}% {d['best_sym']}" if d["best_sym"] else "—"
        worst_s = f"{d['worst']:.2f}% {d['worst_sym']}" if d["worst_sym"] else "—"
        grid.append(row16(
            dk, d["total"], closed, d["wins"], d["losses"],
            f"{wr:.1f}%", pnl_s,
            f"{d['long_w']}W/{d['long_l']}L",
            f"{d['short_w']}W/{d['short_l']}L",
            best_s, worst_s,
            "", "", "", "", ""
        ))

    # ── Write ─────────────────────────────────────────────────────
    try:
        # Save B2 filter value before clearing
        try:
            saved_filter = dws.acell("B2").value or ""
        except Exception:
            saved_filter = filter_date

        dws.clear()
        dws.update(f"A1:P{len(grid)}", grid, value_input_option="RAW")
        # Restore B2 if user had typed a date (clear wipes it)
        if saved_filter and saved_filter not in ("YYYY-MM-DD", ""):
            dws.update("B2", [[saved_filter]], value_input_option="RAW")
        time.sleep(0.4)

        # ── Batch formatting (single API call) ────────────────────
        sh  = _gc.open_by_key(SHEET_ID); sid = dws.id
        reqs = []

        def _cell_fmt(r0, c0, r1, c1, fmt_props):
            reqs.append({"repeatCell": {
                "range": {"sheetId":sid,"startRowIndex":r0,"endRowIndex":r1,
                          "startColumnIndex":c0,"endColumnIndex":c1},
                "cell": {"userEnteredFormat": fmt_props},
                "fields": "userEnteredFormat"}})

        # Row 1: header bar
        _cell_fmt(0,0,1,16, {"backgroundColor":_rgb(30,40,58),
            "textFormat":{"bold":True,"fontSize":13,
                "foregroundColor":{"red":1,"green":1,"blue":1}}})

        # Row 2: filter label (A2)
        _cell_fmt(1,0,2,1, {"textFormat":{"bold":True,"fontSize":10,
            "foregroundColor":_rgb(60,70,100)}})
        # B2: yellow input cell
        _cell_fmt(1,1,2,2, {"backgroundColor":_rgb(255,252,220),
            "textFormat":{"bold":True,"fontSize":11,"foregroundColor":_rgb(180,100,0)},
            "horizontalAlignment":"CENTER"})
        # C2-L2: filter stats
        _cell_fmt(1,2,2,12, {"textFormat":{"bold":True,"fontSize":10},
            "horizontalAlignment":"CENTER"})

        # Row 3: label sub-row
        _cell_fmt(2,0,3,12, {"backgroundColor":_rgb(240,242,246),
            "textFormat":{"fontSize":8,"foregroundColor":{"red":0.5,"green":0.5,"blue":0.6}},
            "horizontalAlignment":"CENTER"})

        # Row 5: table header (index 4)
        _cell_fmt(4,0,5,11, {"backgroundColor":_rgb(50,65,90),
            "textFormat":{"bold":True,"fontSize":10,
                "foregroundColor":{"red":1,"green":1,"blue":1}},
            "horizontalAlignment":"CENTER"})

        # Data rows alternating background
        for i, dk in enumerate(sorted_dates):
            ri = 5 + i   # 0-indexed row
            d  = daily[dk]
            if dk == filter_date:
                bg = _rgb(255,248,200)
            elif i % 2 == 0:
                bg = _rgb(252,252,255)
            else:
                bg = _rgb(245,246,250)
            _cell_fmt(ri, 0, ri+1, 11, {"backgroundColor":bg, "textFormat":{"fontSize":10}})
            # Date bold
            _cell_fmt(ri, 0, ri+1, 1, {"textFormat":{"bold":True,"fontSize":10},
                "backgroundColor":bg})
            # Win Rate (col F = index 5) colored
            closed_d = d["wins"] + d["losses"]
            wr_v = d["wins"]/closed_d*100 if closed_d > 0 else 0.0
            _cell_fmt(ri, 5, ri+1, 6, {"textFormat":{"bold":True,"fontSize":10,
                "foregroundColor":_rgb(46,139,87) if wr_v>=50 else _rgb(180,50,50)}})
            # Net PnL (col G = index 6) colored
            _cell_fmt(ri, 6, ri+1, 7, {"textFormat":{"bold":True,"fontSize":10,
                "foregroundColor":_rgb(30,130,60) if d["pnl"]>=0 else _rgb(180,50,50)}})

        # Column widths
        for ci, px in [(0,105),(1,100),(2,55),(3,40),(4,40),(5,72),(6,92),
                        (7,92),(8,92),(9,145),(10,145)]:
            reqs.append({"updateDimensionProperties": {
                "range":{"sheetId":sid,"dimension":"COLUMNS",
                         "startIndex":ci,"endIndex":ci+1},
                "properties":{"pixelSize":px},"fields":"pixelSize"}})

        # Row heights: header taller
        reqs.append({"updateDimensionProperties": {
            "range":{"sheetId":sid,"dimension":"ROWS","startIndex":0,"endIndex":1},
            "properties":{"pixelSize":40},"fields":"pixelSize"}})
        reqs.append({"updateDimensionProperties": {
            "range":{"sheetId":sid,"dimension":"ROWS","startIndex":1,"endIndex":2},
            "properties":{"pixelSize":36},"fields":"pixelSize"}})

        sh.batch_update({"requests": reqs})
        dws.freeze(rows=1, cols=1)
        print(f"[daily] ✅ Updated — {len(sorted_dates)} days, filter={filter_date or 'none'}")
    except Exception as e:
        import traceback
        print(f"[daily] write error: {e}")
        traceback.print_exc()



def _get_dash_ws():
    global _dash_ws
    if _dash_ws is not None:
        return _dash_ws
    try:
        sh = _gc.open_by_key(SHEET_ID)
        try:
            _dash_ws = sh.worksheet(DASHBOARD_TAB)
        except gspread.WorksheetNotFound:
            _dash_ws = sh.add_worksheet(title=DASHBOARD_TAB, rows=80, cols=14)
        return _dash_ws
    except Exception as e:
        print(f"[dashboard] init error: {e}")
        return None


def _rgb(r, g, b):
    return {"red": r/255, "green": g/255, "blue": b/255}


def _fmt(dws, range_: str, fmt: dict):
    try: dws.format(range_, fmt)
    except Exception: pass


def _merge(dws, range_: str):
    try:
        sh = _gc.open_by_key(SHEET_ID)
        sid = dws.id
        body = {"requests": [{"mergeCells": {
            "range": _parse_range(range_, sid),
            "mergeType": "MERGE_ALL"
        }}]}
        sh.batch_update(body)
    except Exception:
        pass


def _parse_range(a1: str, sheet_id: int) -> dict:
    """Convert A1:B2 notation to GridRange dict."""
    import re
    m = re.match(r"([A-Z]+)(\d+):([A-Z]+)(\d+)", a1)
    if not m:
        return {}
    def col_idx(s): return sum((ord(c)-64)*26**i for i, c in enumerate(reversed(s))) - 1
    return {
        "sheetId": sheet_id,
        "startColumnIndex": col_idx(m.group(1)),
        "endColumnIndex":   col_idx(m.group(3)) + 1,
        "startRowIndex":    int(m.group(2)) - 1,
        "endRowIndex":      int(m.group(4)),
    }


def _spark_bar(value: float, max_val: float = 100, width: int = 10) -> str:
    """ASCII progress bar untuk Google Sheets."""
    filled = int(round(value / max_val * width)) if max_val > 0 else 0
    filled = max(0, min(width, filled))
    return "█" * filled + "░" * (width - filled)


def update_dashboard() -> None:
    """
    Tulis ulang tab Dashboard dengan visual dashboard lengkap.

    Layout (kolom A-M, 14 kolom):
    ┌─────────────────────────────────────────────────────┐
    │  ROW 1-2   : Header / judul                         │
    │  ROW 3     : spacer                                  │
    │  ROW 4-5   : KPI cards  (5 metric dalam 1 baris)    │
    │  ROW 6     : spacer                                  │
    │  ROW 7-13  : LONG stats (kiri) | SHORT stats (kanan)│
    │  ROW 14    : spacer                                  │
    │  ROW 15-16 : Win Rate visual bar                     │
    │  ROW 17    : spacer                                  │
    │  ROW 18-...: 15 sinyal terbaru (tabel lengkap)      │
    └─────────────────────────────────────────────────────┘
    """
    if not _enabled or _ws is None:
        return
    dws = _get_dash_ws()
    if dws is None:
        return

    try:
        with _lock:
            rows = _ws.get_all_values()[1:]
    except Exception as e:
        print(f"[dashboard] read error: {e}")
        return

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # ── Hitung stats ──────────────────────────────────────────────────
    total = len(rows)
    stat_idx = COL["Status"] - 1
    open_n = tp1_n = tp2_n = tp3_n = tsl_n = sl_n = 0
    pnl_sum = pnl_tp = pnl_sl = 0.0
    long_tp3 = long_tp2 = long_tp1 = long_tsl = long_sl = 0;  long_pnl = 0.0
    short_tp3 = short_tp2 = short_tp1 = short_tsl = short_sl = 0; short_pnl = 0.0
    pnl_history = []

    for r in rows:
        if len(r) <= stat_idx:
            continue
        status = r[stat_idx]
        dirn   = r[COL["Direction"] - 1].upper()
        raw_p  = r[COL["PnL %"] - 1].replace("%","").replace("+","").strip()
        try:
            pv = float(raw_p.replace(",", ".")) if raw_p and raw_p != "–" else 0.0
        except ValueError:
            pv = 0.0

        if "TP3" in status:
            tp3_n += 1; pnl_sum += pv; pnl_tp += pv; pnl_history.append(pv)
            if dirn == "LONG":  long_tp3  += 1; long_pnl  += pv
            else:               short_tp3 += 1; short_pnl += pv
        elif "TP2" in status and "🔒" not in status:
            tp2_n += 1; pnl_sum += pv; pnl_tp += pv; pnl_history.append(pv)
            if dirn == "LONG":  long_tp2  += 1; long_pnl  += pv
            else:               short_tp2 += 1; short_pnl += pv
        elif "TSL" in status:
            tsl_n += 1; pnl_sum += pv; pnl_tp += pv; pnl_history.append(pv)
            if dirn == "LONG":  long_tsl  += 1; long_pnl  += pv
            else:               short_tsl += 1; short_pnl += pv
        elif status in ("TP1", "TP1 🔒"):
            tp1_n += 1; pnl_sum += pv; pnl_tp += pv; pnl_history.append(pv)
            if dirn == "LONG":  long_tp1  += 1; long_pnl  += pv
            else:               short_tp1 += 1; short_pnl += pv
        elif "SL" in status and "TSL" not in status:
            sl_n  += 1; pnl_sum += pv; pnl_sl  += pv; pnl_history.append(pv)
            if dirn == "LONG":  long_sl   += 1; long_pnl  += pv
            else:               short_sl  += 1; short_pnl += pv
        else:
            open_n += 1

    wins     = tp3_n + tp2_n + tp1_n + tsl_n
    closed   = wins + sl_n
    win_rate = wins / closed * 100 if closed > 0 else 0.0
    avg_win  = pnl_tp / wins  if wins > 0  else 0.0
    avg_loss = pnl_sl / sl_n  if sl_n > 0  else 0.0
    pnl_sign = "+" if pnl_sum >= 0 else ""

    l_wins   = long_tp3 + long_tp2 + long_tp1 + long_tsl
    s_wins   = short_tp3 + short_tp2 + short_tp1 + short_tsl
    l_closed = l_wins + long_sl
    s_closed = s_wins + short_sl
    l_wr = l_wins / l_closed * 100  if l_closed > 0 else 0.0
    s_wr = s_wins / s_closed * 100  if s_closed > 0 else 0.0

    # ── Conviction breakdown ─────────────────────────────────
    conv_idx = COL["Conviction"] - 1
    high_n = medium_n = low_n = 0
    for r in rows:
        if len(r) <= conv_idx: continue
        cv = str(r[conv_idx]).strip().lower()
        if "high" in cv:   high_n   += 1
        elif "medium" in cv: medium_n += 1
        elif "low" in cv:    low_n    += 1

    # ── Win streak (most recent closed signals) ───────────────
    streak = 0; streak_type = ""
    for r in reversed(rows):
        if len(r) <= stat_idx: continue
        st = r[stat_idx]
        is_win = ("TP" in st and "🔒" not in st) or "TSL" in st
        is_loss = "SL" in st and "TSL" not in st
        if not is_win and not is_loss: continue
        if streak == 0:
            streak_type = "W" if is_win else "L"
            streak = 1
        elif (is_win and streak_type == "W") or (is_loss and streak_type == "L"):
            streak += 1
        else:
            break
    streak_str = f"{streak}{'🔥' if streak_type=='W' else '❄️'} {'WIN' if streak_type=='W' else 'LOSS'} streak" if streak > 0 else "—"

    wr_bar   = _spark_bar(win_rate, 100, 20)
    l_wr_bar = _spark_bar(l_wr, 100, 14)
    s_wr_bar = _spark_bar(s_wr, 100, 14)


    # ── Helper layout ─────────────────────────────────────────────────
    # 14 cols: A B C D | E F G | H I J K | L M N (spacer col)
    # LONG block: A=label, D=value  |  SHORT block: H=label, K=value
    # Cols E-G = divider gap between LONG and SHORT
    empty14 = [""] * 14
    def row14(*vals):
        r = list(vals); r += [""] * (14 - len(r)); return r[:14]

    # A=label, B C = span, D=value, E F G = gap, H=label, I J = span, K=value, L M N = empty
    def stat_row(l_label, l_val, r_label, r_val):
        return row14(l_label, "", "", l_val, "", "", "", r_label, "", "", r_val, "", "", "")

    recent = rows[-5:][::-1]
    recent_grid = []
    for r in recent:
        if len(r) <= COL["Status"] - 1: continue
        ts    = r[COL["Timestamp (UTC)"] - 1][5:16]
        sym   = r[COL["Symbol"] - 1];    dirn = r[COL["Direction"] - 1]
        tf    = r[COL["Timeframe"] - 1]; ent  = r[COL["Entry"] - 1]
        sl_v  = r[COL["SL"] - 1];        tp2v = r[COL["TP2"] - 1]
        rr_v  = r[COL["RR"] - 1] if len(r) > COL["RR"]-1 else ""
        conv  = r[COL["Conviction"] - 1]
        curr  = r[COL["Current Price"] - 1]
        pnl_v = r[COL["PnL %"] - 1];    stat = r[COL["Status"] - 1]
        recent_grid.append(row14(ts, sym, dirn, tf, ent, sl_v, tp2v, rr_v, conv, curr, pnl_v, stat))

    l_sign  = "+" if long_pnl  >= 0 else ""
    s_sign  = "+" if short_pnl >= 0 else ""
    l_pnl_s = f"{l_sign}{long_pnl:.2f}%"
    s_pnl_s = f"{s_sign}{short_pnl:.2f}%"

    grid = []
    # ── ROW 1: Header ────────────────────────────────────────────────
    grid.append(row14("📊  VWAP SCREENER — PERFORMANCE DASHBOARD","","","","","","","","","","","",f"🕐 {now_str}",""))
    grid.append(empty14[:])  # ROW 2

    # ── ROW 3-5: KPI cards ───────────────────────────────────────────
    pnl_str_v = f"{pnl_sign}{pnl_sum:.2f}%"
    grid.append(row14("TOTAL SINYAL","","","WIN RATE","","","TOTAL PnL","","","AVG WIN","","AVG LOSS","",""))
    grid.append(row14(total,"","",f"{win_rate:.1f}%","","",pnl_str_v,"","",f"+{avg_win:.2f}%","",f"{avg_loss:.2f}%","",""))
    grid.append(row14(f"{open_n} open · {closed} closed","","",f"{wins}W / {sl_n}L","","","closed trades","","","","","","",""))
    grid.append(empty14[:])  # ROW 6

    # ── ROW 7: LONG / SHORT section headers ──────────────────────────
    grid.append(row14("🟢  LONG performance","","","","","","","🔴  SHORT performance","","","","","",""))

    # ROW 8: Win rate % label + bar
    grid.append(row14(f"Win Rate: {l_wr:.1f}%","","",l_wr_bar,"","","",f"Win Rate: {s_wr:.1f}%","","",s_wr_bar,"","",""))
    grid.append(empty14[:])  # ROW 9

    # ROW 10-14: Result breakdown
    grid.append(stat_row("🚀  TP3 Extended", long_tp3, "🚀  TP3 Extended", short_tp3))
    grid.append(stat_row("🏆  TP2 Target",   long_tp2, "🏆  TP2 Target",   short_tp2))
    grid.append(stat_row("💡  Trailing SL",  long_tsl, "💡  Trailing SL",  short_tsl))
    grid.append(stat_row("🎯  TP1 Target",   long_tp1, "🎯  TP1 Target",   short_tp1))
    grid.append(stat_row("🛑  Stop Loss",    long_sl,  "🛑  Stop Loss",    short_sl))

    # ROW 15-16: Win rate + Net PnL
    grid.append(stat_row("Win Rate",         f"{l_wr:.1f}%",  "Win Rate",        f"{s_wr:.1f}%"))
    grid.append(stat_row("Net PnL",          l_pnl_s,         "Net PnL",         s_pnl_s))
    grid.append(empty14[:])  # ROW 17

    # ── ROW 18-19: Extra stats row ───────────────────────────────────
    grid.append(row14("📋  SIGNAL STATS","","","","","","","","","","","","",""))
    grid.append(row14(
        f"🟡 High: {high_n}", "", f"🟠 Medium: {medium_n}", f"⚫ Low: {low_n}",
        "", "", "",
        f"Current: {streak_str}", "", "", "", "", "", ""
    ))
    grid.append(empty14[:])  # ROW 20

    # ── ROW 21-22: Overall win rate ───────────────────────────────────
    grid.append(row14("📈  OVERALL WIN RATE","","","","","","","","","","","","",""))
    grid.append(row14(f"{win_rate:.1f}%  {wr_bar}  ({wins}W / {sl_n}L dari {closed} closed)","","","","","","","","","","","","",""))
    grid.append(empty14[:])  # ROW 23

    # ── ROW 24+: Recent signals table ────────────────────────────────
    tbl_hdr = len(grid)
    grid.append(row14("Timestamp","Symbol","Dir","TF","Entry","SL","TP2","RR","Conv","Price","PnL %","Status"))
    grid.extend(recent_grid)

    # ── Write ─────────────────────────────────────────────────────────
    try:
        dws.clear()
        # Reset formatting to remove ghost colored rows from old dashboard
        _fmt(dws, "A1:N50", {"backgroundColor": {"red":1,"green":1,"blue":1},
            "textFormat": {"bold":False,"fontSize":10,"foregroundColor":{"red":0,"green":0,"blue":0},
                           "fontFamily":"Arial"}})
        dws.update(f"A1:N{len(grid)}", grid, value_input_option="RAW")
        time.sleep(0.3)

        # ── Formatting ────────────────────────────────────────────────
        # ROW 1: Header bar
        _fmt(dws, "A1:N1", {"backgroundColor": _rgb(30,40,58),
            "textFormat": {"bold":True,"fontSize":14,"foregroundColor":{"red":1,"green":1,"blue":1}},
            "verticalAlignment": "MIDDLE"})

        # ROW 3: KPI label row
        _fmt(dws, "A3:N3", {"backgroundColor": _rgb(235,238,245),
            "textFormat": {"bold":True,"fontSize":9,"foregroundColor":{"red":0.35,"green":0.35,"blue":0.5}},
            "horizontalAlignment": "CENTER"})

        # ROW 4: KPI value row
        _fmt(dws, "A4:N4", {"textFormat": {"bold":True,"fontSize":18},
            "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE"})
        wr_c = _rgb(46,139,87) if win_rate>=60 else (_rgb(200,150,30) if win_rate>=40 else _rgb(180,50,50))
        _fmt(dws, "D4", {"textFormat": {"bold":True,"fontSize":18,"foregroundColor": wr_c}})
        pnl_c = _rgb(46,139,87) if pnl_sum>=0 else _rgb(180,50,50)
        _fmt(dws, "G4", {"textFormat": {"bold":True,"fontSize":18,"foregroundColor": pnl_c}})
        _fmt(dws, "J4", {"textFormat": {"bold":True,"fontSize":18,"foregroundColor": _rgb(46,139,87)}})
        _fmt(dws, "L4", {"textFormat": {"bold":True,"fontSize":18,"foregroundColor": _rgb(180,50,50)}})

        # ROW 5: KPI sub
        _fmt(dws, "A5:N5", {"textFormat": {"fontSize":9,"foregroundColor":{"red":0.5,"green":0.5,"blue":0.6}},
            "horizontalAlignment": "CENTER"})

        # ── LONG/SHORT section (rows 7-16) ────────────────────────────
        # Row 7: section headers with background
        _fmt(dws, "A7:F7", {"backgroundColor": _rgb(235,250,240),
            "textFormat": {"bold":True,"fontSize":12,"foregroundColor": _rgb(22,100,55)}})
        _fmt(dws, "H7:M7", {"backgroundColor": _rgb(253,238,238),
            "textFormat": {"bold":True,"fontSize":12,"foregroundColor": _rgb(150,30,30)}})

        # Row 8: win rate label (A-C) and bar (D-F) in Courier, colored
        _fmt(dws, "A8:C8", {"textFormat": {"bold":True,"fontSize":10,"foregroundColor": _rgb(22,100,55)}})
        _fmt(dws, "D8:F8", {"textFormat": {"fontFamily":"Courier New","fontSize":10,
            "foregroundColor": _rgb(46,139,87)}})
        _fmt(dws, "H8:J8", {"textFormat": {"bold":True,"fontSize":10,"foregroundColor": _rgb(150,30,30)}})
        _fmt(dws, "K8:M8", {"textFormat": {"fontFamily":"Courier New","fontSize":10,
            "foregroundColor": _rgb(180,50,50)}})

        # Rows 10-14: stat rows — label left-aligned, value (col D/K) right-aligned bold
        for row_n in range(10, 15):
            _fmt(dws, f"A{row_n}:C{row_n}", {"textFormat": {"fontSize":10}})
            _fmt(dws, f"D{row_n}", {"textFormat": {"bold":True,"fontSize":11},
                "horizontalAlignment": "CENTER"})
            _fmt(dws, f"H{row_n}:J{row_n}", {"textFormat": {"fontSize":10}})
            _fmt(dws, f"K{row_n}", {"textFormat": {"bold":True,"fontSize":11},
                "horizontalAlignment": "CENTER"})

        # Row 15: Win Rate — colored
        _fmt(dws, "A15:C15", {"textFormat": {"bold":True,"italic":True,"fontSize":10}})
        _fmt(dws, "D15", {"textFormat": {"bold":True,"fontSize":11,"foregroundColor":
            _rgb(46,139,87) if l_wr>=50 else _rgb(180,50,50)}, "horizontalAlignment": "CENTER"})
        _fmt(dws, "H15:J15", {"textFormat": {"bold":True,"italic":True,"fontSize":10}})
        _fmt(dws, "K15", {"textFormat": {"bold":True,"fontSize":11,"foregroundColor":
            _rgb(46,139,87) if s_wr>=50 else _rgb(180,50,50)}, "horizontalAlignment": "CENTER"})

        # Row 16: Net PnL — colored
        _fmt(dws, "A16:C16", {"textFormat": {"bold":True,"italic":True,"fontSize":10}})
        _fmt(dws, "D16", {"textFormat": {"bold":True,"fontSize":12,"foregroundColor":
            _rgb(46,139,87) if long_pnl>=0 else _rgb(180,50,50)}, "horizontalAlignment": "CENTER"})
        _fmt(dws, "H16:J16", {"textFormat": {"bold":True,"italic":True,"fontSize":10}})
        _fmt(dws, "K16", {"textFormat": {"bold":True,"fontSize":12,"foregroundColor":
            _rgb(46,139,87) if short_pnl>=0 else _rgb(180,50,50)}, "horizontalAlignment": "CENTER"})

        # Thin separator line above LONG/SHORT (row 7 top border via bg already done above)
        # Row 18: Signal Stats header
        _fmt(dws, "A18:N18", {"backgroundColor": _rgb(245,247,252),
            "textFormat": {"bold":True,"fontSize":10,"foregroundColor": _rgb(60,70,100)}})

        # Row 19: Conviction + streak data
        _fmt(dws, "A19:D19", {"textFormat": {"fontSize":10}})
        _fmt(dws, "H19:N19", {"textFormat": {"bold":True,"fontSize":10,"foregroundColor":
            _rgb(46,139,87) if streak_type=="W" else (_rgb(180,50,50) if streak_type=="L" else _rgb(80,80,80))}})

        # Row 22: Overall win rate bar
        _fmt(dws, "A22:N22", {"backgroundColor": _rgb(242,247,255),
            "textFormat": {"fontFamily":"Courier New","fontSize":11,"bold":True}})

        # Table header (dynamic)
        thr = tbl_hdr + 1
        _fmt(dws, f"A{thr}:N{thr}", {"backgroundColor": _rgb(50,65,90),
            "textFormat": {"bold":True,"fontSize":10,"foregroundColor":{"red":1,"green":1,"blue":1}},
            "horizontalAlignment": "CENTER"})

        # Data rows
        for i, r in enumerate(recent_grid):
            rn = thr + 1 + i; stat = r[11] if len(r)>11 else ""
            if "TP3" in stat:                              bg = _rgb(200,245,210)
            elif "TSL" in stat:                            bg = _rgb(210,240,230)
            elif "TP2" in stat:                            bg = _rgb(230,248,234)
            elif "TP1" in stat:                            bg = _rgb(225,240,255)
            elif "SL" in stat and "TSL" not in stat:       bg = _rgb(255,232,230)
            else:                                          bg = _rgb(250,250,252)
            _fmt(dws, f"A{rn}:N{rn}", {"backgroundColor": bg, "fontSize": 10})
            try:
                pv = float(str(r[10]).replace("%","").replace("+","").replace(",","."))
                pc = _rgb(30,130,60) if pv>=0 else _rgb(180,50,50)
            except Exception: pc = _rgb(80,80,80)
            _fmt(dws, f"K{rn}", {"textFormat": {"bold":True,"foregroundColor": pc}})

        # ── Batch: widths + row heights + cleanup charts ───────────────
        sh = _gc.open_by_key(SHEET_ID); sid = dws.id
        reqs = []
        # Column widths — designed to fit the 12-column table at the bottom cleanly
        for ci, px in [(0,105),(1,70),(2,60),(3,45),(4,75),(5,75),
                        (6,75),(7,45),(8,75),(9,75),(10,75),(11,90),(12,40),(13,40)]:
            reqs.append({"updateDimensionProperties": {
                "range": {"sheetId":sid,"dimension":"COLUMNS","startIndex":ci,"endIndex":ci+1},
                "properties": {"pixelSize":px}, "fields":"pixelSize"}})
        # Row 1 taller
        reqs.append({"updateDimensionProperties": {
            "range":{"sheetId":sid,"dimension":"ROWS","startIndex":0,"endIndex":1},
            "properties":{"pixelSize":44},"fields":"pixelSize"}})
        # Row 4 KPI values taller
        reqs.append({"updateDimensionProperties": {
            "range":{"sheetId":sid,"dimension":"ROWS","startIndex":3,"endIndex":4},
            "properties":{"pixelSize":40},"fields":"pixelSize"}})

        # Delete leftover charts
        meta = sh.fetch_sheet_metadata()
        for s in meta.get("sheets",[]):
            if s["properties"]["sheetId"] == sid:
                for c in s.get("charts",[]):
                    reqs.append({"deleteEmbeddedObject":{"objectId":c["chartId"]}})

        if reqs: sh.batch_update({"requests": reqs})
        dws.freeze(rows=1)
        print(f"[dashboard] ✅ WR={win_rate:.1f}% PnL={pnl_sign}{pnl_sum:.2f}% streak={streak_str}")
    except Exception as e:
        print(f"[dashboard] write error: {e}")

