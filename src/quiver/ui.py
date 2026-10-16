"""`quiver ui`: the board in a native window whose buttons open Claude Code sessions in Terminal."""

import shlex
import subprocess
import tkinter as tk
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import font, messagebox, ttk
from typing import Literal

from quiver import memory
from quiver.render import GROUP_TITLES
from quiver.store import SessionEvent, Workstream, get_project, get_workstream, now, session_events

# shortcut: only the last 30 days of session lines are scanned; widen if old workstreams lose their Resume button.
LOOKBACK = timedelta(days=30)

Mode = Literal["new", "resume"]

STATUS_WORDS = {"needs_me": "Needs you", "waiting": "Waiting", "in_progress": "In progress", "not_started": "Not started"}
# One color per status, (light, dark): Tk has no semantic orange/green/blue, so both modes are spelled out.
STATUS_COLORS = {"needs_me": ("#A35F00", "#F0B35A"), "waiting": ("#1F63B8", "#6CB2F5"), "in_progress": ("#2E7D45", "#74C68A")}


@dataclass
class Target:
    cwd: str | None  # where a new session opens; None when no local directory is known
    resume: SessionEvent | None  # the workstream's newest session that logged its cwd


def _recent_sessions() -> tuple[dict[tuple[str, str], SessionEvent], dict[str, SessionEvent]]:
    """Newest cwd-bearing session per (project, workstream), and per project."""
    by_ws: dict[tuple[str, str], SessionEvent] = {}
    by_project: dict[str, SessionEvent] = {}
    # +1s: stamps are whole seconds and the window end is exclusive. Oldest first, so later events overwrite.
    for ev in session_events(now() - LOOKBACK, now() + timedelta(seconds=1)):
        if ev.cwd and Path(ev.cwd).is_dir():
            by_project[ev.project] = ev
            if ev.workstream:
                by_ws[(ev.project, ev.workstream)] = ev
    return by_ws, by_project


def target(slug: str, ws: Workstream, sessions: tuple[dict[tuple[str, str], SessionEvent], dict[str, SessionEvent]]) -> Target:
    by_ws, by_project = sessions
    resume = by_ws.get((slug, ws.name))
    project = get_project(slug)
    dir_keys = [k.key for k in project.keys if k.kind == "dir"] if project else []
    last = by_project.get(slug)
    candidates = [ws.location, resume and resume.cwd, last and last.cwd, *dir_keys]
    return Target(next((c for c in candidates if c and Path(c).is_dir()), None), resume)


def _open_workstream(slug: str, name: str) -> Workstream:
    ws = get_workstream(slug, name)
    if ws is None or ws.closed_at is not None:
        raise ValueError(f"no open workstream {name!r} in {slug!r}")
    return ws


def command(slug: str, name: str, mode: Mode) -> str:
    """The shell line that opens this workstream's session; raises ValueError if it can't."""
    ws = _open_workstream(slug, name)
    t = target(slug, ws, _recent_sessions())
    if mode == "resume":
        if t.resume is None:
            raise ValueError(f"no logged session for {name!r}")
        return f"cd {shlex.quote(t.resume.cwd or '')} && claude --resume {shlex.quote(t.resume.session_id)}"
    if t.cwd is None:
        raise ValueError(f"no local directory for {name!r}")
    prompt = (
        f"Pick up quiver workstream {name} in project {slug}: load it with "
        f"get_memory(project={slug!r}, workstream={name!r}) and continue from its next step."
    )
    if ws.status == "needs_me":
        prompt += " It is waiting on my decision: summarize what I need to decide, then wait for me."
    return f"cd {shlex.quote(t.cwd)} && claude {shlex.quote(prompt)}"


def verify_command(slug: str, names: list[str]) -> str:
    """A session that checks each workstream's real state and corrects its status, without doing the work."""
    sessions = _recent_sessions()
    workstreams = [_open_workstream(slug, n) for n in names]
    cwd = next((t.cwd for ws in workstreams if (t := target(slug, ws, sessions)).cwd), None)
    if cwd is None:
        raise ValueError(f"no local directory for project {slug!r}")
    prompt = (
        f"Verify the status of these quiver workstreams in project {slug}: {', '.join(names)}. "
        f"For each, load it with get_memory(project={slug!r}, workstream=<name>), then check the evidence its next step "
        "points at: git log and branches, PRs with gh, tickets in Linear. If the work is finished, set_status done. "
        "If it moved on, set_status with the real status and next step. If you can't tell, leave it and say why. "
        "Don't do the work itself. End with a table: workstream, old status, new status, evidence."
    )
    return f"cd {shlex.quote(cwd)} && claude {shlex.quote(prompt)}"


def _open_terminal(line: str) -> None:
    # The line goes in as argv, so AppleScript never parses it.
    script = ["on run argv", 'tell application "Terminal" to do script (item 1 of argv)', 'tell application "Terminal" to activate', "end run"]
    subprocess.run(["osascript", *(a for s in script for a in ("-e", s)), line], check=True, capture_output=True)


def _ago(at: datetime) -> str:
    days = (now().astimezone().date() - at.astimezone().date()).days
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    return f"{days} days ago" if days < 30 else f"{at:%Y-%m-%d}"


def run() -> None:
    root = tk.Tk()
    root.title("Quiver")
    root.geometry("1080x640")
    root.minsize(760, 420)
    dark = root.tk.call("::tk::unsupported::MacWindowStyle", "isdark", root._w) == 1
    base = font.nametofont("TkDefaultFont").actual()
    title_font = font.Font(family=base["family"], size=20, weight="bold")
    strong = font.Font(family=base["family"], size=base["size"], weight="bold")
    small = font.Font(family=base["family"], size=base["size"] - 2, weight="bold")
    ttk.Style().configure("Treeview", rowheight=30)
    secondary = "systemSecondaryLabelColor"

    split = ttk.PanedWindow(root, orient="horizontal")
    split.pack(fill="both", expand=True)

    left = ttk.Frame(split)
    toolbar = ttk.Frame(left, padding=(10, 8))
    group_by = tk.StringVar(value="status")
    ttk.Label(toolbar, text="Group by", foreground=secondary).pack(side="left", padx=(0, 8))
    list_frame = ttk.Frame(left)
    tree = ttk.Treeview(list_frame, columns=("project", "status", "updated"), selectmode="browse")
    for col, label, width in (("#0", "Workstream", 300), ("project", "Project", 130), ("status", "Status", 100), ("updated", "Updated", 95)):
        tree.heading(col, text=label, anchor="w")
        tree.column(col, width=width, minwidth=60, anchor="w", stretch=col == "#0")
    tree.tag_configure("group", font=strong, foreground=secondary)
    scroll = ttk.Scrollbar(list_frame, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=scroll.set)
    toolbar.pack(fill="x")
    list_frame.pack(fill="both", expand=True)
    tree.pack(side="left", fill="both", expand=True)
    scroll.pack(side="right", fill="y")
    split.add(left, weight=3)

    right = ttk.Frame(split, padding=(24, 20))
    split.add(right, weight=2)
    empty = ttk.Label(right, text="Select a workstream", foreground=secondary)
    detail = ttk.Frame(right)
    title = ttk.Label(detail, font=title_font, justify="left")
    status = ttk.Label(detail, font=strong)
    meta = ttk.Label(detail, foreground=secondary)
    body_head = ttk.Label(detail, font=small, foreground=secondary)
    body = ttk.Label(detail, justify="left")
    dir_head = ttk.Label(detail, text="DIRECTORY", font=small, foreground=secondary)
    dir_text = ttk.Label(detail, justify="left")
    bar = ttk.Frame(detail)
    buttons = [ttk.Button(bar) for _ in range(3)]  # rightmost first; buttons[0] is the primary action

    bar.pack(side="bottom", fill="x")  # packed first so long text can't push the buttons out of the window
    title.pack(anchor="w")
    status.pack(anchor="w", pady=(6, 0))
    meta.pack(anchor="w", pady=(2, 0))
    body_head.pack(anchor="w", pady=(22, 4))
    body.pack(anchor="w", fill="x")
    dir_head.pack(anchor="w", pady=(18, 4))
    dir_text.pack(anchor="w", fill="x")

    def wrap(e: tk.Event) -> None:
        for label in (title, body, dir_text):
            label.configure(wraplength=max(e.width - 48, 200))

    right.bind("<Configure>", wrap)

    Row = tuple[str, Workstream, Target]
    rows: dict[str, Row] = {}  # iid -> workstream row
    groups: dict[str, tuple[str, list[Row]]] = {}  # iid -> (heading, its rows)

    def run_lines(lines: list[str]) -> None:
        try:
            for line in lines:
                _open_terminal(line)
        except subprocess.CalledProcessError as e:
            messagebox.showerror("Could not open Terminal", e.stderr.decode().strip() or str(e), parent=root)

    def launch(mode: Mode) -> None:
        sel = tree.selection()
        if sel and sel[0] in rows:
            slug, ws, _ = rows[sel[0]]
            try:
                run_lines([command(slug, ws.name, mode)])
            except ValueError as e:
                messagebox.showerror("Could not open the session", str(e), parent=root)

    def verify(members: list[Row]) -> None:
        """One verify session per project; projects with no local directory are reported, not dropped silently."""
        by_project: dict[str, list[str]] = {}
        for slug, ws, _ in members:
            by_project.setdefault(slug, []).append(ws.name)
        lines, skipped = [], []
        for slug, names in by_project.items():
            try:
                lines.append(verify_command(slug, names))
            except ValueError as e:
                skipped.append(str(e))
        run_lines(lines)
        if skipped:
            messagebox.showinfo(
                "Some projects were skipped",
                "\n".join(skipped) + "\n\nRun one Claude Code session in each project, then verify again.",
                parent=root,
            )

    def set_buttons(actions: list[tuple[str, object, bool]]) -> None:
        for b in buttons:
            b.pack_forget()
        for i, (b, (label, fn, enabled)) in enumerate(zip(buttons, actions)):
            b.configure(text=label, command=fn, default="active" if i == 0 else "normal")
            b.state(["!disabled" if enabled else "disabled"])
            b.pack(side="right", padx=(0, 0 if i == 0 else 8))

    def show(_: object = None) -> None:
        sel = tree.selection()
        iid = sel[0] if sel else None
        if iid not in rows and iid not in groups:
            detail.pack_forget()
            empty.pack(expand=True)
            return
        empty.pack_forget()
        detail.pack(fill="both", expand=True)
        if iid in groups:
            heading, members = groups[iid]
            projects = sorted({slug for slug, _, _ in members})
            title.configure(text=heading)
            status.configure(text="")
            meta.configure(text=f"{len(members)} open · {len(projects)} project{'s' * (len(projects) != 1)}")
            body_head.configure(text="WORKSTREAMS")
            body.configure(text="\n".join(f"{ws.title or ws.name}  ·  {STATUS_WORDS.get(ws.status, ws.status)}" for _, ws, _ in members))
            dir_head.configure(text="VERIFY")
            dir_text.configure(text="Opens one Claude session per project that checks each item against commits, PRs and tickets, "
                               "then marks it done or corrects its status and next step. It doesn't do the work.")
            set_buttons([(f"Verify all {len(members)}", lambda: verify(members), any(t.cwd for _, _, t in members))])
            return
        slug, ws, t = rows[iid]
        title.configure(text=ws.title or ws.name)
        colors = STATUS_COLORS.get(ws.status)
        status.configure(text=STATUS_WORDS.get(ws.status, ws.status), foreground=colors[dark] if colors else secondary)
        meta.configure(text=f"{slug} · {ws.name} · updated {_ago(ws.updated_at)}")
        body_head.configure(text="NEXT STEP")
        body.configure(text=ws.next_step or "No next step recorded.")
        dir_head.configure(text="DIRECTORY")
        dir_text.configure(text=t.cwd or "No local directory yet. Run one Claude Code session in this project and it will appear here.")
        # The likeliest next click is primary: resume when there is a session to go back to.
        start = [("Resume", lambda: launch("resume"), True), ("New session", lambda: launch("new"), bool(t.cwd))]
        actions = start if t.resume else start[1:]
        set_buttons([*actions, ("Verify status", lambda: verify([(slug, ws, t)]), bool(t.cwd))])

    def load(_: object = None) -> None:
        sel = tree.selection()
        keep = sel[0] if sel else None
        tree.delete(*tree.get_children())
        rows.clear()
        groups.clear()
        sessions = _recent_sessions()
        # Board order is most urgent first, so grouping by project keeps urgent items, and projects, on top.
        ordered = [(g, slug, ws) for g, stacks in memory.board().items() for slug, stack in stacks for ws in stack]
        buckets: dict[str, list[tuple[str, Workstream]]] = {}
        for g, slug, ws in ordered:
            heading = GROUP_TITLES[g] if group_by.get() == "status" else slug
            buckets.setdefault(heading, []).append((slug, ws))
        for heading, members in buckets.items():
            gid = tree.insert("", "end", iid=f"group:{heading}", text=f"{heading}  {len(members)}", open=True, tags=("group",))
            group_rows = []
            for slug, ws in members:
                values = (slug, STATUS_WORDS.get(ws.status, ws.status), _ago(ws.updated_at))
                iid = tree.insert(gid, "end", iid=f"{slug}/{ws.name}", text=ws.title or ws.name, values=values)
                rows[iid] = (slug, ws, target(slug, ws, sessions))
                group_rows.append(rows[iid])
            groups[gid] = (heading, group_rows)
        if keep and tree.exists(keep):
            tree.selection_set(keep)
            tree.see(keep)
        show()

    for value, label in (("status", "Status"), ("project", "Project")):
        ttk.Radiobutton(toolbar, text=label, value=value, variable=group_by, command=load).pack(side="left", padx=(0, 10))

    def default_action(_: object = None) -> None:
        sel = tree.selection()
        if sel and sel[0] in rows:
            launch("resume" if rows[sel[0]][2].resume else "new")

    tree.bind("<<TreeviewSelect>>", show)
    tree.bind("<Double-1>", default_action)
    tree.bind("<Return>", default_action)
    root.bind("<Command-r>", load)
    root.bind("<Activate>", lambda e: load() if e.widget is root else None)  # coming back to the window shows fresh status
    load()
    root.mainloop()
