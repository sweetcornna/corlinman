# corlinman-memory-kernel

Unified memory kernel for corlinman — the canonical, scoped, bi-temporal
memory layer that co-habits `memory.sqlite` with the legacy
conversational store (all `mk_*` tables are additive; the legacy
`files`/`chunks` shapes are never altered).

Three pipelines behind one `MemoryKernel` facade:

- **WRITE** (hot path): `observe()` — one INSERT per completed turn into
  the `mk_observations` ingest queue. No LLM, no embedding, no identity
  resolution on the hot path.
- **READ**: `recall()` — FTS5/BM25 over `mk_items` filtered by
  `(tenant, scope_user, persona)` scope and bi-temporal validity
  (`valid_to_ms IS NULL`).
- **MAINTENANCE** (sleep-time, later waves): reconcile the observation
  queue into atomic `mk_items` facts, decay, trust, dream.

Rollout is gated by `CORLINMAN_MEMORY_KERNEL=off|shadow|on`
(default `shadow`: observations accumulate and recall runs for diff
telemetry only — nothing is injected into prompts).
