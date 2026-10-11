# quiver

Project memory for Claude Code. Every session starts knowing where your work stands, Claude records decisions as they happen, and one board shows everything in flight across projects.

Memory lives in `~/dev/general/quiver-memory` as markdown files you can open and edit. Code lives here.

Optional alias, used below:

```sh
alias quiver=~/dev/general/quiver/.venv/bin/quiver
```

## The model in one minute

- **Project:** one repo. Every worktree of it belongs to the same project, and so does the container folder you open sessions in (`~/dev/work/acme`).
- **Workstream:** one unit of in-flight work, usually one ticket or branch (`abc-533`). It can also have no branch at all (`ask-sam-sg`). Each one has a status, a next step, and optionally a blocker and what you're watching for.
- **Entries:** what Claude records along the way:
  - **decisions:** what was chosen, and why
  - **facts:** commands, environments, conventions
  - **gotchas:** traps and their fixes
  - **events:** a PR merged, QA passed, someone answered
- **Board:** every open workstream, grouped by who has to act: **Needs you**, **Waiting**, **Active**, **Stale** (no update in 3+ days).

Two hooks make it automatic:

| Hook | When | What it does |
|---|---|---|
| SessionStart | A session opens, resumes or compacts | Loads the project's memory into Claude: the board, facts, gotchas, and the current workstream with its stack |
| Stop | Every turn ends | Once per session, if Claude edited files without updating the status, it asks Claude to update it |

## A day with quiver

### Morning: what do I do today?

```sh
quiver board    # everything in flight, all projects
```

Read the board top-down:

- **Needs you:** things only you can unblock, like merges, decisions and reviews. Do these first.
- **Waiting:** blocked on someone else. Check the `watch` condition (e.g. "#41 merged") and nudge people if needed.
- **Active:** in progress. Pick up where the next step says.
- **Stale:** nothing has happened in 3+ days. Either resume it or close it.

You can also ask any Claude session "what's on my board?" or "what should I work on today?".

### Starting or resuming work

Open a session the way you do now, in the container folder or in a worktree. Memory is loaded before you type anything.

- **Existing ticket:** say "let's do ABC-533". Claude loads that workstream, its stack, and the decisions made on it. This replaces `context-dump`.
- **New ticket:** say "starting ABC-610, branch jay/abc-610-…, stacked on abc-573". Claude creates the workstream, and the board shows it in the stack.
- **No ticket:** say "track 'ask Sam about the staging security group' as a workstream, waiting on Sam". It goes on the board like any other.
- **Unregistered project:** on first use in a new repo, Claude asks which name to register it under. Answer once.

### While you work

You don't run anything. Claude records as it goes:

| You say or decide | Claude calls |
|---|---|
| "let's go with X because Y" | `record_decision` |
| a fix after a confusing error | `add_gotcha` |
| "staging DB is on :5433" | `add_fact` |
| "PR #42 merged", "QA passed" | `record_event` |
| "I'm blocked on Sam", "ready for my review" | `set_status` (waiting / needs_me) |

You can always be explicit: "record that as a decision", "log this gotcha", "mark this waiting on CI for #42".

To check what was decided before: "did we already decide how to handle API credentials?". Claude searches all projects.

### Switching between parallel sessions

Each session keeps its own workstream's status current. Before you switch, glance at `quiver board`; it's always the current state. A session you return to reloads its workstream on resume, so you don't need to re-explain anything.

### When context runs out

You don't write a handoff. The workstream file is the handoff:
- When a session compacts, memory is reloaded automatically.
- In a new session in the same worktree, Claude binds to the same workstream and continues from its next step.

This replaces `handoff`.

### End of day

Before you stop:
1. Make sure nothing is wrongly "active". Tell the session "mark this done" or "waiting on X".
2. Commit and push the memory repo, which stays manual:
   ```sh
   git -C ~/dev/general/quiver-memory add -A && git -C ~/dev/general/quiver-memory commit -qm "memory $(date +%F)" && git -C ~/dev/general/quiver-memory push -q
   ```

## Where the old skills went

| Before | Now |
|---|---|
| `/checkpoint` | Automatic. Claude records decisions, facts, gotchas and events as they happen |
| `/context-dump ABC-533` | Automatic at session start; "let's do ABC-533" for a specific one |
| `/handoff` | The workstream's status and next step, kept current by Claude and the Stop nudge |
| `daily` → Start here / Pick up | `quiver board` |
| `STACK.md` status lines | The stack chain on the board. Plans and rules still go in `STACK.md` |

Keep the old skills until the trial week ends, then retire them.

## Per-project setup

Once per project with tickets, add the ticket pattern to its `project.md` so workstreams get named from branches:

```yaml
# ~/dev/general/quiver-memory/acme/project.md
ticket_pattern: abc-\d+
```

Projects without ticketing (like quiver itself) need nothing. Workstreams get short names instead.

## Editing memory by hand

Everything is markdown in `~/dev/general/quiver-memory/<project>/`:
- `workstreams/<name>.md`: frontmatter is the current status; the body is its history.
- `entries/*.md`: one file per decision, fact, gotcha or event.

Edit freely. Keep the frontmatter keys and values valid: status must be one of `not_started`, `in_progress`, `needs_me`, `waiting`, `done`. A broken file shows up as an error naming that file.

## Troubleshooting

| Symptom | Fix |
|---|---|
| A session doesn't know the project | It was opened before quiver was set up. Resume it, or start a new one |
| Claude has no quiver tools | Run `/mcp` and reconnect `quiver`. Check `claude mcp list` shows it connected |
| "not registered with quiver" | Answer Claude's question about the project name |
| A session start shows `quiver: session memory unavailable (...)` | Usually a hand-edited file with bad frontmatter. The message names the error |
| The Stop nudge never appears | It only fires after file edits with no status update since, and only once per session |
| Hooks stop working after moving the repo | Run `uv sync` in the repo. The hooks call `.venv/bin/quiver` by absolute path |
