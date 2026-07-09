"""
Oracle - Autonomous Quantitative Research Laboratory
====================================================
Constitutional Name: Oracle (formerly MarketOracle)
Mission: Validate trading intelligence scientifically and preserve reusable evidence.

Run:
  python main.py

Commands:
  signal <S>         adaptive-fused signal (uses evolved champion if any)
  evolve <S> [gens]  run a full scientific research cycle
  research <S> [gens] alias for evolve
  hypotheses <S>     generate regime-aware hypotheses
  champion <S>       show the regime-aware champion
  backtest <S>       walk-forward validation
  propose <S>        risk-gated trade plan
  learn <S> <+1/-1>  feed a realized outcome so fusion weights adapt
  portfolio | status | quit
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent
for p in (_REPO_ROOT, _REPO_ROOT.parent):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

CONFLICTING_MODULES = [
    "core", "agents", "intelligence", "memory", "research", "models", "training",
    "optimization", "communication", "infrastructure", "security", "api", "interfaces",
    "dashboard", "testing", "benchmarks", "simulations", "datasets", "documentation",
    "configs", "logs", "deployment", "plugins", "prompts", "tools", "constitutional",
    "execution", "registry"
]


def _unload_conflicting_modules():
    modules_to_delete = []
    for mod_name in CONFLICTING_MODULES:
        for m in list(sys.modules.keys()):
            if m == mod_name or m.startswith(mod_name + '.'):
                modules_to_delete.append(m)
    for m in modules_to_delete:
        if m in sys.modules:
            del sys.modules[m]


def _load(folder, rel, cls, **kw):
    root = _REPO_ROOT.parent / folder
    path_added = False
    try:
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
            path_added = True
        import importlib.util
        spec = importlib.util.spec_from_file_location(f"{folder}_{cls}", root / rel)
        if spec is None or spec.loader is None:
            return None
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        inst = getattr(m, cls)(**kw)
        inst.start()
        return inst
    except (ImportError, AttributeError, FileNotFoundError) as exc:
        logging.getLogger("oracle.main").warning("load %s failed: %s", folder, exc)
        return None
    finally:
        if path_added:
            sys.path.pop(0)


def format_genome_dna(genome_dict: dict) -> str:
    """
    Format genome DNA as a human-readable report.
    NEVER returns empty. Always shows full configuration.
    """
    if not genome_dict:
        return "  (no genome available)"

    lines = []
    genome_id = genome_dict.get("genome_id", "unknown")
    generation = genome_dict.get("generation", 0)
    parents = genome_dict.get("parents", [])
    fitness = genome_dict.get("fitness", 0)
    modules = genome_dict.get("modules", {})

    lines.append(f"  Genome ID:    {genome_id}")
    lines.append(f"  Generation:   {generation}")
    lines.append(f"  Parent:       {parents[0] if parents else 'None (seed)'}")
    lines.append(f"  Fitness:      {fitness:.4f}" if isinstance(fitness, float) else f"  Fitness:      {fitness}")
    lines.append("")

    # Trend
    trend = modules.get("trend", {})
    trend_type = trend.get("logic_type", "default")
    trend_params = trend.get("params", {})
    if trend_type == "sma_crossover":
        lines.append(f"  Trend:        SMA Crossover ({trend_params.get('fast', '?')}/{trend_params.get('slow', '?')})")
    elif trend_type == "ema_slope":
        lines.append(f"  Trend:        EMA Slope ({trend_params.get('period', '?')})")
    elif trend_type == "supertrend":
        lines.append(f"  Trend:        Supertrend ({trend_params.get('period', '?')}, mult={trend_params.get('multiplier', '?')})")
    elif trend_type == "donchian_trend":
        lines.append(f"  Trend:        Donchian ({trend_params.get('period', '?')})")
    elif trend_type == "ichimoku_cloud":
        lines.append(f"  Trend:        Ichimoku ({trend_params.get('tenkan', 9)}/{trend_params.get('kijun', 26)})")
    elif trend_type == "vwap_trend":
        lines.append(f"  Trend:        VWAP ({trend_params.get('period', '?')})")
    elif trend_type == "adx_trend":
        lines.append(f"  Trend:        ADX Trend ({trend_params.get('period', '?')}, thresh={trend_params.get('threshold', '?')})")
    elif trend_type == "hma_slope":
        lines.append(f"  Trend:        HMA Slope ({trend_params.get('period', '?')})")
    elif trend_type == "market_structure":
        lines.append(f"  Trend:        Market Structure ({trend_params.get('lookback', '?')})")
    else:
        lines.append(f"  Trend:        {trend_type} {trend_params}")

    # Momentum
    mom = modules.get("momentum", {})
    mom_type = mom.get("logic_type", "default")
    mom_params = mom.get("params", {})
    if mom_type == "rsi":
        lines.append(f"  Momentum:     RSI ({mom_params.get('period', 14)}, {mom_params.get('lower', 30)}-{mom_params.get('upper', 70)})")
    elif mom_type == "macd_hist":
        lines.append(f"  Momentum:     MACD ({mom_params.get('fast', 12)}/{mom_params.get('slow', 26)})")
    elif mom_type == "stochastic":
        lines.append(f"  Momentum:     Stochastic K({mom_params.get('k_period', 14)})")
    elif mom_type == "adx_strength":
        lines.append(f"  Momentum:     ADX Strength ({mom_params.get('period', 14)}, thresh={mom_params.get('threshold', 20)})")
    elif mom_type == "roc":
        lines.append(f"  Momentum:     ROC ({mom_params.get('period', 12)})")
    elif mom_type == "cci":
        lines.append(f"  Momentum:     CCI ({mom_params.get('period', 20)})")
    elif mom_type == "williams_r":
        lines.append(f"  Momentum:     Williams %R ({mom_params.get('period', 14)})")
    elif mom_type == "price_action":
        lines.append(f"  Momentum:     Price Action ({mom_params.get('lookback', 5)})")
    else:
        lines.append(f"  Momentum:     {mom_type} {mom_params}")

    # Volatility
    vol = modules.get("volatility", {})
    vol_type = vol.get("logic_type", "default")
    vol_params = vol.get("params", {})
    if vol_type == "default":
        lines.append(f"  Volatility:   None (all conditions)")
    elif vol_type == "atr_expansion":
        lines.append(f"  Volatility:   ATR Expansion (ratio={vol_params.get('expansion_ratio', '?')})")
    elif vol_type == "bollinger_width":
        lines.append(f"  Volatility:   Bollinger Width (<{vol_params.get('threshold', '?')})")
    else:
        lines.append(f"  Volatility:   {vol_type} {vol_params}")

    # Entry
    entry = modules.get("entry", {})
    threshold = entry.get("params", {}).get("base_threshold",
                entry.get("params", {}).get("threshold", "?"))
    lines.append(f"  Threshold:    {threshold}")

    # Exit
    exit_mod = modules.get("exit", {})
    exit_params = exit_mod.get("params", {})
    sl = exit_params.get("sl_mult", 2.0)  # matches ExitModule.get_stops() runtime default
    tp = exit_params.get("tp_mult", 3.0)  # matches ExitModule.get_stops() runtime default
    lines.append(f"  Exit:         SL={sl}x ATR, TP={tp}x ATR")
    if exit_params.get("trail_mult"):
        lines.append(f"  Trailing:     {exit_params['trail_mult']}x ATR")

    # Regime
    regime_mod = modules.get("market_regime", {})
    allowed = regime_mod.get("params", {}).get("allowed_regimes", ["all"])
    lines.append(f"  Regimes:      {', '.join(allowed)}")

    return "\n".join(lines)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    chronicle = _load("Chronicle", "agents/chronicle_agent.py", "ChronicleAgent",
                      storage_dir=str(_REPO_ROOT.parent / "Chronicle" / "memory" / "store"))
    _unload_conflicting_modules()

    atlas = _load("Atlas", "agents/research_agent.py", "AtlasAgent")
    _unload_conflicting_modules()

    sentinel = _load("Sentinel", "agents/sentinel_agent.py", "SentinelAgent", chronicle_client=chronicle)
    _unload_conflicting_modules()

    pulse = _load("Pulse", "agents/pulse_agent.py", "PulseAgent", chronicle_client=chronicle)
    _unload_conflicting_modules()

    from agents.oracle_agent import OracleAgent  # type: ignore

    agent = OracleAgent(chronicle_client=chronicle, sentinel_client=sentinel,
                        pulse_client=pulse, atlas_client=atlas)
    agent.start()

    print("=" * 64)
    print(" ORACLE - Autonomous Quantitative Research Laboratory")
    print(" Hypotheses. Research escalation. Regime champions. Preserved evidence.")
    print("=" * 64)
    print(f" Paper trading: {agent.risk.paper} | Sentinel:{sentinel is not None} "
          f"Pulse:{pulse is not None} Chronicle:{chronicle is not None}")
    print(" Commands: signal <S> | evolve <S> [gens] | research <S> [gens] | hypotheses <S> |")
    print("           champion <S> | backtest <S> | propose <S> | learn <S> <+1/-1> | portfolio | status | quit")

    last_streams = {}
    while True:
        try:
            line = input("\nOracle> ").strip()
            if not line:
                continue
            if line.lower() in ("quit", "exit", "q"):
                break
            parts = line.split()
            cmd = parts[0]

            if cmd == "signal" and len(parts) >= 2:
                print(json.dumps(agent.act("trade.signal", {"symbol": parts[1], "_sender": "user"}), indent=2))

            elif cmd in ("evolve", "research") and len(parts) >= 2:
                gens = int(parts[2]) if len(parts) > 2 else 5
                print(f"\n Running scientific research cycle for {parts[1]} over {gens} generations...\n")
                out = agent.act("strategy.evolve", {"symbol": parts[1], "generations": gens, "_sender": "user"})
                evo = out.get("evolution", {})
                experiment = out.get("experiment", {})
                context = out.get("context", {})

                # ---- Experiment Header ----
                print(f" {'=' * 56}")
                print(f" EXPERIMENT: {experiment.get('experiment_id', 'unknown')}")
                print(f" Symbol: {context.get('symbol')} | Regime: {context.get('regime')} | Bars: {context.get('bars')}")
                print(f" {'=' * 56}")

                # ---- Verdict (derived from promotion, never contradictory) ----
                promoted = evo.get("promoted_new_champion", False)
                evidence = experiment.get("evidence", {})
                if promoted:
                    print(f"\n ✅ CHAMPION PROMOTED")
                    print(f"    Verdict: accepted | Score: {evidence.get('score', 0):.4f}")
                else:
                    print(f"\n ❌ NOT PROMOTED")
                    print(f"    Verdict: {evidence.get('verdict', 'rejected')} | Score: {evidence.get('score', 0):.4f}")
                    # Show rejection reason from certification audit
                    cert = evo.get("certification_audit", experiment.get("certification_audit", {}))
                    if cert.get("rejection_reason"):
                        print(f"    Reason: {cert['rejection_reason']}")

                # ---- Performance ----
                is_return = evo.get("in_sample_return", 0)
                oos = evo.get("out_of_sample", {})
                print(f"\n Training Return:    {is_return or 0:.4f}")
                if oos:
                    print(f" Validation Return:  {oos.get('total_return', 0):.4f}")
                    print(f" Validation Trades:  {oos.get('trades', 0)}")
                    if oos.get("sharpe_proxy"):
                        print(f" Sharpe:             {oos.get('sharpe_proxy', 0):.3f}")
                    if oos.get("max_drawdown"):
                        print(f" Max Drawdown:       {oos.get('max_drawdown', 0):.4f}")
                    if oos.get("win_rate"):
                        print(f" Win Rate:           {oos.get('win_rate', 0):.3f}")

                # ---- Champion Comparison ----
                comparison = evo.get("champion_comparison") or out.get("champion_comparison")
                if comparison:
                    print(f"\n Champion Comparison:")
                    print(f"    {comparison.get('decision', 'unknown').upper()}: {comparison.get('reason', '')}")
                    if comparison.get("candidate_score") is not None:
                        print(f"    Candidate: {comparison['candidate_score']:.4f} vs Incumbent: {comparison.get('incumbent_score', 'none')}")

                # ---- Stagnation ----
                stag = out.get("stagnation", {})
                if stag.get("stagnant"):
                    print(f"\n Stagnation: {', '.join(stag.get('reasons', []))}")
                    print(f"    Atlas research: {(out.get('research') or {}).get('status', 'not triggered')}")

                # ---- Evolution History ----
                history = evo.get("history", [])
                if history:
                    print(f"\n Evolution ({len(history)} generations):")
                    for h in history[-3:]:  # Show last 3
                        print(f"    Gen {h.get('generation', '?')}: fitness={h.get('best_fitness', 0):.4f} "
                              f"trades={h.get('avg_trades', '?')} div={h.get('diversity', '?')} "
                              f"[{h.get('best_family', '?')}]")

                # ---- Reflection ----
                reflection = out.get("reflection", {})
                if reflection:
                    conclusion = reflection.get("conclusion", "")
                    if conclusion:
                        print(f"\n Research Conclusion:")
                        print(f"    {conclusion}")
                    else:
                        insights = reflection.get("insights", [])
                        if insights:
                            print(f"\n Reflection:")
                            for insight in insights[:4]:
                                print(f"    - {insight}")
                    directives = reflection.get("directives", [])
                    for d in directives[:2]:
                        print(f"    [NEXT] {d}")

                # ---- Hypotheses ----
                hyps = out.get("hypotheses", [])
                if hyps:
                    print(f"\n Hypotheses ({len(hyps)}):")
                    for h in hyps[:3]:
                        print(f"    [{h.get('family')}] {h.get('statement', '')[:80]}")

                # ---- BEST GENOME DNA (Issue 1: NEVER empty) ----
                best_genome = evo.get("best_genome", {})
                print(f"\n {'─' * 40}")
                print(f" BEST GENOME DNA:")
                print(f" {'─' * 40}")
                print(format_genome_dna(best_genome))

                # ---- DNA Report (from scientific_lab if available) ----
                dna_report = out.get("dna_report")
                if dna_report and dna_report.get("training_return"):
                    print(f"\n  IS Return:    {dna_report.get('training_return', 0):.4f}")
                    print(f"  OOS Return:   {dna_report.get('validation_return', 0):.4f}")

                # ---- Lineage ----
                if best_genome.get("parents"):
                    print(f"\n Lineage: {' → '.join(best_genome['parents'][-3:])} → {best_genome.get('genome_id')}")

                print(f"\n {'=' * 56}")

            elif cmd == "hypotheses" and len(parts) >= 2:
                print(json.dumps(agent.act("hypothesis.generate", {"symbol": parts[1], "_sender": "user"}), indent=2))

            elif cmd == "champion" and len(parts) >= 2:
                result = agent.act("strategy.champion", {"symbol": parts[1], "_sender": "user"})
                champ = result.get("champion")
                if champ:
                    print(f"\n Champion for {parts[1]}:")
                    print(format_genome_dna(champ.get("genome", {})))
                    oos = champ.get("out_of_sample", {})
                    if oos:
                        print(f"\n  OOS Return: {oos.get('total_return', 0):.4f}")
                        print(f"  Trades: {oos.get('trades', 0)}")
                        print(f"  Certified: {champ.get('certified_at', 'unknown')}")
                else:
                    print(f"\n No champion for {parts[1]} yet. Run: evolve {parts[1]}")

            elif cmd == "backtest" and len(parts) >= 2:
                print(json.dumps(agent.act("strategy.backtest", {"symbol": parts[1], "_sender": "user"}), indent=2))

            elif cmd == "propose" and len(parts) >= 2:
                out = agent.act("trade.propose", {"symbol": parts[1], "_sender": "user"})
                last_streams = out.get("_streams", {})
                out.pop("_streams", None)
                print(json.dumps(out, indent=2))

            elif cmd == "learn" and len(parts) >= 3:
                rd = 1 if parts[2].strip() in ("+1", "1", "up") else -1
                print(json.dumps(agent.act("fusion.learn",
                                           {"symbol": parts[1], "streams": last_streams, "realized_direction": rd,
                                            "_sender": "user"}), indent=2))

            elif cmd == "portfolio":
                print(json.dumps(agent.act("portfolio.status", {"_sender": "user"}), indent=2))

            elif cmd == "status":
                print(json.dumps(agent.get_status(), indent=2))

            else:
                print(" Unknown command. Try: evolve EURUSD 6")

        except KeyboardInterrupt:
            break
        except Exception as exc:
            print(f" Error: {exc}")
            import traceback
            traceback.print_exc()

    agent.stop()
    for peer in (pulse, sentinel, atlas, chronicle):
        if peer:
            try:
                peer.stop()
            except Exception:
                pass
    print("\n Oracle shutdown complete.")


if __name__ == "__main__":
    main()