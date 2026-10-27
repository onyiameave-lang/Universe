# Oracle MT5 Demo Trading Bridge

Run Oracle's evolved signal pipeline on a MetaTrader 5 **demo account** for all 41 champion symbols.

---

## What's in this package

| File | Purpose |
|------|---------|
| `mt5_demo_trader.py` | Main script — signal loop, order execution, kill switch |
| `mt5_config.py` | All configuration in one place (credentials, symbols, risk params) |
| `mt5_logger.py` | CSV trade log + session summary writer |
| `README_MT5.md` | This file |

**Drop all three files into:** `Universal_AI/Oracle/tools/`

---

## Prerequisites

### 1. MetaTrader 5 terminal (Windows only)
- Download from your broker or [metatrader5.com](https://www.metatrader5.com)
- Log into a **DEMO account** (the script blocks real-money accounts by default)
- Keep the terminal **running** while the script runs

### 2. Python packages
```cmd
pip install MetaTrader5 yfinance pandas numpy python-dotenv
```

### 3. Environment variables (optional)
Copy `.env.example` to `.env` in `Universal_AI/` and fill in:
```env
# MT5 credentials (optional if terminal is already logged in)
MT5_LOGIN=12345678
MT5_PASSWORD=your_password
MT5_SERVER=YourBroker-Demo

# Safety locks (defaults are maximally safe)
ORACLE_PAPER_TRADING=true      # true = signals only, no orders sent
ORACLE_ALLOW_LIVE=false        # extra lock for real-money accounts

# Risk limits
RL_RISK_PER_TRADE=0.01         # 1% of equity per trade
RL_MAX_POSITIONS=1
RL_MAX_DRAWDOWN_PCT=0.20
```

---

## Quick Start

```cmd
cd C:\Users\HP\Documents\Universe\Universal_AI

:: Paper mode (default) — signals computed, NO orders sent
python -m Oracle.tools.mt5_demo_trader

:: Enable order execution on demo account
python -m Oracle.tools.mt5_demo_trader --live

:: Run only 3 cycles then stop
python -m Oracle.tools.mt5_demo_trader --live --cycles 3

:: FX majors only, 5-minute cycles
python -m Oracle.tools.mt5_demo_trader --preset fx --interval 300 --live

:: Evolve champions first, then trade
python -m Oracle.tools.mt5_demo_trader --evolve-first --live
```

---

## All 41 Champion Symbols

| Category | Symbols |
|----------|---------|
| FX majors + crosses | EURUSD, GBPUSD, USDJPY, AUDUSD, USDCAD, USDCHF, NZDUSD, EURGBP, EURJPY, GBPJPY |
| Metals / commodities | XAUUSD, USOIL |
| Crypto | BTCUSD, ETHUSD, SOLUSD, XRPUSD, BNBUSD, ADAUSD |
| Global indices | SPX, NASDAQ, DJI, RUT, VIX, FTSE, DAX, CAC40, NIKKEI, HSI, SENSEX, ASX200 |
| US mega-cap stocks | AAPL, MSFT, NVDA, GOOGL, AMZN, META, TSLA, BRKB, LLY, V, JPM |

### Symbol presets (--preset)
```cmd
--preset all      # all 41 symbols (default)
--preset fx       # 10 FX pairs
--preset metals   # XAUUSD + USOIL
--preset crypto   # 6 crypto pairs
--preset indices  # 12 global indices
--preset stocks   # 11 US mega-caps
```

---

## CLI Reference

```
python -m Oracle.tools.mt5_demo_trader [OPTIONS]

Options:
  --symbols SYM [SYM ...]   specific symbols to trade
  --preset NAME             symbol preset: all|fx|metals|crypto|indices|stocks
  --interval SEC            seconds between cycles (default: 900 = 15 min)
  --cycles N                stop after N cycles (default: run forever)
  --max-trades N            max trades per session (default: 20)
  --max-loss FRAC           kill-switch loss fraction (default: 0.05 = 5%)
  --live                    enable order execution (paper mode by default)
  --confirm-live            required for real-money accounts
  --evolve-first            run evolution cycle per symbol before trading
  --evolve-generations N    generations per symbol (default: 6)
  --verbose                 DEBUG logging
```

---

## How It Works

```
For each symbol every cycle:
  1. SIGNAL     Oracle fuses technicals + news (Sentinel) + social (Pulse)
                + memory (Chronicle) with evolved, walk-forward-validated
                champion strategy genomes.
  2. RISK GATE  RiskManager sizes the trade (ATR-based), checks confidence
                floor, max positions, drawdown limit. Rejects if any check fails.
  3. EXECUTE    MT5Broker places a market order with broker-side stop + target.
                Auto-detects filling mode (FOK/IOC/RETURN) per symbol.
                Clamps volume to broker's min/max/step constraints.
  4. LOG        Every outcome (filled/rejected/hold/error/timeout) written to CSV.
  5. KILL SWITCH  If session equity loss > --max-loss, close all + stop.
```

---

## Output Files

```
Oracle/memory/memory/logs/mt5_demo_trades.csv    — one row per signal (all outcomes)
Oracle/memory/memory/logs/mt5_demo_sessions.csv  — one row per session summary
```

### Trade log columns
`timestamp, session_id, cycle, symbol, broker_symbol, direction, confidence,
regime, size, price, stop, target, reward_risk, outcome, retcode, reason,
account_type, equity_before, note`

### Outcome values
| Outcome | Meaning |
|---------|---------|
| `filled` | Order placed and confirmed by MT5 |
| `paper` | Signal approved but MT5 not connected (paper mode) |
| `rejected` | Risk gate or broker rejected the order |
| `hold` | Oracle signal is HOLD (no trade) |
| `error` | Oracle or MT5 error |
| `timeout` | Oracle signal took too long (>60s) |
| `unmapped` | Symbol not found at broker |
| `blocked` | Paper mode or real-account lock active |

---

## Broker Symbol Mapping

Oracle uses canonical names (EURUSD, USOIL, SPX…). Your broker may use
different names (XTIUSD, US500…). The script auto-discovers mappings from
the broker's live symbol list.

For symbols it can't auto-map, add the broker's exact MT5 symbol name to
`MT5_BROKER_SYMBOL_MAP` in `.env`:
```env
MT5_BROKER_SYMBOL_MAP=USOIL:<your-MT5-symbol>,SPX:US500,NASDAQ:USTEC,DJI:US30
```

`BROKER_SYMBOL_MAP` is reserved for the cTrader adapter and is ignored by the
MT5 trader. MT5 also only uses its built-in MetaQuotes-Demo aliases when the
connected server is `MetaQuotes-Demo`; other servers use their discovered
symbols or `MT5_BROKER_SYMBOL_MAP`.

---

## Safety Features

| Feature | Default | Override |
|---------|---------|---------|
| Paper mode | ON | `--live` or `ORACLE_PAPER_TRADING=false` |
| Real-account block | ON | `ORACLE_ALLOW_LIVE=true` + `--confirm-live` |
| Kill switch | 5% session loss | `--max-loss 0.03` |
| Max trades | 20/session | `--max-trades 10` |
| Per-symbol timeout | 60s | `ORACLE_SYMBOL_TIMEOUT_SEC=30` |
| Confidence floor | 0.50 (paper) / 0.60 (live) | `ORACLE_CONFIDENCE_FLOOR=0.55` |
| Max positions | 1 | `RL_MAX_POSITIONS=3` |
| Risk per trade | 1% | `RL_RISK_PER_TRADE=0.005` |

---

## Troubleshooting

### "MT5 not connected"
- Ensure MetaTrader 5 terminal is running and logged in
- Check MT5_LOGIN / MT5_PASSWORD / MT5_SERVER in .env
- Try leaving credentials blank — MT5 uses the currently-logged-in account

### "UNMAPPED — no broker symbol found"
- Find the exact name in the connected MT5 terminal's symbol list and add it
  to `MT5_BROKER_SYMBOL_MAP` in `.env`; don't assume another broker's aliases.

### "MetaTrader5 package not installed"
```cmd
pip install MetaTrader5
```
Note: MetaTrader5 Python package is **Windows-only**.

### "REJECT conf=0.48 < floor=0.50"
- Oracle's confidence is below the risk gate floor — this is correct behaviour
- Lower the floor: `ORACLE_CONFIDENCE_FLOOR=0.40` in .env (not recommended for live)

### Signals are slow / timing out
- Reduce symbol list: `--preset fx` instead of all 41
- Increase timeout: `ORACLE_SYMBOL_TIMEOUT_SEC=120`
- Check Gemini API rate limits (429 errors in logs)

### "maximum recursion depth exceeded"
- Apply the Nexus fix: change `result.get("result", {})` → `result.get("result")`
  in `Nexus/main.py` line ~179 (this is a Nexus bug, not Oracle)

---

## Architecture

```
mt5_demo_trader.py
    │
    ├── mt5_config.py          ← all config (credentials, symbols, risk)
    ├── mt5_logger.py          ← CSV audit trail
    │
    ├── Oracle/execution/mt5_broker.py    ← MT5 order execution (v3)
    │       ├── auto-detect filling mode (FOK/IOC/RETURN)
    │       └── clamp volume to broker constraints
    │
    ├── Oracle/execution/live_trader.py   ← SymbolMapper (auto-discovery)
    │
    └── Oracle/agents/oracle_agent.py     ← signal pipeline
            ├── MarketData (yfinance)
            ├── EvolutionLab (champion genomes)
            ├── SignalFusion (technicals + news + social + memory)
            ├── AdaptiveFusion (learned weights per symbol)
            └── RiskManager (sizing, stops, kill switch)
```
