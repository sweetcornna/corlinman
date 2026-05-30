"""In-memory agent-card registry, loaded from a directory of yaml files.

The registry is intentionally minimal: it maps ``name -> AgentCard``
and exposes a read-only lookup surface. Hot-reload, file watching, and
validation reporting belong to an operator-facing admin layer that is
out of scope for this workstream.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml  # type: ignore[import-untyped]

from corlinman_agent.agents.card import AgentCard, AgentSource

logger = logging.getLogger(__name__)

#: Name of the built-in fallback subagent card. W1.1: when an LLM emits a
#: ``subagent_spawn`` call without specifying ``subagent_type`` (or with an
#: unknown value), the dispatcher resolves to this card via
#: :meth:`AgentCardRegistry.get_or_default`. The card is shipped at the
#: repo's ``agents/general-purpose.yaml`` and uses ``tools_allowed: ["*"]``
#: to mean "inherit the parent's full tool set" (see the runner's
#: wildcard handling).
DEFAULT_SUBAGENT_NAME: str = "general-purpose"


class AgentCardLoadError(RuntimeError):
    """Raised when a yaml file under the agents dir is unparseable or
    missing required fields. The file path is included so operators can
    locate the offender without re-running the loader."""

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f"{path}: {reason}")
        self.path = path
        self.reason = reason


def _as_str_list(value: object, field_name: str, path: Path) -> list[str]:
    """Coerce an optional yaml list-of-strings field. ``None`` / missing
    is the empty list; anything else must be a list of strings or the
    file is rejected."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise AgentCardLoadError(path, f"{field_name} must be a list of strings")
    out: list[str] = []
    for entry in value:
        if not isinstance(entry, str):
            raise AgentCardLoadError(path, f"{field_name} entries must be strings")
        out.append(entry)
    return out


def _as_optional_str(value: object, field_name: str, path: Path) -> str | None:
    """Coerce an optional yaml scalar-string field. ``None`` / missing
    yields ``None``; any non-string value is rejected so a stray int /
    bool can't silently become an upstream model id."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise AgentCardLoadError(path, f"{field_name} must be a string")
    return value


def _as_optional_bool(
    value: object,
    field_name: str,
    path: Path,
    *,
    default: bool,
) -> bool:
    """Coerce an optional yaml boolean field.

    ``None`` / missing yields ``default``; anything else must already be
    a YAML boolean. This deliberately rejects strings like ``"false"``
    so typoed frontmatter cannot silently flip operator-facing UI
    behaviour.
    """
    if value is None:
        return default
    if not isinstance(value, bool):
        raise AgentCardLoadError(path, f"{field_name} must be a boolean")
    return value


def _as_str_dict(value: object, field_name: str, path: Path) -> dict[str, str]:
    """Coerce the ``variables:`` mapping. Values are stringified
    (yaml may parse ``"15"`` as an int) to keep the expander's
    substitution step type-safe."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise AgentCardLoadError(path, f"{field_name} must be a mapping")
    out: dict[str, str] = {}
    for k, v in value.items():
        if not isinstance(k, str):
            raise AgentCardLoadError(path, f"{field_name} keys must be strings")
        out[k] = str(v)
    return out


def _load_card(path: Path, *, source: AgentSource = "built-in") -> AgentCard:
    """Parse one ``<name>.yaml`` file into an :class:`AgentCard`.

    The filename stem is authoritative for ``name`` — if the yaml body
    also carries a ``name:`` key it must agree, otherwise the file is
    rejected (protects against copy-paste mistakes where a file was
    renamed without updating its body).

    ``source`` tags which tier (built-in / user / project) the file
    came from. The default ``"built-in"`` preserves the pre-W1.2
    behaviour for callers that still use :meth:`load_from_dir`.
    """
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise AgentCardLoadError(path, f"yaml parse error: {exc}") from exc

    if raw is None:
        raise AgentCardLoadError(path, "file is empty")
    if not isinstance(raw, dict):
        raise AgentCardLoadError(path, "top-level yaml must be a mapping")

    stem = path.stem
    declared_name = raw.get("name")
    if declared_name is not None:
        if not isinstance(declared_name, str):
            raise AgentCardLoadError(path, "name must be a string")
        if declared_name != stem:
            raise AgentCardLoadError(
                path,
                f"declared name {declared_name!r} does not match filename stem {stem!r}",
            )
    name = stem

    description = raw.get("description", "")
    if not isinstance(description, str):
        raise AgentCardLoadError(path, "description must be a string")

    system_prompt = raw.get("system_prompt")
    if not isinstance(system_prompt, str) or not system_prompt.strip():
        raise AgentCardLoadError(path, "system_prompt is required and must be a non-empty string")

    variables = _as_str_dict(raw.get("variables"), "variables", path)
    tools_allowed = _as_str_list(raw.get("tools_allowed"), "tools_allowed", path)
    skill_refs = _as_str_list(raw.get("skill_refs"), "skill_refs", path)

    # W-D1: optional per-agent model binding. Both fields are independent —
    # an operator may set just ``model``, just ``provider``, or both. ``None``
    # / absent keeps the pre-W-D1 dispatch path (request-body-driven routing).
    model = _as_optional_str(raw.get("model"), "model", path)
    provider = _as_optional_str(raw.get("provider"), "provider", path)
    show_action_trace = _as_optional_bool(
        raw.get("show_action_trace"),
        "show_action_trace",
        path,
        default=True,
    )

    return AgentCard(
        name=name,
        description=description,
        system_prompt=system_prompt,
        variables=variables,
        tools_allowed=tools_allowed,
        skill_refs=skill_refs,
        source_path=path,
        model=model,
        provider=provider,
        show_action_trace=show_action_trace,
        source=source,
    )


class AgentCardRegistry:
    """Read-only lookup over agent cards loaded from disk.

    The registry is built via :meth:`load_from_dir`, which scans a
    directory for ``*.yaml`` / ``*.yml`` files and parses each into an
    :class:`AgentCard`. Failed files raise :class:`AgentCardLoadError`
    immediately rather than being silently skipped — silent skips cause
    hard-to-debug "why won't my agent expand" tickets.
    """

    def __init__(self, cards: dict[str, AgentCard]) -> None:
        self._cards = cards

    @classmethod
    def load_from_dir(cls, root: Path) -> AgentCardRegistry:
        """Load every ``*.yaml`` / ``*.yml`` file under ``root``.

        Non-existent roots yield an empty registry (lets operators run
        with no agents configured yet). A path that exists but isn't a
        directory is a configuration error and raises.
        """
        cards: dict[str, AgentCard] = {}
        if not root.exists():
            return cls(cards)
        if not root.is_dir():
            raise AgentCardLoadError(root, "agents root must be a directory")

        # Sorted so load order is deterministic across platforms — matters
        # when two files declare the same name and we want a stable
        # "first wins / last wins" story (we reject duplicates below,
        # but the sorted scan keeps error messages predictable).
        for path in sorted(root.iterdir()):
            if path.suffix.lower() not in (".yaml", ".yml"):
                continue
            if not path.is_file():
                continue
            card = _load_card(path)
            if card.name in cards:
                raise AgentCardLoadError(
                    path,
                    f"duplicate agent name {card.name!r} "
                    f"(also defined in {cards[card.name].source_path})",
                )
            cards[card.name] = card
        return cls(cards)

    @classmethod
    def load_from_dir_stack(
        cls,
        dirs: list[tuple[Path, AgentSource]],
    ) -> AgentCardRegistry:
        """Load cards from multiple dirs in precedence order.

        Each entry is ``(path, source)`` where ``source`` is one of
        ``"built-in"`` / ``"user"`` / ``"project"``. Later tiers win on
        name collision so the canonical stack
        ``[(repo, "built-in"), (user, "user"), (project, "project")]``
        gives operators a natural override surface: bundled cards as
        defaults, ``$DATA_DIR/agents/`` for per-install overrides, and
        ``.corlinman/agents/`` for per-checkout overrides.

        Loading rules:

        * ``.yaml`` / ``.yml`` → :func:`_load_card` (existing yaml path).
        * ``.md`` → :func:`corlinman_agent.agents.markdown.parse_markdown_card`
          (frontmatter MD).
        * Files starting with ``_`` or ``.`` are ignored.
        * Parse errors in **user** / **project** overlays are *logged*
          but do not break boot — operators editing live get warned via
          the logs and the registry stays usable. Errors in built-in
          tiers are also logged (no crash) so a corrupt bundled file
          can't bring the gateway down; tests still pin the strict-mode
          surface via :meth:`load_from_dir`.

        Within a single tier, ``.yaml`` is preferred over ``.md`` when
        both files declare the same name — the second one is logged and
        skipped (this matches Claude Code's behaviour where the explicit
        long form wins).
        """
        # Lazy import — registry.py needs to be importable without the
        # markdown module being present (it lives next door and imports
        # back into us for the helper coercers).
        from corlinman_agent.agents.markdown import parse_markdown_card

        cards: dict[str, AgentCard] = {}
        # Track per-name "first source seen" so we can log clearly when
        # a later tier shadows an earlier one (operators must know they
        # overrode a default).
        first_seen_source: dict[str, AgentSource] = {}

        for root, source in dirs:
            if not root.exists():
                continue
            if not root.is_dir():
                logger.warning(
                    "agent_registry.skip_non_directory path=%s source=%s",
                    str(root),
                    source,
                )
                continue
            # Per-tier "winners by name" so the .yaml/.md tie-break is
            # deterministic and operator-visible.
            tier_cards: dict[str, AgentCard] = {}
            for path in sorted(root.iterdir()):
                if not path.is_file():
                    continue
                stem = path.stem
                if not stem or stem.startswith("_") or stem.startswith("."):
                    continue
                suffix = path.suffix.lower()
                if suffix in (".yaml", ".yml"):
                    parser = "yaml"
                elif suffix == ".md":
                    parser = "md"
                else:
                    continue
                try:
                    if parser == "yaml":
                        card = _load_card(path, source=source)
                    else:
                        card = parse_markdown_card(
                            path.read_text(encoding="utf-8"),
                            name=stem,
                            source_path=path,
                            source=source,
                        )
                except AgentCardLoadError as exc:
                    logger.warning(
                        "agent_registry.load_error path=%s source=%s reason=%s",
                        str(exc.path),
                        source,
                        exc.reason,
                    )
                    continue
                except OSError as exc:
                    logger.warning(
                        "agent_registry.read_error path=%s source=%s error=%s",
                        str(path),
                        source,
                        str(exc),
                    )
                    continue
                if card.name in tier_cards:
                    logger.warning(
                        "agent_registry.duplicate_in_tier "
                        "agent=%s source=%s winner=%s loser=%s",
                        card.name,
                        source,
                        str(tier_cards[card.name].source_path),
                        str(path),
                    )
                    continue
                tier_cards[card.name] = card

            # Promote tier_cards into the merged registry; log shadows.
            for card_name, card in tier_cards.items():
                if card_name in cards:
                    prior_source = first_seen_source.get(card_name, "built-in")
                    if prior_source != source:
                        logger.warning(
                            "agent_registry.shadow "
                            "agent=%s shadowing_source=%s shadowed_source=%s "
                            "shadowing_path=%s",
                            card_name,
                            source,
                            prior_source,
                            str(card.source_path),
                        )
                cards[card_name] = card
                first_seen_source.setdefault(card_name, source)
        return cls(cards)

    def get(self, name: str) -> AgentCard | None:
        """Return the card for ``name`` or ``None`` if not registered."""
        return self._cards.get(name)

    def get_or_builtin_default(self, name: str | None) -> AgentCard | None:
        """Like :meth:`get_or_default`, but the *default* (omitted name) and
        an explicit ``general-purpose`` always resolve — falling back to the
        in-code :func:`builtin_general_purpose` card when no bundled
        ``general-purpose`` was loaded (Claude-Code "offline-first").

        An explicit OTHER unknown name still returns ``None`` so the
        dispatcher can surface ``unknown_subagent_type`` (typo protection).
        """
        if name and name != DEFAULT_SUBAGENT_NAME:
            return self._cards.get(name)
        return self._cards.get(DEFAULT_SUBAGENT_NAME) or builtin_general_purpose()

    def get_or_default(self, name: str | None) -> AgentCard | None:
        """Resolve ``name`` to a card, falling back to ``general-purpose``.

        W1.1 — backstops the new ``subagent_type`` arg on the
        ``subagent_spawn`` tool. When the LLM omits ``subagent_type``
        (or passes ``None``/empty), the registry returns the built-in
        ``general-purpose`` card. When the LLM passes a non-empty name
        the registry returns that card if registered, otherwise ``None``
        — letting the dispatcher emit a ``REJECTED`` /
        ``unknown_subagent_type`` envelope rather than silently
        substituting the default (which would mask typos).

        The fallback card itself is loaded from
        ``agents/general-purpose.yaml`` at registry load time; if that
        file is absent (e.g. a deployment that stripped the bundled
        cards) ``get_or_default(None)`` returns ``None`` and the
        dispatcher's unknown-type path fires.
        """
        if name:
            card = self._cards.get(name)
            if card is not None:
                return card
            return None
        return self._cards.get(DEFAULT_SUBAGENT_NAME)

    def names(self) -> list[str]:
        """Return all registered agent names, sorted."""
        return sorted(self._cards.keys())

    def cards(self) -> list[AgentCard]:
        """Return every loaded card, sorted by name. Used by the admin
        list endpoint so we don't have to expose ``_cards``."""
        return [self._cards[name] for name in sorted(self._cards.keys())]

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._cards

    def __len__(self) -> int:
        return len(self._cards)


def builtin_general_purpose() -> AgentCard:
    """The in-code ``general-purpose`` subagent card.

    Claude-Code-style "offline-first" fallback: spawns resolve to this when
    the bundled ``agents/general-purpose.yaml`` wasn't loaded (e.g. a
    deployment whose registry only scans ``<DATA_DIR>/agents``). Mirrors the
    on-disk card: ``tools_allowed=["*"]`` (inherit the parent's full set,
    bounded downstream), ``model=None`` (inherit the parent's resolved
    model). Never registered — ``source_path=None``.
    """
    return AgentCard(
        name=DEFAULT_SUBAGENT_NAME,
        description="General-purpose subagent (built-in fallback).",
        system_prompt=(
            "You are a general-purpose subagent dispatched by a parent agent "
            "to handle one focused task. You have the parent's tool set "
            "(subject to its whitelist). Stay strictly within the goal — do "
            "not branch into new topics and do not recursively spawn your own "
            "subagents. When done, return a concise structured summary: what "
            "you did, what succeeded/failed (with paths/URLs), any caveats, "
            "and a one-line verdict."
        ),
        variables={},
        tools_allowed=["*"],
        skill_refs=[],
        source_path=None,
        model=None,
        provider=None,
        show_action_trace=True,
        source="built-in",
    )


__all__ = [
    "DEFAULT_SUBAGENT_NAME",
    "AgentCardLoadError",
    "AgentCardRegistry",
    "builtin_general_purpose",
]
