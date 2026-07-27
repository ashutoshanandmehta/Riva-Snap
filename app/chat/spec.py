"""`ToolSpec` — the shape every companion tool is declared in.

Split out of `tools.py` so the spec *definitions* (`read_tools.py`,
`write_tools.py`) can import the dataclass without importing the registry that
is assembled from them. `tools.py` re-exports both names, so
`tools.ToolSpec` / `tools.normalize_slug` still resolve.

The security boundary lives here: `_FORBIDDEN_ARG_KEYS` is the list of argument
names that would let a caller nominate a subject. The verified `user_id` is
bound by `tools.dispatch()`, never declared in an `input_schema`.
"""

from collections.abc import Callable
from dataclasses import dataclass, field

from ..config import Settings

# Argument names that would let a caller nominate a subject. A tool that needs
# one of these is a design error, not a configuration choice.
FORBIDDEN_ARG_KEYS = frozenset(
    {
        "user_id",
        "userid",
        "user",
        "uid",
        "account_id",
        "account",
        "subject",
        "patient_id",
        "patient",
        "profile_id",
        "auth_id",
        "email",
    }
)


def normalize_slug(raw: str) -> str:
    """`/Retrieve-Weight-Log` and `retrieve_weight_log` are the same command."""
    return raw.strip().lstrip("/").replace("-", "_").lower()


@dataclass(frozen=True)
class ToolSpec:
    """One tool, usable from either entry path.

    `handler` takes `(config, user_id, arguments)` and returns a JSON-safe dict.
    `aliases` are extra slash-commands beyond `/<name>` — the app's UI buttons
    send the hyphenated spellings.

    `writes` marks a tool that mutates the user's health data. A write tool must
    also supply `confirm_summary(arguments) -> str`, the one-line plain-English
    statement of what would be saved; `confirm.py` puts it in front of the user
    before anything is written. See that module for why the flag is not enough
    on its own.
    """

    name: str
    description: str
    input_schema: dict
    handler: Callable[[Settings, str, dict], dict]
    aliases: tuple[str, ...] = field(default=())
    writes: bool = False
    confirm_summary: Callable[[dict], str] | None = None

    def __post_init__(self) -> None:
        if self.writes and self.confirm_summary is None:
            raise RuntimeError(
                f"Write tool {self.name!r} has no confirm_summary; a write the user"
                " cannot see stated plainly must not be offered."
            )
        offending = FORBIDDEN_ARG_KEYS.intersection(key.lower() for key in self.properties)
        if offending:
            raise RuntimeError(
                f"Tool {self.name!r} declares subject argument(s) {sorted(offending)}."
                " The user is bound from the verified token, never from tool input."
            )

    @property
    def slugs(self) -> tuple[str, ...]:
        """Every command slug that resolves to this tool, normalised."""
        return tuple(dict.fromkeys(normalize_slug(s) for s in (self.name, *self.aliases)))

    @property
    def properties(self) -> dict:
        return self.input_schema.get("properties", {})
