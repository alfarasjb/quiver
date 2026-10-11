"""Markdown views shared by the MCP tools and the CLI/hooks."""

from datetime import date, datetime, tzinfo

from quiver.memory import Board, Context, Group, Period, Span
from quiver.project import Unregistered
from quiver.store import Entry, Workstream

BODY_PREVIEW = 600
GROUP_TITLES: dict[Group, str] = {"needs_me": "Needs you", "waiting": "Waiting", "active": "Active", "stale": "Stale (3+ days quiet)"}


def workstream_line(w: Workstream) -> str:
    extras = [f"{label}: {value}" for label, value in (("blocker", w.blocker), ("watch", w.watch)) if value]
    tail = f" ({'; '.join(extras)})" if extras else ""
    return f"**{w.name}** [{w.status}] next: {w.next_step}{tail} · updated {w.updated_at:%Y-%m-%d}"


def render_entry(e: Entry, prefix: str = "- ") -> str:
    head = f"{prefix}[{e.kind}] **{e.title}** ({e.created_at:%Y-%m-%d}, {e.status}, id {e.id})"
    body = e.body if len(e.body) <= BODY_PREVIEW else e.body[:BODY_PREVIEW] + " …"
    return head + "".join(f"\n  {line}" for line in body.splitlines() if line.strip())


def render_board(board: Board, heading: str = "##") -> str:
    sections = []
    for group, stacks in board.items():
        if not stacks:
            continue
        lines = [f"{heading} {GROUP_TITLES[group]}"]
        for slug, stack in stacks:
            if len(stack) > 1:
                lines.append(f"- {slug} stack: {' → '.join(w.name for w in stack)}")
                lines += [f"  - {workstream_line(w)}" for w in stack]
            else:
                lines.append(f"- {slug}: {workstream_line(stack[0])}")
        sections.append("\n".join(lines))
    return "\n\n".join(sections) or "No open workstreams."


def _section(title: str, entries: list[Entry]) -> str | None:
    return f"## {title}\n" + "\n".join(map(render_entry, entries)) if entries else None


def render_context(slug: str, ctx: Context) -> str:
    parts: list[str | None] = [f"# Quiver memory: {slug}"]
    if ws := ctx.workstream:
        where = ", ".join(f"{k}: {v}" for k, v in (("ticket", ws.ticket), ("branch", ws.branch), ("at", ws.location)) if v)
        head = f"## Current workstream\n{workstream_line(ws)}" + (f"\n{ws.title}" if ws.title else "") + (f"\n{where}" if where else "")
        parts.append(head)
        if len(ctx.chain) > 1:
            parts.append("## Stack\n" + "\n".join(f"- {workstream_line(w)}" for w in ctx.chain))
        parts.append(_section("Decisions on this stack", ctx.chain_decisions))
        parts.append(_section("Recent events", ctx.events))
    parts.append("## Board\n" + render_board(ctx.board, heading="###"))
    parts.append(_section("Facts", ctx.facts))
    parts.append(_section("Gotchas", ctx.gotchas))
    parts.append(_section("Recent decisions", ctx.decisions))
    return "\n\n".join(p for p in parts if p)


def render_unregistered(found: Unregistered, cwd: str) -> str:
    if len(found.candidates) == 1:
        return (
            f"{found.key.key} is not registered with quiver. "
            f"Call register_project(cwd={cwd!r}, slug={found.candidates[0]!r}), then retry."
        )
    return (
        f"{found.key.key} is not registered with quiver. Ask the owner which slug to use "
        f"(suggestions: {', '.join(found.candidates)}), then call register_project(cwd={cwd!r}, "
        "slug=<their choice>) and retry."
    )


def _clock(at: datetime, tz: tzinfo | None) -> str:
    return f"{at.astimezone(tz):%H:%M}"


def _span(span: Span, tz: tzinfo | None) -> str:
    start, end = (_clock(t, tz) for t in span)
    return start if start == end else f"{start}–{end}"


def render_activity(day: date, periods: list[Period], tz: tzinfo | None = None) -> str:
    """tz=None renders in the system local timezone, matching memory.activity's default."""
    if not periods:
        return f"No activity on {day}."
    parts = [f"# Activity {day}"]
    for p in periods:
        lines = [f"## {p.name}" + (f" · {', '.join(_span(s, tz) for s in p.spans)}" if p.spans else "")]
        for at, project, what in p.items:
            if isinstance(what, Entry):
                scope = f" ({what.workstream})" if what.workstream else ""
                lines.append(f"- {project} · {_clock(at, tz)} [{what.kind}] {what.title}{scope}")
            else:
                lines.append(f"- {project} · {_clock(at, tz)} {what.workstream}: {what.text}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)
