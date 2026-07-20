"""
Oracle/tools/mt5_config.py
==========================
Single source of truth for all MT5 demo-trading configuration.
Edit this file OR set the corresponding environment variables in your .env.

Drop this file into:  Universal_AI/Oracle/tools/mt5_config.py
"""
from __future__ import annotations

import os
from typing import Dict, List, Optional

# ─────────────────────────────────────────────────────────────────────────────
#  MT5 ACCOUNT CREDENTIALS
#  Set these in your .env file (preferred) or directly here.
#  Leave blank to let MT5 use the currently-logged-in terminal account.
# ─────────────────────────────────────────────────────────────────────────────
MT5_LOGIN:    Optional[int] = int(os.getenv("MT5_LOGIN", "0")) or None
MT5_PASSWORD: Optional[str] = os.getenv("MT5_PASSWORD") or None
MT5_SERVER:   Optional[str] = os.getenv("MT5_SERVER")  or None

# ─────────────────────────────────────────────────────────────────────────────
#  SAFETY LOCKS  (defaults are maximally safe)
# ─────────────────────────────────────────────────────────────────────────────
# True  → paper mode: signals are computed but NO orders are sent to MT5.
# False → orders ARE sent.  Requires MT5 to be connected to a DEMO account.
PAPER_TRADING: bool = os.getenv("ORACLE_PAPER_TRADING", "true").lower() != "false"

# Extra lock for real-money accounts.  Must be explicitly set to "true" to
# allow live trading.  Demo accounts are always allowed when PAPER_TRADING=false.
ALLOW_LIVE: bool = os.getenv("ORACLE_ALLOW_LIVE", "false").lower() == "true"

# ─────────────────────────────────────────────────────────────────────────────
#  TRADING LOOP PARAMETERS
# ─────────────────────────────────────────────────────────────────────────────
# Seconds between full symbol scans (default 15 min = 900 s).
INTERVAL_SEC: int = int(os.getenv("MT5_INTERVAL_SEC", "900"))

# Maximum number of trades to place in one session (safety cap).
MAX_TRADES_PER_SESSION: int = int(os.getenv("MT5_MAX_TRADES", "20"))

# Kill switch: close all positions and stop if session equity loss exceeds this
# fraction of starting equity (default 5%).
SESSION_MAX_LOSS_PCT: float = float(os.getenv("MT5_MAX_LOSS_PCT", "0.05"))

# Per-symbol Oracle signal timeout in seconds.  If oracle.act() takes longer
# than this, the symbol is skipped for this cycle.
SYMBOL_TIMEOUT_SEC: int = int(os.getenv("ORACLE_SYMBOL_TIMEOUT_SEC", "60"))

# Run evolve cycle for each symbol before the first trading cycle?
EVOLVE_FIRST: bool = os.getenv("MT5_EVOLVE_FIRST", "false").lower() == "true"
EVOLVE_GENERATIONS: int = int(os.getenv("MT5_EVOLVE_GENERATIONS", "6"))

# ─────────────────────────────────────────────────────────────────────────────
#  41 CHAMPION SYMBOLS  (from Oracle/tools/evolution_soak.py DEFAULT_SYMBOLS)
# ─────────────────────────────────────────────────────────────────────────────
ALL_41_SYMBOLS: List[str] = [
    # FX majors + crosses
    "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF", "NZDUSD",
    "EURGBP", "EURJPY", "GBPJPY",
    # metals / commodities
    "XAUUSD", "USOIL",
    # crypto
    "BTCUSD", "ETHUSD", "SOLUSD", "XRPUSD", "BNBUSD", "ADAUSD",
    # global indices
    "SPX", "NASDAQ", "DJI", "RUT", "VIX", "FTSE", "DAX", "CAC40",
    "NIKKEI", "HSI", "SENSEX", "ASX200",
    # US mega-cap stocks
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "BRKB",
    "LLY", "V", "JPM",
]

# Subset presets — pass --preset <name> on the CLI
SYMBOL_PRESETS: Dict[str, List[str]] = {
    "fx":      ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF",
                "NZDUSD", "EURGBP", "EURJPY", "GBPJPY"],
    "metals":  ["XAUUSD", "USOIL"],
    "crypto":  ["BTCUSD", "ETHUSD", "SOLUSD", "XRPUSD", "BNBUSD", "ADAUSD"],
    "indices": ["SPX", "NASDAQ", "DJI", "RUT", "VIX", "FTSE", "DAX", "CAC40",
                "NIKKEI", "HSI", "SENSEX", "ASX200"],
    "stocks":  ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA",
                "BRKB", "LLY", "V", "JPM"],
    "all":     ALL_41_SYMBOLS,
}

# ─────────────────────────────────────────────────────────────────────────────
#  BROKER SYMBOL MAP
#  Oracle canonical name → your broker's exact symbol name.
#  The live_trader.py SymbolMapper auto-discovers most mappings from the
#  broker's symbol list.  Add overrides here for anything it misses.
#
#  Common broker aliases:
#    USOIL  → XTIUSD (most brokers)  or  WTI  or  OILCash
#    SPX    → US500  or  SP500
#    NASDAQ → USTEC  or  NAS100
#    DJI    → US30   or  DJ30
#    FTSE   → UK100
#    DAX    → DE40   or  GER40
#    CAC40  → FRA40
#    NIKKEI → JPN225
#    HSI    → HK50
#    SENSEX → IN50
#    ASX200 → AUS200
#    BTCUSD → BTCUSDm (some brokers add 'm' suffix)
#
#  Format: "ORACLE_NAME": "BROKER_NAME"
#  Leave empty ({}) to rely entirely on auto-discovery.
# ─────────────────────────────────────────────────────────────────────────────
SYMBOL_MAP_OVERRIDES: Dict[str, str] = {
    # ── MetaQuotes-Demo broker overrides (pre-filled for the 13 unmapped symbols) ──
    # Crypto — MetaQuotes-Demo adds 'm' suffix to all crypto pairs
    "BTCUSD": "BTCUSDm",
    "ETHUSD": "ETHUSDm",
    "SOLUSD": "SOLUSDm",
    "XRPUSD": "XRPUSDm",
    "BNBUSD": "BNBUSDm",
    "ADAUSD": "ADAUSDm",
    # Indices — MetaQuotes-Demo uses these names
    "NASDAQ": "NAS100",    # NASDAQ → NAS100
    "FTSE":   "UK100",     # FTSE   → UK100
    "CAC40":  "FRA40",     # CAC40  → FRA40
    "NIKKEI": "JPN225",    # NIKKEI → JPN225
    "SENSEX": "IN50",      # SENSEX → IN50
    "ASX200": "AUS200",    # ASX200 → AUS200
    # Stocks — BRKB is listed as-is on MetaQuotes-Demo
    "BRKB":   "BRKB",
    # ── Other common broker aliases (uncomment if needed) ──
    # "USOIL":  "XTIUSD",   # most non-MetaQuotes brokers
    # "SPX":    "US500",
    # "DJI":    "US30",
    # "RUT":    "US2000",
    # "DAX":    "DE40",
    # "HSI":    "HK50",
}

# ─────────────────────────────────────────────────────────────────────────────
#  RISK PARAMETERS  (also readable from .env — see Oracle/core/risk.py)
# ─────────────────────────────────────────────────────────────────────────────
# Fraction of equity risked per trade (default 1%).
RISK_PER_TRADE: float = float(os.getenv("RL_RISK_PER_TRADE", "0.01"))

# Maximum concurrent open positions.
MAX_POSITIONS: int = int(os.getenv("RL_MAX_POSITIONS", "1"))

# Portfolio drawdown kill-switch (fraction of peak equity).
MAX_DRAWDOWN_PCT: float = float(os.getenv("RL_MAX_DRAWDOWN_PCT", "0.20"))

# Confidence floor: signals below this are rejected by the risk gate.
# Paper mode uses the lower floor; live mode uses the higher one.
CONFIDENCE_FLOOR_PAPER: float = float(os.getenv("ORACLE_CONFIDENCE_FLOOR", "0.50"))
CONFIDENCE_FLOOR_LIVE:  float = float(os.getenv("ORACLE_CONFIDENCE_FLOOR_LIVE", "0.60"))

# ─────────────────────────────────────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────────────────────────────────────
# CSV trade log path (relative to Oracle/ directory).
TRADE_LOG_PATH: str = "logs/mt5_demo_trades.csv"

# Session summary log path.
SESSION_LOG_PATH: str = "logs/mt5_demo_sessions.csv"
