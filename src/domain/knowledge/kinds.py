"""What a ``Service`` node is: anything a team owns.

A ``Service`` node carries ``kind``: a service or system that can go down, a process
that stalls, a product or account that is affected, a policy or playbook that no longer
applies. The label stays ``Service`` so every traversal sees every kind; ``kind`` only
changes the wording of replies and keeps accounts out of dependency answers (nothing
"depends on" an account, so it is never in a blast radius or a load-bearing ranking).
Nodes written before ``kind`` existed have none and read as ``service``.
"""

SERVICE_KINDS = ("service", "system", "process", "product", "account", "policy", "playbook")
DEFAULT_KIND = "service"
# Kinds that never take part in DEPENDS_ON: they carry OWNS, WORKED_ON and ABOUT edges only.
NOT_DEPENDABLE = frozenset({"account"})

# For queries: the kind of a node, with the default for nodes written before ``kind``.
KIND_OF = "coalesce({var}.kind, 'service')"

_DOWN = {
    "service": "goes down",
    "system": "goes down",
    "process": "stalls",
    "product": "is affected",
    "account": "is affected",
    "policy": "no longer applies",
    "playbook": "no longer applies",
}
# Kinds that go down with what they depend on. A blast radius lists them together ("these
# stop with it"); every other kind gets its own clause with its own verb.
GOES_DOWN = frozenset(kind for kind, verb in _DOWN.items() if verb == "goes down")


def normalize_kind(value) -> str:
    """One of SERVICE_KINDS. Empty means the default; anything else unknown is refused, never mapped."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return DEFAULT_KIND
    kind = str(value).strip().lower()
    if kind not in SERVICE_KINDS:
        raise ValueError(f"unknown kind {value!r}; expected one of {', '.join(SERVICE_KINDS)}")
    return kind


def kind_or_default(value) -> str:
    """Like ``normalize_kind`` for values read back from the graph: an unknown value reads as the default."""
    try:
        return normalize_kind(value)
    except ValueError:
        return DEFAULT_KIND


def _named_by_kind(name: str, kind: str) -> bool:
    last = name.lower().split()[-1] if name.strip() else ""
    return last in (kind, f"{kind}s")


def subject(name: str, kind: str | None) -> str:
    """How a sentence names the thing: "Billing Service", "the Renewals process", "the Pricing playbook"."""
    kind = kind_or_default(kind)
    if kind == DEFAULT_KIND:
        return name
    if name.strip() and name.lower().split()[-1] == f"{kind}s":
        # A plural name is the name alone: "Enterprise accounts is owned by", not "The Enterprise accounts is".
        return name
    return f"the {name}" if _named_by_kind(name, kind) else f"the {name} {kind}"


def listed(name: str, kind: str | None) -> str:
    """How a list names the thing: "Billing Service", "Renewals process", "Pricing playbook"."""
    kind = kind_or_default(kind)
    if kind == DEFAULT_KIND or _named_by_kind(name, kind):
        return name
    return f"{name} {kind}"


def down(kind: str | None) -> str:
    """What happens to it when it fails: goes down, stalls, is affected, no longer applies."""
    return _DOWN[kind_or_default(kind)]


def owned(kind: str | None) -> str:
    """"run by" for a process, "owned by" for everything else."""
    return "run by" if kind_or_default(kind) == "process" else "owned by"


def capitalized(text: str) -> str:
    return text[:1].upper() + text[1:]
