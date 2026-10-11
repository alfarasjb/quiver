"""The quiver MCP server: project memory tools for Claude Code sessions."""

import datetime as dt
from pathlib import Path
from typing import Annotated, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from quiver import memory
from quiver.project import Project, Unregistered, register, resolve
from quiver.render import render_activity, render_board, render_context, render_entry, render_unregistered
from quiver.store import (
    Entry,
    EntryKind,
    EntryStatus,
    WorkstreamStatus,
    add_entry,
    close_entry,
    get_entry,
    get_project,
    get_workstream,
    list_entries,
    list_projects as _list_projects,
    supersede,
)

SEARCH_LIMIT = 20

mcp = FastMCP(
    "quiver",
    instructions=(
        "Quiver is the owner's persistent memory across Claude Code sessions and projects: workstream status, "
        "decisions, facts, gotchas and events. Tools that act on one project take `cwd`, the absolute path of the "
        "directory you are working in (a worktree, or the container folder holding worktrees), or `project`, a slug "
        "from list_projects; board, search_memory and get_activity take only `project`. Prefer cwd: inside a worktree it "
        "identifies the branch, so writes land on that branch's workstream. Record things when they happen, not at the end of the session. If a tool says a directory "
        "is unregistered, follow its instructions before retrying."
    ),
)

Cwd = Annotated[
    str | None, Field(description="Absolute path of the directory you are working in (worktree or container).")
]
Slug = Annotated[str | None, Field(description="Project slug from list_projects; use when cwd is not inside the project.")]
WorkstreamName = Annotated[
    str | None, Field(description="Workstream name (see board). Defaults to the workstream bound to cwd's branch.")
]
Supersedes = Annotated[str | None, Field(description="Full id of the entry this replaces; that entry is marked superseded.")]
Scope = Literal["workstream", "project"]


def _require_absolute(cwd: str) -> None:
    if not Path(cwd).expanduser().is_absolute():
        raise ToolError(f"cwd must be an absolute path, got {cwd!r}")


def _require_project(slug: str) -> None:
    if not get_project(slug):
        raise ToolError(f"no project {slug!r}: see list_projects.")


def _project(cwd: str | None, project: str | None) -> Project:
    found = None
    if cwd:
        _require_absolute(cwd)
        found = resolve(cwd)
    if project:
        # An explicit project wins; cwd only contributes its branch when it is the same project.
        _require_project(project)
        return found if isinstance(found, Project) and found.slug == project else Project(slug=project)
    if not cwd:
        raise ToolError("pass cwd (absolute path) or project=<slug>.")
    if isinstance(found, Project):
        return found
    if isinstance(found, Unregistered):
        raise ToolError(render_unregistered(found, cwd))
    raise ToolError(f"{cwd!r} is not inside a quiver project: pass project=<slug> (see list_projects).")


def _workstream(p: Project, name: str | None) -> str:
    if name:
        if get_workstream(p.slug, name) is None:
            raise ToolError(f"no workstream {name!r} in {p.slug}: see board, or start it with set_status.")
        return name
    if bound := memory.find_by_branch(p.slug, p.branch):
        return bound.name
    raise ToolError(
        "no workstream for this directory: pass workstream=<name> (see board) or call set_status to start one."
    )


def _record(p: Project, entry: Entry, supersedes: str | None) -> str:
    if supersedes:
        old = get_entry(p.slug, supersedes)
        if old is None:
            raise ToolError(f"no entry {supersedes} in {p.slug}: see search_memory.")
        if old.kind != entry.kind:
            raise ToolError(f"entry {supersedes} is a {old.kind}; a {entry.kind} cannot supersede it.")
    try:
        if supersedes:
            supersede(p.slug, supersedes, entry)
        else:
            add_entry(p.slug, entry)
    except ValueError as e:
        raise ToolError(str(e)) from e
    scope = f"workstream {entry.workstream}" if entry.workstream else "project-wide"
    return f"Recorded {entry.kind} {entry.id} in {p.slug} ({scope})."


def _scoped(p: Project, scope: Scope, workstream: str | None) -> str | None:
    # Naming a workstream scopes to it even when the tool defaults to project scope.
    return _workstream(p, workstream) if workstream or scope == "workstream" else None


# --- read ---


@mcp.tool(output_schema=None)
def get_memory(cwd: Cwd = None, project: Slug = None, workstream: WorkstreamName = None) -> str:
    """Load a project's memory: open workstreams, project-wide facts, gotchas and decisions, plus the current
    workstream's stack, the decisions made on it and its recent events. Call before starting work on a project
    or when switching to another project or workstream. The session-start context already covers the
    starting directory."""
    p = _project(cwd, project)
    bound = memory.find_by_branch(p.slug, p.branch)
    name = workstream or (bound.name if bound else None)
    try:
        ctx = memory.context(p.slug, name)
    except KeyError as e:
        raise ToolError(f"{e.args[0]}: see board.") from e
    return render_context(p.slug, ctx)


@mcp.tool(output_schema=None)
def board(project: Slug = None) -> str:
    """Show open workstreams across all projects (or one), grouped by who must act: needs you, waiting,
    active, stale. Call when the owner asks what's going on, what needs them, or what to work on next."""
    if project:
        _require_project(project)
    return render_board(memory.board(project))


@mcp.tool(output_schema=None)
def search_memory(
    query: Annotated[str, Field(min_length=1)],
    project: Annotated[str | None, Field(description="Project slug. Omit to search all projects.")] = None,
    kind: EntryKind | None = None,
    status: Annotated[
        EntryStatus | None,
        Field(description="'active' (default); 'superseded' or 'resolved' for history; null for any."),
    ] = "active",
) -> str:
    """Find past decisions, facts, gotchas and events by keyword. Call before deciding something that may
    already have been decided, when you hit an error that may be a known gotcha, or when the owner asks
    "did we..." or "why did we...". Matches entries containing every word of query (case-insensitive,
    title and body), newest first."""
    if project:
        _require_project(project)
    terms = query.lower().split()
    slugs = [project] if project else [p.slug for p in _list_projects()]
    hits = [
        (slug, e)
        for slug in slugs
        for e in list_entries(slug, status)
        if kind in (None, e.kind) and all(t in f"{e.title}\n{e.body}".lower() for t in terms)
    ]
    hits.sort(key=lambda h: (h[1].created_at, h[1].id), reverse=True)
    if not hits:
        return f"No entries match {query!r}."
    # ponytail: fixed cap; add paging if 20 newest is not enough.
    lines = [render_entry(e, prefix=f"- {slug} ") for slug, e in hits[:SEARCH_LIMIT]]
    if len(hits) > SEARCH_LIMIT:
        lines.append(f"... {len(hits) - SEARCH_LIMIT} more; narrow the query.")
    return "\n".join(lines)


@mcp.tool(output_schema=None)
def get_activity(
    date: Annotated[dt.date | None, Field(description="Local calendar day, YYYY-MM-DD. Defaults to today.")] = None,
    project: Annotated[str | None, Field(description="Project slug. Omit for all projects.")] = None,
) -> str:
    """What the owner did on a day: session times grouped into night, morning, afternoon and evening, with the
    status changes, decisions, facts, gotchas and events recorded in each. Call when the owner asks what they
    did today or on a given day, or for a daily summary or timesheet."""
    if project:
        _require_project(project)
    day = date or dt.date.today()
    return render_activity(day, memory.activity(day, project))


@mcp.tool(output_schema=None)
def list_projects() -> str:
    """List registered projects and their slugs. Call when you need a slug for `project`."""
    projects = _list_projects()
    return "\n".join(f"- {p.slug} ({p.name})" for p in projects) or "No projects registered."


# --- write ---


@mcp.tool(output_schema=None)
def set_status(
    status: Annotated[
        WorkstreamStatus,
        Field(
            description="in_progress; needs_me = the owner must act; waiting = on someone or something else "
            "(set blocker and watch); done; not_started."
        ),
    ],
    next_step: Annotated[str, Field(description="The next concrete action, specific enough for a fresh session.")],
    cwd: Cwd = None,
    project: Slug = None,
    name: Annotated[
        str | None,
        Field(
            description="Workstream name. Defaults to the branch's workstream, else derived from ticket or "
            "branch; required in a container folder or on main."
        ),
    ] = None,
    ticket: str | None = None,
    branch: str | None = None,
    base: Annotated[str | None, Field(description="Branch this one is stacked on.")] = None,
    title: str | None = None,
    blocker: Annotated[str | None, Field(description="Who or what blocks it.")] = None,
    watch: Annotated[str | None, Field(description="What signals the wait is over, e.g. 'CI green on #42'.")] = None,
    location: Annotated[str | None, Field(description="Worktree path or PR URL.")] = None,
) -> str:
    """Create or update a workstream's status and next step. Call whenever either changes: when you start
    work, when you become blocked or wait on someone, when you hand back to the owner (needs_me), and when
    done. Don't wait for the end of the session. In a worktree, branch and ticket come from cwd. Omitted
    fields keep their value; pass "" to clear one."""
    p = _project(cwd, project)
    # cwd's branch and ticket describe the branch's own workstream, not one named explicitly.
    inherit = name is None
    try:
        ws = memory.set_status(
            p.slug,
            status,
            next_step,
            name=name,
            ticket=p.ticket if ticket is None and inherit else ticket,
            branch=p.branch if branch is None and inherit else branch,
            base=base,
            title=title,
            blocker=blocker,
            watch=watch,
            location=location,
        )
    except ValueError as e:
        raise ToolError(str(e)) from e
    return f"{p.slug}: {ws.name} is {ws.status}. Next: {ws.next_step}"


@mcp.tool(output_schema=None)
def record_decision(
    title: Annotated[str, Field(description="The decision in one line.")],
    rationale: str,
    cwd: Cwd = None,
    project: Slug = None,
    alternatives: Annotated[list[str] | None, Field(description="Options considered and rejected.")] = None,
    decided_by: Annotated[str | None, Field(description="e.g. 'Jay'.")] = None,
    source: Annotated[str | None, Field(description="PR, ticket, meeting or link where it was decided.")] = None,
    supersedes: Supersedes = None,
    workstream: WorkstreamName = None,
    scope: Annotated[
        Scope, Field(description="'workstream' ties it to the current workstream's stack; 'project' applies project-wide.")
    ] = "workstream",
) -> str:
    """Record a decision and why, at the moment it is made (by you or the owner), not at the end of the
    session. Call when choosing between approaches or settling scope or a design question, so future
    sessions don't reopen it. If it replaces an earlier decision, pass that decision's id as supersedes."""
    p = _project(cwd, project)
    entry = Entry(
        kind="decision",
        title=title,
        body=rationale,
        alternatives=alternatives or [],
        decided_by=decided_by,
        source=source,
        workstream=_scoped(p, scope, workstream),
        branch=p.branch,
    )
    return _record(p, entry, supersedes)


@mcp.tool(output_schema=None)
def add_fact(
    title: str,
    detail: str = "",
    cwd: Cwd = None,
    project: Slug = None,
    supersedes: Supersedes = None,
    workstream: WorkstreamName = None,
    scope: Scope = "project",
) -> str:
    """Record a durable fact a future session would otherwise rediscover: commands, environments,
    conventions, owners, external constraints. Call as soon as you learn it. Pass supersedes when correcting
    an older fact."""
    p = _project(cwd, project)
    entry = Entry(kind="fact", title=title, body=detail, workstream=_scoped(p, scope, workstream), branch=p.branch)
    return _record(p, entry, supersedes)


@mcp.tool(output_schema=None)
def add_gotcha(
    title: str,
    symptom: Annotated[str, Field(description="Error message or behavior, verbatim where possible.")],
    fix: Annotated[str, Field(description="What resolved it, and why it happens if known.")],
    cwd: Cwd = None,
    project: Slug = None,
    supersedes: Supersedes = None,
    workstream: WorkstreamName = None,
    scope: Scope = "project",
) -> str:
    """Record a trap that cost time: a symptom with a non-obvious cause, and its fix. Call right after you
    resolve it, while the error text is exact, so the next session finds it by searching the error."""
    p = _project(cwd, project)
    entry = Entry(
        kind="gotcha",
        title=title,
        body=f"## Symptom\n\n{symptom}\n\n## Fix\n\n{fix}",
        workstream=_scoped(p, scope, workstream),
        branch=p.branch,
    )
    return _record(p, entry, supersedes)


@mcp.tool(output_schema=None)
def record_event(
    title: str,
    detail: str = "",
    cwd: Cwd = None,
    project: Slug = None,
    workstream: WorkstreamName = None,
) -> str:
    """Log something that happened on the current workstream that isn't a decision or status change: PR
    opened or merged, review feedback, a deploy, a meeting outcome, an instruction from the owner. Call when
    it happens."""
    p = _project(cwd, project)
    entry = Entry(kind="event", title=title, body=detail, workstream=_workstream(p, workstream), branch=p.branch)
    return _record(p, entry, None)


@mcp.tool(output_schema=None)
def resolve_gotcha(entry_id: str, note: str, cwd: Cwd = None, project: Slug = None) -> str:
    """Mark a gotcha resolved once its cause is gone for good (upstream fix, dependency removed), so it stops
    appearing in session context. Don't call it just because you worked around the problem."""
    p = _project(cwd, project)
    entry = get_entry(p.slug, entry_id)
    if entry is None or entry.kind != "gotcha":
        raise ToolError(f"no gotcha {entry_id} in {p.slug}: see search_memory.")
    try:
        close_entry(p.slug, entry_id, "resolved", note)
    except ValueError as e:
        raise ToolError(str(e)) from e
    return f"Resolved gotcha {entry_id} in {p.slug}."


@mcp.tool(output_schema=None)
def register_project(
    cwd: Annotated[str, Field(description="Absolute path of the git repo, worktree or worktree container.")],
    slug: Annotated[str, Field(description="Lowercase letters, digits and dashes, e.g. 'acme'.")],
) -> str:
    """Register the git repo or worktree container at cwd as a quiver project. Call only after a quiver tool
    reports the directory as unregistered, using the slug the owner chose (or the single suggested slug)."""
    _require_absolute(cwd)
    try:
        meta = register(cwd, slug)
    except ValueError as e:
        raise ToolError(str(e)) from e
    return f"Registered {meta.slug}."
