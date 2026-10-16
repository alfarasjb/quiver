import argparse
import json
import re
import sys
import tempfile
from datetime import date
from pathlib import Path

from quiver import memory
from quiver.project import Project, Unregistered, resolve
from quiver.render import render_activity, render_board, render_context, render_unregistered
from quiver.store import check_slug, get_project, log_session_event


NUDGE = (
    "Before you stop: update this workstream with set_status (status + next step) and record any "
    "decisions, facts or gotchas from this session. If the board is already current, just stop."
)
_EDITS = {"Edit", "Write", "NotebookEdit"}
_SET_STATUS = "mcp__quiver__set_status"
# Quoted names only, so the prefilter holds whatever JSON separators the transcript uses.
_TOOL_NAMES = tuple(f'"{n}"' for n in (*_EDITS, _SET_STATUS))


def _resolve_and_log(hook: dict) -> tuple[Project | Unregistered | None, str | None]:
    """Resolve the hook's cwd; for a project, log a session event with its bound workstream."""
    found = resolve(hook["cwd"])
    if not isinstance(found, Project):
        return found, None
    bound = memory.find_by_branch(found.slug, found.branch)
    name = bound.name if bound else None
    if session := hook.get("session_id"):
        log_session_event(session, found.slug, name, hook["cwd"])
    return found, name


def _context(cwd: str | None) -> str:
    """Session-start memory for cwd. Run by the SessionStart hook with its JSON on stdin."""
    hook = {"cwd": cwd} if cwd else json.load(sys.stdin)
    found, name = _resolve_and_log(hook)  # logs before rendering, so session time survives a render error
    if isinstance(found, Unregistered):
        return render_unregistered(found, hook["cwd"])
    if found is None:
        return ""
    return render_context(found.slug, memory.context(found.slug, name))


def _edited_since_status(transcript: str) -> bool:
    """True if the session used Edit/Write/NotebookEdit after its last quiver set_status."""
    dirty = False
    with open(transcript, encoding="utf-8", errors="replace") as f:
        for line in f:  # streamed: transcripts reach MBs; the substring test skips nearly every line
            if '"tool_use"' not in line or not any(name in line for name in _TOOL_NAMES):
                continue
            try:
                content = json.loads(line)["message"]["content"]
            except (ValueError, KeyError, TypeError):
                continue
            for item in content if isinstance(content, list) else []:
                if isinstance(item, dict) and item.get("type") == "tool_use":
                    if item.get("name") in _EDITS:
                        dirty = True
                    elif item.get("name") == _SET_STATUS:
                        dirty = False
    return dirty


def _stop(hook: dict) -> str:
    """Stop hook: log the turn; once per session, ask Claude to update the board."""
    found, _ = _resolve_and_log(hook)
    session, transcript = str(hook.get("session_id", "")), hook.get("transcript_path")
    if (
        not isinstance(found, Project)
        or hook.get("stop_hook_active")
        or not transcript
        or not re.fullmatch(r"[\w-]+", session)  # used as a filename below
        or not _edited_since_status(transcript)
    ):
        return ""
    marker = Path(tempfile.gettempdir()) / "quiver-nudged" / session
    marker.parent.mkdir(exist_ok=True)
    try:
        with open(marker, "x"):  # atomic: only the first nudge of a session wins
            pass
    except FileExistsError:
        return ""
    return json.dumps({"decision": "block", "reason": NUDGE})


def _slug(value: str) -> str:
    try:
        check_slug(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from e
    if not get_project(value):
        raise argparse.ArgumentTypeError(f"no project {value!r}")
    return value


def _day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"{value!r} is not a date: use YYYY-MM-DD") from e


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="quiver", description="Project memory for Claude Code sessions.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help="Run the MCP server over stdio.")
    context = commands.add_parser("context", help="Print session-start memory (reads hook JSON on stdin).")
    context.add_argument("--cwd", help="Directory to resolve instead of reading hook JSON.")
    commands.add_parser("stop", help="Stop hook: log the turn, ask once for a status update (reads hook JSON).")
    board = commands.add_parser("board", help="Show open workstreams.")
    board.add_argument("--project", type=_slug)
    log = commands.add_parser("log", help="Show a day's activity by session block.")
    log.add_argument("--date", type=_day, help="Local day, YYYY-MM-DD. Defaults to today.")
    log.add_argument("--project", type=_slug)
    commands.add_parser("ui", help="Open the board in a window that starts Claude Code sessions.")
    args = parser.parse_args(argv)

    if args.command == "serve":
        # Imported here: FastMCP takes ~0.5s to import, and hook commands must stay fast.
        from quiver.server import mcp

        mcp.run(show_banner=False)
    elif args.command == "context":
        try:
            text = _context(args.cwd)
        except Exception as e:  # a hook must never break the session; Claude sees this line instead
            text = f"quiver: session memory unavailable ({type(e).__name__}: {' '.join(str(e).split())[:200]})"
        if text:
            print(text)
    elif args.command == "stop":
        try:
            out = _stop(json.load(sys.stdin))
        except Exception as e:  # a hook must never break the session; stderr stays out of Claude's context
            print(f"quiver stop: {type(e).__name__}: {e}", file=sys.stderr)
            out = ""
        if out:
            print(out)
    elif args.command == "board":
        print(render_board(memory.board(args.project)))
    elif args.command == "log":
        day = args.date or date.today()
        print(render_activity(day, memory.activity(day, args.project)))
    elif args.command == "ui":
        from quiver.ui import run

        run()
