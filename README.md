# AI Ecosystem (Universal_AI)

## Constitutional Architecture v1.0.0

> "A civilization is only as intelligent as the agents that compose it."

## Repositories (all institutional-grade)

| Name | Domain | Former | Mission |
|---|---|---|---|
| **Chronicle** | memory | MemoryAI | Preserve, anticipate, reconcile, and evolve the ecosys... |
| **Oracle** | prediction | MarketOracle | Evolve strategies, fuse evidence adaptively, and trade... |
| **Nexus** | coordination | Universal AI | Route, orchestrate in parallel under SLAs, and learn h... |
| **Sentinel** | news | NewsIntel | Acquire, validate, cluster, and distribute credible ne... |
| **Pulse** | social | SocialIntel | Read authentic social sentiment, flag manipulation, de... |
| **Atlas** | research | Research AI | Investigate with multi-source rigor, corroborate, and ... |
| **Forge** | training | Training Engine | Train with rigor and continually adopt better methods ... |
| **Genesis** | creation | Agent Factory | Create, certify, and responsibly deploy new autonomous... |
| **Aegis** | governance | Auditor | Continuously govern, score risk, detect anomalies, and... |

## Quick Start
```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # add keys you have
python ecosystem.py            # boot the whole civilization
python -m pytest tests/
```

## Run a single repo
```bash
cd Atlas && python main.py
cd Oracle && python main.py
```

## Live trading (Oracle, MT5)
```bash
# paper-safe first!
cd Oracle && python execution/live_trader.py --symbols EURUSD --evolve-first
```

## Principles
1. Everything is an Agent.
2. Everything Communicates.
3. Memory First: retrieve before generating.
4. Research Before Assumption.
5. Everything Evolves.
6. Nothing Dies Without Leaving Knowledge.
7. Security by Design.
8. Scalability Without Redesign.

Every repository follows the Universal Repository Standard and inherits the constitutional BaseAgent (evidence-reasoning + learn-from-mistakes + memory).
