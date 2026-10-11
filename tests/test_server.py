import asyncio

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from test_project import git, make_repo

from quiver import server
from quiver.store import get_project, list_entries, save_project


def test_unregistered_then_worktree_writes_land_on_bound_workstream(tmp_path):
    main = make_repo(tmp_path / "c" / "widget-main", "git@github.com:Acme/Widget.git")
    wt = tmp_path / "c" / "widget-ABC-533"
    git(main, "worktree", "add", str(wt), "-b", "jay/abc-533-foo")

    with pytest.raises(ToolError, match="register_project"):
        server.record_decision(title="x", rationale="y", cwd=str(wt))
    assert server.register_project(cwd=str(main), slug="widget") == "Registered widget."
    meta = get_project("widget")
    meta.ticket_pattern = r"abc-\d+"
    save_project(meta)

    assert server.set_status(status="in_progress", next_step="wire the server", cwd=str(wt)).startswith(
        "widget: abc-533 is in_progress"
    )
    server.record_decision(title="Markdown tool output", rationale="One renderer for tools and hooks.", cwd=str(wt))
    [decision] = list_entries("widget")
    assert (decision.workstream, decision.branch) == ("abc-533", "jay/abc-533-foo")

    text = server.get_memory(cwd=str(wt))
    assert "Markdown tool output" in text and "wire the server" in text

    with pytest.raises(ToolError, match="pass workstream="):
        server.record_event(title="merged", cwd=str(main))

    # Naming a workstream scopes the entry to it, even for project-default tools.
    assert "workstream abc-533" in server.add_fact(title="scoped", workstream="abc-533", cwd=str(main))
    # Naming another workstream from a worktree does not copy the worktree's branch onto it.
    side = server.set_status(status="waiting", next_step="ask Sam", name="ask-sam", cwd=str(wt))
    assert side.startswith("widget: ask-sam is waiting")


def test_not_a_project(tmp_path):
    with pytest.raises(ToolError, match="pass project="):
        server.record_event(title="x", cwd=str(tmp_path))
    with pytest.raises(ToolError, match="absolute"):
        server.get_memory(cwd="relative/path")
    with pytest.raises(ToolError, match="pass cwd"):
        server.get_memory()


def test_search_across_projects_and_history(tmp_path):
    for slug in ("alpha", "beta"):
        make_repo(tmp_path / slug, f"git@github.com:Acme/{slug}.git")
        server.register_project(cwd=str(tmp_path / slug), slug=slug)
    server.add_gotcha(title="Prisma migrate dev hangs", symptom="hangs in CI", fix="use migrate diff", project="alpha")
    old = server.add_fact(title="Staging uses Prisma 5", project="beta").split()[2]
    server.add_fact(title="Staging uses Prisma 6", supersedes=old, project="beta")

    hits = server.search_memory(query="PRISMA staging")
    assert "Prisma 6" in hits and "Prisma 5" not in hits and "migrate" not in hits
    assert "Prisma 5" in server.search_memory(query="prisma staging", status=None)
    all_prisma = server.search_memory(query="prisma")
    heads = sorted(line.split()[1:3] for line in all_prisma.splitlines() if not line.startswith(" "))
    assert heads == [["alpha", "[gotcha]"], ["beta", "[fact]"]]  # same-second order is arbitrary

    # An explicit project wins over cwd; an unknown project is an error, not an empty result.
    assert "in beta" in server.add_fact(title="cross-project", cwd=str(tmp_path / "alpha"), project="beta")
    with pytest.raises(ToolError, match="list_projects"):
        server.search_memory(query="prisma", project="nope")


def test_mcp_wiring():
    async def call():
        async with Client(server.mcp) as client:
            tools = {t.name for t in await client.list_tools()}
            result = await client.call_tool("board", {})
            return tools, result

    tools, result = asyncio.run(call())
    assert tools == {
        "get_memory", "board", "search_memory", "list_projects", "set_status", "record_decision",
        "add_fact", "add_gotcha", "record_event", "resolve_gotcha", "register_project", "get_activity",
    }  # fmt: skip
    assert result.structured_content is None
    assert result.content[0].text == "No open workstreams."
