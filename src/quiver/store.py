"""Markdown + YAML frontmatter store under $QUIVER_HOME.

Layout:
    <slug>/project.md
    <slug>/workstreams/<name>.md
    <slug>/entries/<YYYY-MM-DD>-<title-slug>-<id8>.md
    _sessions/<UTC YYYY-MM-DD>.jsonl   (one line per session event; "_" keeps it out of slug space)
"""

import json
import os
import re
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from uuid import uuid4

from dataclasses import dataclass

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

DEFAULT_HOME = "~/dev/general/quiver-memory"
UTC_STAMP = "%Y-%m-%dT%H:%M:%SZ"  # stored timestamps in status-log lines and session events
# libyaml is ~10x faster on full scans; fall back where PyYAML was built without it.
_YAML_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)

WorkstreamStatus = Literal["not_started", "in_progress", "needs_me", "waiting", "done"]
EntryKind = Literal["decision", "fact", "gotcha", "event"]
ClosedEntryStatus = Literal["superseded", "resolved"]
EntryStatus = Literal["active", ClosedEntryStatus]


def now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:120]


def check_slug(v: str) -> str:
    """Slugs are used as folder and file names, so they must already be slugs."""
    if not v or v != slugify(v):
        raise ValueError(f"{v!r} must be lowercase letters, digits and dashes, e.g. {slugify(v)!r}")
    return v


class _Doc(BaseModel):
    # extra="allow" keeps keys added by hand when a file is rewritten.
    model_config = ConfigDict(extra="allow", validate_assignment=True)


class Key(_Doc):
    key: str
    # remote: normalized origin. gitdir: shared .git of a repo with no origin.
    # dir: a container folder, matched as an ancestor of the cwd.
    kind: Literal["remote", "gitdir", "dir"]


class ProjectMeta(_Doc):
    slug: str
    name: str
    keys: list[Key]
    ticket_pattern: str | None = None

    _slug = field_validator("slug")(check_slug)

    @field_validator("ticket_pattern")
    @classmethod
    def _compiles(cls, v: str | None) -> str | None:
        if v is not None:
            try:
                re.compile(v)
            except re.error as e:  # re.error is not a ValueError, so pydantic would not wrap it
                raise ValueError(f"invalid ticket_pattern: {e}") from e
        return v


class Workstream(_Doc):
    name: str  # also the filename; Entry.workstream refers to it
    branch: str | None = None
    base: str | None = None
    ticket: str | None = None
    title: str | None = None
    status: WorkstreamStatus = "not_started"
    blocker: str | None = None
    watch: str | None = None
    location: str | None = None
    next_step: str | None = None
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)
    closed_at: datetime | None = None
    body: str = Field(default="", exclude=True)

    _slug = field_validator("name")(check_slug)


class Entry(_Doc):
    id: str = Field(default_factory=lambda: str(uuid4()))
    kind: EntryKind
    status: EntryStatus = "active"
    workstream: str | None = None
    branch: str | None = None
    supersedes: str | None = None
    decided_by: str | None = None
    source: str | None = None
    alternatives: list[str] = []
    created_at: datetime = Field(default_factory=now)
    closed_at: datetime | None = None
    title: str = Field(exclude=True, pattern=r"^[^\n]+$")  # stored as the body's "# " heading
    body: str = Field(default="", exclude=True)


def home() -> Path:
    return Path(os.environ.get("QUIVER_HOME", DEFAULT_HOME)).expanduser()


def _read(path: Path) -> tuple[dict, str]:
    text = path.read_text()
    # Split on the closing delimiter line only; "---" inside a value is data.
    parts = text.removeprefix("---\n").split("\n---\n", 1) if text.startswith("---\n") else []
    if len(parts) != 2:
        raise ValueError(f"{path}: missing YAML frontmatter")
    meta = yaml.load(parts[0], Loader=_YAML_LOADER) or {}
    if not isinstance(meta, dict):
        raise ValueError(f"{path}: frontmatter must be a mapping")
    return meta, parts[1].lstrip("\n")


def _load[T: _Doc](model: type[T], path: Path, from_body: Callable[[str], dict] = lambda _: {}) -> T:
    meta, body = _read(path)
    try:
        return model(**{**meta, **from_body(body)})  # body wins over a hand-added title/body key
    except ValueError as e:  # includes pydantic's ValidationError
        raise ValueError(f"{path}: {e}") from e


def _write_temp(path: Path, meta: dict, body: str) -> str:
    """Write a complete file next to path and return its name, ready to publish."""
    path.parent.mkdir(parents=True, exist_ok=True)
    front = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True, width=1000)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=".", suffix=".tmp", delete=False) as f:
        f.write(f"---\n{front}---\n\n{body}")
        f.flush()
        os.fsync(f.fileno())
    return f.name


def _write(path: Path, meta: dict, body: str = "") -> None:
    os.replace(_write_temp(path, meta, body), path)


def _dump(doc: _Doc) -> dict:
    return doc.model_dump(mode="json")


# --- projects ---


def _project_dir(slug: str) -> Path:
    return home() / check_slug(slug)


def _project_path(slug: str) -> Path:
    return _project_dir(slug) / "project.md"


def get_project(slug: str) -> ProjectMeta | None:
    path = _project_path(slug)
    return _load(ProjectMeta, path) if path.exists() else None


def list_projects() -> list[ProjectMeta]:
    return [_load(ProjectMeta, p) for p in sorted(home().glob("*/project.md"))]


def save_project(p: ProjectMeta) -> None:
    # ponytail: unlocked read-modify-write; a lost dir key is re-derived on the next container scan. Add fcntl.flock if keys stop being derivable.
    _write(_project_path(p.slug), _dump(p))


def create_project(p: ProjectMeta) -> ProjectMeta:
    """Create project.md only if absent; os.link lets exactly one racing session win.

    If the slug already holds a project with one of the same keys, that project is returned.
    """
    path = _project_path(p.slug)
    tmp = _write_temp(path, _dump(p), "")
    try:
        os.link(tmp, path)
    except FileExistsError:
        existing = get_project(p.slug)
        if existing and {k.key for k in existing.keys} & {k.key for k in p.keys}:
            return existing
        raise ValueError(f"project slug {p.slug!r} is taken by another repo")
    finally:
        os.unlink(tmp)
    return p


# --- workstreams ---


def _workstream_path(slug: str, name: str) -> Path:
    return _project_dir(slug) / "workstreams" / f"{check_slug(name)}.md"


def _load_workstream(path: Path) -> Workstream:
    return _load(Workstream, path, lambda body: {"body": body})


def get_workstream(slug: str, name: str) -> Workstream | None:
    path = _workstream_path(slug, name)
    return _load_workstream(path) if path.exists() else None


def list_workstreams(slug: str) -> list[Workstream]:
    return [_load_workstream(p) for p in sorted((_project_dir(slug) / "workstreams").glob("*.md"))]


def save_workstream(slug: str, ws: Workstream) -> None:
    _write(_workstream_path(slug, ws.name), _dump(ws), ws.body)


# --- entries ---


def _entries_dir(slug: str) -> Path:
    return _project_dir(slug) / "entries"


def _save_entry(path: Path, e: Entry) -> None:
    _write(path, _dump(e), f"# {e.title}\n\n{e.body}")


def add_entry(slug: str, e: Entry) -> Path:
    path = _entries_dir(slug) / f"{e.created_at:%Y-%m-%d}-{slugify(e.title)[:60]}-{e.id[:8]}.md"
    _save_entry(path, e)
    return path


def _entry_path(slug: str, entry_id: str) -> Path | None:
    if not re.fullmatch(r"[0-9a-f-]{36}", entry_id):  # ids reach a glob pattern below
        return None
    for path in _entries_dir(slug).glob(f"*-{entry_id[:8]}.md"):
        if _read(path)[0].get("id") == entry_id:
            return path
    return None


def _title_and_body(body: str) -> dict:
    heading, _, rest = body.partition("\n")
    if not heading.startswith("# "):
        raise ValueError(f"entry body must start with a '# ' title heading, got {heading!r}")
    return {"title": heading.removeprefix("# "), "body": rest.strip("\n")}


def _load_entry(path: Path) -> Entry:
    return _load(Entry, path, _title_and_body)


def list_entries(slug: str, status: EntryStatus | None = "active") -> list[Entry]:
    """Newest first; status=None returns every status."""
    # ponytail: full scan per call; add an index file if entries exceed ~10k.
    entries = (_load_entry(p) for p in _entries_dir(slug).glob("*.md"))
    return sorted((e for e in entries if status in (None, e.status)), key=lambda e: (e.created_at, e.id), reverse=True)


def get_entry(slug: str, entry_id: str) -> Entry | None:
    path = _entry_path(slug, entry_id)
    return _load_entry(path) if path else None


def _active_entry(slug: str, entry_id: str) -> tuple[Path, Entry]:
    path = _entry_path(slug, entry_id)
    if path is None:
        raise KeyError(f"entry {entry_id} not found in project {slug!r}")
    e = _load_entry(path)
    if e.status != "active":
        raise ValueError(f"entry {entry_id} is already {e.status}")
    return path, e


def close_entry(slug: str, entry_id: str, status: ClosedEntryStatus, note: str = "") -> Entry:
    # ponytail: no lock; two sessions closing one entry at once is last-writer-wins.
    path, e = _active_entry(slug, entry_id)
    e.status = status
    e.closed_at = now()
    if note:
        e.body = f"{e.body}\n\n## Resolution\n\n{note}".lstrip("\n")
    _save_entry(path, e)
    return e


def supersede(slug: str, old_id: str, new: Entry) -> Entry:
    _active_entry(slug, old_id)  # fail before writing anything
    # New entry first: a crash in between leaves the old entry active, which is
    # recoverable; closing first could leave nothing in its place.
    new.supersedes = old_id
    add_entry(slug, new)
    close_entry(slug, old_id, "superseded")
    return new


# --- sessions ---


def _sessions_dir() -> Path:
    return home() / "_sessions"


def log_session_event(session_id: str, project: str, workstream: str | None, at: datetime | None = None) -> None:
    """Append one line for a session event. Contract: time (UTC Z), session_id, project, workstream."""
    at = (at or now()).astimezone(UTC)
    line = {"time": f"{at:{UTC_STAMP}}", "session_id": session_id, "project": project, "workstream": workstream}
    path = _sessions_dir() / f"{at:%Y-%m-%d}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    # ponytail: relies on O_APPEND for small single writes on a local fs; add fcntl.flock if lines ever interleave.
    with path.open("a") as f:
        f.write(json.dumps(line) + "\n")


@dataclass(frozen=True)
class SessionEvent:
    time: datetime
    session_id: str
    project: str
    workstream: str | None


def session_events(start: datetime, end: datetime) -> list[SessionEvent]:
    """Events with start <= time < end, read from every UTC-dated file the window touches."""
    events = []
    day, last = start.astimezone(UTC).date(), (end - timedelta(microseconds=1)).astimezone(UTC).date()
    while day <= last:
        path = _sessions_dir() / f"{day:%Y-%m-%d}.jsonl"
        for raw in path.read_text().splitlines() if path.exists() else []:
            try:
                d = json.loads(raw)
                event = SessionEvent(
                    datetime.strptime(d["time"], UTC_STAMP).replace(tzinfo=UTC),
                    str(d["session_id"]),
                    str(d["project"]),
                    d.get("workstream"),
                )
            except (ValueError, KeyError, TypeError):
                continue  # a torn or hand-mangled line costs one event, not the day
            if start <= event.time < end:
                events.append(event)
        day += timedelta(days=1)
    return events
