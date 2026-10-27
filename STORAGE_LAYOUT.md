# Runtime storage layout

Persistent agent state belongs under that agent's `memory/` directory. In the
ecosystem runtime, Chronicle is the authoritative shared store at
`Chronicle/memory/store`; agent-local files are bounded fallbacks, outboxes, or
component-specific runtime state.

| Purpose | Location |
| --- | --- |
| Shared Chronicle records and indexes | `Chronicle/memory/store/` |
| Oracle benchmark snapshots and histories | `Oracle/memory/benchmarks/` |
| Oracle market-data cache | `Oracle/memory/data_cache/` |
| Aegis standalone audit and policy fallback | `Aegis/memory/security/` |
| Genesis created-agent registry and rollback versions | `Genesis/memory/registry/` |
| Pytest scratch files | system temporary directory; never commit |

The market-data cache is regenerable. Legacy Oracle cache and benchmark, Aegis
security, and Genesis registry files are copied into the new memory paths on
first use; legacy sources are retained during migration.
