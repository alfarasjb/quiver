# quiver

Personal project memory for Claude Code: an MCP server plus a CLI for hooks. PRD in Notion: "PRD — quiver (Global Project Memory MCP)".

## Layout

- `src/quiver/store.py`: the only module that knows the file format (markdown + YAML frontmatter, `_sessions/*.jsonl`).
- `src/quiver/project.py`: cwd → project (normalized origin, `gitdir` for repos with no origin, container `dir` keys).
- `src/quiver/memory.py`: workstream logic (set_status, binding, stacks, board, context, activity).
- `src/quiver/render.py`: the one markdown renderer, shared by tools and hooks.
- `src/quiver/server.py`: FastMCP tools. Docstrings and `Field` descriptions are the tool schema Claude reads: treat them as product copy.
- `src/quiver/__init__.py`: CLI (`serve`, `context`, `stop`, `board`, `log`).

Memory lives in a separate repo: `$QUIVER_HOME`, default `~/dev/general/quiver-memory`. Never write to it from tests or experiments; point `QUIVER_HOME` at a temp dir.

## Contracts (changing these breaks stored data)

- Frontmatter field names and enum values in `store.py` models.
- Status-log line: `- <UTC_STAMP> <status> <free text>` (`memory._log_line`, parsed by `_LOG_LINE`).
- Session line: `{"time": UTC_STAMP, "session_id", "project", "workstream"}` in `_sessions/<UTC date>.jsonl`.
- Remote key format: lowercase `host/path`.

## Rules

- No boolean fields: state is a `Literal` enum validated by pydantic.
- Hook commands (`context`, `stop`) must never fail the session: catch-all, exit 0, and stay fast (< 150ms). Never import `quiver.server` (FastMCP, ~0.5s) outside `serve`.
- Deliberate shortcuts carry a `ponytail:` comment naming the ceiling and the upgrade path.

## Commands

- `uv run pytest -q`
- `.venv/bin/quiver board | log [--date YYYY-MM-DD] | context --cwd PATH` (`context` and `stop` also read hook JSON on stdin)
- Hooks are configured in `~/.claude/settings.json` and call `.venv/bin/quiver` by absolute path; run `uv sync` after moving the repo.
