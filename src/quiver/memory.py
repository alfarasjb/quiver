"""Workstream logic: status updates, branch binding, stacks, the board and session context."""

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, tzinfo
from typing import Literal, get_args

from quiver.store import (
    Entry,
    EntryKind,
    Workstream,
    WorkstreamStatus,
    get_workstream,
    list_entries,
    list_projects,
    list_workstreams,
    now,
    UTC_STAMP,
    save_workstream,
    session_events,
    slugify,
)

# ponytail: repos defaulting to develop/trunk bind a workstream to it; upgrade = read origin/HEAD in project.resolve().
DEFAULT_BRANCHES = {"main", "master"}
STALE = timedelta(days=3)
FACT_CAP, GOTCHA_CAP, DECISION_CAP, EVENT_CAP = 15, 15, 10, 20

Group = Literal["needs_me", "waiting", "active", "stale"]
GROUPS: tuple[Group, ...] = get_args(Group)
Board = dict[Group, list[tuple[str, list[Workstream]]]]


def find_by_branch(slug: str, branch: str | None) -> Workstream | None:
    if not branch or branch in DEFAULT_BRANCHES:
        return None
    return next((w for w in list_workstreams(slug) if w.branch == branch and w.closed_at is None), None)


def _derive_name(ticket: str | None, branch: str | None) -> str:
    if ticket:
        return slugify(ticket)
    if branch and branch not in DEFAULT_BRANCHES:
        return slugify(branch)
    raise ValueError("name required: no ticket and no feature branch to derive one from")


def _log_line(ws: Workstream) -> str:
    parts = [f"next: {ws.next_step}"]
    if ws.blocker:
        parts.append(f"blocker: {ws.blocker}")
    if ws.watch:
        parts.append(f"watch: {ws.watch}")
    # Contract: "- <UTC ts>Z <status> " is parsed by the activity log; the rest is free text.
    text = " ".join(" | ".join(parts).split())
    return f"- {ws.updated_at:{UTC_STAMP}} {ws.status} {text}"


def set_status(
    slug: str,
    status: WorkstreamStatus,
    next_step: str,
    *,
    name: str | None = None,
    ticket: str | None = None,
    branch: str | None = None,
    base: str | None = None,
    title: str | None = None,
    blocker: str | None = None,
    watch: str | None = None,
    location: str | None = None,
) -> Workstream:
    """Create or update a workstream. A field left as None keeps its value; "" clears it.

    A done workstream comes back to life when the same name or branch is used again.
    """
    if branch in DEFAULT_BRANCHES:
        branch = None  # never stored: a branch on main would parent every stack based on main
    # ponytail: unlocked read-modify-write; one session per workstream is the norm. Add fcntl.flock if two share one.
    # An explicit name wins; otherwise the branch's bound workstream, so a ticket added later reuses it.
    ws = get_workstream(slug, name) if name else find_by_branch(slug, branch)
    if ws is None:
        name = name or _derive_name(ticket, branch)
        ws = get_workstream(slug, name) or Workstream(name=name)

    fields = dict(ticket=ticket, branch=branch, base=base, title=title, blocker=blocker, watch=watch, location=location)
    for key, value in fields.items():
        if value is not None:
            setattr(ws, key, value or None)
    ws.status = status
    ws.next_step = next_step

    bound = find_by_branch(slug, ws.branch)
    if bound and bound.name != ws.name:
        raise ValueError(f"branch {ws.branch!r} already belongs to open workstream {bound.name!r}")

    ws.updated_at = now()
    ws.closed_at = (ws.closed_at or ws.updated_at) if status == "done" else None
    ws.body = f"{ws.body.rstrip()}\n{_log_line(ws)}\n".lstrip()
    save_workstream(slug, ws)
    return ws


def chain(ws: Workstream, workstreams: list[Workstream]) -> list[Workstream]:
    """The stack ws belongs to, root first: open workstreams linked by base -> branch."""
    open_ = [w for w in workstreams if w.closed_at is None]
    by_branch = {w.branch: w for w in open_ if w.branch}
    seen = {ws.name}

    below: list[Workstream] = []
    cur = ws
    while cur.base and (parent := by_branch.get(cur.base)) and parent.name not in seen:
        seen.add(parent.name)
        below.insert(0, parent)
        cur = parent

    above: list[Workstream] = []

    def climb(w: Workstream) -> None:
        for child in open_:
            if w.branch and child.base == w.branch and child.name not in seen:
                seen.add(child.name)
                above.append(child)
                climb(child)

    climb(ws)
    return [*below, ws, *above]


def _rank(w: Workstream) -> int:
    if now() - w.updated_at > STALE:
        return GROUPS.index("stale")
    return GROUPS.index(w.status) if w.status in ("needs_me", "waiting") else GROUPS.index("active")


def board(slug: str | None = None) -> Board:
    """Open workstreams grouped by who has to act; a stack is one chain in its most urgent group."""
    groups: Board = {g: [] for g in GROUPS}
    for s in [slug] if slug else [p.slug for p in list_projects()]:
        open_ = [w for w in list_workstreams(s) if w.closed_at is None]
        branches = {w.branch for w in open_ if w.branch}
        placed: set[str] = set()
        # Roots first; anything left over sits in a base cycle and gets its own line.
        for w in [*(w for w in open_ if w.base not in branches), *open_]:
            if w.name in placed:
                continue
            stack = chain(w, open_)
            placed.update(m.name for m in stack)
            groups[GROUPS[min(map(_rank, stack))]].append((s, stack))
    return groups


@dataclass
class Context:
    board: Board
    facts: list[Entry]
    gotchas: list[Entry]
    decisions: list[Entry]
    workstream: Workstream | None = None
    chain: list[Workstream] = field(default_factory=list)
    chain_decisions: list[Entry] = field(default_factory=list)
    events: list[Entry] = field(default_factory=list)


def context(slug: str, workstream: str | None = None) -> Context:
    """Session-start memory for a project, plus one workstream and its stack when named."""
    entries = list_entries(slug)

    def of(kind: EntryKind, scope: set[str | None]) -> list[Entry]:
        return [e for e in entries if e.kind == kind and e.workstream in scope]

    project_wide: set[str | None] = {None}
    ctx = Context(
        board=board(slug),
        facts=of("fact", project_wide)[:FACT_CAP],
        gotchas=of("gotcha", project_wide)[:GOTCHA_CAP],
        decisions=of("decision", project_wide)[:DECISION_CAP],
    )
    if workstream is None:
        return ctx

    ws = get_workstream(slug, workstream)
    if ws is None:
        raise KeyError(f"workstream {workstream!r} not found in project {slug!r}")
    ctx.workstream = ws
    ctx.chain = chain(ws, list_workstreams(slug))
    names: set[str | None] = {w.name for w in ctx.chain}
    ctx.chain_decisions = of("decision", names)
    ctx.events = of("event", names)[:EVENT_CAP]
    return ctx


# --- activity ---

# Parses the _log_line contract: "- <UTC_STAMP> <status> <free text>".
_LOG_LINE = re.compile(r"^- (\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ) (.+)$", re.MULTILINE)
PERIODS = (("Night", 0), ("Morning", 6), ("Afternoon", 12), ("Evening", 18))  # local start hours

Span = tuple[datetime, datetime]


@dataclass
class StatusUpdate:
    workstream: str
    text: str  # "<status> <free text>"


Item = tuple[datetime, str, Entry | StatusUpdate]  # (when, project, what)


@dataclass
class Period:
    name: str
    spans: list[Span]  # merged session time, parallel sessions counted once
    items: list[Item]  # oldest first


def _union(spans: list[Span]) -> list[Span]:
    merged: list[Span] = []
    for s, e in sorted(spans):
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def activity(day: date, slug: str | None = None, tz: tzinfo | None = None) -> list[Period]:
    """What happened on a local calendar day, as non-empty periods (night, morning, afternoon, evening)."""
    tz = tz or datetime.now().astimezone().tzinfo  # today's fixed offset; fine for DST-free zones like UTC+8
    start = datetime.combine(day, time(), tz)
    end = start + timedelta(days=1)
    slugs = [slug] if slug else [p.slug for p in list_projects()]

    items: list[Item] = []
    for s in slugs:
        items += [(e.created_at, s, e) for e in list_entries(s, None) if start <= e.created_at < end]
        for w in list_workstreams(s):
            for m in _LOG_LINE.finditer(w.body):
                at = datetime.fromisoformat(m[1])
                if start <= at < end:
                    items.append((at, s, StatusUpdate(w.name, m[2])))
    items.sort(key=lambda item: item[0])

    # ponytail: a session spans its first..last event (Stop logs every turn), idle time included;
    # split spans on idle gaps (>30 min) if lunch shows up as work.
    sessions: dict[str, Span] = {}
    for ev in session_events(start, end):
        if ev.project in slugs:
            first, last = sessions.get(ev.session_id, (ev.time, ev.time))
            sessions[ev.session_id] = (min(first, ev.time), max(last, ev.time))
    spans = _union(list(sessions.values()))

    periods = []
    bounds = [start + timedelta(hours=h) for _, h in PERIODS] + [end]
    for (name, _), p_start, p_end in zip(PERIODS, bounds, bounds[1:]):
        # Half-open periods: a span ending exactly at p_start belongs to the previous period only.
        clipped = [(max(s, p_start), min(e, p_end)) for s, e in spans if s < p_end and (e > p_start or s >= p_start)]
        in_period = [item for item in items if p_start <= item[0] < p_end]
        if clipped or in_period:
            periods.append(Period(name, clipped, in_period))
    return periods
