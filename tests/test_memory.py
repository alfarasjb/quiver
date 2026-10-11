import re
from datetime import UTC, date, datetime, timedelta, timezone
from typing import get_args

import pytest

from quiver import memory, store
from quiver.render import render_activity
from quiver.memory import activity, board, chain, context, find_by_branch, set_status
from quiver.store import (
    Entry,
    Key,
    ProjectMeta,
    WorkstreamStatus,
    add_entry,
    create_project,
    get_workstream,
    list_workstreams,
    log_session_event,
    session_events,
    supersede,
)

def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


LOG_LINE = re.compile(rf"^- \d{{4}}-\d\d-\d\dT\d\d:\d\d:\d\dZ ({'|'.join(get_args(WorkstreamStatus))}) ")


def test_set_status_lifecycle():
    ws = set_status("p", "in_progress", "build\nthe store", ticket="ABC-533", branch="jay/abc-533-foo", title="Store")
    assert ws.name == "abc-533"
    set_status("p", "waiting", "merge #42", ticket="ABC-533", blocker="Jay", watch="CI green")
    set_status("p", "waiting", "merge #42", name="abc-533", blocker="")
    done = set_status("p", "done", "nothing", name="abc-533")

    assert get_workstream("p", "abc-533") == done
    assert (done.title, done.blocker, done.watch) == ("Store", None, "CI green")
    assert done.closed_at is not None
    lines = done.body.strip().splitlines()
    assert [LOG_LINE.match(line).group(1) for line in lines] == ["in_progress", "waiting", "waiting", "done"]
    assert "build the store" in lines[0] and "blocker: Jay" in lines[1] and "blocker" not in lines[2]

    assert set_status("p", "in_progress", "x", branch="jay/foo").name == "jay-foo"
    on_main = set_status("p", "needs_me", "x", branch="main", name="ask-sam")
    assert (on_main.name, on_main.branch) == ("ask-sam", None)
    with pytest.raises(ValueError):
        set_status("p", "in_progress", "x", branch="main")


def test_binding_reuses_the_branch_workstream():
    set_status("p", "in_progress", "start", branch="jay/abc-1-x")
    assert find_by_branch("p", "jay/abc-1-x").name == "jay-abc-1-x"
    assert find_by_branch("p", "main") is None

    # The ticket arrives later: same workstream, no duplicate.
    set_status("p", "waiting", "review", branch="jay/abc-1-x", ticket="ABC-1")
    assert [w.name for w in list_workstreams("p")] == ["jay-abc-1-x"]
    with pytest.raises(ValueError):
        set_status("p", "in_progress", "dup", name="other", branch="jay/abc-1-x")

    set_status("p", "done", "merged", branch="jay/abc-1-x")
    assert find_by_branch("p", "jay/abc-1-x") is None


def test_chain_and_cycle_guard():
    for n, base in [("a", "main"), ("b", "a"), ("c", "b")]:
        set_status("p", "in_progress", "x", name=n, branch=n, base=base)
    wss = list_workstreams("p")
    b = get_workstream("p", "b")
    assert [w.name for w in chain(b, wss)] == ["a", "b", "c"]

    set_status("q", "in_progress", "x", name="x", branch="x", base="y")
    set_status("q", "in_progress", "x", name="y", branch="y", base="x")
    loop = list_workstreams("q")
    assert sorted(w.name for w in chain(loop[0], loop)) == ["x", "y"]
    assert sum(len(stack) for _, stack in board("q")["active"]) == 2


def test_board_groups(monkeypatch):
    set_status("p", "needs_me", "merge", name="base-pr", branch="base-pr")
    set_status("p", "in_progress", "build", name="top-pr", branch="top-pr", base="base-pr")
    set_status("p", "done", "shipped", name="old")
    set_status("p", "needs_me", "ask Sam", name="ask-sam", branch="main")
    set_status("p", "in_progress", "build", name="feat", branch="feat", base="main")
    real_now = store.now
    with monkeypatch.context() as m:
        m.setattr(memory, "now", lambda: real_now() - timedelta(days=4))
        set_status("p", "in_progress", "forgotten", name="stale-one")

    groups = board("p")
    assert list(groups) == ["needs_me", "waiting", "active", "stale"]
    names = {g: [[w.name for w in stack] for _, stack in stacks] for g, stacks in groups.items()}
    # A workstream on main is not the parent of features based on main.
    assert names == {"needs_me": [["ask-sam"], ["base-pr", "top-pr"]], "waiting": [], "active": [["feat"]], "stale": [["stale-one"]]}


def test_context_scoping_and_caps():
    set_status("p", "in_progress", "x", name="a", branch="a", base="main")
    set_status("p", "in_progress", "x", name="b", branch="b", base="a")
    set_status("p", "in_progress", "x", name="z", branch="z")
    add_entry("p", Entry(kind="fact", title="pnpm only"))
    add_entry("p", Entry(kind="fact", title="scoped fact", workstream="b"))
    on_a = Entry(kind="decision", title="ship the stack", workstream="a")
    add_entry("p", on_a)
    add_entry("p", Entry(kind="decision", title="unrelated", workstream="z"))
    old = Entry(kind="decision", title="old call")
    add_entry("p", old)
    supersede("p", old.id, Entry(kind="decision", title="new call"))
    for i in range(16):
        add_entry("p", Entry(kind="gotcha", title=f"gotcha {i}", created_at=store.now() + timedelta(seconds=i)))

    ctx = context("p", "b")
    assert [e.title for e in ctx.facts] == ["pnpm only"]
    assert [e.title for e in ctx.decisions] == ["new call"]
    assert [e.title for e in ctx.chain_decisions] == ["ship the stack"]
    assert [w.name for w in ctx.chain] == ["a", "b"]
    assert len(ctx.gotchas) == 15 and ctx.gotchas[0].title == "gotcha 15"
    assert context("p").workstream is None


def test_activity_local_day_by_period(monkeypatch):
    ph = timezone(timedelta(hours=8))  # local day 2026-10-08 = 10-07T16:00Z .. 10-08T16:00Z

    for slug in ("alpha", "beta"):
        create_project(ProjectMeta(slug=slug, name=slug, keys=[Key(key=slug, kind="remote")]))
    add_entry("alpha", Entry(kind="decision", title="early call", created_at=utc(2026, 10, 7, 17)))  # 01:00 local
    add_entry("beta", Entry(kind="fact", title="morning fact", created_at=utc(2026, 10, 8, 3)))  # 11:00 local
    add_entry("alpha", Entry(kind="fact", title="previous day", created_at=utc(2026, 10, 7, 15, 59)))
    add_entry("alpha", Entry(kind="fact", title="next day", created_at=utc(2026, 10, 8, 16)))
    with monkeypatch.context() as m:
        m.setattr(memory, "now", lambda: utc(2026, 10, 8, 5, 30))  # 13:30 local
        set_status("alpha", "waiting", "merge #42", name="abc-533", blocker="Sam")

    # s1 crosses UTC midnight (two files) and overlaps s2: 07:00-10:00 local, counted once.
    for sid, project, at in [
        ("s1", "alpha", utc(2026, 10, 7, 23)), ("s1", "alpha", utc(2026, 10, 8, 1)),
        ("s2", "alpha", utc(2026, 10, 8, 0, 30)), ("s2", "alpha", utc(2026, 10, 8, 2)),
        ("s3", "beta", utc(2026, 10, 8, 3, 30)), ("s3", "beta", utc(2026, 10, 8, 5)),  # 11:30-13:00 local
        ("s4", "alpha", utc(2026, 10, 8, 17)),  # next local day
    ]:  # fmt: skip
        log_session_event(sid, project, None, at=at)
    # s5 ends exactly at local 06:00: Night only, no empty 06:00 sliver in Morning.
    log_session_event("s5", "beta", None, at=utc(2026, 10, 7, 21))
    log_session_event("s5", "beta", None, at=utc(2026, 10, 7, 22))
    # Torn or mangled lines cost one event each, never the day.
    with (store.home() / "_sessions" / "2026-10-08.jsonl").open("a") as f:
        f.write('not json\n{"time": "2026-10-08T06:00:00", "session_id": "x", "project": "alpha"}\n')
        f.write('{"time": "2026-10-08T06:00:00Z", "project": "alpha"}\n')

    periods = activity(date(2026, 10, 8), tz=ph)
    assert [p.name for p in periods] == ["Night", "Morning", "Afternoon"]
    night, morning, afternoon = periods
    assert [i[2].title for i in night.items] == ["early call"]
    assert [(s.astimezone(ph).hour, e.astimezone(ph).hour) for s, e in night.spans] == [(5, 6)]
    assert [(s.astimezone(ph).hour, e.astimezone(ph).hour) for s, e in morning.spans] == [(7, 10), (11, 12)]
    assert [(s.astimezone(ph).hour, e.astimezone(ph).hour) for s, e in afternoon.spans] == [(12, 13)]
    assert afternoon.items[0][2].text.startswith("waiting next: merge #42")

    text = render_activity(date(2026, 10, 8), periods, ph)
    assert "## Morning · 07:00–10:00, 11:30–12:00" in text
    assert "- beta · 11:00 [fact] morning fact" in text
    assert "- alpha · 13:30 abc-533: waiting next: merge #42 | blocker: Sam" in text
    assert len(session_events(utc(2026, 10, 6), utc(2026, 10, 10))) == 9  # spans 4 UTC files
