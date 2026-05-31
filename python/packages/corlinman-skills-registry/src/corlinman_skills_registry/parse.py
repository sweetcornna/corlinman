"""Hand-rolled splitter for ``---`` YAML frontmatter + Markdown body.

We deliberately avoid a dedicated frontmatter library: the format is trivial
and we want **verbatim body preservation** (leading/trailing whitespace
intact) for downstream prompt injection. Mirrors the Rust ``parse`` module
behaviour byte-for-byte so the test suites can share fixtures.
"""

from __future__ import annotations

import hashlib
import os
import re
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast, get_args

import yaml

from .errors import MissingFieldError, SkillLoadError, YamlParseError
from .skill import Skill, SkillOrigin, SkillRequirements, SkillState


def split_frontmatter(text: str) -> tuple[str, str] | None:
    """Split ``text`` into ``(yaml_str, body_str)``.

    Returns ``None`` if the file does not start with a ``---`` frontmatter
    fence. Recognised fence: a line that is exactly ``---`` (optionally
    followed by ``\\r``). The opening fence MUST be the very first line of
    the file — same rule as the Rust implementation.
    """
    if text.startswith("---\n"):
        rest = text[len("---\n") :]
    elif text.startswith("---\r\n"):
        rest = text[len("---\r\n") :]
    else:
        return None

    # Walk lines (keeping their terminators) looking for a closing `---`.
    offset = 0
    # ``splitlines(keepends=True)`` preserves \n / \r\n / etc. on each line,
    # which is what we need to track byte offsets the way the Rust
    # ``split_inclusive('\n')`` iterator does.
    for line in rest.splitlines(keepends=True):
        trimmed = line.rstrip("\r\n")
        if trimmed == "---":
            yaml_str = rest[:offset]
            body_start = offset + len(line)
            body = rest[body_start:]
            return yaml_str, body
        offset += len(line)
    return None


def _required_non_empty(value: Any, path: Path, field: str) -> str:
    """Return ``value`` if it is a non-empty/non-whitespace string; otherwise
    raise :class:`MissingFieldError` with the same wording the Rust crate
    emits.
    """
    if isinstance(value, str) and value.strip():
        return value
    raise MissingFieldError(path=path, field=field)


def _coerce_str_list(value: Any) -> list[str]:
    """Lenient coercion for YAML list fields.

    The Rust code uses serde defaults which silently fall back to ``vec![]``
    when a key is missing; we mirror that for missing/``None`` here and
    fail-soft (empty list) for non-list shapes — invalid YAML structures are
    rejected upstream by :func:`yaml.safe_load`.
    """
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


def _coerce_opt_str(value: Any) -> str | None:
    """Return a non-empty trimmed string, else ``None`` (missing/blank)."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _coerce_mapping(value: Any) -> dict[str, Any]:
    """Carry an arbitrary YAML mapping verbatim; fail-soft to ``{}`` for
    missing / non-mapping shapes so a malformed ``hooks`` block never blocks
    skill loading (interpretation happens in the hook runner downstream)."""
    if isinstance(value, dict):
        # Normalise keys to str so the model's ``dict[str, Any]`` is honest;
        # values stay verbatim.
        return {str(k): v for k, v in value.items()}
    return {}


def _coerce_bool(value: Any) -> bool:
    """Strict-ish bool coercion. Accepts real bools plus the common YAML
    string spellings so ``disable-model-invocation: "true"`` still works."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "1", "on"}
    return False


_VALID_ORIGINS = set(get_args(SkillOrigin))
_VALID_STATES = set(get_args(SkillState))


def _coerce_origin(value: Any) -> SkillOrigin | None:
    """Accept a raw YAML value for ``origin`` and return the canonical
    literal, or ``None`` if absent/invalid. Defaults are applied by the
    caller (registry inference) — not here — so we can distinguish
    "missing" from "explicitly set"."""
    if isinstance(value, str) and value in _VALID_ORIGINS:
        return value  # type: ignore[return-value]
    return None


def _coerce_state(value: Any) -> SkillState | None:
    if isinstance(value, str) and value in _VALID_STATES:
        return value  # type: ignore[return-value]
    return None


def _coerce_datetime(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp defensively. Returns ``None`` on any
    error so that a malformed ``created_at`` never blocks skill loading.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def parse_skill(source_path: Path, text: str) -> Skill:
    """Parse a single skill file's raw text into a :class:`Skill`.

    Raises :class:`MissingFieldError` if the frontmatter fence is absent or
    a required field (``name`` / ``description``) is missing/empty.
    Raises :class:`YamlParseError` if the frontmatter is malformed YAML.
    """
    split = split_frontmatter(text)
    if split is None:
        raise MissingFieldError(path=source_path, field="frontmatter")
    yaml_str, body = split

    try:
        raw: Any = yaml.safe_load(yaml_str) if yaml_str.strip() else {}
    except yaml.YAMLError as err:
        raise YamlParseError(path=source_path, err=err) from err

    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        # The Rust deserializer would reject this as "invalid type";
        # surface it as a YAML parse error for the same callsite shape.
        raise YamlParseError(
            path=source_path,
            err=TypeError(f"frontmatter must be a mapping, got {type(raw).__name__}"),
        )

    name = _required_non_empty(raw.get("name"), source_path, "name")
    description = _required_non_empty(raw.get("description"), source_path, "description")

    metadata = raw.get("metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}
    openclaw = metadata.get("openclaw") or {}
    if not isinstance(openclaw, dict):
        openclaw = {}

    requires_raw = openclaw.get("requires") or {}
    if not isinstance(requires_raw, dict):
        requires_raw = {}

    requires = SkillRequirements(
        bins=_coerce_str_list(requires_raw.get("bins")),
        # Rust uses ``rename = "anyBins"`` — accept the camelCase YAML key.
        any_bins=_coerce_str_list(requires_raw.get("anyBins")),
        config=_coerce_str_list(requires_raw.get("config")),
        env=_coerce_str_list(requires_raw.get("env")),
    )

    emoji_raw = openclaw.get("emoji")
    emoji: str | None = emoji_raw if isinstance(emoji_raw, str) else None

    install_raw = openclaw.get("install")
    install: str | None = install_raw if isinstance(install_raw, str) else None

    # Rust uses ``rename = "allowed-tools"`` — accept the kebab-case YAML key.
    allowed_tools = _coerce_str_list(raw.get("allowed-tools"))

    # --- Lifecycle metadata (W4) -------------------------------------
    # Read from top-level frontmatter to mirror hermes' SKILL.md style
    # (see ``/tmp/hermes-agent-shallow/skills/yuanbao/SKILL.md`` — ``version``
    # sits at the top level, not under ``metadata.openclaw``). Missing keys
    # fall back to the pydantic defaults on :class:`Skill`.
    version_raw = raw.get("version")
    version: str = (
        version_raw.strip()
        if isinstance(version_raw, str) and version_raw.strip()
        else "1.0.0"
    )

    origin = _coerce_origin(raw.get("origin")) or "user-requested"
    state = _coerce_state(raw.get("state")) or "active"

    pinned_raw = raw.get("pinned")
    pinned: bool = bool(pinned_raw) if isinstance(pinned_raw, bool) else False

    created_at = _coerce_datetime(raw.get("created_at"))

    # --- Progressive-disclosure / model-selection metadata -----------
    # Read from top-level frontmatter (openclaw / claude-code SKILL.md
    # style). Accept both camelCase (``whenToUse``) and snake/kebab
    # spellings so hand-authored and hub-published files both load. These
    # were previously parsed-and-dropped; carry them through now.
    when_to_use = _coerce_opt_str(
        raw.get("whenToUse")
        if raw.get("whenToUse") is not None
        else raw.get("when_to_use")
    )
    paths = _coerce_str_list(raw.get("paths"))
    platforms = _coerce_str_list(raw.get("platforms"))
    model = _coerce_opt_str(raw.get("model"))
    effort = _coerce_opt_str(raw.get("effort"))
    hooks = _coerce_mapping(raw.get("hooks"))
    # Frontmatter has been seen with kebab-case and camelCase spellings.
    dmi_raw = raw.get("disable-model-invocation")
    if dmi_raw is None:
        dmi_raw = raw.get("disableModelInvocation")
    if dmi_raw is None:
        dmi_raw = raw.get("disable_model_invocation")
    disable_model_invocation = _coerce_bool(dmi_raw)

    return Skill(
        name=name,
        description=description,
        emoji=emoji,
        requires=requires,
        install=install,
        allowed_tools=allowed_tools,
        when_to_use=when_to_use,
        paths=paths,
        platforms=platforms,
        model=model,
        effort=effort,
        hooks=hooks,
        disable_model_invocation=disable_model_invocation,
        body_markdown=body,
        source_path=source_path,
        version=version,
        origin=origin,
        state=state,
        pinned=pinned,
        created_at=created_at,
    )


# ---------------------------------------------------------------------------
# Writers — round-trip a :class:`Skill` back to disk
# ---------------------------------------------------------------------------

# Canonical YAML key order: existing fields stay where they were, then the
# new W4 lifecycle fields tail the block (so hand-edited SKILL.md files keep
# their visual shape). Hermes' frontmatter convention is "human fields first,
# operational metadata last" — we follow it.
_TRAILING_LIFECYCLE_KEYS = (
    "version",
    "origin",
    "state",
    "pinned",
    "created_at",
)


def render_skill_frontmatter(skill: Skill) -> str:
    """Render ``skill`` as a YAML frontmatter block (no fences).

    Output is a deterministic ordered mapping so two calls with the same
    :class:`Skill` produce byte-identical YAML — important for the curator's
    "did I actually change anything?" diff check.
    """
    # Use an ordered dict so PyYAML emits keys in our canonical order.
    doc: dict[str, Any] = {}
    doc["name"] = skill.name
    doc["description"] = skill.description

    if skill.allowed_tools:
        doc["allowed-tools"] = list(skill.allowed_tools)

    # Progressive-disclosure / model-selection metadata — only emit
    # non-default keys so legacy hand-written files keep their shape. We
    # use the camelCase ``whenToUse`` spelling on write (matches the
    # openclaw / claude-code convention) and kebab-case for the boolean.
    if skill.when_to_use is not None:
        doc["whenToUse"] = skill.when_to_use
    if skill.paths:
        doc["paths"] = list(skill.paths)
    if skill.platforms:
        doc["platforms"] = list(skill.platforms)
    if skill.model is not None:
        doc["model"] = skill.model
    if skill.effort is not None:
        doc["effort"] = skill.effort
    if skill.hooks:
        doc["hooks"] = dict(skill.hooks)
    if skill.disable_model_invocation:
        doc["disable-model-invocation"] = True

    # ``metadata.openclaw`` sub-block — only emit keys that are non-default
    # so we don't bloat hand-written files.
    openclaw: dict[str, Any] = {}
    if skill.emoji is not None:
        openclaw["emoji"] = skill.emoji
    if skill.install is not None:
        openclaw["install"] = skill.install

    req = skill.requires
    if req.bins or req.any_bins or req.config or req.env:
        requires_block: dict[str, Any] = {}
        # Always emit all four lists when ``requires`` is non-empty, matching
        # the fixture style; PyYAML emits ``[]`` for empties which is the
        # idiomatic shape downstream tools expect.
        requires_block["bins"] = list(req.bins)
        requires_block["anyBins"] = list(req.any_bins)
        requires_block["config"] = list(req.config)
        requires_block["env"] = list(req.env)
        openclaw["requires"] = requires_block

    if openclaw:
        doc["metadata"] = {"openclaw": openclaw}

    # Lifecycle keys last — always emit them so the curator can rely on
    # round-trip stability without inferring defaults on every read.
    doc["version"] = skill.version
    doc["origin"] = skill.origin
    doc["state"] = skill.state
    doc["pinned"] = skill.pinned
    if skill.created_at is not None:
        # Serialise as ISO-8601 string; PyYAML's default datetime emitter
        # produces a non-ISO format that hermes can't round-trip.
        doc["created_at"] = skill.created_at.isoformat()

    # ``sort_keys=False`` honours our explicit insertion order;
    # ``default_flow_style=False`` keeps the block-style layout (one key per
    # line) that matches the existing fixtures.
    # PyYAML ships no type stubs (yaml is treated as ``Any``), so the
    # call is untyped; with ``stream=None`` ``safe_dump`` returns ``str``
    # at runtime, so narrowing the dynamic boundary here is sound.
    return cast(
        "str",
        yaml.safe_dump(
            doc,
            sort_keys=False,
            default_flow_style=False,
            allow_unicode=True,
        ),
    )


def write_skill_md(path: Path, skill: Skill, body: str | None = None) -> None:
    """Atomically write a SKILL.md file for ``skill``.

    The body defaults to ``skill.body_markdown``; pass an explicit ``body``
    when the curator has rewritten it but not yet committed to the model.

    Uses ``tempfile.NamedTemporaryFile`` + :func:`os.replace` so an
    interrupted write never leaves a half-written SKILL.md on disk — same
    pattern as hermes ``tools/skill_usage.py:11``.
    """
    if body is None:
        body = skill.body_markdown
    frontmatter = render_skill_frontmatter(skill)
    payload = f"---\n{frontmatter}---\n{body}"

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fp:
            fp.write(payload)
        os.replace(tmp_name, path)
    except Exception:
        # Best-effort cleanup if os.replace never ran.
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Trust scan — verify a downloaded skill tarball before it is materialised.
# ---------------------------------------------------------------------------
#
# The hub installer (``corlinman_server.system.skill_hub.installer``) does not
# own this package, so the verify + static-scan logic lives here where the
# skill data-model also lives. The installer is expected to call
# :func:`verify_and_scan_tarball` BEFORE extracting an untrusted tarball — see
# the wire_contract returned by lane-skills-meta.


class SkillHashMismatchError(SkillLoadError):
    """The downloaded tarball's sha256 did not match the declared
    ``content_hash``. A mismatch means the bytes were tampered with (or the
    CDN edge served a stale/corrupt blob); we refuse to install rather than
    materialise unverified content."""

    def __init__(self, *, expected: str, actual: str) -> None:
        super().__init__(
            f"skill tarball hash mismatch: declared content_hash "
            f"{expected!r} but downloaded bytes hash to {actual!r}"
        )
        self.expected = expected
        self.actual = actual


# Lightweight static-scan signatures. This is intentionally a cheap,
# heuristic line-scan — NOT a sandbox. It exists to flag the *obviously*
# dangerous patterns a malicious SKILL.md / helper script might smuggle in,
# so an operator gets a warning chip before the skill is trusted. False
# positives are acceptable (they surface as advisory flags, not hard
# failures); the goal is "loud enough to notice", not airtight.
_DANGEROUS_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("eval-exec", re.compile(r"\b(?:eval|exec)\s*\(")),
    ("os-system", re.compile(r"\bos\.system\s*\(")),
    ("subprocess-shell", re.compile(r"subprocess\.(?:Popen|call|run|check_output)\b.*shell\s*=\s*True")),
    ("pickle-loads", re.compile(r"\bpickle\.loads?\s*\(")),
    ("rm-rf", re.compile(r"\brm\s+-rf\b")),
    ("curl-pipe-shell", re.compile(r"\bcurl\b[^\n|]*\|\s*(?:ba)?sh\b")),
    ("wget-pipe-shell", re.compile(r"\bwget\b[^\n|]*\|\s*(?:ba)?sh\b")),
    ("base64-decode-exec", re.compile(r"base64\.b64decode\s*\([^\n]*\)[^\n]*(?:exec|eval)")),
    ("dunder-import", re.compile(r"__import__\s*\(")),
    ("sensitive-path", re.compile(r"(?:/etc/(?:passwd|shadow)|~/\.ssh/|\.aws/credentials)")),
)


def scan_text_for_dangerous_patterns(text: str) -> list[str]:
    """Return a sorted list of flag tags for any dangerous patterns found in
    ``text``. Empty list means the scan found nothing notable.

    Cheap and dependency-free — a regex line-scan, not real static analysis.
    Callers surface the tags as advisory warnings; they do not block install
    on their own (a hash mismatch does).
    """
    flags: set[str] = set()
    for tag, pattern in _DANGEROUS_PATTERNS:
        if pattern.search(text):
            flags.add(tag)
    return sorted(flags)


def scan_tarball_members(tarball: bytes) -> list[str]:
    """Static-scan the text members of a (gzip/plain) tar ``tarball``.

    Reads each regular member up to a small per-file cap, decodes as UTF-8
    (errors ignored — we only care about ASCII-ish payload patterns), and
    runs :func:`scan_text_for_dangerous_patterns`. Returns the de-duplicated,
    sorted union of flags across all members.

    Fail-soft: an unreadable / non-tar blob yields ``[]`` — the hash verify
    is the hard gate; the scan is advisory and must never crash the install.
    """
    import io

    _PER_MEMBER_SCAN_CAP = 1 * 1024 * 1024  # 1 MiB is plenty for source text
    flags: set[str] = set()
    try:
        with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:*") as tar:
            for member in tar.getmembers():
                if not member.isfile():
                    continue
                src = tar.extractfile(member)
                if src is None:
                    continue
                with src:
                    data = src.read(_PER_MEMBER_SCAN_CAP)
                text = data.decode("utf-8", errors="ignore")
                flags.update(scan_text_for_dangerous_patterns(text))
    except (tarfile.TarError, OSError):
        # Not a readable tar (or read error) — leave the hard gate to the
        # hash verify + the installer's own safe-extract path.
        return []
    return sorted(flags)


def verify_and_scan_tarball(
    tarball: bytes,
    declared_hash: str | None,
) -> list[str]:
    """Verify ``tarball``'s sha256 against ``declared_hash`` and static-scan
    its members.

    * If ``declared_hash`` is provided, the sha256 of ``tarball`` MUST match
      (case-insensitive; an optional ``sha256:`` prefix is tolerated). A
      mismatch raises :class:`SkillHashMismatchError`. When ``declared_hash``
      is ``None`` (ClawHub's ``X-Content-Hash`` header is best-effort and
      absent on some edges) the verify step is skipped — we can't check what
      upstream never declared.
    * Always returns the advisory static-scan flags (possibly empty). The
      installer should record these on the skill's sidecar / audit log so an
      operator can review what tripped the scanner.
    """
    actual = hashlib.sha256(tarball).hexdigest()
    if declared_hash:
        expected = declared_hash.strip().lower()
        if expected.startswith("sha256:"):
            expected = expected[len("sha256:") :]
        if expected and expected != actual:
            raise SkillHashMismatchError(expected=expected, actual=actual)
    return scan_tarball_members(tarball)


__all__ = [
    "SkillHashMismatchError",
    "parse_skill",
    "render_skill_frontmatter",
    "scan_tarball_members",
    "scan_text_for_dangerous_patterns",
    "split_frontmatter",
    "verify_and_scan_tarball",
    "write_skill_md",
]
