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
    "Entry", "SL", "TP1", "TP2", "RR", "RSI",
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
        ws.format("A1:V1", {
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
        ws.format(f"A{row}:V{row}", {"backgroundColor": color})
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
                  trailing_sl: float = 0.0) -> str:
    """
    Quick check vs current price only (for very fresh signals).
    🆕 Trailing Stop: after TP1, SL moves to breakeven then trails.
    """
    active_sl = trailing_sl if trailing_sl > 0 else sl

    if direction == "LONG":
        if current >= tp2: return "TP2 ✅"
        if current >= tp1: return "TP1 🔒"   # trailing activated
        if current <= active_sl: return "SL ❌"
    else:
        if current <= tp2: return "TP2 ✅"
        if current <= tp1: return "TP1 🔒"
        if current >= active_sl: return "SL ❌"
    return "OPEN"


def _calc_trailing_sl(direction: str, entry: float, sl: float,
                      current: float, old_trailing: float) -> float:
    """
    🆕 Calculate trailing stop level.
    After TP1: SL moves to entry (breakeven).
    Then trails at 50% of original risk distance behind price.
    """
    risk = abs(entry - sl)
    trail_dist = risk * 0.5   # trail at 50% of original risk

    if direction == "LONG":
        # New trailing = current price - trail distance
        new_trail = current - trail_dist
        # Never go below entry (breakeven) once trailing starts
        new_trail = max(new_trail, entry)
        # Only move UP, never down
        if old_trailing > 0:
            return max(new_trail, old_trailing)
        return new_trail
    else:
        # SHORT: trailing goes above price
        new_trail = current + trail_dist
        new_trail = min(new_trail, entry)  # never above entry
        if old_trailing > 0:
            return min(new_trail, old_trailing)
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
        if status in ("OPEN", "TP1", "TP1 🔒", ""):
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

        # 🆕 Read existing trailing SL
        old_trailing = 0.0
        try:
            trail_idx = COL["Trailing SL"] - 1
            if len(row) > trail_idx and row[trail_idx]:
                old_trailing = _tofloat(row[trail_idx])
        except (ValueError, IndexError):
            pass

        cur_status = row[COL["Status"] - 1]

        # 🆕 Trailing Stop logic
        new_trailing = old_trailing
        if cur_status in ("TP1", "TP1 🔒"):
            # Already hit TP1 — calculate trailing SL
            new_trailing = _calc_trailing_sl(dirn, entry, sl, curr, old_trailing)

        pnl        = _calc_pnl(dirn, entry, curr)
        new_status = _resolve_live(dirn, entry, sl, tp1, tp2, curr, new_trailing)

        # If just hit TP1, activate trailing
        if new_status == "TP1 🔒" and cur_status == "OPEN":
            new_trailing = _calc_trailing_sl(dirn, entry, sl, curr, 0.0)

        # 🆕 Check trailing SL hit for TP1 positions
        if cur_status in ("TP1", "TP1 🔒") and new_trailing > 0:
            if dirn == "LONG" and curr <= new_trailing:
                new_status = "TSL ✅"   # Trailing Stop hit (profit)
            elif dirn == "SHORT" and curr >= new_trailing:
                new_status = "TSL ✅"

        # Update O + P + Q sekaligus tiap 60s
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

        if new_status in ("TP2 ✅", "SL ❌", "TSL ✅"):
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
            if status == "TP2 ✅":
                color = {"red": 0.72, "green": 0.93, "blue": 0.72}
            elif status == "TSL ✅":
                color = {"red": 0.78, "green": 0.92, "blue": 0.85}   # teal for trailing win
            else:
                color = {"red": 0.95, "green": 0.72, "blue": 0.72}
            _ws.format(f"A{row_num}:V{row_num}", {"backgroundColor": color})
        except Exception:
            pass

    # Refresh dashboard setiap live update
    update_dashboard()


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
        if cur_status not in ("OPEN", "TP1", "TP1 🔒", ""):
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
            if new_status in ("TP2 ✅", "SL ❌", "TSL ✅"):
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
            if status == "TP2 ✅":
                color = {"red": 0.72, "green": 0.93, "blue": 0.72}
            elif status == "TSL ✅":
                color = {"red": 0.78, "green": 0.92, "blue": 0.85}
            else:
                color = {"red": 0.95, "green": 0.72, "blue": 0.72}
            _ws.format(f"A{row_num}:V{row_num}", {"backgroundColor": color})
        except Exception:
            pass
    update_dashboard()
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
        open_n  = sum(1 for r in rows if len(r) >= 17 and r[16] in ("OPEN",))
        tp2_n   = sum(1 for r in rows if len(r) >= 17 and "TP2" in r[16])
        tp1_n   = sum(1 for r in rows if len(r) >= 17 and r[16] in ("TP1", "TP1 🔒"))
        tsl_n   = sum(1 for r in rows if len(r) >= 17 and "TSL" in r[16])
        sl_n    = sum(1 for r in rows if len(r) >= 17 and "SL" in r[16] and "TSL" not in r[16])
        wins    = tp2_n + tsl_n
        win_rate = wins / (wins + sl_n) * 100 if (wins + sl_n) else 0
        return {
            "total": total, "open": open_n,
            "tp2": tp2_n, "tp1": tp1_n, "tsl": tsl_n, "sl": sl_n,
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
                    and row_stat in ("OPEN", "TP1", "TP1 🔒", "")):
                return True
        return False
    except Exception:
        return False   # fail-open: kalau error, izinkan sinyal lewat



# ── Dashboard ─────────────────────────────────────────────────────────────────
DASHBOARD_TAB = "Dashboard"
_dash_ws = None


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
    open_n = tp1_n = tp2_n = sl_n = 0
    pnl_sum = pnl_tp = pnl_sl = 0.0
    long_tp2 = long_tp1 = long_sl = 0;  long_pnl = 0.0
    short_tp2 = short_tp1 = short_sl = 0; short_pnl = 0.0
    pnl_history = []

    for r in rows:
        if len(r) < 17:
            continue
        status = r[COL["Status"] - 1]
        dirn   = r[COL["Direction"] - 1].upper()
        raw_p  = r[COL["PnL %"] - 1].replace("%","").replace("+","").strip()
        try:
            pv = float(raw_p.replace(",", ".")) if raw_p and raw_p != "–" else 0.0
        except ValueError:
            pv = 0.0

        if "TP2" in status:
            tp2_n += 1; pnl_sum += pv; pnl_tp += pv; pnl_history.append(pv)
            if dirn == "LONG":  long_tp2  += 1; long_pnl  += pv
            else:               short_tp2 += 1; short_pnl += pv
        elif status == "TP1":
            tp1_n += 1; pnl_sum += pv; pnl_tp += pv; pnl_history.append(pv)
            if dirn == "LONG":  long_tp1  += 1; long_pnl  += pv
            else:               short_tp1 += 1; short_pnl += pv
        elif "SL" in status:
            sl_n  += 1; pnl_sum += pv; pnl_sl  += pv; pnl_history.append(pv)
            if dirn == "LONG":  long_sl   += 1; long_pnl  += pv
            else:               short_sl  += 1; short_pnl += pv
        else:
            open_n += 1

    closed   = tp2_n + tp1_n + sl_n
    win_rate = (tp2_n + tp1_n) / closed * 100 if closed > 0 else 0.0
    avg_win  = pnl_tp / (tp2_n + tp1_n)  if (tp2_n + tp1_n) > 0 else 0.0
    avg_loss = pnl_sl / sl_n              if sl_n > 0          else 0.0
    pnl_sign = "+" if pnl_sum >= 0 else ""

    l_closed = long_tp2 + long_tp1 + long_sl
    s_closed = short_tp2 + short_tp1 + short_sl
    l_wr = (long_tp2 + long_tp1) / l_closed * 100  if l_closed > 0 else 0.0
    s_wr = (short_tp2 + short_tp1) / s_closed * 100 if s_closed > 0 else 0.0

    wr_bar   = _spark_bar(win_rate, 100, 20)
    l_wr_bar = _spark_bar(l_wr, 100, 14)
    s_wr_bar = _spark_bar(s_wr, 100, 14)

    # ── Build grid 14 kolom (A–N) ─────────────────────────────────────
    # Kolom mapping: A B C D E F G H I J K L M N
    empty14 = [""] * 14

    def row14(*vals):
        r = list(vals)
        r += [""] * (14 - len(r))
        return r[:14]

    # Sinyal terbaru (15 rows)
    recent = rows[-15:][::-1]
    recent_grid = []
    for r in recent:
        if len(r) < 17:
            continue
        ts   = r[COL["Timestamp (UTC)"] - 1][5:16]
        sym  = r[COL["Symbol"]    - 1]
        dirn = r[COL["Direction"] - 1]
        tf   = r[COL["Timeframe"] - 1]
        ent  = r[COL["Entry"]     - 1]
        sl_v = r[COL["SL"]        - 1]
        tp2v = r[COL["TP2"]       - 1]
        rr   = r[COL["RR"]        - 1] if len(r) > COL["RR"]-1 else ""
        conv = r[COL["Conviction"]- 1]
        curr = r[COL["Current Price"] - 1]
        pnl  = r[COL["PnL %"]     - 1]
        stat = r[COL["Status"]    - 1]
        recent_grid.append(row14(ts, sym, dirn, tf, ent, sl_v, tp2v, rr, conv, curr, pnl, stat))

    # Assemble full grid
    grid = []

    # ROW 1-2: Header
    grid.append(row14("📊  VWAP SCREENER — PERFORMANCE DASHBOARD", "", "", "", "", "", "", "", "", "", "", "", f"🕐 {now_str}", ""))
    grid.append(empty14[:])

    # ROW 3: KPI labels
    grid.append(row14("TOTAL SINYAL","","","WIN RATE","","","TOTAL PnL","","","AVG PROFIT","","","AVG LOSS",""))

    # ROW 4: KPI values
    pnl_str = f"{pnl_sign}{pnl_sum:.2f}%"
    grid.append(row14(total,"","",f"{win_rate:.1f}%","","",pnl_str,"","",f"+{avg_win:.2f}%","","",f"{avg_loss:.2f}%",""))

    # ROW 5: KPI sub
    grid.append(row14(f"{open_n} open  |  {closed} closed","","",f"{tp2_n} TP2  ·  {tp1_n} TP1  ·  {sl_n} SL","","","closed trades","","","per trade menang","","","per trade kalah",""))

    grid.append(empty14[:])

    # ROW 7: Section headers LONG | SHORT
    grid.append(row14("🟢  LONG  —  Performance","","","","","","","🔴  SHORT  —  Performance","","","","","",""))

    # ROW 8: Win rate bars
    grid.append(row14(f"Win Rate  {l_wr:.1f}%","","","","","","",f"Win Rate  {s_wr:.1f}%","","","","","",""))
    grid.append(row14(l_wr_bar,"","","","","","",s_wr_bar,"","","","","",""))

    # ROW 10-13: Stats rows
    for label, lv, sv in [
        ("TP2 ✅",    long_tp2,  short_tp2),
        ("TP1",       long_tp1,  short_tp1),
        ("SL ❌",      long_sl,   short_sl),
        ("Net PnL",   f"{'+'if long_pnl>=0 else''}{long_pnl:.2f}%", f"{'+'if short_pnl>=0 else''}{short_pnl:.2f}%"),
    ]:
        grid.append(row14(label, lv, "", "", "", "", "", label, sv, "", "", "", "", ""))

    grid.append(empty14[:])

    # ROW 15-16: Overall win rate bar
    grid.append(row14("📈  OVERALL WIN RATE","","","","","","","","","","","","",""))
    grid.append(row14(f"{win_rate:.1f}%  {wr_bar}  ({tp2_n+tp1_n} win / {sl_n} loss dari {closed} closed)","","","","","","","","","","","","",""))

    grid.append(empty14[:])

    # ROW 18: Table header
    grid.append(row14("Timestamp","Symbol","Direction","TF","Entry","SL","TP2","RR","Conviction","Current Price","PnL %","Status"))

    # ROW 19+: Data rows
    grid.extend(recent_grid)

    # ── Write ──────────────────────────────────────────────────────────
    try:
        dws.clear()
        end_row = len(grid)
        dws.update(f"A1:N{end_row}", grid, value_input_option="RAW")
        time.sleep(0.5)

        # ── Formatting ─────────────────────────────────────────────────
        # Row 1: header bar
        _fmt(dws, "A1:N1", {
            "backgroundColor": _rgb(30, 40, 58),
            "textFormat": {"bold": True, "fontSize": 14,
                           "foregroundColor": {"red":1,"green":1,"blue":1}},
            "verticalAlignment": "MIDDLE",
        })

        # KPI label row (row 3)
        _fmt(dws, "A3:N3", {
            "backgroundColor": _rgb(240, 242, 246),
            "textFormat": {"bold": True, "fontSize": 9,
                           "foregroundColor": {"red":0.4,"green":0.4,"blue":0.5}},
            "horizontalAlignment": "CENTER",
        })

        # KPI value row (row 4)
        _fmt(dws, "A4:N4", {
            "textFormat": {"bold": True, "fontSize": 18},
            "horizontalAlignment": "CENTER",
            "verticalAlignment": "MIDDLE",
        })
        # Win rate color
        wr_color = _rgb(46,139,87) if win_rate >= 60 else (_rgb(200,150,30) if win_rate >= 40 else _rgb(180,50,50))
        _fmt(dws, "D4", {"textFormat": {"bold":True,"fontSize":18,"foregroundColor": wr_color}})

        # PnL color
        pnl_color = _rgb(46,139,87) if pnl_sum >= 0 else _rgb(180,50,50)
        _fmt(dws, "G4", {"textFormat": {"bold":True,"fontSize":18,"foregroundColor": pnl_color}})

        # KPI sub row (row 5)
        _fmt(dws, "A5:N5", {
            "textFormat": {"fontSize": 9, "foregroundColor": {"red":0.5,"green":0.5,"blue":0.6}},
            "horizontalAlignment": "CENTER",
        })

        # LONG section header (row 7)
        _fmt(dws, "A7:G7", {
            "backgroundColor": _rgb(232, 248, 238),
            "textFormat": {"bold": True, "fontSize": 11,
                           "foregroundColor": _rgb(30, 120, 70)},
        })
        # SHORT section header (row 7)
        _fmt(dws, "H7:N7", {
            "backgroundColor": _rgb(255, 235, 235),
            "textFormat": {"bold": True, "fontSize": 11,
                           "foregroundColor": _rgb(160, 40, 40)},
        })

        # Win rate label rows (8-9)
        _fmt(dws, "A8:G8", {"textFormat": {"bold": True, "fontSize": 11}})
        _fmt(dws, "H8:N8", {"textFormat": {"bold": True, "fontSize": 11}})
        _fmt(dws, "A9:G9", {"textFormat": {"fontFamily": "Courier New", "fontSize": 10,
                              "foregroundColor": _rgb(46,139,87)}})
        _fmt(dws, "H9:N9", {"textFormat": {"fontFamily": "Courier New", "fontSize": 10,
                              "foregroundColor": _rgb(160,40,40)}})

        # Win rate overall bar (row 16)
        _fmt(dws, "A16:N16", {
            "backgroundColor": _rgb(245, 248, 255),
            "textFormat": {"fontFamily": "Courier New", "fontSize": 11, "bold": True},
        })

        # Table header (row 18)
        table_row = 18
        _fmt(dws, f"A{table_row}:N{table_row}", {
            "backgroundColor": _rgb(50, 65, 90),
            "textFormat": {"bold": True, "fontSize": 10,
                           "foregroundColor": {"red":1,"green":1,"blue":1}},
            "horizontalAlignment": "CENTER",
        })

        # Data rows — color by status
        for i, r in enumerate(recent_grid):
            row_num = table_row + 1 + i
            stat = r[11] if len(r) > 11 else ""
            if "TP2" in stat:
                bg = _rgb(230, 248, 234)
            elif stat == "TP1":
                bg = _rgb(225, 240, 255)
            elif "SL" in stat:
                bg = _rgb(255, 232, 230)
            else:
                bg = _rgb(250, 250, 252)
            _fmt(dws, f"A{row_num}:N{row_num}", {"backgroundColor": bg, "fontSize": 10})
            # Bold PnL column (K = col 11)
            pnl_val_str = r[10] if len(r) > 10 else "0"
            try:
                pv = float(str(pnl_val_str).replace("%","").replace("+","").replace(",","."))
                pc = _rgb(30,130,60) if pv >= 0 else _rgb(180,50,50)
            except Exception:
                pc = _rgb(80,80,80)
            _fmt(dws, f"K{row_num}", {"textFormat": {"bold": True, "foregroundColor": pc}})

        # Column widths via spreadsheet API
        sh = _gc.open_by_key(SHEET_ID)
        sid = dws.id
        col_widths = [
            (0, 150),   # A: timestamp
            (1, 80),    # B: symbol
            (2, 70),    # C: direction
            (3, 45),    # D: TF
            (4, 90),    # E: entry
            (5, 90),    # F: SL
            (6, 90),    # G: TP2
            (7, 45),    # H: RR
            (8, 90),    # I: conviction
            (9, 100),   # J: current price
            (10, 75),   # K: PnL
            (11, 80),   # L: status
        ]
        requests = [{"updateDimensionProperties": {
            "range": {"sheetId": sid, "dimension": "COLUMNS",
                      "startIndex": ci, "endIndex": ci+1},
            "properties": {"pixelSize": px},
            "fields": "pixelSize"
        }} for ci, px in col_widths]
        # Row heights
        requests.append({"updateDimensionProperties": {
            "range": {"sheetId": sid, "dimension": "ROWS", "startIndex": 0, "endIndex": 1},
            "properties": {"pixelSize": 44}, "fields": "pixelSize"
        }})
        requests.append({"updateDimensionProperties": {
            "range": {"sheetId": sid, "dimension": "ROWS", "startIndex": 3, "endIndex": 4},
            "properties": {"pixelSize": 40}, "fields": "pixelSize"
        }})
        sh.batch_update({"requests": requests})

        # Freeze header row
        dws.freeze(rows=1)

        print(f"[dashboard] ✅ Updated — WR={win_rate:.1f}% PnL={pnl_sign}{pnl_sum:.2f}%  ({len(recent_grid)} rows)")
    except Exception as e:
        print(f"[dashboard] write error: {e}")
