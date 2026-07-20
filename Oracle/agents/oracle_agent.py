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
import sys
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
        self.fusion = AdaptiveFusion(storage_dir=str(_REPO_ROOT / "memory"))
        self.sentinel = sentinel_client
        self.pulse = pulse_client
        self.chronicle = chronicle_client
        # FIX O-3: instantiate SignalFusion with the same sub-agent clients.
        # SignalFusion builds rich stream dicts; AdaptiveFusion fuses them with
        # learned weights. They are complementary, not duplicates.
        self._signal_fusion = SignalFusion(
            sentinel=sentinel_client,
            pulse=pulse_client,
            chronicle=chronicle_client,
        )

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
        return {"status": "complete", "symbol": symbol, "signal": fused,
                "last": market["last"], "regime": (market["technicals"].get("regime") or {}).get("regime"),
                "using_evolved_champion": streams["technical"].get("source") == "evolved_champion",
                "_technicals": market["technicals"]}

    # ---- BaseAgent contract ----

    def execute(self, task, context):
        ctx = context
        symbol = ctx.get("symbol") or ctx.get("query", "").upper()

        if task == "market.analyze":
            return self.analyze_market(symbol)
        if task == "trade.signal":
            out = self.signal(symbol); out.pop("_technicals", None); return out
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
            return {"status": "complete", "champion": info} if info else \
                {"status": "complete", "champion": None,
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
            atr = sig["_technicals"].get("atr_14") or (sig["last"] * 0.01)
            direction = "long" if s["call"] == "buy" else "short"
            plan = self.risk.evaluate(symbol, direction, sig["last"], atr, s["confidence"])
            self._preserve(symbol, sig)
            if not plan["approved"]:
                return {"status": "error", "message": "risk gate rejected", "risk": plan}
            return {"status": "complete", "plan": plan, "signal": s,
                    "using_evolved_champion": sig["using_evolved_champion"],
                    "_streams": s["streams"]}
        if task == "trade.execute":
            plan = ctx.get("plan")
            return self.risk.open_position(plan) if plan else \
                {"status": "error", "message": "no plan; run trade.propose"}
        if task == "fusion.learn":
            return {"status": "complete", **self.fusion.learn_from_outcome(
                symbol, ctx.get("streams", {}), ctx.get("realized_direction", 0))}
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
        return self.signal(symbol).get("signal", {})