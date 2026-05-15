# VWAP Weekly Screener  ·  Enhanced Edition

Crypto screener berbasis **Weekly-anchored VWAP** dengan entry di zona **FVG**, minimum **RR 1:2**, dan **auto-alert Telegram** setiap candle 15m.

---

## Apa yang baru

| Fitur | Sebelum | Sekarang |
|---|---|---|
| Entry filter | Hanya VWAP mid + RSI | VWAP mid + RSI **+ harus di FVG** |
| Risk:Reward | Tidak dihitung | **Minimum 1:2** (TP2 = entry ± 2× risk) |
| SL placement | Tidak ada | Di bawah/atas FVG dengan buffer 0.2% |
| Alert | Hanya summary | **Per sinyal langsung** (1 coin pun langsung kirim) |
| Interval | 60 menit | **15 menit** (setiap candle) |

---

## Signal Logic

### 🟢 LONG — semua kriteria harus terpenuhi:
1. Candle 15m `close` **di atas** VWAP Weekly mid-line
2. RSI < 60
3. Harga berada **di dalam atau baru bounce dari Bullish FVG**
4. SL = bawah FVG − 0.2% buffer
5. TP1 = entry + 1× risk  *(intermediate target)*
6. TP2 = entry + 2× risk  *(minimum RR 1:2)*

### 🔴 SHORT — semua kriteria harus terpenuhi:
1. Candle 15m `close` **di bawah** VWAP Weekly mid-line
2. RSI > 40
3. Harga berada **di dalam atau baru rejection dari Bearish FVG**
4. SL = atas FVG + 0.2% buffer
5. TP1 = entry − 1× risk
6. TP2 = entry − 2× risk  *(minimum RR 1:2)*

### 🔥 STRONG
- Long Strong: bounce dari FVG + RSI < 50
- Short Strong: rejection dari FVG + RSI > 55

### Conviction
| Level | Kriteria |
|---|---|
| 🟢 High | RR ≥ 3.0 **dan** RSI menunjukkan momentum kuat |
| 🟡 Medium | RR ≥ 2.0 **atau** RSI cukup mendukung |
| 🔴 Low | RR tepat 2.0, setup masih valid |

---

## Setup

### 1. Clone & configure
```bash
git clone https://github.com/annisarhy/vwap-screener
cd vwap-screener
cp .env.example .env
# Edit .env — isi TELEGRAM_BOT_TOKEN dan TELEGRAM_CHAT_ID
```

### 2. Local test dengan Docker
```bash
docker compose up --build
```

### 3. Deploy ke Railway
1. Push ke GitHub
2. Railway → New Project → Deploy from GitHub
3. Set environment variables:

| Variable | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Token dari @BotFather |
| `TELEGRAM_CHAT_ID` | Chat ID kamu |
| `SCREENER_INTERVAL` | `15` (setiap candle 15m) |
| `SCREENER_TIMEFRAME` | `15m` |
| `SCREENER_TOP_N` | `50` |
| `SCREENER_MIN_CONVICTION` | `1` |
| `SCREENER_TOP_DISPLAY` | `5` |

4. Start command: `python main.py`

---

## Telegram Commands

```
/run          → run screener sekarang (15m)
/run 1h       → run dengan timeframe lain
/status       → info run terakhir + backtest 7 hari
/summary      → ringkasan performa
/help         → daftar command
```

---

## Contoh output Telegram

### Alert individual (langsung per sinyal):
```
────────────────────────────────────
🟢🔥 LONG STRONG  —  INJ  •  15m
────────────────────────────────────
📍 Entry   : 22.4150
🛑 SL      : 21.9820   (-1.93%)
🎯 TP1     : 22.8480   (+1.93%)
🏆 TP2     : 23.2810   (+3.86%)  ← RR 1:2.0

📊 RSI : 44.2   |   Dist VWAP : +0.18%
🔲 FVG : 22.0000 – 22.1500  (bullish)
📈 RR  : 1 : 2.00   🟢 High
────────────────────────────────────
```

### Summary (setelah semua alert individual):
```
📊 VWAP WEEKLY SCREENER  •  15m  •  2026-05-14 08:00 UTC
🔍 Scanned: 50 coins  |  Signals: 3L  2S

🟢 LONG  —  FVG bounce + above VWAP weekly mid
Symbol    RSI    Dist    RR    Conviction
🟢🔥 INJ      44.2  +0.18%   2.0  🟢 High
🟢   SOL      51.0  +0.08%   2.3  🟡 Medium
```

---

## FVG — Fair Value Gap

**Bullish FVG** terbentuk ketika:
- `candle[i-2].high  <  candle[i].low`
- Ada gap antara dua candle — bullish imbalance
- Entry zona: price masuk ke gap = area support kuat

**Bearish FVG** terbentuk ketika:
- `candle[i-2].low  >  candle[i].high`
- Gap bearish imbalance
- Entry zona: price masuk ke gap = area resistance kuat

FVG yang sudah **mitigated** (harga melewati midpoint gap) akan diabaikan.

---

## Stack

- **Data**: Bybit + Gate + OKX Perpetuals via CCXT
- **Signal**: Weekly VWAP + FVG + RR filter
- **Notify**: Telegram Bot API (per-sinyal + summary)
- **Deploy**: Docker → Railway
