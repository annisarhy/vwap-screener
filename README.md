# VWAP Weekly Screener

Crypto screener berbasis **Weekly-anchored VWAP** dengan signal long/short pada candle 15m.

## Signal Logic

| Signal | Kriteria |
|--------|----------|
| 🟢 **LONG** | Candle 15m close **di atas** VWAP weekly mid-line + RSI < 60 |
| 🟢🔥 **LONG STRONG** | LONG + candle sebelumnya low menyentuh mid-line (bounce confirmed) |
| 🔴 **SHORT** | Candle 15m close **di bawah** VWAP weekly mid-line + RSI > 40 |
| 🔴🔥 **SHORT STRONG** | SHORT + candle sebelumnya high menyentuh mid-line (rejection confirmed) |

**VWAP Weekly** reset setiap Senin 00:00 UTC.
**Bands** = VWAP ± 1 StdDev dari (typical_price - VWAP).

## Setup

### 1. Clone & configure

```bash
git clone <your-repo>
cd vwap-screener
cp .env.example .env
# Edit .env dengan token Telegram kamu
```

### 2. Local test dengan Docker

```bash
docker compose up --build
```

### 3. Deploy ke Railway

1. Push ke GitHub
2. Railway → New Project → Deploy from GitHub
3. Set environment variables di dashboard Railway:

| Variable | Value |
|----------|-------|
| `TELEGRAM_BOT_TOKEN` | Token dari @BotFather |
| `TELEGRAM_CHAT_ID` | Chat ID kamu |
| `SCREENER_INTERVAL` | `60` (menit antar auto-run) |
| `SCREENER_TIMEFRAME` | `15m` |
| `SCREENER_TOP_N` | `50` |
| `SCREENER_MIN_CONVICTION` | `3` |
| `SCREENER_TOP_DISPLAY` | `8` |

4. Start command: `python main.py`

## Telegram Commands

```
/run          → run screener sekarang (15m)
/run 1h       → run dengan timeframe lain
/status       → info run terakhir
/help         → daftar command
```

## Contoh output Telegram

```
📊 VWAP WEEKLY SCREENER  •  15m  •  2026-05-14 08:00 UTC
🔍 Scanned: 50 coins  |  Signals: 5L  3S

🟢 LONG  —  close above VWAP weekly mid
Symbol      RSI   Dist    Conviction
🟢🔥 INJ        42  +0.15%  🟢 High
🟢   SOL        48  +0.08%  🟡 Low

🔴 SHORT  —  close below VWAP weekly mid
🔴🔥 BTC        68  -0.22%  🟢 High
```

## Stack

- **Data**: Bybit Perpetuals via CCXT (tidak diblock)
- **Signal**: Weekly VWAP + StdDev bands
- **Notify**: Telegram Bot API
- **Deploy**: Docker → Railway
