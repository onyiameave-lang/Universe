# Runtime storage layout

Persistent agent state belongs under that agent's `memory/` directory. In the
ecosystem runtime, Chronicle is the authoritative shared store at
`Chronicle/memory/store`; agent-local files are bounded fallbacks, outboxes, or
component-specific runtime state.

| Purpose | Location |
| --- | --- |
| Shared Chronicle records and indexes | `Chronicle/memory/store/` |
| Forge model registry and artifacts | `Forge/memory/models/` |
| Forge training request queue | `Forge/memory/training_requests/` |
| Forge cross-agent reuse registry | `Forge/memory/reuse/` |
| Forge experiments, drift, challenger and deployment state | `Forge/memory/{experiments,drift,challenger,deployments}/` |
| Forge authorization nonces | `Forge/memory/security/` |
| Oracle benchmark snapshots and histories | `Oracle/memory/benchmarks/` |
| Oracle market-data cache | `Oracle/memory/data_cache/` |
| Oracle generated run logs | `Oracle/memory/logs/` |
| Oracle strategy research data and experiment outputs | `Chronicle/memory/research/` |
| Aegis standalone audit and policy fallback | `Aegis/memory/security/` |
| Genesis created-agent registry and rollback versions | `Genesis/memory/registry/` |
| Chronicle versioned research datasets and reports | `Chronicle/memory/research/` |
| Pytest scratch files | system temporary directory; never commit |

Research datasets and reports live under Chronicle's memory root and retain
their fingerprints and review lifecycle. Legacy Forge model/request/reuse,
Oracle cache/benchmark, Chronicle research, Aegis security, and Genesis registry
files are copied into the new memory paths on first use; legacy sources are
retained during migration.
