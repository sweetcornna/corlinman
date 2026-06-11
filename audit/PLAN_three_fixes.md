# Three fixes in flight (2026-05-30, post-v1.12.2)

Scratch plan (untracked, audit/). Survives context compaction.

## ✅ STATUS-CARD CLUSTER — COMPLETE & VERIFIED (2026-05-30)
All 7 channels now append "{public_url}/status/{token}" to replies + public read-only page.
Backend route status.py (JSON /data + SSE /events/live + #30 redaction, 403 verify) mounted in register.py.
Channels: _status.format_status_footer_line + service.configure_status_links (closure-injected minter,
no channels->server import) + _status_link_line wired into all 7 reply paths (4 spinner via
_build_footer_for_outcome, QQ/QQ-official/WeChat inline). Bootstrap: entrypoint._wire_status_links(cfg,
data_dir) armed before sibling-bootstrap loop; config.example.toml [server].public_url + [channels].
status_url_in_replies(default True); agent_servicer._dispatch_agent_status_card falls back to config
public_url when env unset. UI salvaged (ui/lib/status.ts + ui/app/status/[token]/*) — status-client.tsx
uses real paths @/lib/sessions/store + @/components/sessions/event-timeline (verified exist).
VERIFY (all independently re-run): ruff clean (full surface), mypy 5 files clean, import-linter 1 contract
KEPT 0 broken (no module-top server import in channels), 709 focused tests pass, config.example.toml parses.
Done via workflow wf_ef6bf309 (3 impl agents disjoint files + 1 verifier). REMAINING: deploy to VPS +
push + close issues #28-34; UI needs a real Next build (can't offline) to confirm static-export shell serving.

## Bug A — subagent children return output_text:"" (CONFIRMED by code read)
**Root cause:** the child reasoning loop has NO tool-execution wiring.
- `runner._drain_events` (runner.py ~L471): on `ToolCallEvent` it only does
  `tool_calls.append(_summarise_tool_call(event))` — it never executes the tool
  and never calls `loop.feed_tool_result(...)`.
- `output_text = "".join(output_chunks)` where output_chunks only gets `TokenEvent.text`.
- So a child that emits `web_search` calls gets no results back, never synthesizes,
  returns empty text. Prod log proof: `{"output_text":"","tool_calls_made":[web_search,...]}`.
- Parent does it right: agent_servicer L1271-1416 `async for event in loop.run()` →
  `ToolCallEvent` in BUILTIN_TOOLS → `self._dispatch_builtin(event, start, provider, file_state)`
  → `loop.feed_tool_result(ToolResult(call_id, content=result_json, is_error))`.
- Loop API: `ReasoningLoop.feed_tool_result(result: ToolResult)` (reasoning_loop.py L806).

**Fix design:** thread a `tool_dispatch` async callback into `run_child` (via
dispatch_subagent_spawn{,_many,_inline} → _run_child_under_slot → run_child). In
`_drain_events`, on `ToolCallEvent`: execute via callback, then `loop.feed_tool_result`.
Servicer supplies the callback as a closure over `self._dispatch_builtin` (+ child start/
provider/file_state). MVP: handle builtin tools; reject/skip subagent_spawn* from inside
a child (grandchild-ctx threading is out of MVP scope; general-purpose card prompt already
says "do not recursively spawn"). Must still record tool_calls_made for the summary.
NOTE drain is async + feed must happen mid-stream (loop blocks on _tool_results queue with
tool_result_timeout 0.05s). Confirm the drain executes the tool BEFORE the loop times out
the wait — may need to dispatch concurrently / ensure feed lands. Verify against workflow #2.

## Bug B — PDF/doc gen ignored skills, produced letter-spaced garbage (CONFIRMED)
**Root cause:** NO document/PDF/report-generation skill exists among the 20 bundled skills
(bundled_skills/ in corlinman-server: brainstorming, code_review, deep-research, web_search,
plan, TDD, debugging, configure-persona, darwin/nuwa/huashu, etc. — all SWE/persona, none doc-gen).
Agent improvised: chrome headless (exit 133 w/ old --headless), reportlab (no pip in venv),
then hand-rolled raw-PDF python (make_ai_github_pdf.py) → glyph-advance garbage.
**Box facts (good news):** google-chrome 143 present; WenQuanYi Micro Hei/Zen Hei CJK fonts
installed; `--headless=new --no-sandbox --disable-gpu --disable-dev-shm-usage` exits 0. NO
python pdf libs (reportlab/weasyprint/markdown/fpdf all missing), no pip.
**Fix design:** add a `document-generation` bundled skill that prescribes the reliable
on-box pipeline (clean semantic HTML w/ `font-family:'WenQuanYi Micro Hei',sans-serif` +
@page CSS → chrome --headless=new --print-to-pdf → verify w/ file → send_attachment),
explicitly FORBID hand-rolling raw PDFs, cover CJK. Seed into default profile (re-seed).
Confirm skill-surfacing mechanism via workflow #2 (how does the agent discover/use a skill?).

## Bug C — status URL not surfaced in channel replies (full plan from workflow #1)
**Root cause:** no end-to-end feature. status_token.py (make/verify) + agent_status_card
tool exist, but (1) nothing injects a URL into channel replies; (2) the public GET
/status/{token} route + UI page DON'T EXIST (links 404 today); (3) CORLINMAN_PUBLIC_URL is
env-only, no TOML; (4) event_emitter not threaded into channel params (footer path dormant).
**7 channels:** spinner (Telegram/Discord/Slack/Feishu) share `_build_footer_for_outcome`;
non-spinner (QQ/OneBot, QQ Official, WeChat Official) need bespoke `try_append_footer`.
**Plan (workflow #1, 7 steps):**
1. `_status.format_status_footer_line(public_url, session_key, signing_key, *, enabled, label)`
   — pure, mints token via lazily-imported make_status_token, returns one line. ONE mint point.
2. Extend `_build_footer_for_outcome(..., status_line='')`; append status_line REGARDLESS of
   footer_state.populated; add public_url/status_signing_key/status_url_enabled to all 7 *ChannelParams.
3. Wire 3 non-spinner channels (QQ before chunk_reply ~L1323; QQ Official after final=summary+body
   ~L3996; WeChat append to body before _split_passive_and_rest ~L4268).
4. Thread public_url + resolve_signing_key(data_dir) + flag ONCE in channels_runtime bootstrap →
   build_channel_tasks → all 7 _build_*_params.
5. BUILD public GET /status/{token} route (gateway/routes/status.py): verify_status_token → 403,
   read journal LAZILY from request.app.state (journal created after routes mount), return JSON
   {session_key,status,turns,events}. Register in routes/register.py at ROOT mount (public).
6. BUILD minimal public UI page ui/app/status/[token]/page.tsx (outside (admin) group), poll ~3s,
   read-only timeline (strip kill/approve), 403 empty state.
7. Config: add public_url + status_url_in_replies to [server] in docs/config.example.toml; env
   CORLINMAN_PUBLIC_URL overrides; agent_servicer._dispatch_agent_status_card also reads config.
**Layering risk:** corlinman-channels must NOT hard-import corlinman-server — resolve key/url
server-side, pass plain bytes/str into params, mint via lazy import (or relocate token fns to a
shared leaf pkg). **404 co-req:** land route+page WITH injection, flag-gated; keep OFF until route live.

## Sequencing
A (most urgent, breaks multi-agent) → B (PDF skill) → C (status URL feature). Deploy A+B first,
then C. All via bundle-over-SSH to VPS 43.133.12.98 (SSHPASS), publish to sweetcornna (needs PAT).
