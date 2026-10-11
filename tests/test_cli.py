import io
import json
import sys

import pytest
from test_project import git, make_repo

from quiver import main, memory
from quiver.memory import set_status
from quiver.project import register
from quiver.store import home, save_project


def run(capsys, *argv, stdin=None, monkeypatch=None):
    if stdin is not None:
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    main(list(argv))
    return capsys.readouterr().out


def test_context_from_a_worktree(tmp_path, capsys, monkeypatch):
    repo = make_repo(tmp_path / "c" / "widget-main", "git@github.com:Acme/Widget.git")
    wt = tmp_path / "c" / "widget-ABC-533"
    git(repo, "worktree", "add", str(wt), "-b", "jay/abc-533-foo")

    assert "register_project" in run(capsys, "context", "--cwd", str(wt))
    meta = register(repo, "widget")
    meta.ticket_pattern = r"abc-\d+"
    save_project(meta)
    set_status("widget", "in_progress", "wire the hooks", branch="jay/abc-533-foo", ticket="ABC-533")

    hook = json.dumps({"session_id": "abc", "cwd": str(wt), "hook_event_name": "SessionStart", "source": "startup"})
    out = run(capsys, "context", stdin=hook, monkeypatch=monkeypatch)
    assert "## Current workstream" in out and "wire the hooks" in out
    [line] = [json.loads(raw) for f in (home() / "_sessions").iterdir() for raw in f.read_text().splitlines()]
    assert (line["session_id"], line["project"], line["workstream"]) == ("abc", "widget", "abc-533")

    assert run(capsys, "context", "--cwd", str(tmp_path)) == ""


def test_context_never_fails(capsys, monkeypatch, tmp_path):
    assert run(capsys, "context", stdin="garbage", monkeypatch=monkeypatch).startswith("quiver: session memory unavailable")

    make_repo(tmp_path / "r", "git@github.com:Acme/R.git")
    register(tmp_path / "r", "r")
    monkeypatch.setattr(memory, "context", lambda *a: 1 / 0)
    assert "ZeroDivisionError" in run(capsys, "context", "--cwd", str(tmp_path / "r"))


def test_board_and_log(capsys):
    assert run(capsys, "board").strip() == "No open workstreams."
    assert run(capsys, "log", "--date", "2026-10-08").strip() == "No activity on 2026-10-08."
    with pytest.raises(SystemExit):
        main(["log", "--project", "Bad Slug"])
    with pytest.raises(SystemExit):
        main(["log", "--date", "10/08"])


def transcript(path, *tools):
    """A transcript .jsonl shaped like Claude Code's: prompt, a decoy attachment, tool calls and results."""
    lines = [
        {"type": "user", "message": {"role": "user", "content": "do the thing"}},
        {"type": "attachment", "content": 'mentions "name":"Edit" and "tool_use" as plain text'},
    ]
    for i, name in enumerate(tools):
        call = {"type": "tool_use", "id": f"toolu_{i}", "name": name, "input": {"file_path": "/r/a.py"}}
        lines.append({"type": "assistant", "message": {"role": "assistant", "content": [call]}})
        result = {"type": "tool_result", "tool_use_id": f"toolu_{i}", "content": "ok"}
        lines.append({"type": "user", "message": {"role": "user", "content": [result]}})
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")  # spaced separators: prefilter must not care
    return str(path)


def stop(capsys, monkeypatch, **hook):
    return run(capsys, "stop", stdin=json.dumps(hook), monkeypatch=monkeypatch).strip()


def session_lines():
    return [raw for f in (home() / "_sessions").iterdir() for raw in f.read_text().splitlines()]


def test_stop_logs_and_nudges_once(tmp_path, capsys, monkeypatch):
    repo = make_repo(tmp_path / "r", "git@github.com:Acme/R.git")
    register(repo, "r")
    edited = transcript(tmp_path / "edited.jsonl", "Read", "Edit")
    hook = {"cwd": str(repo), "transcript_path": edited, "stop_hook_active": False}

    assert json.loads(stop(capsys, monkeypatch, session_id="s1", **hook))["decision"] == "block"
    assert stop(capsys, monkeypatch, session_id="s1", **hook) == ""  # once per session
    assert len(session_lines()) == 2  # every turn is logged either way

    assert stop(capsys, monkeypatch, session_id="s2", **{**hook, "stop_hook_active": True}) == ""
    updated = transcript(tmp_path / "updated.jsonl", "Edit", "mcp__quiver__set_status")
    assert stop(capsys, monkeypatch, session_id="s3", **{**hook, "transcript_path": updated}) == ""
    stale = transcript(tmp_path / "stale.jsonl", "mcp__quiver__set_status", "Write")
    assert stop(capsys, monkeypatch, session_id="s4", **{**hook, "transcript_path": stale}) != ""
    quiet = transcript(tmp_path / "quiet.jsonl", "Read")
    assert stop(capsys, monkeypatch, session_id="s5", **{**hook, "transcript_path": quiet}) == ""


def test_stop_never_fails(tmp_path, capsys, monkeypatch):
    assert run(capsys, "stop", stdin="garbage", monkeypatch=monkeypatch) == ""
    assert run(capsys, "stop", stdin="[]", monkeypatch=monkeypatch) == ""

    repo = make_repo(tmp_path / "r", "git@github.com:Acme/R.git")
    register(repo, "r")
    missing = str(tmp_path / "nope.jsonl")
    assert stop(capsys, monkeypatch, session_id="s1", cwd=str(repo), transcript_path=missing) == ""
    assert len(session_lines()) == 1
