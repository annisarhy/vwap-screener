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
        ws.format("A1:R1", {
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
        ws.format(f"A{row}:R{row}", {"backgroundColor": color})
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
                  tp1: float, tp2: float, current: float) -> str:
    """Quick check vs current price only (for very fresh signals)."""
    if direction == "LONG":
        if current >= tp2: return "TP2 ✅"
        if current >= tp1: return "TP1"
        if current <= sl:  return "SL ❌"
    else:
        if current <= tp2: return "TP2 ✅"
        if current <= tp1: return "TP1"
        if current >= sl:  return "SL ❌"
    return "OPEN"


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


def price_updater_loop() -> None:
    """
    Background thread: scan all OPEN/TP1 rows.
    • Sinyal < 2 candle lama  → cek harga live
    • Sinyal lebih lama       → replay candle historis (backtest)
    Runs every PRICE_REFRESH_SEC seconds.
    """
    if not _enabled:
        return
    print(f"[sheets] Price updater + backtest resolver running every {PRICE_REFRESH_SEC}s...")
    while True:
        try:
            _refresh_prices()
        except Exception as e:
            print(f"[sheets] price_updater_loop error: {e}")
        time.sleep(PRICE_REFRESH_SEC)


# Map timeframe string → candle duration in minutes
_TF_MINUTES = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
               "1h": 60, "2h": 120, "4h": 240, "1d": 1440}


def _refresh_prices() -> None:
    """Read all OPEN/TP1 rows, resolve status via candle replay or live price."""
    if _ws is None:
        return

    with _lock:
        all_rows = _ws.get_all_values()

    if len(all_rows) <= 1:
        return

    open_rows: list[tuple[int, list]] = []
    for i, row in enumerate(all_rows[1:], start=2):
        if len(row) < 17:
            continue
        status = row[COL["Status"] - 1] if len(row) >= COL["Status"] else ""
        if status in ("OPEN", "TP1", ""):
            open_rows.append((i, row))

    if not open_rows:
        return

    updates: list[dict] = []
    now_utc = datetime.now(timezone.utc)

    for row_num, row in open_rows:
        sym  = row[COL["Symbol"]    - 1]
        dirn = row[COL["Direction"] - 1]
        tf   = row[COL["Timeframe"] - 1] or "15m"

        try:
            entry = float(row[COL["Entry"] - 1])
            sl    = float(row[COL["SL"]    - 1])
            tp1   = float(row[COL["TP1"]   - 1])
            tp2   = float(row[COL["TP2"]   - 1])
        except (ValueError, IndexError):
            continue

        # Parse signal timestamp
        try:
            ts_str = row[COL["Timestamp (UTC)"] - 1]
            signal_ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except Exception:
            signal_ts = now_utc

        # Decide: use historical replay or live price
        tf_min  = _TF_MINUTES.get(tf, 15)
        age_min = (now_utc - signal_ts).total_seconds() / 60

        if age_min > tf_min * 2:
            # Sinyal sudah lebih dari 2 candle lama → replay historis
            status, exit_price = _backtest_resolve(
                sym, dirn, tf, entry, sl, tp1, tp2, signal_ts
            )
        else:
            # Sinyal masih fresh → cek harga live
            curr = _fetch_price(sym) or entry
            status    = _resolve_live(dirn, entry, sl, tp1, tp2, curr)
            exit_price = curr

        time.sleep(0.15)   # rate limit

        pnl = _calc_pnl(dirn, entry, exit_price)

        price_col  = _col_letter(COL["Current Price"])
        pnl_col    = _col_letter(COL["PnL %"])
        status_col = _col_letter(COL["Status"])

        updates.append({
            "range" : f"{price_col}{row_num}:{status_col}{row_num}",
            "values": [[exit_price, pnl, status]],
        })

        # Re-color closed rows
        if status in ("TP2 ✅", "SL ❌"):
            try:
                color = (
                    {"red": 0.72, "green": 0.93, "blue": 0.72}
                    if status == "TP2 ✅"
                    else {"red": 0.95, "green": 0.72, "blue": 0.72}
                )
                _ws.format(f"A{row_num}:R{row_num}", {"backgroundColor": color})
            except Exception:
                pass

    if updates:
        try:
            with _lock:
                _ws.batch_update(updates, value_input_option="USER_ENTERED")
            print(f"[sheets] Resolved {len(updates)} rows.")
        except Exception as e:
            print(f"[sheets] batch_update error: {e}")


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
        open_n  = sum(1 for r in rows if len(r) >= 17 and r[16] == "OPEN")
        tp2_n   = sum(1 for r in rows if len(r) >= 17 and "TP2" in r[16])
        tp1_n   = sum(1 for r in rows if len(r) >= 17 and r[16] == "TP1")
        sl_n    = sum(1 for r in rows if len(r) >= 17 and "SL" in r[16])
        win_rate = tp2_n / (tp2_n + sl_n) * 100 if (tp2_n + sl_n) else 0
        return {
            "total": total, "open": open_n,
            "tp2": tp2_n, "tp1": tp1_n, "sl": sl_n,
            "win_rate": win_rate,
        }
    except Exception:
        return {}
