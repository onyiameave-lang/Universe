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
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
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

    def _get_ctrader_research_series(self, symbol: str) -> Dict[str, Any]:
        """Read five years of daily bars from Oracle's authenticated cTrader account."""
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

            end = datetime.now(timezone.utc)
            start = end - timedelta(days=5 * 365 + 2)
            history = self._forge_ctrader_broker.historical_bars(
                symbol=symbol,
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
        return {"status": "complete", "series": series, "source": series.source,
                "bars": len(series.bars), "price_basis": "bid"}

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
        research_market = self._get_ctrader_research_series(symbol)
        if not isinstance(research_market, dict) or research_market.get("status") != "complete":
            detail = research_market.get("message") if isinstance(research_market, dict) else None
            return {"status": "blocked", "message": detail or "Oracle could not obtain five-year cTrader research history"}
        series = research_market.get("series")
        if series is None or not getattr(series, "bars", None):
            return {"status": "blocked", "message": "Oracle five-year research history is empty"}
        if str(getattr(series, "source", "")).lower().startswith("synthetic"):
            return {"status": "blocked", "message": "synthetic data cannot be evaluated by Forge"}

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
            "data_source": getattr(series, "source", "unknown"),
            "research_period": "5y",
            "research_interval": "1d",
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
            return ForgeClient(self.forge).request_oracle_improvement(request)
        except Exception as exc:
            log.warning("Forge Oracle improvement request failed for %s::%s: %s", symbol, regime, exc)
            return {"status": "error", "message": f"Forge request failed: {type(exc).__name__}"}

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
