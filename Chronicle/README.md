# Chronicle

*MemoryAI -> Chronicle* | **memory** | security: critical

## Mission
Preserve, anticipate, reconcile, and evolve the ecosystem's knowledge.

## Capabilities
- `memory.store`
- `memory.retrieve`
- `memory.answer`
- `contradiction.detect`
- `belief.revise`
- `provenance.trace`
- `strategy.improve`

## Run
```bash
python main.py
```

## Agent lesson synchronization
Agents persist new lessons in their local learning log and a durable Chronicle
outbox before returning to their work. A single background worker per agent
syncs pending lessons, retries failures with bounded exponential backoff, and
removes an item only after Chronicle confirms storage. Stable memory IDs make
retries safe if Chronicle stored a lesson but the local acknowledgement was
interrupted. Pending entries survive agent restarts; lessons recorded before
the outbox was introduced are not automatically backfilled.

Institutional-grade. Built on the constitutional BaseAgent (reasoning + learning + memory). See `constitutional/COMPLIANCE.md`.
