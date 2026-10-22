"""
Oracle.agents.oracle_agent
=========================
Oracle (formerly MarketOracle): a SELF-EVOLVING financial intelligence desk,
on the constitutional BaseAgent. (Book I Article X, XIII; Book VI capital
sovereignty.)

Beyond institutional. Oracle no longer chooses from a fixed menu of hand-written
strategies. It:
 * EVOLVES its own strategies as genetic genomes (composable indicator rules),
   validated by walk-forward backtest + OUT-OF-SAMPLE certification (overfitting
   guard). Champions persist and are used for live signals.
 * FUSES technical + news (Sentinel) + social (Pulse) + memory (Chronicle) with
   weights that LEARN per-symbol from realized trade outcomes (adaptive_fusion).
 * RISK-GATES every trade (sizing, stops, drawdown kill-switch); paper by default.
 * PRESERVES evolved strategies + outcomes to Chronicle (auditable DNA).

Nothing bypasses the risk gate. Evolved strategies are human-readable rule sets,
not black boxes, so every decision remains explainable and constitutional.

FIX O-3 (2026-07-17): Wire SignalFusion as the stream builder.
  Root cause: signal_fusion.py was never imported. oracle_agent.py reimplemented
  _news(), _social(), _memory() inline as inferior copies of SignalFusion's
  methods (missing reasons, cross_source, articles fields). AdaptiveFusion.fuse()
  received impoverished stream dicts, reducing fusion quality.
  Fix: SignalFusion builds the streams (rich output with reasons/cross_source);
  AdaptiveFusion.fuse() consumes them with learned per-symbol weights.
  The two classes are COMPLEMENTARY, not duplicates:
    SignalFusion  = stream builder  (calls sentinel/pulse/chronicle, returns rich dicts)
    AdaptiveFusion = weight learner (fuses streams with learned weights, updates on outcomes)
"""
from __future__ import annotations

import logging
import json
import os
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CTRADER_RESEARCH_ALIASES_FILE = _REPO_ROOT / "config" / "ctrader_research_symbol_aliases.json"
_RESEARCH_TIMEFRAMES = {
    "W1": 7 * 24 * 60 * 60 * 1000,
    "D1": 24 * 60 * 60 * 1000,
    "H4": 4 * 60 * 60 * 1000,
    "H1": 60 * 60 * 1000,
    "M45": 45 * 60 * 1000,
    "M30": 30 * 60 * 1000,
    "M15": 15 * 60 * 1000,
}
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _ctrader_research_aliases() -> Dict[str, str]:
    """Read explicit canonical-to-broker mappings for research only."""
    try:
        document = json.loads(_CTRADER_RESEARCH_ALIASES_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log.warning("cTrader research symbol alias file is unavailable or invalid")
        return {}
    if not isinstance(document, dict) or document.get("schema") != "oracle.ctrader_research_symbol_aliases.v1":
        return {}
    aliases = document.get("aliases")
    if not isinstance(aliases, dict):
        return {}
    return {
        str(canonical).strip().upper(): str(broker).strip().upper()
        for canonical, broker in aliases.items()
        if str(canonical).strip() and str(broker).strip()
    }
_ECO_ROOT = Path(__file__).resolve().parents[2]
if str(_ECO_ROOT) not in sys.path:
    sys.path.insert(0, str(_ECO_ROOT))

# FIX: dual-import fallbacks — works whether Oracle/ is on sys.path (standalone)
# or Oracle is loaded as a sub-package via api.py ecosystem.
try:
    from core.market_data import MarketData, Series, Bar  # type: ignore
except ImportError:
    from Oracle.core.market_data import MarketData, Series, Bar  # type: ignore
try:
    from core.risk import RiskManager  # type: ignore
except ImportError:
    from Oracle.core.risk import RiskManager  # type: ignore
try:
    from core.backtester import Backtester  # type: ignore
except ImportError:
    from Oracle.core.backtester import Backtester  # type: ignore
try:
    from intelligence.technicals import analyze  # type: ignore
except ImportError:
    from Oracle.intelligence.technicals import analyze  # type: ignore
try:
    from intelligence.evolution import EvolutionLab  # type: ignore
except ImportError:
    from Oracle.intelligence.evolution import EvolutionLab  # type: ignore
try:
    from intelligence.strategy_genome import StrategyGenome  # type: ignore
except ImportError:
    from Oracle.intelligence.strategy_genome import StrategyGenome  # type: ignore
try:
    from intelligence.adaptive_fusion import AdaptiveFusion  # type: ignore
except ImportError:
    from Oracle.intelligence.adaptive_fusion import AdaptiveFusion  # type: ignore
try:
    from intelligence.scientific_lab import ScientificResearchLab  # type: ignore
except ImportError:
    from Oracle.intelligence.scientific_lab import ScientificResearchLab  # type: ignore
try:
    from intelligence.scientific_lab import StrategyPromotionAuthorization  # type: ignore
except ImportError:
    from Oracle.intelligence.scientific_lab import StrategyPromotionAuthorization  # type: ignore
# FIX O-3: import SignalFusion — the stream builder (was never imported before)
try:
    from intelligence.signal_fusion import SignalFusion  # type: ignore
except ImportError:
    from Oracle.intelligence.signal_fusion import SignalFusion  # type: ignore

# News Intelligence (Phase 1 roadmap item 2)
try:
    from intelligence.news_impact import NewsImpactClassifier  # type: ignore
except ImportError:
    from Oracle.intelligence.news_impact import NewsImpactClassifier  # type: ignore

# Social Risk — Pulse manipulation detection (pump/dump-style coordinated activity)
try:
    from intelligence.social_risk import SocialRiskClassifier  # type: ignore
except ImportError:
    from Oracle.intelligence.social_risk import SocialRiskClassifier  # type: ignore

# Champion Confidence (roadmap Phase 1 item 4) — reads the same live
# confidence store the Continuous Trade Manager's learning loop writes to.
try:
    from intelligence.trade_learning import ChampionConfidenceTracker  # type: ignore
except ImportError:
    from Oracle.intelligence.trade_learning import ChampionConfidenceTracker  # type: ignore

# Trade Journal (roadmap Phase 3 groundwork: Trade Explainability) —
# entry/hold/rejection reasons + outcomes, wired to Chronicle.
try:
    from intelligence.trade_journal import TradeJournal  # type: ignore
except ImportError:
    from Oracle.intelligence.trade_journal import TradeJournal  # type: ignore

try:
    from shared.agent import BaseAgent
    _HAS_SHARED = True
except Exception:
    _HAS_SHARED = False
    class BaseAgent:
        reasoning = None
        def __init__(self, **kw): self._started = False; self._handled = 0; self._failed = 0; self.llm = None
        def act(self, task, context=None): return self.execute(task, context or {})
        def get_status(self): return {"name": getattr(self, "name", "oracle")}
        def solve(self, *a, **k): return {"status": "error", "message": "no reasoning"}
        has_brain = False
        def on_start(self): ...
        def start(self): self._started = True; self.on_start()
        def stop(self): self._started = False

log = logging.getLogger("oracle")


def _strategy_promotion_verifier():
    """Load Oracle's independently scoped Aegis trust configuration."""
    raw_keys = os.environ.get("ORACLE_AEGIS_TRUSTED_ISSUERS_JSON")
    if not raw_keys:
        return None
    try:
        keys = json.loads(raw_keys)
        if not isinstance(keys, dict) or not keys:
            raise ValueError("ORACLE_AEGIS_TRUSTED_ISSUERS_JSON must be a non-empty object")
        from Forge.core.authorization import Ed25519AuthorizationVerifier
        return Ed25519AuthorizationVerifier(
            trusted_issuers=keys,
            audience="oracle.strategy.promotion",
            database_path=_REPO_ROOT / "security" / "authorization_nonces.sqlite3",
        )
    except Exception as exc:
        raise RuntimeError(
            f"Oracle strategy promotion authorization configuration is invalid: {type(exc).__name__}"
        ) from exc


class OracleAgent(BaseAgent):
    name = "oracle"
    repository = "Oracle"
    domain = "prediction"
    description = "Scientific validator of trading intelligence and regime-aware champions."
    capabilities = ["market.analyze", "trade.signal", "trade.propose", "trade.execute",
                    "strategy.evolve", "strategy.champion", "strategy.backtest",
                    "strategy.research", "research.cycle", "strategy.improvement.request",
                    "strategy.catalog.snapshot",
                    "broker.cost.observations", "market.data.drift.assess",
                    "market.data.window.fingerprint",
                    "hypothesis.generate",
                    "strategy.promotion.authorization_request", "strategy.promotion.authorize",
                    "fusion.learn", "risk.assess", "portfolio.status"]
    channels = ["ecosystem.trading", "ecosystem.prediction", "ecosystem.broadcast"]
    memory_namespace = "oracle_memory"
    security_level = "critical"
    mission = {"purpose": "Form, validate, reject, improve, and preserve trading hypotheses."}

    def __init__(self, chronicle_client=None, sentinel_client=None, pulse_client=None,
                 atlas_client=None, forge_client=None, equity: float = 10000.0,
                 strategy_promotion_verifier=None, **kw):
        strategy_promotion_verifier = (
            strategy_promotion_verifier or _strategy_promotion_verifier()
        )
        super().__init__(chronicle_client=chronicle_client, atlas_client=atlas_client,
                         storage_dir=str(_REPO_ROOT / "memory"), **kw)
        self.data = MarketData()
        self._forge_ctrader_broker = None
        self._forge_ctrader_history_lock = threading.Lock()
        self._research_bundle_cache: Dict[str, Any] = {}
        self.risk = RiskManager(equity=equity)
        self.backtester = Backtester(risk_per_trade=self.risk.risk_per_trade, starting_equity=equity)
        self.evolution = EvolutionLab(chronicle=chronicle_client, atlas=atlas_client,
                                     storage_dir=str(_REPO_ROOT / "memory"))
        self.lab = ScientificResearchLab(chronicle=chronicle_client, atlas=atlas_client,
                                        storage_dir=str(_REPO_ROOT / "memory"),
                                        promotion_verifier=strategy_promotion_verifier)
        self.fusion = AdaptiveFusion(storage_dir=str(_REPO_ROOT / "memory"), chronicle_client=chronicle_client)
        self.sentinel = sentinel_client
        self.pulse = pulse_client
        self.chronicle = chronicle_client
        self.forge = forge_client
        self.trade_journal = TradeJournal(chronicle_client=chronicle_client)
        # FIX O-3: instantiate SignalFusion with the same sub-agent clients.
        # SignalFusion builds rich stream dicts; AdaptiveFusion fuses them with
        # learned weights. They are complementary, not duplicates.
        self._signal_fusion = SignalFusion(
            sentinel=sentinel_client,
            pulse=pulse_client,
            chronicle=chronicle_client,
        )
        # Champion Confidence (roadmap Phase 1 item 4): reads the same
        # champion_confidence.json the Continuous Trade Manager's Demo Trade
        # Learning loop writes to (default path resolves to the same
        # Oracle/memory/ dir used above). Read-mostly here — this instance
        # only writes if something calls record_outcome() directly on it,
        # which nothing in oracle_agent.py currently does.
        self._champion_confidence = ChampionConfidenceTracker(chronicle_client=chronicle_client)

    def on_start(self) -> None:
        log.info("Oracle scientific research lab online. Paper: %s | champions: %s | Sentinel:%s Pulse:%s",
                 self.risk.paper, self.evolution.stats()["champion_keys"],
                 self.sentinel is not None, self.pulse is not None)

    # ---- evidence streams (SignalFusion builds; AdaptiveFusion weights) ----

    def _streams(self, symbol: str, technicals: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        """Build evidence streams for fusion.

        FIX O-3: Use SignalFusion to build the stream dicts. SignalFusion
        produces richer output than the old inline _news()/_social()/_memory()
        methods — it includes 'reasons', 'cross_source', 'articles' fields that
        AdaptiveFusion can use for better confidence calibration.

        Technical stream: still uses evolved champion if available (Oracle's
        own domain), then falls back to SignalFusion._technical_signal().
        """
        regime = (technicals.get("regime") or {}).get("regime", "ranging")
        champ = self.evolution.champion(symbol, regime)
        champion_source = "evolution_library"
        if not champ:
            lab_champion = self.lab.champion_info(symbol, regime)
            champ = StrategyGenome.from_dict(lab_champion["genome"]) if lab_champion and lab_champion.get("genome") else None
            champion_source = "scientific_library" if lab_champion else "evolution_library"

        if champ:
            md = self.data.get(symbol)
            if md["status"] == "complete":
                s = md["series"]
                vote = champ.vote(s)
                tech = {"direction": vote, "confidence": 0.75, "source": "evolved_champion",
                        "champion_source": champion_source, "regime": regime, "genome": champ.genome_id}
            else:
                tech = {"direction": 0.0, "confidence": 0.0}
        else:
            # FIX O-3: use SignalFusion._technical_signal() for richer output
            # (includes 'reasons' list explaining the technical call)
            tech = self._signal_fusion._technical_signal(technicals)

        # FIX O-3: use SignalFusion for news/social/memory streams.
        # These methods call sentinel.sentiment_for(), pulse.sentiment_for(),
        # chronicle.search() and return richer dicts than the old inline methods.
        news_stream   = self._signal_fusion._news_signal(symbol)
        social_stream = self._signal_fusion._social_signal(symbol)
        memory_stream = self._signal_fusion._memory_signal(symbol, regime)

        return {
            "technical": tech,
            "news":      news_stream,
            "social":    social_stream,
            "memory":    memory_stream,
        }

    # ---- market view + signal ----

    def analyze_market(self, symbol):
        md = self.data.get(symbol)
        if md["status"] != "complete":
            return {"status": "error", "message": md.get("message"), "symbol": symbol}
        return {"status": "complete", "symbol": symbol, "source": md["source"],
                "last": md["series"].last, "technicals": analyze(md["series"])}

    def signal(self, symbol):
        market = self.analyze_market(symbol)
        if market["status"] != "complete":
            return market
        streams = self._streams(symbol, market["technicals"])
        fused = self.fusion.fuse(symbol, streams)
        atr = market["technicals"].get("atr_14") or (market["last"] * 0.01)
        return {"status": "complete", "symbol": symbol, "signal": fused,
                "last": market["last"], "regime": (market["technicals"].get("regime") or {}).get("regime"),
                "using_evolved_champion": streams["technical"].get("source") == "evolved_champion",
                "atr": atr,
                "_technicals": market["technicals"]}

    def _ensure_ctrader_research_broker(self) -> Dict[str, Any]:
        """Connect the private read-only cTrader research client once."""
        try:
            from Oracle.execution.ctrader_broker import CTraderBroker  # type: ignore
        except ImportError:
            from execution.ctrader_broker import CTraderBroker  # type: ignore
        use_demo = os.getenv("CTRADER_USE_DEMO", "true").strip().lower() not in {"0", "false", "no"}
        with self._forge_ctrader_history_lock:
            if self._forge_ctrader_broker is None:
                broker = CTraderBroker(use_demo=use_demo)
                connection = broker.connect()
                if not isinstance(connection, dict) or not connection.get("connected"):
                    reason = connection.get("reason", "cTrader authentication failed") if isinstance(connection, dict) else "cTrader authentication failed"
                    return {"status": "error", "message": f"cTrader research connection failed: {reason}"}
                self._forge_ctrader_broker = broker
            return {"status": "complete", "broker": self._forge_ctrader_broker}

    def collect_ctrader_cost_observations(self, symbols: List[str]) -> Dict[str, Any]:
        """Build research-only cost profiles from recent broker tick/deal data."""
        import bisect
        import math

        connection = self._ensure_ctrader_research_broker()
        if connection.get("status") != "complete":
            return {"status": "blocked", "message": connection.get("message"), "observations": []}
        broker = connection["broker"]
        aliases = _ctrader_research_aliases()
        broker_catalog = set(broker.symbol_catalog())
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        window_hours = max(1, min(24, int(os.getenv("ORACLE_FORGE_COST_WINDOW_HOURS", "6"))))
        window_start_ms = now_ms - window_hours * 60 * 60 * 1000
        deal_history_days = max(7, min(3650, int(os.getenv("ORACLE_FORGE_DEAL_HISTORY_DAYS", "1827"))))
        deals_result = broker.historical_deals(
            now_ms - deal_history_days * 24 * 60 * 60 * 1000,
            now_ms, max_rows=10000, timeout=60.0)
        observations = []
        profiles = {}
        deal_status_ok = deals_result.get("status") == "complete"
        if not deal_status_ok:
            log.warning("cTrader cost profile deal sample unavailable: %s",
                        deals_result.get("message", deals_result.get("status")))
        all_deals = deals_result.get("deals", []) if isinstance(deals_result, dict) else []
        account_currency = str(getattr(broker.status, "currency", "")).upper()
        conversion_graph: Dict[str, Dict[str, float]] = {"USD": {"USD": 1.0}}

        def add_conversion(base: str, quote: str, rate: float) -> None:
            base, quote = str(base or "").upper(), str(quote or "").upper()
            if not base or not quote or base == quote or not math.isfinite(rate) or rate <= 0:
                return
            conversion_graph.setdefault(base, {})[quote] = rate
            conversion_graph.setdefault(quote, {})[base] = 1.0 / rate

        def conversion_to_usd(asset: str) -> Optional[float]:
            asset = str(asset or "").upper()
            if asset == "USD":
                return 1.0
            if not asset:
                return None
            pending = [(asset, 1.0)]
            visited = set()
            while pending:
                current, rate = pending.pop(0)
                if current == "USD":
                    return rate
                if current in visited:
                    continue
                visited.add(current)
                for next_asset, edge_rate in conversion_graph.get(current, {}).items():
                    if next_asset not in visited:
                        pending.append((next_asset, rate * edge_rate))
            return None

        for raw_symbol in symbols:
            symbol = str(raw_symbol).strip().upper()
            broker_symbol = aliases.get(symbol, symbol)
            try:
                if broker_symbol not in broker_catalog:
                    observations.append({"status": "blocked", "symbol": symbol,
                                         "broker_symbol": broker_symbol,
                                         "message": f"broker does not list mapped symbol {broker_symbol}"})
                    continue
                metadata = broker.symbol_metadata(broker_symbol)
                symbol_deals = [
                    d for d in all_deals
                    if d.get("symbol") == broker_symbol
                    and d.get("deal_status") in {2, 3}
                    and float(d.get("filled_volume_cents") or 0) > 0
                ]
                minimum_deals = max(1, int(os.getenv("ORACLE_FORGE_MINIMUM_COST_DEALS", "20")))
                if not deal_status_ok or len(symbol_deals) < minimum_deals:
                    if not deal_status_ok:
                        reason = "complete cTrader deal history is required"
                    else:
                        reason = (
                            f"only {len(symbol_deals)} actual filled deals; "
                            f"{minimum_deals} required before requesting quote data"
                        )
                    observations.append({
                        "symbol": symbol, "broker_symbol": broker_symbol,
                        "status": "blocked", "metadata": metadata,
                        "sample": {
                            "filled_deals": len(symbol_deals),
                            "slippage_deals_checked": 0,
                            "matched_slippage_deals": 0,
                            "matched_spreads": 0,
                            "bid_tick_status": "not_requested",
                            "ask_tick_status": "not_requested",
                        },
                        "cost_profile": None, "reasons": [reason],
                    })
                    continue
                chunk_minutes = max(1, min(60, int(os.getenv("ORACLE_FORGE_TICK_CHUNK_MINUTES", "15"))))
                chunk_ms = chunk_minutes * 60 * 1000
                bid_ticks, ask_ticks = [], []
                bid_statuses, ask_statuses = [], []
                cursor = window_start_ms
                while cursor < now_ms:
                    chunk_end = min(now_ms, cursor + chunk_ms)
                    bid_chunk = broker.historical_ticks(broker_symbol, "BID", cursor, chunk_end, timeout=60.0)
                    ask_chunk = broker.historical_ticks(broker_symbol, "ASK", cursor, chunk_end, timeout=60.0)
                    bid_statuses.append(bid_chunk.get("status"))
                    ask_statuses.append(ask_chunk.get("status"))
                    bid_ticks.extend(bid_chunk.get("ticks", []))
                    ask_ticks.extend(ask_chunk.get("ticks", []))
                    cursor = chunk_end
                bid_ticks = sorted({item["timestamp_ms"]: item for item in bid_ticks}.values(), key=lambda item: item["timestamp_ms"])
                ask_ticks = sorted({item["timestamp_ms"]: item for item in ask_ticks}.values(), key=lambda item: item["timestamp_ms"])
                bid_complete = bool(bid_statuses) and all(value == "complete" for value in bid_statuses)
                ask_complete = bool(ask_statuses) and all(value == "complete" for value in ask_statuses)
                bid_raw_ticks = [int(item["raw_tick"]) for item in bid_ticks if isinstance(item.get("raw_tick"), int)]
                ask_raw_ticks = [int(item["raw_tick"]) for item in ask_ticks if isinstance(item.get("raw_tick"), int)]
                ask_times = [item["timestamp_ms"] for item in ask_ticks]
                pip_position = metadata.get("pipPosition")
                paired_spreads = []
                paired_mids = []
                quote_tolerance_ms = max(100, int(os.getenv("ORACLE_FORGE_QUOTE_MATCH_TOLERANCE_MS", "1000")))
                for bid_tick in bid_ticks:
                    idx = bisect.bisect_left(ask_times, bid_tick["timestamp_ms"])
                    candidates = ask_ticks[max(0, idx - 1):min(len(ask_ticks), idx + 1)]
                    if not candidates:
                        continue
                    ask_tick = min(candidates, key=lambda item: abs(item["timestamp_ms"] - bid_tick["timestamp_ms"]))
                    if abs(ask_tick["timestamp_ms"] - bid_tick["timestamp_ms"]) > quote_tolerance_ms:
                        continue
                    mid = (float(bid_tick["price"]) + float(ask_tick["price"])) / 2.0
                    if mid > 0 and ask_tick["price"] >= bid_tick["price"]:
                        paired_spreads.append((ask_tick["price"] - bid_tick["price"]) / mid * 10000.0)
                        paired_mids.append(mid)

                base_asset = str(metadata.get("baseAssetName") or "").upper()
                quote_asset = str(metadata.get("quoteAssetName") or "").upper()
                current_mid = sorted(paired_mids)[len(paired_mids) // 2] if paired_mids else None
                if current_mid is not None:
                    add_conversion(base_asset, quote_asset, current_mid)

                commission_bps_samples, slippage_bps_samples = [], []
                maximum_slippage_deals = max(
                    minimum_deals,
                    min(500, int(os.getenv("ORACLE_FORGE_MAXIMUM_SLIPPAGE_DEALS", "100"))),
                )
                attempted_slippage_deals = 0
                fill_quote_scale_mismatches = 0
                ordered_deals = sorted(
                    symbol_deals,
                    key=lambda item: int(item.get("execution_timestamp_ms") or 0),
                    reverse=True,
                )
                for deal in ordered_deals:
                    if attempted_slippage_deals >= maximum_slippage_deals:
                        break
                    execution_price = deal.get("execution_price")
                    units = float(deal.get("filled_volume_cents") or 0) / 100.0
                    conversion = deal.get("base_to_usd_conversion_rate")
                    if (isinstance(execution_price, (int, float)) and execution_price > 0
                            and units > 0 and isinstance(conversion, (int, float)) and conversion > 0):
                        notional_usd = units * float(conversion)
                        money_digits = deal.get("money_digits")
                        commission = (abs(float(deal.get("commission_raw") or 0)) / (10 ** int(money_digits))
                                      if isinstance(money_digits, int) and 1 <= money_digits <= 12 else 0.0)
                        if notional_usd > 0 and commission > 0 and account_currency == "USD":
                            commission_bps_samples.append(commission / notional_usd * 10000.0)
                        # Compare the fill against the broker quote at execution
                        # time, not the deal's submission time or the unrelated
                        # recent quote window collected for spread measurement.
                        reference_timestamp = int(
                            deal.get("execution_timestamp_ms")
                            or deal.get("create_timestamp_ms")
                            or 0
                        )
                        trade_side = deal.get("trade_side")
                        if reference_timestamp and trade_side in {1, 2}:
                            attempted_slippage_deals += 1
                            quote_type = "ASK" if trade_side == 1 else "BID"
                            quote_result = broker.historical_ticks(
                                broker_symbol,
                                quote_type,
                                max(0, reference_timestamp - quote_tolerance_ms),
                                reference_timestamp + quote_tolerance_ms + 1,
                                timeout=15.0,
                            )
                            quote_candidates = quote_result.get("ticks", [])
                            if quote_result.get("status") == "complete" and quote_candidates:
                                quote = min(
                                    quote_candidates,
                                    key=lambda item: abs(item["timestamp_ms"] - reference_timestamp),
                                )
                                if (abs(quote["timestamp_ms"] - reference_timestamp)
                                        <= quote_tolerance_ms and quote["price"] > 0):
                                    lower_price = min(float(execution_price), float(quote["price"]))
                                    higher_price = max(float(execution_price), float(quote["price"]))
                                    # A fill and its execution-time quote should be on
                                    # the same price scale. A >10x difference indicates
                                    # malformed or misdecoded market data, not usable
                                    # slippage evidence. Fail closed instead of turning
                                    # it into a gigantic but apparently valid cost.
                                    if lower_price <= 0 or higher_price / lower_price > 10.0:
                                        fill_quote_scale_mismatches += 1
                                        continue
                                    adverse = (
                                        float(execution_price) - quote["price"]
                                        if trade_side == 1
                                        else quote["price"] - float(execution_price)
                                    )
                                    slippage_bps_samples.append(
                                        max(0.0, adverse / quote["price"] * 10000.0)
                                    )
                                    if len(slippage_bps_samples) >= minimum_deals:
                                        break

                minimum_quotes = max(20, int(os.getenv("ORACLE_FORGE_MINIMUM_COST_QUOTES", "100")))
                profile = None
                reasons = []
                commission_type = metadata.get("commissionType")
                precise_rate = metadata.get("preciseTradingCommissionRate")
                commission_base = None
                commission_source = "cTrader symbol commission schedule"
                quote_to_usd = conversion_to_usd(quote_asset)
                if not isinstance(precise_rate, (int, float)):
                    reasons.append("cTrader precise symbol commission rate is unavailable")
                elif commission_type == 1:  # USD per million USD notional
                    if account_currency != "USD":
                        reasons.append("USD-denominated commission schedule requires a USD account")
                    else:
                        usd_per_million = float(precise_rate) / 1e8
                        commission_base = usd_per_million / 1_000_000.0 * 10_000.0
                elif commission_type == 2:  # USD per lot
                    if account_currency != "USD":
                        reasons.append("USD-denominated commission schedule requires a USD account")
                    elif float(precise_rate) == 0:
                        commission_base = 0.0
                    else:
                        lot_size = metadata.get("lotSize")
                        if (current_mid is None or quote_to_usd is None
                                or not isinstance(lot_size, (int, float)) or lot_size <= 0):
                            reasons.append("cannot convert cTrader per-lot commission into USD notional bps")
                        else:
                            usd_per_lot = float(precise_rate) / 1e8
                            usd_notional_per_lot = (float(lot_size) / 100.0) * current_mid * quote_to_usd
                            if usd_notional_per_lot <= 0:
                                reasons.append("cTrader symbol metadata produced an invalid USD notional per lot")
                            else:
                                commission_base = usd_per_lot / usd_notional_per_lot * 10_000.0
                elif commission_type == 3:  # percentage of trading value
                    commission_base = (float(precise_rate) / 1e5) * 100.0
                else:
                    reasons.append(f"unsupported cTrader commission type: {commission_type}")
                if (commission_base is not None
                        and (not math.isfinite(commission_base)
                             or commission_base < 0 or commission_base > 10000)):
                    reasons.append("derived commission cost is outside supported [0, 10000] bps bounds")
                if fill_quote_scale_mismatches:
                    reasons.append(
                        f"{fill_quote_scale_mismatches} fill quotes differ from execution prices by more than 10x; "
                        "market price scale is invalid"
                    )
                if not bid_complete or not ask_complete:
                    reasons.append("complete, non-truncated bid and ask tick windows are required")
                if len(paired_spreads) < minimum_quotes:
                    reasons.append(f"only {len(paired_spreads)} matched bid/ask observations; {minimum_quotes} required")
                if not deal_status_ok:
                    reasons.append("complete cTrader deal history is required")
                minimum_commission = metadata.get("preciseMinCommission")
                if isinstance(minimum_commission, (int, float)) and minimum_commission > 0:
                    reasons.append("symbol minimum commission requires per-trade modeling, which the current bps backtester does not support")
                if len(slippage_bps_samples) < minimum_deals:
                    reasons.append(
                        f"only {len(slippage_bps_samples)} matched fill/slippage observations; {minimum_deals} required"
                    )

                def percentile(values, fraction):
                    ordered = sorted(float(value) for value in values)
                    return ordered[min(len(ordered) - 1, max(0, math.ceil(fraction * len(ordered)) - 1))]

                if not reasons:
                    spread_base = percentile(paired_spreads, 0.95)
                    spread_stress = percentile(paired_spreads, 0.99)
                    slippage_base = percentile(slippage_bps_samples, 0.95)
                    slippage_stress = percentile(slippage_bps_samples, 0.99)
                    if spread_stress < spread_base or slippage_stress < slippage_base:
                        reasons.append("stress percentile calculation was inconsistent")
                    else:
                        profile = {
                            "broker": "cTrader account metadata",
                            "account_type": str(getattr(broker.status, "account_type", "unknown")),
                            "broker_symbol": broker_symbol,
                            "alias_schema": "oracle.ctrader_research_symbol_aliases.v1",
                            "profile_version": f"ctrader-cost-v2-{now_ms}-{symbol.lower()}",
                            "profile_status": "api_observed_costs",
                            "promotion_eligible": False,
                            "price_basis": "bid",
                            "commission_bps": commission_base,
                            "slippage_bps": slippage_base,
                            "spread_bps": spread_base,
                            "stress_slippage_bps": slippage_stress,
                            "stress_spread_bps": spread_stress,
                            "commission_source": commission_source,
                            "slippage_status": "api_observed",
                            "slippage_method": "matched cTrader fills versus execution-time historical quotes; p95 base / p99 stress",
                            "method": "cTrader commission schedule and historical BID/ASK ticks; p95 base / p99 stress",
                            "evidence": {
                                "observed_at": datetime.now(timezone.utc).isoformat(),
                                "window_start_ms": window_start_ms, "window_end_ms": now_ms,
                                "window_hours": window_hours,
                                "matched_spread_quotes": len(paired_spreads),
                                "deal_history_records": len(all_deals),
                                "deal_history_days": deal_history_days,
                                "deal_history_request_count": deals_result.get("request_count"),
                                "commission_deals": len(commission_bps_samples),
                                "matched_slippage_deals": len(slippage_bps_samples),
                                "fill_quote_scale_mismatches": fill_quote_scale_mismatches,
                                "commission_type": metadata.get("commissionType"),
                                "precise_trading_commission_rate": metadata.get("preciseTradingCommissionRate"),
                                "minimum_commission": metadata.get("preciseMinCommission"),
                                "base_asset": base_asset, "quote_asset": quote_asset,
                                "quote_asset_to_usd_rate": quote_to_usd,
                                "median_mid_price": current_mid,
                                "bid_raw_tick_min": min(bid_raw_ticks) if bid_raw_ticks else None,
                                "bid_raw_tick_max": max(bid_raw_ticks) if bid_raw_ticks else None,
                                "ask_raw_tick_min": min(ask_raw_ticks) if ask_raw_ticks else None,
                                "ask_raw_tick_max": max(ask_raw_ticks) if ask_raw_ticks else None,
                                "deal_history_status": deals_result.get("status", "error"),
                                "pip_position": pip_position,
                            },
                        }
                        profiles[symbol] = profile
                observations.append({
                    "symbol": symbol,
                    "broker_symbol": broker_symbol,
                    "status": "complete" if profile else "blocked",
                    "metadata": metadata,
                    "sample": {"matched_spreads": len(paired_spreads),
                               "median_mid_price": current_mid,
                               "bid_raw_tick_min": min(bid_raw_ticks) if bid_raw_ticks else None,
                               "bid_raw_tick_max": max(bid_raw_ticks) if bid_raw_ticks else None,
                               "ask_raw_tick_min": min(ask_raw_ticks) if ask_raw_ticks else None,
                               "ask_raw_tick_max": max(ask_raw_ticks) if ask_raw_ticks else None,
                               "filled_deals": len(symbol_deals),
                               "slippage_deals_checked": attempted_slippage_deals,
                               "commission_observations": len(commission_bps_samples),
                               "matched_slippage_deals": len(slippage_bps_samples),
                               "fill_quote_scale_mismatches": fill_quote_scale_mismatches,
                               "bid_tick_status": "complete" if bid_complete else "incomplete",
                               "ask_tick_status": "complete" if ask_complete else "incomplete"},
                    "cost_profile": profile,
                    "reasons": reasons,
                })
            except Exception as exc:
                log.warning("cTrader cost observation failed for %s: %s", symbol, type(exc).__name__)
                observations.append({"status": "error", "symbol": symbol,
                                     "message": f"cost observation failed: {type(exc).__name__}"})
        unmatched = [item for item in observations
                     if item.get("status") == "blocked"
                     and str(item.get("message", "")).startswith("broker does not list mapped symbol ")]
        return {"status": "complete", "source": "ctrader_open_api",
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "count": len(observations), "observations": observations,
                "cost_profiles": profiles,
                "deal_history_status": deals_result.get("status", "error"),
                "deal_history_records": len(all_deals),
                "deal_history_days": deal_history_days,
                "deal_history_request_count": deals_result.get("request_count"),
                # Exact names are diagnostic evidence only. Oracle never maps a
                # canonical symbol to a broker instrument implicitly.
                "broker_symbol_catalog": broker.symbol_catalog() if unmatched else []}

    def _get_ctrader_research_series(self, symbol: str) -> Dict[str, Any]:
        """Read five years of daily bars from Oracle's authenticated cTrader account."""
        connection = self._ensure_ctrader_research_broker()
        if connection.get("status") != "complete":
            return connection
        broker = connection["broker"]
        broker_symbol = _ctrader_research_aliases().get(str(symbol).strip().upper(), str(symbol).strip().upper())

        with self._forge_ctrader_history_lock:
            end = datetime.now(timezone.utc)
            start = end - timedelta(days=5 * 365 + 2)
            history = broker.historical_bars(
                symbol=broker_symbol,
                from_timestamp_ms=int(start.timestamp() * 1000),
                to_timestamp_ms=int(end.timestamp() * 1000),
                period="D1",
                count=2000,
                timeout=60.0,
            )
        if not isinstance(history, dict) or history.get("status") != "complete":
            detail = history.get("message", "cTrader returned no historical bars") if isinstance(history, dict) else "cTrader returned an invalid history response"
            return {"status": "error", "message": detail}

        try:
            bars = [Bar(
                ts=str(item["timestamp"]), open=float(item["open"]),
                high=float(item["high"]), low=float(item["low"]),
                close=float(item["close"]), volume=float(item.get("volume", 0.0)),
            ) for item in history["bars"]]
            series = Series(symbol=symbol.upper(), bars=bars, source="ctrader_open_api_bid")
            invalid = self.data._series_validation_error(series)
            if invalid:
                return {"status": "error", "message": f"cTrader returned invalid OHLCV history: {invalid}"}
        except (KeyError, TypeError, ValueError) as exc:
            return {"status": "error", "message": f"invalid cTrader history response: {type(exc).__name__}: {exc}"}
        if len(series.bars) < 75:
            return {"status": "error", "message": f"cTrader returned only {len(series.bars)} daily bars; at least 75 are required"}
        # Keep the research input stable during six-hour runs by excluding the
        # currently forming UTC D1 candle from every downstream evaluation.
        today_utc = datetime.now(timezone.utc).date()
        series.bars = [
            bar for bar in series.bars
            if datetime.fromisoformat(str(bar.ts).replace("Z", "+00:00")).date() < today_utc
        ]
        if len(series.bars) < 75:
            return {"status": "error", "message": "fewer than 75 completed daily bars remain after excluding the current UTC day"}
        return {"status": "complete", "series": series, "source": series.source,
                "bars": len(series.bars), "price_basis": "bid",
                "broker_symbol": broker_symbol}

    def _get_ctrader_research_bundle(self, symbol: str) -> Dict[str, Any]:
        """Fetch five years of completed cTrader bars for the top-down stack.

        Trendbar requests are split adaptively: backend range limits and response
        caps vary by timeframe and account. A chunk that is rejected or truncated
        is bisected until complete; irreducible gaps block the bundle.
        """
        import hashlib

        key = str(symbol).strip().upper()
        cached = self._research_bundle_cache.get(key)
        if isinstance(cached, dict) and (datetime.now(timezone.utc) - cached["cached_at"]).total_seconds() < 20 * 60:
            return cached["value"]

        connection = self._ensure_ctrader_research_broker()
        if connection.get("status") != "complete":
            return connection
        broker = connection["broker"]
        broker_symbol = _ctrader_research_aliases().get(key, key)
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=5 * 365 + 2)
        start_ms, end_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
        collected: Dict[str, List[Dict[str, Any]]] = {}
        fingerprints: Dict[str, str] = {}
        coverage: Dict[str, Dict[str, Any]] = {}

        with self._forge_ctrader_history_lock:
            for period, interval_ms in _RESEARCH_TIMEFRAMES.items():
                # Limit initial windows both by expected bar count and broad
                # calendar duration. Failures/truncation trigger finer splits.
                chunk_ms = min(interval_ms * 1800, 365 * 24 * 60 * 60 * 1000)
                windows = []
                cursor = start_ms
                while cursor < end_ms:
                    boundary = min(end_ms, cursor + chunk_ms)
                    windows.append((cursor, boundary, 0))
                    cursor = boundary
                pieces: List[Dict[str, Any]] = []
                requests = 0
                while windows:
                    left, right, depth = windows.pop()
                    requests += 1
                    history = broker.historical_bars(
                        symbol=broker_symbol, from_timestamp_ms=left,
                        to_timestamp_ms=right, period=period, count=2000,
                        timeout=60.0,
                    )
                    split = (left + right) // 2
                    too_wide = not isinstance(history, dict) or history.get("status") != "complete"
                    truncated = (isinstance(history, dict) and history.get("status") == "complete"
                                 and bool(history.get("has_more")))
                    can_split = depth < 16 and right - left > interval_ms * 2 and split > left and split < right
                    detail = (str(history.get("message", "")) if isinstance(history, dict) else "")
                    lower_detail = detail.lower()
                    range_limited = any(marker in lower_detail for marker in (
                        "range", "too large", "maximum", "max distance", "timestamp constraint",
                        "period constraint", "historical request limit", "invalid request",
                    ))
                    if requests > 1000:
                        return {"status": "blocked", "symbol": key,
                                "message": f"{period} history exceeded the bounded request budget"}
                    if ((truncated or (too_wide and range_limited)) and can_split):
                        windows.extend(((left, split, depth + 1), (split, right, depth + 1)))
                        continue
                    if too_wide:
                        return {"status": "blocked", "symbol": key,
                                "message": f"{period} history unavailable: {detail or 'invalid cTrader trendbar response'}"}
                    if truncated:
                        return {"status": "blocked", "symbol": key,
                                "message": f"{period} history remains truncated at the minimum request window"}
                    pieces.extend(history.get("bars", []))

                by_timestamp = {item["timestamp"]: item for item in pieces}
                bars = [by_timestamp[timestamp] for timestamp in sorted(by_timestamp)]
                completed = []
                for item in bars:
                    try:
                        opened = datetime.fromisoformat(str(item["timestamp"]).replace("Z", "+00:00"))
                    except (TypeError, ValueError):
                        return {"status": "blocked", "symbol": key,
                                "message": f"{period} contains an invalid timestamp"}
                    if opened.tzinfo is None:
                        opened = opened.replace(tzinfo=timezone.utc)
                    if opened + timedelta(milliseconds=interval_ms) <= end:
                        completed.append(item)
                if not completed:
                    return {"status": "blocked", "symbol": key,
                            "message": f"{period} contains no completed bars"}
                first = datetime.fromisoformat(str(completed[0]["timestamp"]).replace("Z", "+00:00"))
                last = datetime.fromisoformat(str(completed[-1]["timestamp"]).replace("Z", "+00:00"))
                if first.tzinfo is None:
                    first = first.replace(tzinfo=timezone.utc)
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                if first > start + timedelta(milliseconds=interval_ms * 3):
                    return {"status": "blocked", "symbol": key,
                            "message": f"{period} does not provide the requested five-year history"}
                if last < end - timedelta(days=7):
                    return {"status": "blocked", "symbol": key,
                            "message": f"{period} history is stale; latest completed bar is {last.isoformat()}"}
                try:
                    series = Series(
                        symbol=key,
                        bars=[Bar(ts=str(item["timestamp"]), open=float(item["open"]),
                                  high=float(item["high"]), low=float(item["low"]),
                                  close=float(item["close"]), volume=float(item.get("volume", 0.0)))
                              for item in completed],
                        source="ctrader_open_api_bid",
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    return {"status": "blocked", "symbol": key,
                            "message": f"{period} OHLCV is invalid: {type(exc).__name__}"}
                invalid = self.data._series_validation_error(series)
                if invalid:
                    return {"status": "blocked", "symbol": key,
                            "message": f"{period} OHLCV validation failed: {invalid}"}
                payload = [{"timestamp": bar.ts, "open": bar.open, "high": bar.high,
                            "low": bar.low, "close": bar.close, "volume": bar.volume}
                           for bar in series.bars]
                encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode("utf-8")
                collected[period] = payload
                fingerprints[period] = hashlib.sha256(encoded).hexdigest()
                coverage[period] = {
                    "bars": len(payload), "first_completed_bar": series.bars[0].ts,
                    "last_completed_bar": series.bars[-1].ts,
                    "history_start": series.bars[0].ts,
                    "history_end": series.bars[-1].ts,
                    "request_count": requests,
                }

        combined = hashlib.sha256(json.dumps(
            {"symbol": key, "period_fingerprints": fingerprints,
             "price_basis": "bid", "schema": "oracle.topdown_ohlcv.v1"},
            sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")).hexdigest()
        value = {
            "status": "complete", "symbol": key, "broker_symbol": broker_symbol,
            "source": "ctrader_open_api_bid", "price_basis": "bid",
            "history_target": "5y", "schema": "oracle.topdown_ohlcv.v1",
            "timeframes": collected, "timeframe_fingerprints": fingerprints,
            "timeframe_coverage": coverage, "data_fingerprint": combined,
            "as_of": end.isoformat(),
        }
        self._research_bundle_cache.clear()
        self._research_bundle_cache[key] = {"cached_at": end, "value": value}
        return value

    def research_window_fingerprint(self, symbol: str) -> Dict[str, Any]:
        """Return a stable data-window fingerprint for scheduled deduplication."""
        market = self._get_ctrader_research_bundle(symbol)
        if market.get("status") != "complete":
            return {"status": "blocked", "symbol": symbol, "message": market.get("message")}
        daily = market["timeframes"]["D1"]
        return {"status": "complete", "symbol": symbol.upper(),
                "data_fingerprint": market["data_fingerprint"],
                "timeframe_fingerprints": market["timeframe_fingerprints"],
                "timeframe_coverage": market["timeframe_coverage"],
                "last_completed_bar": daily[-1]["timestamp"],
                "bars": {period: coverage["bars"]
                         for period, coverage in market["timeframe_coverage"].items()},
                "latest_close": float(daily[-1]["close"]), "price_basis": "bid",
                "history_target": market["history_target"], "broker_symbol": market["broker_symbol"]}

    def assess_market_data_drift(self, symbol: str) -> Dict[str, Any]:
        """Compare recent completed daily return/range distributions with history.

        This is a market-data distribution monitor, not a claim about strategy
        performance drift or news causality. It never changes an Oracle genome.
        """
        market = self._get_ctrader_research_series(symbol)
        if market.get("status") != "complete":
            return {"status": "blocked", "symbol": symbol, "message": market.get("message", "market history unavailable")}
        bars = market["series"].bars
        if len(bars) < 281:
            return {"status": "blocked", "symbol": symbol, "message": "daily data drift requires 250 baseline and 30 recent completed bars"}
        # cTrader D1 may include the still-forming UTC daily candle. Exclude it
        # so repeated six-hour runs do not treat changing intraday data as a
        # completed daily observation.
        today_utc = datetime.now(timezone.utc).date()
        bars = [bar for bar in bars if datetime.fromisoformat(str(bar.ts).replace("Z", "+00:00")).date() < today_utc]
        if len(bars) < 281:
            return {"status": "blocked", "symbol": symbol, "message": "fewer than 281 completed daily bars after excluding the current UTC day"}

        import hashlib
        import math
        import statistics

        baseline, recent = bars[-280:-30], bars[-30:]
        def features(items):
            result = []
            for previous, current in zip(items, items[1:]):
                ret = math.log(current.close / previous.close)
                normalized_range = (current.high - current.low) / previous.close
                result.append((ret, normalized_range))
            return result
        baseline_features = features(baseline)
        recent_features = features(recent)
        def psi(reference, observed, bins=10):
            ordered = sorted(reference)
            edges = [ordered[min(len(ordered) - 1, int(i * len(ordered) / bins))] for i in range(1, bins)]
            def proportions(values):
                counts = [0] * bins
                for value in values:
                    index = 0
                    while index < len(edges) and value > edges[index]:
                        index += 1
                    counts[index] += 1
                return [(count + 0.5) / (len(values) + bins * 0.5) for count in counts]
            expected, actual = proportions(reference), proportions(observed)
            return sum((a - e) * math.log(a / e) for e, a in zip(expected, actual))
        return_series = psi([row[0] for row in baseline_features], [row[0] for row in recent_features])
        range_psi = psi([row[1] for row in baseline_features], [row[1] for row in recent_features])
        recent_volatility = statistics.pstdev(row[0] for row in recent_features)
        baseline_volatility = statistics.pstdev(row[0] for row in baseline_features)
        fingerprint_data = "|".join(str(bar.ts) for bar in bars[-30:]).encode("utf-8")
        fingerprint = hashlib.sha256(fingerprint_data).hexdigest()
        maximum_psi = max(return_series, range_psi)
        level = "high" if maximum_psi >= 0.25 else "moderate" if maximum_psi >= 0.10 else "low"
        return {
            "status": "complete", "symbol": symbol.upper(),
            "monitor": "oracle_daily_market_data_distribution_v1",
            "classification": level,
            "features": {
                "daily_log_return_psi": return_series,
                "normalized_daily_range_psi": range_psi,
                "baseline_daily_return_volatility": baseline_volatility,
                "recent_daily_return_volatility": recent_volatility,
            },
            "sample": {"baseline_bars": len(baseline), "recent_bars": len(recent), "bar_interval": "D1", "completed_bars_only": True},
            "window_fingerprint": fingerprint,
            "interpretation": "Distribution shift indicator only; not strategy-performance drift, causal attribution, or a trading signal.",
        }

    def request_forge_improvement(self, symbol: str, regime: Optional[str] = None,
                                  reason: str = "Oracle performance review requested",
                                  commission_bps: Optional[float] = None,
                                  slippage_bps: Optional[float] = None,
                                  execution_costs: Optional[Dict[str, Any]] = None,
                                  minimum_executed_trades: Optional[int] = None,
                                  ablation_targets: Optional[List[str]] = None) -> Dict[str, Any]:
        """Send Forge a read-only incumbent snapshot for governed research."""
        if self.forge is None:
            return {"status": "blocked", "message": "Forge is not connected to Oracle"}
        research_market = self._get_ctrader_research_bundle(symbol)
        if not isinstance(research_market, dict) or research_market.get("status") != "complete":
            detail = research_market.get("message") if isinstance(research_market, dict) else None
            return {"status": "blocked", "message": detail or "Oracle could not obtain five-year cTrader history across all seven timeframes"}
        bars_by_timeframe = research_market.get("timeframes")
        if not isinstance(bars_by_timeframe, dict) or set(bars_by_timeframe) != set(_RESEARCH_TIMEFRAMES):
            return {"status": "blocked", "message": "Oracle five-year top-down dataset is incomplete"}
        try:
            daily_bars = bars_by_timeframe["D1"]
            series = Series(
                symbol=str(symbol).upper(),
                bars=[Bar(ts=item["timestamp"], open=float(item["open"]),
                          high=float(item["high"]), low=float(item["low"]),
                          close=float(item["close"]), volume=float(item.get("volume", 0.0)))
                      for item in daily_bars],
                source="ctrader_open_api_bid",
            )
        except (KeyError, TypeError, ValueError) as exc:
            return {"status": "blocked", "message": f"Oracle daily context is invalid: {type(exc).__name__}"}
        if not series.bars:
            return {"status": "blocked", "message": "Oracle five-year daily context is empty"}

        if not regime:
            regime = (analyze(series).get("regime") or {}).get("regime", "unknown")
        champion = self.evolution.champion(symbol, regime)
        if champion is None:
            lab_record = self.lab.champion_info(symbol, regime)
            genome_payload = lab_record.get("genome") if isinstance(lab_record, dict) else None
        else:
            genome_payload = champion.to_dict()
        if not isinstance(genome_payload, dict):
            return {"status": "blocked", "message": "Oracle has no active champion genome to challenge"}

        confidence = self._champion_confidence.get(symbol, regime)
        observed = int(confidence.get("wins", 0)) + int(confidence.get("losses", 0))
        try:
            if execution_costs is None:
                profiles = json.loads(os.getenv("ORACLE_FORGE_COST_PROFILES_JSON", "{}"))
                execution_costs = profiles.get(symbol.upper()) if isinstance(profiles, dict) else None
            if isinstance(execution_costs, dict):
                execution_costs = dict(execution_costs)
                if commission_bps is not None:
                    execution_costs["commission_bps"] = commission_bps
                if slippage_bps is not None:
                    execution_costs["slippage_bps"] = slippage_bps
                for key in ("commission_bps", "slippage_bps", "spread_bps",
                            "stress_slippage_bps", "stress_spread_bps"):
                    if key in execution_costs:
                        execution_costs[key] = float(execution_costs[key])
        except (TypeError, ValueError, json.JSONDecodeError):
            execution_costs = None
        if (isinstance(execution_costs, dict)
                and execution_costs.get("price_basis") != research_market.get("price_basis")):
            return {
                "status": "blocked",
                "message": (
                    "execution-cost profile price_basis does not match cTrader historical bars; "
                    "cTrader trendbars are bid-based and the active profile must declare bid"
                ),
            }
        if minimum_executed_trades is None:
            try:
                minimum_executed_trades = int(os.getenv("ORACLE_FORGE_MINIMUM_EXECUTED_TRADES", "100"))
            except ValueError:
                minimum_executed_trades = None

        request = {
            "symbol": symbol.upper(),
            "regime": regime,
            "reason": reason,
            "baseline_genome": genome_payload,
            "performance_metrics": {
                "observed_trades": observed,
                "wins": int(confidence.get("wins", 0)),
                "losses": int(confidence.get("losses", 0)),
                "observed_win_rate": (int(confidence.get("wins", 0)) / observed) if observed else None,
                "bayesian_live_confidence": confidence.get("live_confidence"),
                "current_drawdown_r": confidence.get("current_drawdown_r"),
                "as_of": confidence.get("last_updated"),
            },
            "bars": [{
                "timestamp": bar.ts,
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": bar.volume,
            } for bar in series.bars],
            "bars_by_timeframe": bars_by_timeframe,
            "timeframe_fingerprints": research_market["timeframe_fingerprints"],
            "timeframe_coverage": research_market["timeframe_coverage"],
            "data_fingerprint": research_market["data_fingerprint"],
            "data_as_of": research_market["as_of"],
            "history_target": "5y",
            "data_source": getattr(series, "source", "unknown"),
            "research_period": "5y",
            "research_timeframes": list(_RESEARCH_TIMEFRAMES),
            "execution_costs": execution_costs,
            "minimum_executed_trades": minimum_executed_trades,
            "ablation_targets": list(ablation_targets or []),
            "risk_policy": {
                "source": "Oracle RiskManager.max_drawdown",
                "max_drawdown_fraction": getattr(self.risk, "max_drawdown", None),
            },
        }
        try:
            from shared.forge_client import ForgeClient
            try:
                return ForgeClient(self.forge).request_oracle_improvement(request)
            finally:
                self._research_bundle_cache.pop(str(symbol).strip().upper(), None)
        except Exception as exc:
            log.warning("Forge Oracle improvement request failed for %s::%s: %s", symbol, regime, exc)
            return {"status": "error", "message": f"Forge request failed: {type(exc).__name__}"}

    def strategy_catalog_snapshot(self) -> Dict[str, Any]:
        """Export Oracle's saved strategy identities for Forge to persist in Chronicle."""
        try:
            from Oracle.intelligence.strategy_catalog import build_strategy_catalog_snapshot
        except ImportError:
            from intelligence.strategy_catalog import build_strategy_catalog_snapshot  # type: ignore
        return build_strategy_catalog_snapshot()

    # ---- BaseAgent contract ----

    def execute(self, task, context):
        ctx = context
        symbol = ctx.get("symbol") or ctx.get("query", "").upper()

        if task == "market.analyze":
            return self.analyze_market(symbol)
        if task == "trade.signal":
            out = self.signal(symbol)
            if out.get("status") == "complete":
                self._preserve(symbol, out)
                streams = (out.get("signal") or {}).get("streams", {})
                (streams.get("news") or {}).pop("_risk_articles", None)
                (streams.get("social") or {}).pop("_manipulation_flags", None)
            out.pop("_technicals", None)
            return out
        if task == "strategy.improvement.request":
            return self.request_forge_improvement(
                symbol=symbol,
                regime=ctx.get("regime"),
                reason=ctx.get("reason", "Oracle performance review requested"),
                commission_bps=ctx.get("commission_bps"),
                slippage_bps=ctx.get("slippage_bps"),
                execution_costs=ctx.get("execution_costs"),
                minimum_executed_trades=ctx.get("minimum_executed_trades"),
                ablation_targets=ctx.get("ablation_targets"),
            )
        if task == "strategy.catalog.snapshot":
            return self.strategy_catalog_snapshot()
        if task == "market.data.drift.assess":
            return self.assess_market_data_drift(symbol)
        if task == "market.data.window.fingerprint":
            return self.research_window_fingerprint(symbol)
        if task == "broker.cost.observations":
            symbols = ctx.get("symbols") or ([symbol] if symbol else [])
            if not isinstance(symbols, list) or any(not isinstance(item, str) for item in symbols):
                return {"status": "error", "message": "symbols must be a list of strings"}
            return self.collect_ctrader_cost_observations(symbols)
        if task == "strategy.promotion.authorization_request":
            try:
                experiment_id = ctx.get("experiment_id")
                claims = self.lab.promotion_authorization_claims(
                    experiment_id, ctx.get("authorized_by")
                )
                return {
                    "status": "pending_authorization",
                    "experiment_id": experiment_id,
                    "audience": "oracle.strategy.promotion",
                    "required_claims": claims,
                    "required_fields": [
                        "authorization_id", "challenge_id", "authorized_by", "decision",
                        "risk_category", "issued_at", "expires_at", "issuer",
                        "audience", "signature", "nonce", "claims",
                    ],
                    "authorization_configured": self.lab._promotion_verifier is not None,
                }
            except Exception as exc:
                return {"status": "error", "message": f"promotion authorization request failed: {type(exc).__name__}: {exc}"}
        if task == "strategy.promotion.authorize":
            try:
                authorization = StrategyPromotionAuthorization.model_validate(
                    ctx.get("authorization", {})
                )
                return self.lab.authorize_promotion(ctx.get("experiment_id"), authorization)
            except Exception as exc:
                return {"status": "error", "message": f"strategy promotion rejected: {type(exc).__name__}: {exc}"}
        if task == "strategy.evolve":
            md = self.data.get(symbol)
            series = md["series"] if md["status"] == "complete" else self.data.synthetic(symbol, n=160)
            return self.lab.run_scientific_cycle(
                series,
                lambda candidates: self.evolution.evolve(series, generations=ctx.get("generations", 5), planned_candidates=candidates),
            )
        if task in ("strategy.research", "research.cycle"):
            md = self.data.get(symbol)
            series = md["series"] if md["status"] == "complete" else self.data.synthetic(symbol, n=160)
            return self.lab.run_scientific_cycle(
                series,
                lambda candidates: self.evolution.evolve(series, generations=ctx.get("generations", 5), planned_candidates=candidates),
            )
        if task == "hypothesis.generate":
            md = self.data.get(symbol)
            series = md["series"] if md["status"] == "complete" else self.data.synthetic(symbol, n=160)
            context = self.lab.market_context(series)
            memory = self.lab.consult_memory(context["symbol"], context["regime"])
            return {"status": "complete", "context": {k: v for k, v in context.items() if k != "technicals"},
                    "hypotheses": self.lab.generate_hypotheses(context["symbol"], context["regime"], memory)}
        if task == "strategy.champion":
            regime = ctx.get("regime")
            if not regime:
                md = self.data.get(symbol)
                if md["status"] == "complete":
                    regime = self.lab.market_context(md["series"])["regime"]
            info = self.evolution.champion_info(symbol, regime) or self.lab.champion_info(symbol, regime)
            # Champion Confidence (roadmap Phase 1 item 4): live win/loss
            # record from real (demo/paper) trades, additive to the existing
            # static backtested `info` — doesn't touch or replace it.
            live = self._champion_confidence.get(symbol, regime or "ranging")
            live["status"] = self._champion_confidence.status(symbol, regime or "ranging")
            return {"status": "complete", "champion": info, "live_confidence": live} if info else \
                {"status": "complete", "champion": None, "live_confidence": live,
                 "note": "no regime-aware champion yet; run strategy.evolve"}
        if task == "strategy.backtest":
            md = self.data.get(symbol)
            series = md["series"] if md["status"] == "complete" else self.data.synthetic(symbol)
            regime = (analyze(series).get("regime") or {}).get("regime", "ranging")
            champ = self.evolution.champion(symbol, regime)
            if champ:
                def decide(c, h, l, **kwargs):
                    class _S:
                        pass
                    s = _S(); s.closes = c; s.highs = h; s.lows = l
                    return {"call": champ.call(s)}
            else:
                decide = lambda c, h, l, **kwargs: {"call": self._indicator_call(c, h, l)}
            return {"status": "complete", "backtest": self.backtester.run(series, decide),
                    "used_champion": champ is not None}
        if task == "trade.propose":
            sig = self.signal(symbol)
            if sig["status"] != "complete":
                return sig
            s = sig["signal"]
            if s["call"] == "hold":
                return {"status": "error", "message": "signal is hold", "signal": s}

            # News Intelligence (Phase 1 roadmap item 2): never ignore major
            # news. A high-impact event drives confidence to 0, which makes
            # risk.evaluate() below reject the trade outright — reusing the
            # existing confidence floor instead of a parallel "paused" path.
            # A medium-impact event dampens confidence/size but doesn't halt.
            streams = s.get("streams", {})
            news_stream = streams.get("news", {})
            social_stream = streams.get("social", {})
            news = NewsImpactClassifier().assess(
                news_stream.get("_risk_articles", []), symbol)
            # Social Risk: Pulse-detected coordinated pump/dump-style activity.
            # Compounds with news multiplicatively — each is an independent
            # reason to distrust the signal, so both firing should be more
            # conservative than either alone, not just take the worse of the two.
            social = SocialRiskClassifier().assess(
                social_stream.get("_manipulation_flags", []), symbol)
            # These are private decision inputs. Do not expose full provider
            # payloads in the public trade.propose response.
            news_stream.pop("_risk_articles", None)
            social_stream.pop("_manipulation_flags", None)
            effective_confidence = s["confidence"] * news.confidence_multiplier * social.confidence_multiplier
            if news.level != "none":
                log.info("[%s] news impact=%s conf %.3f -> %.3f: %s",
                         symbol, news.level, s["confidence"], effective_confidence, news.reason)
            if social.level != "none":
                log.info("[%s] social risk=%s: %s", symbol, social.level, social.reason)

            atr = sig["_technicals"].get("atr_14") or (sig["last"] * 0.01)
            direction = "long" if s["call"] == "buy" else "short"
            plan = self.risk.evaluate(symbol, direction, sig["last"], atr, effective_confidence)
            self._preserve(symbol, sig)
            if not plan["approved"]:
                self.trade_journal.log_rejection(
                    symbol, direction, effective_confidence, plan.get("reasons", []))
                return {"status": "error", "message": "risk gate rejected", "risk": plan,
                        "news_impact": news.level, "news_reason": news.reason,
                        "social_risk": social.level, "social_reason": social.reason}
            return {"status": "complete", "plan": plan, "signal": s,
                    "effective_confidence": effective_confidence,
                    "using_evolved_champion": sig["using_evolved_champion"],
                    "news_impact": news.level, "news_reason": news.reason,
                    "social_risk": social.level, "social_reason": social.reason,
                    "atr": atr,
                    "_streams": s["streams"]}
        if task == "trade.execute":
            plan = ctx.get("plan")
            return self.risk.open_position(plan) if plan else \
                {"status": "error", "message": "no plan; run trade.propose"}
        if task == "fusion.learn":
            trade_won = ctx.get("trade_won")
            if not isinstance(trade_won, bool):
                return {"status": "error",
                        "message": "fusion.learn requires the realized trade_won boolean"}
            return {"status": "complete", **self.fusion.learn_from_outcome(
                symbol, ctx.get("streams", {}), ctx.get("realized_direction", 0),
                trade_won=trade_won)}
        if task == "risk.assess":
            return {"status": "complete", "risk": self.risk.evaluate(
                symbol, ctx.get("direction", "long"), ctx.get("entry", 0),
                ctx.get("atr", 0), ctx.get("confidence", 0))}
        if task == "portfolio.status":
            return {"status": "complete", "portfolio": self.risk.status()}
        return {"status": "error", "message": f"Unknown task: {task}"}

    def _indicator_call(self, closes, highs, lows):
        from intelligence.technicals import analyze as _an
        class _S:
            pass
        s = _S(); s.closes = closes; s.highs = highs; s.lows = lows
        t = _an(s)
        if "error" in t:
            return "hold"
        d = self._signal_fusion._technical_signal(t)["direction"]
        return "buy" if d > 0.15 else "sell" if d < -0.15 else "hold"

    def _preserve(self, symbol, sig):
        if self.chronicle is None:
            return
        try:
            content = (f"Oracle {symbol} [{sig.get('regime')}]: {sig['signal']['call']} "
                       f"conf {sig['signal']['confidence']} "
                       f"({'evolved' if sig['using_evolved_champion'] else 'indicator'}).")
            tags = ["oracle", symbol, sig.get("regime", "?")]
            store_fn = getattr(self.chronicle, "store", None)
            if callable(store_fn):
                store_fn(content=content, memory_type="episodic", domain="trading",
                         tags=tags, source="oracle")
            elif hasattr(self.chronicle, "act"):
                self.chronicle.act("memory.store", {
                    "content": content,
                    "pillar": "episodic",
                    "domain": "trading",
                    "tags": tags,
                    "_sender": "oracle",
                })
        except Exception:
            log.debug("chronicle persist failed")

    def get_status(self):
        base = super().get_status() if _HAS_SHARED else {"name": self.name}
        base["portfolio"] = self.risk.status()
        base["evolution"] = self.evolution.stats()
        base["scientific_lab"] = self.lab.stats()
        base["adaptive_fusion"] = self.fusion.stats()
        # FIX O-3: expose SignalFusion weights for observability
        base["signal_fusion"] = {
            "sentinel_wired": self.sentinel is not None,
            "pulse_wired":    self.pulse is not None,
            "chronicle_wired": self.chronicle is not None,
        }
        return base

    def sentiment_for(self, symbol: str):
        signal = self.signal(symbol).get("signal", {})
        streams = signal.get("streams", {})
        (streams.get("news") or {}).pop("_risk_articles", None)
        (streams.get("social") or {}).pop("_manipulation_flags", None)
        return signal
