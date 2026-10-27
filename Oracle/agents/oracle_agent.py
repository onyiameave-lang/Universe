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
import math
import sys
import time
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
    from core.market_data import MarketData  # type: ignore
except ImportError:
    from Oracle.core.market_data import MarketData  # type: ignore
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


class OracleAgent(BaseAgent):
    name = "oracle"
    repository = "Oracle"
    domain = "prediction"
    description = "Scientific validator of trading intelligence and regime-aware champions."
    capabilities = ["market.analyze", "trade.signal", "trade.propose", "trade.execute",
                    "strategy.evolve", "strategy.champion", "strategy.backtest",
                    "strategy.research", "research.cycle", "hypothesis.generate",
                    "fusion.learn", "risk.assess", "portfolio.status"]
    channels = ["ecosystem.trading", "ecosystem.prediction", "ecosystem.broadcast"]
    memory_namespace = "oracle_memory"
    security_level = "critical"
    mission = {"purpose": "Form, validate, reject, improve, and preserve trading hypotheses."}

    def __init__(self, chronicle_client=None, sentinel_client=None, pulse_client=None,
                 atlas_client=None, equity: float = 10000.0, **kw):
        super().__init__(chronicle_client=chronicle_client, atlas_client=atlas_client,
                         storage_dir=str(_REPO_ROOT / "memory"), **kw)
        self.data = MarketData()
        self.risk = RiskManager(equity=equity)
        self.backtester = Backtester(risk_per_trade=self.risk.risk_per_trade, starting_equity=equity)
        self.evolution = EvolutionLab(chronicle=chronicle_client, atlas=atlas_client,
                                     storage_dir=str(_REPO_ROOT / "memory"))
        self.lab = ScientificResearchLab(chronicle=chronicle_client, atlas=atlas_client,
                                        storage_dir=str(_REPO_ROOT / "memory"))
        self.fusion = AdaptiveFusion(storage_dir=str(_REPO_ROOT / "memory"), chronicle_client=chronicle_client)
        self.sentinel = sentinel_client
        self.pulse = pulse_client
        self.chronicle = chronicle_client
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
        stage_started = time.monotonic()
        news_stream = self._signal_fusion._news_signal(symbol)
        stage_elapsed = time.monotonic() - stage_started
        if stage_elapsed >= 3.0:
            log.warning("[%s] proposal evidence stage Sentinel/news took %.1fs", symbol, stage_elapsed)
        stage_started = time.monotonic()
        social_stream = self._signal_fusion._social_signal(symbol)
        stage_elapsed = time.monotonic() - stage_started
        if stage_elapsed >= 3.0:
            log.warning("[%s] proposal evidence stage Pulse/social took %.1fs", symbol, stage_elapsed)
        stage_started = time.monotonic()
        memory_stream = self._signal_fusion._memory_signal(symbol, regime)
        stage_elapsed = time.monotonic() - stage_started
        if stage_elapsed >= 3.0:
            log.warning("[%s] proposal evidence stage Chronicle/memory took %.1fs", symbol, stage_elapsed)

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
        started = time.monotonic()
        market = self.analyze_market(symbol)
        market_elapsed = time.monotonic() - started
        if market_elapsed >= 5.0:
            log.warning("[%s] proposal stage market_data took %.1fs", symbol, market_elapsed)
        if market["status"] != "complete":
            return market
        streams_started = time.monotonic()
        streams = self._streams(symbol, market["technicals"])
        streams_elapsed = time.monotonic() - streams_started
        if streams_elapsed >= 5.0:
            log.warning("[%s] proposal stage evidence_streams took %.1fs", symbol, streams_elapsed)
        fusion_started = time.monotonic()
        fused = self.fusion.fuse(symbol, streams)
        fusion_elapsed = time.monotonic() - fusion_started
        if fusion_elapsed >= 5.0:
            log.warning("[%s] proposal stage fusion took %.1fs", symbol, fusion_elapsed)
        total_elapsed = time.monotonic() - started
        if total_elapsed >= 10.0:
            log.warning("[%s] proposal signal complete after %.1fs", symbol, total_elapsed)
        atr = market["technicals"].get("atr_14") or (market["last"] * 0.01)
        return {"status": "complete", "symbol": symbol, "signal": fused,
                "last": market["last"], "regime": (market["technicals"].get("regime") or {}).get("regime"),
                "using_evolved_champion": streams["technical"].get("source") == "evolved_champion",
                "atr": atr,
                "_technicals": market["technicals"]}

    @staticmethod
    def _decision_context(signal_result: Dict[str, Any], *,
                          effective_confidence: Optional[float] = None,
                          risk: Optional[Dict[str, Any]] = None,
                          news_impact: Optional[str] = None,
                          social_risk: Optional[str] = None) -> Dict[str, Any]:
        """Build compact, non-text decision evidence for Forge's diagnosis loop.

        Raw news articles, social posts, memory summaries, and provider payloads
        are deliberately excluded. This is observability only; it does not
        change the signal or risk decision.
        """
        def finite_number(value: Any) -> Optional[float]:
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                number = float(value)
                return number if math.isfinite(number) else None
            return None

        fused = signal_result.get("signal") or {}
        technicals = signal_result.get("_technicals") or {}
        fused_context: Dict[str, Any] = {}
        call = fused.get("call")
        if call in {"buy", "sell", "hold"}:
            fused_context["call"] = call
        for field in ("direction", "confidence", "agreement", "entry_threshold"):
            number = finite_number(fused.get(field))
            if number is not None:
                fused_context[field] = number
        effective = finite_number(effective_confidence)
        if effective is not None:
            fused_context["effective_confidence"] = effective
        weights = fused.get("learned_weights")
        if isinstance(weights, dict):
            safe_weights = {
                str(name): number
                for name, value in weights.items()
                if name in {"technical", "news", "social", "memory"}
                and (number := finite_number(value)) is not None
            }
            if safe_weights:
                fused_context["learned_weights"] = safe_weights

        market: Dict[str, Any] = {}
        regime = signal_result.get("regime")
        if isinstance(regime, str) and regime.strip():
            market["regime"] = regime.strip()[:64]
        last_price = finite_number(signal_result.get("last"))
        atr = finite_number(signal_result.get("atr"))
        if last_price is not None:
            market["last_price"] = last_price
        if atr is not None:
            market["atr_14"] = atr
        regime_details = technicals.get("regime") if isinstance(technicals, dict) else None
        if isinstance(regime_details, dict):
            for source, target in (("slope_20", "slope_20"),
                                   ("volatility", "realized_volatility")):
                number = finite_number(regime_details.get(source))
                if number is not None:
                    market[target] = number
            if isinstance(regime_details.get("high_volatility"), bool):
                market["high_volatility"] = regime_details["high_volatility"]
        for field in ("rsi_14",):
            number = finite_number(technicals.get(field)) if isinstance(technicals, dict) else None
            if number is not None:
                market[field] = number

        streams: Dict[str, Any] = {}
        raw_streams = fused.get("streams")
        if isinstance(raw_streams, dict):
            for name in ("technical", "news", "social", "memory"):
                raw = raw_streams.get(name)
                if not isinstance(raw, dict):
                    continue
                item: Dict[str, Any] = {}
                for field in ("direction", "confidence"):
                    number = finite_number(raw.get(field))
                    if number is not None:
                        item[field] = number
                for field in ("source", "evidence_state"):
                    value = raw.get(field)
                    if isinstance(value, str) and value.strip():
                        item[field] = value.strip()[:64]
                for field in ("cross_source", "manipulation_warning"):
                    if isinstance(raw.get(field), bool):
                        item[field] = raw[field]
                count_field = {"news": "articles", "social": "posts", "memory": "memories"}.get(name)
                count = raw.get(count_field) if count_field else None
                if isinstance(count, int) and not isinstance(count, bool) and 0 <= count <= 1_000_000:
                    item["evidence_count"] = count
                if item:
                    streams[name] = item

        result: Dict[str, Any] = {
            "schema": "oracle.decision_context.v1",
            "fused": fused_context,
            "market": market,
            "streams": streams,
        }
        if isinstance(risk, dict):
            risk_context: Dict[str, Any] = {}
            mode = risk.get("mode")
            if mode in {"paper", "live"}:
                risk_context["mode"] = mode
            floor = finite_number(risk.get("confidence_floor"))
            if floor is not None:
                risk_context["confidence_floor"] = floor
            result["risk"] = risk_context
        for key, value in (("news_impact", news_impact), ("social_risk", social_risk)):
            if isinstance(value, str) and value in {"none", "low", "medium", "high", "critical", "unknown"}:
                result[key] = value
        return result

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
            propose_started = time.monotonic()
            sig = self.signal(symbol)
            signal_elapsed = time.monotonic() - propose_started
            if signal_elapsed >= 5.0:
                log.warning("[%s] trade.propose signal phase took %.1fs", symbol, signal_elapsed)
            if sig["status"] != "complete":
                return sig
            s = sig["signal"]
            if s["call"] == "hold":
                return {"status": "error", "message": "signal is hold", "signal": s,
                        "decision_context": self._decision_context(sig)}

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
            preserve_started = time.monotonic()
            self._preserve(symbol, sig)
            preserve_elapsed = time.monotonic() - preserve_started
            if preserve_elapsed >= 3.0:
                log.warning("[%s] trade.propose Chronicle preserve took %.1fs", symbol, preserve_elapsed)
            if not plan["approved"]:
                self.trade_journal.log_rejection(
                    symbol, direction, effective_confidence, plan.get("reasons", []))
                return {"status": "error", "message": "risk gate rejected", "risk": plan,
                        "news_impact": news.level, "news_reason": news.reason,
                        "social_risk": social.level, "social_reason": social.reason,
                        "signal": s, "regime": sig.get("regime"),
                        "strategy_id": ((streams.get("technical") or {}).get("genome")),
                        "decision_context": self._decision_context(
                            sig, effective_confidence=effective_confidence, risk=plan,
                            news_impact=news.level, social_risk=social.level)}
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
