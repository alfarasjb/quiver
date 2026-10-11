import subprocess

import pytest

from quiver.project import Project, Unregistered, normalize_remote, register, resolve
from quiver.store import get_project, list_projects, save_project


def git(d, *args):
    subprocess.run(["git", "-C", str(d), *args], check=True, capture_output=True)


def make_repo(path, origin):
    path.mkdir(parents=True)
    git(path, "init", "-b", "main")
    git(path, "commit", "--allow-empty", "-m", "init")
    git(path, "remote", "add", "origin", origin)
    return path


@pytest.mark.parametrize(
    "url",
    [
        "git@github.com:Acme/Widget.git",
        "https://github.com/Acme/Widget.git",
        "https://github.com/Acme/Widget/",
        "ssh://git@github.com:22/Acme/Widget.git",
        "  https://github.com/Acme/Widget  ",
    ],
)
def test_normalize_remote(url):
    assert normalize_remote(url) == "github.com/acme/widget"


def test_worktrees_share_project_with_branch_and_ticket(tmp_path):
    main = make_repo(tmp_path / "c" / "widget-main", "git@github.com:Acme/Widget.git")
    wt = tmp_path / "c" / "widget-ABC-533"
    git(main, "worktree", "add", str(wt), "-b", "jay/abc-533-foo")

    found = resolve(main)
    assert isinstance(found, Unregistered) and found.candidates == ["widget"]
    meta = register(main, "widget")
    meta.ticket_pattern = r"abc-\d+"
    save_project(meta)

    assert resolve(main) == Project(slug="widget", branch="main", ticket=None)
    assert resolve(wt) == Project(slug="widget", branch="jay/abc-533-foo", ticket="ABC-533")
    assert len(list_projects()) == 1


def test_container_asks_then_resolves_by_saved_path(tmp_path):
    container = tmp_path / "acme"
    main = make_repo(container / "acme-main", "https://github.com/Acme/acme-platform.git")
    git(main, "worktree", "add", str(container / "acme-ABC-1"), "-b", "jay/abc-1")
    (container / "tickets").mkdir()

    found = resolve(container)
    assert isinstance(found, Unregistered)
    assert found.candidates == ["acme", "acme-platform"]

    register(container, "acme")
    assert {k.key for k in get_project("acme").keys} == {
        "github.com/acme/acme-platform",
        str(container.resolve()),
    }
    assert resolve(container / "tickets") == Project(slug="acme")
    assert resolve(main).slug == "acme"


def test_mixed_container_resolves_to_nothing(tmp_path):
    make_repo(tmp_path / "a", "git@github.com:Acme/A.git")
    make_repo(tmp_path / "b", "git@github.com:Acme/B.git")
    assert resolve(tmp_path) is None
    with pytest.raises(ValueError):
        register(tmp_path, "a")
    assert list_projects() == []
