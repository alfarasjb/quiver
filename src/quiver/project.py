"""Resolve a working directory to a quiver project.

Never reads the process cwd: callers pass the directory (hook stdin `cwd`,
or the worktree path Claude is working in).
"""

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from quiver.store import Key, ProjectMeta, create_project, list_projects, save_project

_SCP = re.compile(r"^(?:[^@/]+@)?([^:/]+):(.+)$")
_GIT_ENV_DROP = ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE")


@dataclass(frozen=True, kw_only=True)
class Project:
    slug: str
    branch: str | None = None
    ticket: str | None = None


@dataclass(frozen=True)
class Unregistered:
    """Known repo, no project yet. Claude registers it, asking the owner when candidates differ."""

    key: Key
    candidates: list[str]


def normalize_remote(url: str) -> str:
    url = url.strip().rstrip("/").removesuffix(".git")
    if "://" not in url and (m := _SCP.match(url)):
        host, path = m.groups()
    else:
        parts = urlsplit(url)
        host, path = parts.hostname or "", parts.path
    return f"{host}/{path.strip('/')}".strip("/").lower()


def _git(d: Path, *args: str) -> str | None:
    env = {k: v for k, v in os.environ.items() if k not in _GIT_ENV_DROP}
    r = subprocess.run(["git", "-C", str(d), *args], capture_output=True, text=True, timeout=5, env=env)
    return r.stdout.strip() if r.returncode == 0 else None


def _repo_key(d: Path) -> Key | None:
    if _git(d, "rev-parse", "--show-toplevel") is None:
        return None
    if origin := _git(d, "remote", "get-url", "origin"):
        return Key(key=normalize_remote(origin), kind="remote")
    # No origin: the shared .git dir, so every worktree of the repo maps to one key.
    common = _git(d, "rev-parse", "--path-format=absolute", "--git-common-dir")
    return Key(key=str(Path(common).resolve()), kind="gitdir") if common else None


def _repo_name(key: Key) -> str:
    return PurePosixPath(key.key.removesuffix("/.git")).name


def _find(key: str) -> ProjectMeta | None:
    # O(projects): reads every project.md. Fine for tens of projects.
    return next((p for p in list_projects() if any(k.key == key for k in p.keys)), None)


def _ticket(pattern: str | None, branch: str | None) -> str | None:
    if not pattern or not branch:
        return None
    m = re.search(pattern, branch, re.IGNORECASE)
    return m.group(0).upper() if m else None


def _container_key(d: Path) -> Key | None:
    """The single remote shared by every git checkout directly under d, else None."""
    keys = {k.key: k for c in d.iterdir() if c.is_dir() and (c / ".git").exists() and (k := _repo_key(c))}
    if len(keys) != 1:
        return None
    (key,) = keys.values()
    return key if key.kind == "remote" else None


def _dir_key(cwd: Path) -> Key:
    return Key(key=str(cwd), kind="dir")


def _by_dir_key(cwd: Path) -> ProjectMeta | None:
    paths = {str(cwd), *map(str, cwd.parents)}
    best: tuple[int, ProjectMeta] | None = None
    for p in list_projects():
        for k in p.keys:
            if k.kind == "dir" and k.key in paths and (best is None or len(k.key) > best[0]):
                best = (len(k.key), p)
    return best[1] if best else None


def resolve(cwd: str | Path) -> Project | Unregistered | None:
    """Map cwd to a project. Side effect: a container of a known repo gets its dir key saved."""
    cwd = Path(cwd).expanduser().resolve()
    if key := _repo_key(cwd):
        meta = _find(key.key)
        if meta is None:
            return Unregistered(key, [_repo_name(key)])
        branch = _git(cwd, "branch", "--show-current") or None
        return Project(slug=meta.slug, branch=branch, ticket=_ticket(meta.ticket_pattern, branch))

    if meta := _by_dir_key(cwd):
        return Project(slug=meta.slug)

    key = _container_key(cwd)
    if key is None:
        return None
    meta = _find(key.key)
    if meta is None:
        return Unregistered(key, list(dict.fromkeys([cwd.name, _repo_name(key)])))
    meta.keys.append(_dir_key(cwd))
    save_project(meta)
    return Project(slug=meta.slug)


def register(cwd: str | Path, slug: str) -> ProjectMeta:
    """Register the repo or container at cwd under the slug the owner chose.

    Idempotent by key: if the repo is already registered, that project is returned
    under its existing slug, so callers use the returned meta.slug.
    """
    cwd = Path(cwd).expanduser().resolve()
    if key := _repo_key(cwd):
        keys = [key]
    elif key := _container_key(cwd):
        keys = [key, _dir_key(cwd)]
    else:
        raise ValueError(f"{cwd} is not a git repo or a container of one repo's checkouts")
    if existing := _find(key.key):
        # Raced with another session: still record this container's dir key.
        if missing := [k for k in keys if k.key not in {e.key for e in existing.keys}]:
            existing.keys.extend(missing)
            save_project(existing)
        return existing
    return create_project(ProjectMeta(slug=slug, name=_repo_name(key), keys=keys))
