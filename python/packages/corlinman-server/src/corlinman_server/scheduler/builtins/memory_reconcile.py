"""``memory.reconcile`` builtin — the W5 sleep-time write pipeline.

Drains the kernel's ``mk_observations`` queue (raw completed turns,
scope-stamped at ingest) and turns it into curated, bi-temporal
``mk_items`` facts — entirely off the chat hot path:

1. Group pending observations by (scope_user, persona, session).
2. LLM salient-fact extraction via the agent-brain extractor (reused as
   a library), with a hermes-style "do NOT capture" anti-pattern
   preamble folded into the prompt.
3. PII redaction + risk classification (agent-brain stages).
4. mem0-style reconciliation against the scope's existing items:
   near-duplicate → NOOP; contradiction/update → bi-temporal invalidate
   + insert + ``refines`` edge; else → INSERT. Nothing is ever deleted.
5. Optional embedding stamp per new item (``app_state.memory_embed_fn``
   seam; the live wiring lands with the W6 affect axes).
6. Core-block rebuild per touched scope: the highest trust×importance
   preferences/facts render into the ``user_profile`` block the W3 read
   pipeline injects. Only rewritten when the bytes changed
   (prefix-cache discipline).
7. A JSON report per run under ``<data_dir>/reports/memory-curator/``.

``dry_run`` (the default) computes and reports every action without
writing anything and WITHOUT consuming the queue — hermes curator
discipline: a mutating run must be an explicit operator decision.

Behaviour matrix mirrors the sibling builtins: every missing dependency
returns an ``{"ok": False, "reason": ...}`` envelope instead of raising.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from corlinman_server.scheduler.builtins.registry import (
    BuiltinContext,
    register_builtin,
    resolve_data_dir,
)

_logger = logging.getLogger(
    "corlinman_server.scheduler.builtins.memory_reconcile"
)

MEMORY_RECONCILE_BUILTIN_NAME: str = "memory.reconcile"

#: Jaccard token-overlap thresholds for the heuristic reconciler.
#: >= DUP: the fact is already known — NOOP. >= UPDATE: same topic,
#: changed content — bi-temporal supersede. Below: novel — ADD.
_DUP_JACCARD = 0.9
_UPDATE_JACCARD = 0.5

#: agent-brain MemoryKind → mk_items.kind. CONFLICT candidates are
#: stored as low-trust facts rather than dropped — the trust loop (W7)
#: governs them from there.
_KIND_MAP = {
    "project_context": "fact",
    "user_preference": "preference",
    "agent_persona": "persona_self",
    "decision": "decision",
    "task_state": "task_state",
    "concept": "fact",
    "relationship": "relationship",
    "conflict": "fact",
}

#: In dry-run mode the queue is never drained, so a scheduled dry run
#: would re-extract the SAME observations (full LLM cost) on every fire.
#: A small sample keeps the report representative while bounding the
#: recurring token spend to a constant.
_DRY_RUN_SAMPLE = 20

#: Returned-report cap: the scheduler persists the builtin's return
#: value verbatim into run history (sibling builtins keep reports tiny).
#: The full action list still lands in the on-disk JSON report.
_RETURNED_ACTIONS_CAP = 20


# CJK-aware similarity shared with the trust loop (kernel textsim).
try:
    from corlinman_memory_kernel.textsim import jaccard as _jaccard
except Exception:  # pragma: no cover — partial install; builtin degrades

    def _jaccard(a: str, b: str) -> float:
        return 0.0


def _curator_config(app_state: Any) -> dict[str, Any]:
    cfg = {
        "enabled": False,
        "dry_run": True,
        "max_observations": 200,
        "min_confidence": 0.5,
        "core_block_items": 8,
    }
    raw = getattr(app_state, "memory_curator_config", None)
    if isinstance(raw, dict):
        for key in ("enabled", "dry_run"):
            value = raw.get(key, cfg[key])
            if isinstance(value, bool):
                cfg[key] = value
        for key in ("max_observations", "core_block_items"):
            value = raw.get(key, cfg[key])
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                cfg[key] = value
        value = raw.get("min_confidence", cfg["min_confidence"])
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and 0.0 <= float(value) <= 1.0
        ):
            cfg["min_confidence"] = float(value)
    return cfg


async def _rebuild_core_block(
    kernel: Any, scope: Any, *, max_items: int, dry_run: bool
) -> bool:
    """Render the scope's user_profile block from its best items.

    Returns True when a (non-dry) write happened. Content is compared
    against the current block so unchanged bytes never touch the DB.
    """
    items = await kernel.top_items_for_scope(scope, limit=max_items)
    if not items:
        return False
    content = "\n".join(f"- {item.text}" for item in items)
    existing = dict(await kernel.core_blocks(scope)).get("user_profile")
    if existing == content or dry_run:
        return False
    await kernel.set_core_block(scope, "user_profile", content)
    return True


async def _memory_reconcile_action(context: BuiltinContext) -> dict[str, Any]:
    app_state = context.app_state
    if app_state is None:
        return {"ok": False, "reason": "app_state_unavailable"}
    cfg = _curator_config(app_state)
    if not cfg["enabled"]:
        return {"ok": False, "reason": "disabled"}

    kernel = getattr(app_state, "memory_kernel", None)
    if kernel is None:
        return {"ok": False, "reason": "memory_kernel_unavailable"}
    runner_fn = getattr(app_state, "agent_runner_fn", None)
    if runner_fn is None:
        return {"ok": False, "reason": "agent_runner_unavailable"}

    try:
        from corlinman_agent_brain.config import CuratorConfig
        from corlinman_agent_brain.extractor import extract_candidates
        from corlinman_agent_brain.models import BundleMessage, SessionBundle
        from corlinman_agent_brain.risk_classifier import (
            classify_risk_batch,
            redact_sensitive,
        )
        from corlinman_memory_kernel import KernelScope
    except Exception as exc:  # noqa: BLE001 — partial install degrades
        return {"ok": False, "reason": f"deps_unavailable: {exc}"}

    brain_cfg = CuratorConfig(
        draft_min_confidence=cfg["min_confidence"],
        # Observations are single turns; even a 2-message bundle can
        # carry a durable fact ("my name is..."), so don't skip small.
        min_messages_for_curation=1,
    )

    async def _provider(*, prompt: str) -> str:
        # The do-NOT-capture exclusions live in the agent-brain
        # SYSTEM_PROMPT itself (rules 7-10) so every extractor consumer
        # gets them — not appended here where only this caller would.
        result = await runner_fn(prompt)
        if isinstance(result, dict) and result.get("ok"):
            return str(result.get("reply", ""))
        raise RuntimeError(
            f"extraction provider failed: {result!r}"
            if not isinstance(result, dict)
            else str(result.get("error", "unknown"))
        )

    report: dict[str, Any] = {
        "builtin": MEMORY_RECONCILE_BUILTIN_NAME,
        "dry_run": cfg["dry_run"],
        "observations": 0,
        "bundles": 0,
        "candidates": 0,
        "added": 0,
        "updated": 0,
        "noop": 0,
        "blocked": 0,
        "core_blocks_rebuilt": 0,
        "actions": [],
    }

    # Dry runs never drain the queue, so a scheduled dry run would
    # re-extract the same rows (full LLM cost) every fire — sample small.
    obs_limit = (
        min(cfg["max_observations"], _DRY_RUN_SAMPLE)
        if cfg["dry_run"]
        else cfg["max_observations"]
    )
    observations = await kernel.pending_observations(limit=obs_limit)
    report["observations"] = len(observations)
    if not observations:
        return {"ok": True, **report}

    # Group by scope+session so one extraction sees one conversation.
    groups: dict[tuple[str | None, str, str], list[Any]] = {}
    for obs in observations:
        groups.setdefault(
            (obs.scope_user_id, obs.persona_id, obs.session_key), []
        ).append(obs)

    touched_scopes: dict[tuple[str | None, str], Any] = {}
    processed_ids: list[str] = []

    for (scope_user, persona, session_key), group in groups.items():
        report["bundles"] += 1
        messages: list[BundleMessage] = []
        for i, obs in enumerate(group):
            messages.append(
                BundleMessage(
                    seq=i * 2, role="user", content=obs.user_text,
                    ts_ms=obs.ts_ms,
                )
            )
            messages.append(
                BundleMessage(
                    seq=i * 2 + 1, role="assistant", content=obs.reply_text,
                    ts_ms=obs.ts_ms,
                )
            )
        bundle = SessionBundle(
            session_id=session_key,
            tenant_id=group[0].tenant_id,
            user_id=scope_user or "",
            agent_id=persona,
            messages=messages,
            started_at_ms=group[0].ts_ms,
            ended_at_ms=group[-1].ts_ms,
        )
        try:
            candidates = await extract_candidates(
                bundle=bundle, config=brain_cfg, provider=_provider
            )
        except Exception as exc:  # noqa: BLE001 — one bad bundle ≠ dead run
            _logger.warning("memory.reconcile extract failed: %s", exc)
            continue
        classify_risk_batch(candidates, brain_cfg)
        report["candidates"] += len(candidates)

        scope = KernelScope(scope_user_id=scope_user, persona_id=persona)
        touched_scopes[(scope_user, persona)] = scope
        for cand in candidates:
            text = redact_sensitive(cand.summary, brain_cfg).strip()
            if not text:
                continue
            risk = str(cand.risk)
            if risk == "blocked":
                report["blocked"] += 1
                continue
            neighbors = await kernel.recall(scope, text, top_k=5)
            best, best_score = None, 0.0
            for n in neighbors:
                score = _jaccard(text, n.text)
                if score > best_score:
                    best, best_score = n, score
            if best is not None and best_score >= _DUP_JACCARD:
                action = {"op": "noop", "text": text, "dup_of": best.id}
                report["noop"] += 1
            elif best is not None and best_score >= _UPDATE_JACCARD:
                action = {"op": "update", "text": text, "supersedes": best.id}
                report["updated"] += 1
                if not cfg["dry_run"]:
                    await kernel.invalidate_item(
                        best.id, reason="superseded", by="memory.reconcile"
                    )
                    new_id = await kernel.add_item(
                        scope,
                        text=text,
                        kind=_KIND_MAP.get(str(cand.kind), "fact"),
                        source="reconcile",
                        source_ref=session_key,
                        risk=risk,
                        confidence=cand.confidence,
                        trust=0.5,
                        importance=min(0.9, 0.4 + cand.confidence / 2),
                    )
                    await kernel.add_edge(new_id, best.id, "refines")
                    await _maybe_embed(app_state, kernel, new_id, text)
            else:
                action = {"op": "add", "text": text}
                report["added"] += 1
                if not cfg["dry_run"]:
                    new_id = await kernel.add_item(
                        scope,
                        text=text,
                        kind=_KIND_MAP.get(str(cand.kind), "fact"),
                        source="reconcile",
                        source_ref=session_key,
                        risk=risk,
                        confidence=cand.confidence,
                        trust=0.5,
                        importance=min(0.9, 0.4 + cand.confidence / 2),
                    )
                    await _maybe_embed(app_state, kernel, new_id, text)
            report["actions"].append(action)
        processed_ids.extend(obs.id for obs in group if obs.id)

    if not cfg["dry_run"]:
        await kernel.mark_observations_processed(processed_ids)
        for scope in touched_scopes.values():
            rebuilt = await _rebuild_core_block(
                kernel,
                scope,
                max_items=cfg["core_block_items"],
                dry_run=False,
            )
            if rebuilt:
                report["core_blocks_rebuilt"] += 1

    data_dir = resolve_data_dir(context)
    if data_dir is not None:
        try:
            reports_dir = data_dir / "reports" / "memory-curator"
            reports_dir.mkdir(parents=True, exist_ok=True)
            # ms suffix + mode tag: a dry-run and the live run it gates
            # often land within one second and must not overwrite.
            mode = "dry" if cfg["dry_run"] else "live"
            stamp = "{}-{:03d}-{}".format(
                time.strftime("%Y%m%d-%H%M%S", time.gmtime()),
                int(time.time() * 1000) % 1000,
                mode,
            )
            (reports_dir / f"{stamp}.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:  # pragma: no cover — report is best-effort
            _logger.warning("memory.reconcile report write failed: %s", exc)

    # The on-disk report keeps the full action list; the RETURNED dict
    # goes verbatim into scheduler history, so cap it there.
    if len(report["actions"]) > _RETURNED_ACTIONS_CAP:
        omitted = len(report["actions"]) - _RETURNED_ACTIONS_CAP
        report = {
            **report,
            "actions": report["actions"][:_RETURNED_ACTIONS_CAP],
            "actions_omitted": omitted,
        }
    return {"ok": True, **report}


async def _maybe_embed(
    app_state: Any, kernel: Any, item_id: str, text: str
) -> None:
    """Stamp embedding + EPA affect on a new item when the seam is wired.

    Both ride one embed call: the item's vector feeds the hybrid recall
    branch, and its projection onto the (process-cached) affect anchors
    feeds the W6 mood-congruent ranking term.
    """
    embed_fn = getattr(app_state, "memory_embed_fn", None)
    if embed_fn is None:
        return
    try:
        vector = await embed_fn(text)
        if not vector:
            return
        await kernel.set_embedding(item_id, list(vector))
        from corlinman_memory_kernel.affect import affect_from_embedding

        from corlinman_server.gateway.memory_affect import get_affect_anchors

        anchors = await get_affect_anchors(app_state)
        if anchors is None:
            return
        affect = affect_from_embedding(list(vector), anchors)
        if affect.salience > 0.0:
            await kernel.set_affect(
                item_id, affect.e, affect.p, affect.a, affect.salience
            )
    except Exception as exc:  # noqa: BLE001 — embeddings are an enhancement
        _logger.warning("memory.reconcile embed failed: %s", exc)


register_builtin(MEMORY_RECONCILE_BUILTIN_NAME, _memory_reconcile_action)
