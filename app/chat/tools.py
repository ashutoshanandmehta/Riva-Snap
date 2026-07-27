"""The tool registry: one place every companion tool is assembled and run.

`router.py` looks specs up by slash-command slug; `agent.py` derives the
Anthropic `tools` payload from the same specs. Adding a tool means one entry in
`read_tools.SPECS` or `write_tools.SPECS`, and both entry paths pick it up.

Security boundary — the reason this module is small and strict:

- The verified `user_id` is bound by `dispatch()`, never declared in an
  `input_schema`. `ToolSpec` rejects a subject argument at import time, so a
  future tool cannot quietly reopen the hole.
- `dispatch()` drops any argument the schema does not declare.
- A `writes=True` tool goes through `confirm.gate()` first, so it cannot save
  anything the user was not shown and did not approve in an earlier turn.
"""

import logging

from ..config import Settings
from . import confirm, read_tools, write_tools
from .spec import ToolSpec, normalize_slug

logger = logging.getLogger("scan.chat.tools")

__all__ = [
    "COMMAND_INDEX",
    "REGISTRY",
    "ToolSpec",
    "anthropic_tools",
    "command_catalogue",
    "dispatch",
    "normalize_slug",
]


def _build_registry() -> dict[str, ToolSpec]:
    registry: dict[str, ToolSpec] = {}
    for spec in (*read_tools.SPECS, *write_tools.SPECS):
        if spec.name in registry:
            raise RuntimeError(f"Two tools are both named {spec.name!r}.")
        registry[spec.name] = spec
    return registry


def _build_command_index(registry: dict[str, ToolSpec]) -> dict[str, ToolSpec]:
    index: dict[str, ToolSpec] = {}
    for spec in registry.values():
        for slug in spec.slugs:
            if slug in index:
                raise RuntimeError(
                    f"Command /{slug} is claimed by both {index[slug].name!r} and {spec.name!r}."
                )
            index[slug] = spec
    return index


REGISTRY: dict[str, ToolSpec] = _build_registry()
COMMAND_INDEX: dict[str, ToolSpec] = _build_command_index(REGISTRY)


def anthropic_tools() -> list[dict]:
    """The registry in Anthropic Messages `tools` shape — derived, never a
    second hand-maintained copy."""
    return [
        {
            "name": spec.name,
            "description": spec.description,
            "input_schema": spec.input_schema,
        }
        for spec in REGISTRY.values()
    ]


def command_catalogue() -> list[dict]:
    """The commands as data, so the app can render its buttons from the server
    instead of hardcoding slugs that drift.

    `writes` is exposed so the client can style a mutating command differently —
    the confirmation step is enforced server-side either way.
    """
    return [
        {
            "tool": spec.name,
            "commands": [f"/{slug}" for slug in spec.slugs],
            "description": spec.description,
            "arguments": sorted(spec.properties),
            "writes": spec.writes,
        }
        for spec in REGISTRY.values()
    ]


def dispatch(
    spec: ToolSpec,
    config: Settings,
    user_id: str,
    arguments: dict,
    approved: set[str] | None = None,
) -> dict:
    """Runs a tool for the verified user.

    Arguments not declared in the schema are dropped rather than forwarded — the
    model can hallucinate a key, and a typed command can invent one, but neither
    reaches a handler. `user_id` is positional and comes from the caller, so it
    cannot be overridden by anything in `arguments`.

    `approved` is the set of write fingerprints the user has assented to on this
    request (see `confirm.resolve`). It defaults to empty, which means an
    unconfirmed write previews rather than saves — the safe default for any new
    call site.

    **`approved` is consumed in place.** A completed write removes its own
    fingerprint from the set before returning, so the caller's set shrinks. This
    is what makes one assent mean one write *within* a turn: the stored stamp
    only becomes visible on the next request, and the model can emit the same
    confirmed tool_use block twice in a single response.
    """
    accepted = {key: value for key, value in arguments.items() if key in spec.properties}
    dropped = sorted(set(arguments) - set(accepted))
    if dropped:
        logger.warning("tool %s: ignoring undeclared argument(s) %s", spec.name, dropped)

    pending = confirm.gate(spec, accepted, approved)
    if pending is not None:
        return pending

    # Captured before the confirm flag is stripped, so the value matches the
    # fingerprint the user approved.
    spent = confirm.fingerprint(spec.name, accepted) if spec.writes else None
    # The confirm flag is a gate, not data: the handler never sees it.
    accepted.pop(confirm.CONFIRM_KEY, None)
    # Argument *keys* only — values are the user's health data and must not be
    # written to logs.
    logger.info("tool %s dispatched with args %s", spec.name, sorted(accepted))
    result = spec.handler(config, user_id, accepted)
    if spent is not None:
        # Spend the approval twice over, because a turn and a thread are two
        # different replay windows:
        #   - in this turn, by removing it from the live set, so a second
        #     identical tool_use block in the same response previews instead;
        #   - across turns, by stamping the stored result, which is what
        #     `confirm.consumed` reads back on the next request.
        # A handler that raises never reaches here, so a rejected write leaves
        # the approval intact for the user's corrected retry.
        if isinstance(approved, set):
            approved.discard(spent)
        return confirm.stamp(result, spent)
    return result
