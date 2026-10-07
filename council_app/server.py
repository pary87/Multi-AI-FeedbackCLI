"""The Council app: five pages served on this computer only (127.0.0.1).

Pages:  /              threads
        /new           new question
        /run           live run (and the result of the last run)
        /thread/{id}   one thread: question, synthesis, rounds, audit trail
        /settings      model levels, time limit, connections, client data, GitHub sync

Everything goes through council_engine, exactly like the terminal commands, so
a thread started here and one started with `python -m council_engine ask` are
the same kind of folder.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from nicegui import app, run, ui

from council_engine.agents import Config, load_config, unwrap_cli_bullets
from council_engine.config_edit import (
    get_levels,
    level_choices,
    seat_kind,
    set_agent_value,
    set_default,
    set_levels,
)
from council_engine.store import CouncilError, Thread, Workspace, slugify

from . import helpers as h
from .jobs import AppState, Job, JobManager, config_with_levels, fmt_duration
from .security import LocalOnly

LOGO_SVG = (
    '<svg width="30" height="30" viewBox="0 0 28 28" fill="none" stroke="#E8EAE7" '
    'stroke-width="1.6" aria-hidden="true"><circle cx="14" cy="7" r="3.5"/>'
    '<circle cx="6.5" cy="20" r="3.5"/><circle cx="21.5" cy="20" r="3.5"/>'
    '<path d="M12.3 10.1l-4 6.8M15.7 10.1l4 6.8M10 20h8"/></svg>'
)

CSS = r"""
:root { --ink:#15181B; --muted:#4A5058; --line:#D5D9D4; --soft:#E3E6E2; --bg:#EDEFEC; --brand:#1B4F8A; }
body { background: var(--bg); color: var(--ink);
       font-family: 'IBM Plex Sans', system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif; }
.mono { font-family: 'IBM Plex Mono', ui-monospace, Consolas, monospace; }
.q-drawer { background: #15181B !important; }
.nav { color: #E8EAE7; padding: 28px 18px; gap: 28px; min-height: 100%; }
.nav a.nav-link { display:flex; align-items:center; gap:10px; min-height:44px; padding:0 12px;
       border-radius:6px; color:#C9CEC8; text-decoration:none; font-size:15px; }
.nav a.nav-link:hover { background:#22272C; color:#fff; }
.nav a.nav-link.active { background:#2A2F35; color:#fff; font-weight:500; }
.nav .status-box { margin-top:auto; padding:14px 12px; border:1px solid #343A41; border-radius:8px;
       gap:6px; font-size:12px; color:#A7ADA6; }
.nav .status-box .strong { font-size:13px; color:#fff; font-weight:500; }
.nav .status-box a { color:#9CC3F0; font-size:13px; }
.page { width:100%; max-width:1080px; padding:40px 44px 64px; gap:24px; }
@media (max-width: 700px) { .page { padding: 24px 16px 48px; } }
.h1 { font-size:30px; font-weight:600; letter-spacing:-0.01em; line-height:1.2; margin:0; }
.h2 { font-size:19px; font-weight:600; margin:0; }
.sub { font-size:15px; color:var(--muted); }
.small { font-size:13px; color:var(--muted); }
.panel { background:#fff; border:1px solid var(--line); border-radius:10px; padding:20px 22px;
         width:100%; gap:14px; }
.badge { display:inline-flex; align-items:center; padding:2px 10px; border-radius:999px;
         font-size:13px; font-weight:500; white-space:nowrap; }
.badge.ok { background:#E3F1E7; color:#1E5E36; }
.badge.warn { background:#FBEFD9; color:#7A4B00; }
.badge.running { background:#E1ECF8; color:#123A66; }
.badge.idle { background:#ECEEEB; color:#4A5058; }
.badge.bad { background:#FBE4E2; color:#8C1D18; }
.crumbs { font-size:14px; color:var(--muted); gap:8px; }
.crumbs a { color: var(--brand); }
a { color: var(--brand); }
.threads-table { width:100%; border:1px solid var(--line); border-radius:10px; }
.threads-table th { font-size:13px; color:var(--muted); font-weight:500; }
.threads-table td { font-size:15px; }
.threads-table tbody tr { cursor:pointer; }
.t-id { font-size:12px; color:var(--muted); }
.card { background:#fff; border:1px solid var(--line); border-radius:10px; padding:18px; gap:8px;
        min-width:220px; flex:1 1 220px; }
.card.thinking { border-color:#8DB3DE; box-shadow: 0 0 0 1px #8DB3DE inset; }
.card.bad { border-color:#E3A8A3; }
.timer { font-size:30px; font-weight:500; letter-spacing:0.02em; }
.step { flex:1 1 180px; gap:2px; padding:12px 14px; border-radius:8px; border:1px solid var(--line);
        background:#fff; }
.step.running { border-color:#8DB3DE; background:#F3F8FD; }
.step .num { font-size:12px; color:var(--muted); }
.log { font-size:13px; gap:4px; max-height:260px; overflow-y:auto; }
.log .t { color:var(--muted); min-width:68px; }
.seat-row { width:100%; padding:12px 0; border-top:1px solid var(--soft); gap:16px; align-items:center; }
.seat-row:first-child { border-top:none; }
.seat-name { font-weight:600; font-size:15px; min-width:110px; }
.reply { font-size:15px; line-height:1.6; width:100%; overflow-x:auto; }
.reply h1 { font-size:22px; line-height:1.3; margin:18px 0 8px; font-weight:600; }
.reply h2 { font-size:19px; line-height:1.3; margin:16px 0 6px; font-weight:600; }
.reply h3, .reply h4 { font-size:16px; line-height:1.3; margin:14px 0 4px; font-weight:600; }
.reply p { margin: 0 0 10px; }
.reply table { border-collapse:collapse; margin:8px 0 14px; }
.reply th, .reply td { border:1px solid var(--line); padding:6px 10px; text-align:left; vertical-align:top; }
.reply pre { background:#F4F5F3; padding:10px 12px; border-radius:6px; overflow-x:auto; }
.reply code { font-family:'IBM Plex Mono', ui-monospace, Consolas, monospace; font-size:13px; }
.danger-btn { color:#8C1D18 !important; }
.mobile-bar { display:none; }
@media (max-width: 899px) { .mobile-bar { display:flex; } }
"""


# ----------------------------------------------------------------- the context


class Context:
    """What every page needs: the workspace folder, the run manager, app memory."""

    def __init__(self, root: Path, state_path: Path | None = None):
        self.root = root
        state_path = state_path or Path.home() / ".council-app" / "state.json"
        self.state = AppState(state_path, root)
        self.jobs = JobManager(self.state)

    def workspace(self) -> Workspace:
        return Workspace.open(self.root)

    def config(self) -> Config:
        return load_config(self.workspace().config_path)

    def level_text(self, config: Config) -> dict[str, str]:
        return {key: h.describe_levels(spec) for key, spec in config.agents.items()}


# ------------------------------------------------------------------ the frame


def _status_line(ctx: Context) -> dict:
    try:
        config = ctx.config()
        enabled = [s for s in config.agents.values() if s.enabled]
    except CouncilError:
        enabled = []
    memory = ctx.state.get()
    test = memory.get("last_test") or {}
    results = test.get("results", {})
    ok = sum(1 for s in enabled if results.get(s.key, {}).get("status") == "ok")
    tested = [s for s in enabled if s.key in results]
    if not tested:
        connected = "Connections not tested yet"
    else:
        connected = f"{ok} of {len(enabled)} agents connected"
    push = memory.get("last_push") or {}
    if not push:
        sync = "No uploads recorded yet"
    elif push.get("result") == "pushed":
        sync = f"Uploaded to GitHub, {h.nice_time(push.get('at'))}"
    elif push.get("result") == "no remote":
        sync = "Not connected to GitHub"
    else:
        sync = f"Last upload failed, {h.nice_time(push.get('at'))}"
    return {
        "connected": connected,
        "tested": f"Last tested {h.nice_time(test.get('at'))}" if test.get("at") else "",
        "sync": sync,
    }


def frame(ctx: Context, active: str) -> ui.column:
    """Side navigation plus the main column. Returns the main column."""
    ui.colors(primary="#1B4F8A", negative="#B3261E", positive="#2E7D4F", warning="#A86A00")
    with ui.left_drawer(fixed=True).props("width=248 breakpoint=900") as drawer:
        with ui.column().classes("nav w-full"):
            with ui.row().classes("items-center no-wrap gap-3 px-1"):
                ui.html(LOGO_SVG, sanitize=False)
                with ui.column().classes("gap-0"):
                    ui.label("Council").classes("text-lg font-semibold")
                    ui.label("PMPB Foundations").classes("text-xs").style("color:#A7ADA6")
            with ui.column().classes("w-full gap-1"):
                for key, text, href, icon in (
                    ("threads", "Threads", "/", "list"),
                    ("new", "New question", "/new", "add"),
                    ("run", "Live run", "/run", "play_circle_outline"),
                    ("settings", "Settings", "/settings", "tune"),
                ):
                    with ui.link(target=href).classes("nav-link" + (" active" if key == active else "")):
                        ui.icon(icon).classes("text-base")
                        ui.label(text)

            @ui.refreshable
            def status_box() -> None:
                info = _status_line(ctx)
                job = ctx.jobs.job
                with ui.column().classes("status-box w-full"):
                    if job and job.running:
                        step = job.current_step()
                        ui.label("Council running").classes("strong")
                        where = job.thread_id or job.title
                        ui.label(f"{where} · {step.title if step else 'starting'}")
                        ui.link("Watch live", "/run")
                    ui.label(info["connected"]).classes("strong")
                    if info["tested"]:
                        ui.label(info["tested"])
                    with ui.row().classes("items-center gap-1 no-wrap"):
                        ui.icon("cloud_upload").classes("text-sm")
                        ui.label(info["sync"])
                    ui.link("Test again", "/settings#connections")

            status_box()
            last = {"key": None}

            def maybe_refresh() -> None:
                job = ctx.jobs.job
                step = job.current_step() if job and job.running else None
                key = (repr(_status_line(ctx)), job.running if job else None,
                       step.title if step else None)
                if key != last["key"]:
                    if last["key"] is not None:
                        status_box.refresh()
                    last["key"] = key

            ui.timer(1.0, maybe_refresh)

    with ui.row().classes("mobile-bar w-full items-center px-4 pt-3"):
        ui.button(icon="menu", on_click=drawer.toggle).props("flat round color=dark")
        ui.label("Council").classes("text-lg font-semibold")
    return ui.column().classes("page")


def badge(text: str, colour: str) -> None:
    ui.label(text).classes(f"badge {colour}")


def page_header(title: str, subtitle: str = "") -> None:
    with ui.column().classes("gap-1"):
        ui.label(title).classes("h1")
        if subtitle:
            ui.label(subtitle).classes("sub")


# ======================================================================= pages


def build(ctx: Context) -> None:
    ui.button.default_props("no-caps unelevated")
    ui.add_head_html(
        '<link rel="preconnect" href="https://fonts.googleapis.com">'
        '<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500'
        '&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">',
        shared=True,
    )
    ui.add_css(CSS, shared=True)

    # ------------------------------------------------------------- threads

    @ui.page("/", title="Council – Threads")
    def threads_page() -> None:
        main = frame(ctx, "threads")
        with main:
            with ui.row().classes("w-full items-end justify-between gap-4"):
                page_header("Threads", "Every question you have put to the council, newest first.")
                ui.button("New question", icon="add", on_click=lambda: ui.navigate.to("/new"))

            def rows() -> list[dict]:
                ws = ctx.workspace()
                config = ctx.config()
                running = ctx.jobs.running_thread_id()
                step = ctx.jobs.job.current_step() if running and ctx.jobs.job else None
                out = []
                for path in ws.thread_dirs():
                    try:
                        thread = Thread.load(ws, path)
                    except (CouncilError, ValueError):
                        continue
                    out.append(h.thread_row(thread, config, running, step.title if step else ""))
                return sorted(out, key=lambda r: r["sort"], reverse=True)

            first = rows()
            if not first:
                with ui.column().classes("panel"):
                    ui.label("No questions yet").classes("h2")
                    ui.label("Ask your first question and every seat answers it on its own, "
                             "then they read each other and respond.").classes("sub")
                    ui.button("New question", icon="add", on_click=lambda: ui.navigate.to("/new"))
            else:
                search = ui.input("Search threads", placeholder="Title or thread number") \
                    .props("outlined dense clearable").classes("w-full max-w-md bg-white")
                columns = [
                    {"name": "title", "label": "Thread", "field": "title", "align": "left"},
                    {"name": "status", "label": "Status", "field": "status", "align": "left"},
                    {"name": "rounds", "label": "Rounds", "field": "rounds", "align": "left"},
                    {"name": "synthesis", "label": "Synthesis", "field": "synthesis", "align": "left"},
                    {"name": "client", "label": "Client data", "field": "client", "align": "left"},
                    {"name": "started", "label": "Started", "field": "started", "align": "left"},
                    {"name": "action", "label": "", "field": "id", "align": "right"},
                ]
                table = ui.table(columns=columns, rows=first, row_key="id") \
                    .props("flat wrap-cells hide-pagination :pagination=\"{rowsPerPage: 0}\"") \
                    .classes("threads-table")
                table.add_slot("body-cell-title", r"""
                    <q-td :props="props">
                      <div style="font-weight:500">{{ props.row.title }}</div>
                      <div class="t-id mono">{{ props.row.id }}</div>
                    </q-td>""")
                table.add_slot("body-cell-status", r"""
                    <q-td :props="props"><span :class="'badge ' + props.row.colour">{{ props.value }}</span></q-td>""")
                table.add_slot("body-cell-action", r"""
                    <q-td :props="props">
                      <q-btn flat no-caps color="primary"
                             :label="props.row.colour === 'running' ? 'Watch' : 'Open'"
                             @click.stop="() => $parent.$emit('open', props.row)" />
                    </q-td>""")

                def open_row(e) -> None:
                    args = e.args if isinstance(e.args, list) else [e.args]
                    row = next((a for a in args if isinstance(a, dict) and "id" in a), None)
                    if row:
                        target = "/run" if row.get("colour") == "running" else f"/thread/{row['id']}"
                        ui.navigate.to(target)

                table.on("open", open_row)
                table.on("rowClick", open_row)
                search.bind_value_to(table, "filter")

                def refresh_rows() -> None:
                    table.rows = rows()
                    table.update()

                ui.timer(3.0, refresh_rows)

            ws = ctx.workspace()
            with ui.column().classes("panel"):
                ui.label("Where your threads live").classes("font-medium")
                remote = h.github_web_url(ws.remote_url())
                text = (f"Each thread is a folder in {ws.threads_dir}. Every finished round is saved "
                        "there and committed to git")
                if remote:
                    text += (", then uploaded to your private GitHub repository, so nothing is lost "
                             "if the app closes and your desktop can read it.")
                else:
                    text += ". This workspace has no GitHub remote, so threads stay on this computer."
                ui.label(text).classes("sub")
                if remote:
                    ui.link(remote, remote, new_tab=True).classes("text-sm")

    # --------------------------------------------------------- new question

    @ui.page("/new", title="Council – New question")
    def new_page() -> None:
        main = frame(ctx, "new")
        try:
            config = ctx.config()
            ws = ctx.workspace()
        except CouncilError as exc:
            with main:
                page_header("New question")
                ui.label(str(exc)).classes("panel")
            return
        seats = [s for s in config.agents.values() if s.enabled]
        staging = Path(tempfile.mkdtemp(prefix="council-upload-"))
        # Uploads wait here until Start copies them into the thread; the folder goes
        # when this page is closed for good.
        ui.context.client.on_delete(lambda: shutil.rmtree(staging, ignore_errors=True))
        form = {
            "title": "",
            "question": "",
            "client": False,
            "rounds": 2,
            "synth": "claude" if "claude" in config.agents else (seats[0].key if seats else "none"),
            "take": {s.key: not h.seat_problem(s) for s in seats},
            "levels": {s.key: get_levels(s) for s in seats if h.has_levels(s)},
            "attachments": [],  # paths in staging
        }

        def taking_part() -> list[str]:
            return [
                s.key for s in seats
                if form["take"].get(s.key) and not h.seat_problem(s)
                and (s.client_data or not form["client"])
            ]

        def synth_options() -> dict[str, str]:
            options = {"none": "Nobody, I will read the rounds"}
            for s in seats:
                if h.seat_problem(s) or (form["client"] and not s.client_data):
                    continue
                options[s.key] = s.label
            return options

        @ui.refreshable
        def synth_box() -> None:
            options = synth_options()
            if form["synth"] not in options:
                form["synth"] = next((k for k in options if k != "none"), "none")
            ui.radio(options, on_change=lambda _: summary.refresh()) \
                .props("inline").bind_value(form, "synth")

        remote = ws.remote_url()

        @ui.refreshable
        def summary() -> None:
            n = len(taking_part())
            rounds = int(form["rounds"])
            synth = form["synth"] != "none"
            low, high = 3 * rounds + (3 if synth else 0), 8 * rounds + (6 if synth else 0)
            ui.label(f"{n} taking part · usually about {low} to {high} minutes").classes("font-medium")
            upload = ("Results upload to GitHub after every round." if remote and config.auto_push
                      else "Results are saved and committed on this computer after every round.")
            ui.label("Runs on your subscriptions. No per-use API charges. " + upload).classes("small")

        with main:
            page_header("New question",
                        "Ask once. Each agent answers on its own first, then they read each other and respond.")

            with ui.column().classes("panel"):
                title = ui.input("Title", placeholder="For example: 1V8 rail LDO choice") \
                    .props("outlined").classes("w-full").bind_value(form, "title")
                folder_hint = ui.label().classes("small")

                def update_hint() -> None:
                    folder_hint.text = (f"Folder: {ws.next_thread_id()}-{slugify(form['title'])}"
                                        if form["title"].strip() else "The title becomes the folder name.")

                title.on_value_change(lambda _: update_hint())
                update_hint()

                with ui.row().classes("w-full items-center justify-between"):
                    ui.label("Question").classes("font-medium")

                    async def load_question(e) -> None:
                        try:
                            text = await e.file.text()
                        except UnicodeDecodeError:
                            ui.notify("That file is not plain text (UTF-8). Save it as .md or .txt.",
                                      type="negative")
                            return
                        form["question"] = text
                        question.value = text
                        if not form["title"].strip():
                            first = next((l.strip("# ").strip() for l in text.splitlines() if l.strip()), "")
                            form["title"] = first[:60]
                            title.value = form["title"]
                        ui.notify(f"Loaded {h.safe_filename(e.file.name)}", type="positive")
                        md_upload.reset()

                    md_upload = ui.upload(auto_upload=True, on_upload=load_question,
                                          max_file_size=2_000_000) \
                        .props('accept=".md,.txt,.markdown"').classes("hidden")
                    ui.button("Load from a .md file", icon="upload_file",
                              on_click=lambda: md_upload.run_method("pickFiles")).props("outline")
                question = ui.textarea(placeholder="What do you want the council to work on?") \
                    .props("outlined autogrow input-style=\"min-height: 220px\"") \
                    .classes("w-full").bind_value(form, "question")

            with ui.column().classes("panel"):
                ui.label("Attachments").classes("font-medium")
                ui.label("Every agent can read them: PDF, text, CSV, Markdown and images. "
                         "Up to 50 MB each.").classes("small")

                @ui.refreshable
                def attachment_list() -> None:
                    if not form["attachments"]:
                        return
                    with ui.row().classes("gap-2"):
                        for path in list(form["attachments"]):
                            def remove(p: Path = path) -> None:
                                form["attachments"].remove(p)
                                p.unlink(missing_ok=True)
                                attachment_list.refresh()

                            ui.chip(path.name, icon="attach_file", removable=True, color="grey-3",
                                    text_color="dark",
                                    on_value_change=lambda e, f=remove: (None if e.value else f()))

                async def add_attachments(e) -> None:
                    for upload in e.files:
                        name = h.safe_filename(upload.name)
                        dest = staging / name
                        if dest.exists():
                            ui.notify(f"There is already an attachment called {name}", type="warning")
                            continue
                        await upload.save(dest)
                        form["attachments"].append(dest)
                    e.sender.reset()  # the chips below are the list; empty the drop zone again
                    attachment_list.refresh()

                ui.upload(multiple=True, auto_upload=True, on_multi_upload=add_attachments,
                          max_file_size=50_000_000,
                          label="Drop datasheets, schematics, netlists or notes here, or press +",
                          on_rejected=lambda: ui.notify("That file is over 50 MB", type="warning")) \
                    .props("flat bordered color=grey-2 text-color=grey-9 hide-upload-btn") \
                    .classes("w-full")
                attachment_list()

            with ui.column().classes("panel"):
                ui.label("Who takes part").classes("font-medium")
                with ui.row().classes("w-full items-start no-wrap gap-3 p-3 rounded") \
                        .style("background:#F4F5F3"):
                    ui.switch(on_change=lambda _: (seat_rows.refresh(), synth_box.refresh(),
                                                   summary.refresh())) \
                        .bind_value(form, "client")
                    with ui.column().classes("gap-1"):
                        ui.label("This question contains client data").classes("font-medium")
                        ui.label("Turn on for anything from a client: schematics, layouts, BOMs, "
                                 "requirements. Only seats you cleared for client data take part. "
                                 "Kimi and GLM are run by China-based companies, so they sit these "
                                 "out by default. Change the list in Settings.").classes("small")

                @ui.refreshable
                def seat_rows() -> None:
                    with ui.column().classes("w-full gap-0"):
                        for spec in seats:
                            problem = h.seat_problem(spec)
                            blocked = form["client"] and not spec.client_data
                            with ui.row().classes("seat-row no-wrap"):
                                box = ui.checkbox(spec.label).classes("seat-name")
                                if problem or blocked:
                                    box.value = False
                                    box.disable()
                                else:
                                    box.bind_value(form["take"], spec.key)
                                    box.on_value_change(lambda _: (synth_box.refresh(), summary.refresh()))
                                with ui.column().classes("gap-1 grow"):
                                    if problem:
                                        ui.label(f"Can't take part: {problem}").classes("small")
                                    elif blocked:
                                        ui.label("Sits out: not cleared for client data").classes("small")
                                    elif h.has_levels(spec):
                                        with ui.row().classes("gap-3 items-center"):
                                            for knob, values in level_choices(spec).items():
                                                ui.select(
                                                    {v: h.choice_label(spec, knob, v) for v in values},
                                                    label=h.KNOB_LABELS.get(knob, knob),
                                                ).props("outlined dense options-dense") \
                                                    .classes("min-w-[200px]") \
                                                    .bind_value(form["levels"][spec.key], knob)
                                        ui.label("For this question only. Your usual level is set "
                                                 "in Settings.").classes("small")
                                    else:
                                        ui.label(h.describe_levels(spec) or "Default level").classes("small")
                                company, china = h.provider(spec)
                                if company:
                                    ui.label(company).classes("small text-right")

                seat_rows()

            with ui.column().classes("panel"):
                ui.label("Rounds").classes("font-medium")
                ui.toggle({1: "1 · Quick", 2: "2 · Standard", 3: "3 · Deep"},
                          on_change=lambda _: summary.refresh()).props("no-caps").bind_value(form, "rounds")
                ui.label("Round 1: independent answers. Round 2: each reads the others and responds. "
                         "Round 3: one more round of debate.").classes("small")
                ui.label("Synthesis by").classes("font-medium pt-2")

                synth_box()
                ui.label("The synthesis credits every position by name. Each writer leans toward its "
                         "own side, so you can add a second synthesis from the thread later and "
                         "compare.").classes("small")

            with ui.row().classes("panel items-center justify-between"):
                with ui.column().classes("gap-1"):
                    summary()
                with ui.row().classes("gap-2"):
                    ui.button("Cancel", on_click=lambda: ui.navigate.to("/")).props("flat")
                    start_btn = ui.button("Start council", icon="play_arrow")
            busy_note = ui.label("A run is in progress. You can start this when it finishes, "
                                 "or stop it on the Live run page.").classes("small")

            def sync_busy() -> None:
                busy = ctx.jobs.busy
                start_btn.set_enabled(not busy)
                busy_note.set_visibility(busy)

            sync_busy()
            ui.timer(2.0, sync_busy)

            async def start() -> None:
                title_text = form["title"].strip()
                question_text = form["question"].strip()
                participants = taking_part()
                synth = None if form["synth"] == "none" else form["synth"]
                if not title_text:
                    ui.notify("Give the question a title", type="warning")
                    return
                if not question_text:
                    ui.notify("Write the question first", type="warning")
                    return
                if not participants:
                    ui.notify("Choose at least one seat", type="warning")
                    return
                if synth and form["client"] and not config.get(synth).client_data:
                    ui.notify(f"{config.get(synth).label} is not cleared for client data", type="warning")
                    return
                fresh = ctx.config()
                wanted = {k: form["levels"][k] for k in set(participants + ([synth] if synth else []))
                          if k in form["levels"]}
                run_config = config_with_levels(fresh, wanted)
                start_btn.props("loading")
                try:
                    ws_run = ctx.workspace()
                    ws_run.auto_push = run_config.auto_push
                    plan = {
                        "rounds": int(form["rounds"]),
                        "synthesis_by": synth,
                        "levels": {k: v for k, v in wanted.items() if k in participants or k == synth},
                    }
                    thread = await run.io_bound(
                        ws_run.create_thread, title_text, form["question"], participants,
                        list(form["attachments"]), client_data=bool(form["client"]), plan=plan,
                    )
                    ctx.state.record_push(ws_run.last_push)
                    ctx.jobs.start_council(ws_run, run_config, thread, rounds=int(form["rounds"]),
                                           synth_by=synth, level_text=ctx.level_text(run_config))
                except CouncilError as exc:
                    ui.notify(str(exc), type="negative", multi_line=True, close_button=True)
                    return
                finally:
                    start_btn.props(remove="loading")
                shutil.rmtree(staging, ignore_errors=True)
                ui.navigate.to("/run")

            start_btn.on_click(start)

    # ------------------------------------------------------------- live run

    @ui.page("/run", title="Council – Live run")
    def run_page() -> None:
        main = frame(ctx, "run")
        timers: list[tuple[ui.label, ui.label | None, object, float]] = []
        seen = {"key": None}

        def describe_step(job: Job, index: int) -> tuple[str, str]:
            step = job.steps[index]
            state = {
                "waiting": "Waiting", "running": "In progress", "done": "Done",
                "incomplete": "Finished with gaps", "stopped": "Stopped",
                "skipped": "Not run", "error": "Could not run",
            }[step.state]
            if step.kind == "round" and step.state in ("waiting", "running"):
                state += " · blind answers" if step.round_no == 1 else " · they respond to each other"
            if step.kind == "synthesis" and step.state in ("waiting", "running"):
                state += f" · written by {step.cards[step.by].label if step.by in step.cards else step.by}"
            return step.title, state

        def card_status(job: Job, card) -> tuple[str, str]:
            return {
                "waiting": ("Waiting", "idle"), "thinking": ("Thinking", "running"),
                "ok": ("Done", "ok"), "failed": ("Failed", "bad"), "timeout": ("Timed out", "bad"),
                "not_found": ("Not installed", "bad"), "error": ("Could not start", "bad"),
                "cancelled": ("Stopped", "warn"),
            }.get(card.status, (card.status, "idle"))

        def sub_line(job: Job, card) -> str:
            limit = fmt_duration(job.timeout_s)
            if card.status == "thinking":
                return f"{fmt_duration(card.elapsed())} of a {limit} limit"
            if card.status == "waiting":
                return "Starting…" if job.running else ""
            if card.status == "cancelled":
                return "Closed when Stop was pressed"
            if card.status == "ok":
                step = job.current_step()
                if job.running and step and any(c.status == "thinking" for c in step.cards.values()):
                    return "Waiting for the others to finish"
                return "Saved" if job.kind != "ping" else "Connected"
            return card.detail

        def render() -> None:
            timers.clear()
            body.clear()
            job = ctx.jobs.job
            with body:
                if job is None:
                    page_header("Live run", "Nothing is running right now.")
                    with ui.column().classes("panel"):
                        ui.label("Start a question and you can watch every agent here as it works. "
                                 "You can close this tab; the run keeps going while the Council "
                                 "window is open.").classes("sub")
                        ui.button("New question", icon="add", on_click=lambda: ui.navigate.to("/new"))
                    return
                ws = ctx.workspace()
                thread = None
                if job.thread_id:
                    try:
                        thread = ws.thread(job.thread_id)
                    except CouncilError:
                        thread = None
                with ui.row().classes("crumbs items-center"):
                    if thread:
                        ui.link("Threads", "/")
                        ui.label("/")
                        ui.link(thread.id, f"/thread/{thread.id}").classes("mono")
                    else:
                        ui.link("Settings", "/settings")
                        ui.label("/")
                        ui.label("Connection test")
                with ui.row().classes("w-full items-center justify-between gap-3"):
                    ui.label(job.title).classes("h1")
                    colour = {"running": "running", "done": "ok", "incomplete": "warn",
                              "stopped": "warn", "error": "bad"}[job.state]
                    badge({"running": "Running", "done": "Finished", "incomplete": "Finished with gaps",
                           "stopped": "Stopped", "error": "Could not finish"}[job.state], colour)
                meta = [f"Started {h.nice_time(job.started_at)}"]
                if thread:
                    plan = thread.manifest.get("plan") or {}
                    meta.append(f"{len(thread.participants)} agents")
                    if plan.get("rounds"):
                        meta.append(f"{plan['rounds']} rounds")
                    by = plan.get("synthesis_by")
                    cfg = ctx.config()
                    if by:
                        meta.append(f"synthesis by {cfg.agents[by].label if by in cfg.agents else by}")
                    meta.append("client data" if thread.client_data else "no client data")
                else:
                    meta.append(f"{len(job.steps[0].agents)} seats")
                ui.label(" · ".join(meta)).classes("sub")

                if len(job.steps) > 1 or job.kind != "ping":
                    with ui.row().classes("w-full gap-3"):
                        for i, step in enumerate(job.steps):
                            name, state = describe_step(job, i)
                            with ui.column().classes("step" + (" running" if step.state == "running" else "")):
                                ui.label(str(i + 1)).classes("num")
                                ui.label(name).classes("font-medium")
                                ui.label(state).classes("small")

                with ui.row().classes("w-full gap-4 items-stretch"):
                    for card in job.cards():
                        text, colour = card_status(job, card)
                        cls = "card" + (" thinking" if card.status == "thinking" else "") + \
                              (" bad" if colour == "bad" else "")
                        with ui.column().classes(cls):
                            with ui.row().classes("w-full items-center justify-between"):
                                ui.label(card.label).classes("font-semibold text-base")
                                badge(text, colour)
                            if card.level:
                                ui.label(card.level).classes("small")
                            clock = ui.label(fmt_duration(card.elapsed()) if card.status != "waiting"
                                             else "0:00").classes("timer mono")
                            line = ui.label(sub_line(job, card)).classes("small")
                            timers.append((clock, line, card, 0))

                step = job.current_step() or next(
                    (s for s in reversed(job.steps) if s.state not in ("waiting", "skipped")), None)
                remote = ws.remote_url()
                cfg = ctx.config()
                if step and step.kind == "round" and step.round_no == 1:
                    note = "Round 1 is blind: no agent sees another's answer until round 2 starts. "
                elif step and step.kind == "round":
                    note = (f"In round {step.round_no} every agent reads all of round "
                            f"{(step.round_no or 2) - 1} and responds to the others by name. ")
                elif step and step.kind == "synthesis":
                    note = "The synthesis reads every round and credits each position by name. "
                elif step and step.kind == "retry":
                    note = "A retry asks only the seats that failed, with the same prompt they missed. "
                else:
                    note = ("Each seat gets a one-line test prompt through the same path a real round "
                            "uses. Nothing is saved to any thread.")
                if job.kind != "ping":
                    note += ("Answers are saved, committed and uploaded to GitHub the moment the whole "
                             "step finishes." if remote and cfg.auto_push else
                             "Answers are saved and committed the moment the whole step finishes.")
                ui.label(note).classes("small")

                with ui.column().classes("panel"):
                    ui.label("Activity").classes("font-medium")
                    with ui.column().classes("log w-full mono"):
                        for stamp, text in job.log[-60:]:
                            with ui.row().classes("no-wrap gap-3"):
                                ui.label(stamp).classes("t")
                                ui.label(text)

                if job.running:
                    with ui.row().classes("items-center gap-4"):
                        ui.button("Stop run", icon="stop_circle", color="white",
                                  on_click=confirm_stop.open) \
                            .props("outline").classes("danger-btn")
                        ui.label("Stops every running agent. Nothing from the unfinished step is saved; "
                                 "finished rounds stay saved. Closing the Council window also stops "
                                 "the run.").classes("small max-w-xl")
                else:
                    with ui.column().classes("panel"):
                        ui.label(job.message or "Finished").classes("font-medium")
                        with ui.row().classes("gap-2"):
                            if thread:
                                ui.button("Open thread", icon="article",
                                          on_click=lambda t=thread.id: ui.navigate.to(f"/thread/{t}"))
                            elif job.kind == "ping":
                                ui.button("Back to Settings",
                                          on_click=lambda: ui.navigate.to("/settings#connections"))
                            ui.button("All threads", on_click=lambda: ui.navigate.to("/")).props("flat")

        with ui.dialog() as confirm_stop, ui.card().classes("gap-3 p-6"):
            ui.label("Stop this run?").classes("h2")
            ui.label("Every running agent is closed now. Nothing from the unfinished step is saved. "
                     "Rounds that already finished stay saved.").classes("sub max-w-md")
            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("Keep running", on_click=confirm_stop.close).props("flat")

                def do_stop() -> None:
                    confirm_stop.close()
                    if ctx.jobs.stop():
                        ui.notify("Stopping: closing every agent…", type="warning")

                ui.button("Stop run", color="negative", on_click=do_stop)

        with main:
            body = ui.column().classes("w-full gap-6")

        def tick() -> None:
            job = ctx.jobs.job
            key = (job.id, job.version) if job else None
            if key != seen["key"]:
                seen["key"] = key
                render()
                return
            for clock, line, card, _ in timers:
                if card.status == "thinking":
                    clock.text = fmt_duration(card.elapsed())
                    line.text = sub_line(job, card)

        tick()
        ui.timer(1.0, tick)

    # ----------------------------------------------------------- thread view

    @ui.page("/thread/{tid}", title="Council – Thread")
    def thread_page(tid: str) -> None:
        main = frame(ctx, "threads")
        with main:
            body = ui.column().classes("w-full gap-6")
        seen = {"key": None}

        def snapshot_key():
            job = ctx.jobs.job
            try:
                manifest_mtime = (ctx.workspace().thread(tid).path / "thread.json").stat().st_mtime
            except (CouncilError, OSError):
                manifest_mtime = None
            running = job is not None and job.running and job.thread_id == tid
            return (manifest_mtime, running, ctx.jobs.busy)

        def start(kind: str, by: str | None = None) -> None:
            try:
                ws = ctx.workspace()
                config = ctx.config()
                thread = ws.thread(tid)
                text = ctx.level_text(config)
                if kind == "round":
                    plan = thread.manifest.get("plan")
                    target = thread.latest_round() + 1
                    if plan is not None:
                        plan["rounds"] = max(int(plan.get("rounds") or 0), target)
                        thread.save()
                    ctx.jobs.start_council(ws, config, thread, rounds=target, synth_by=None,
                                           level_text=text)
                elif kind == "resume":
                    plan = thread.manifest.get("plan") or {}
                    by = plan.get("synthesis_by")
                    if by and h.synth_entries(thread):
                        by = None
                    # the levels chosen for this question when it was asked
                    levels = {k: v for k, v in (plan.get("levels") or {}).items() if k in config.agents}
                    config = config_with_levels(config, levels)
                    text = ctx.level_text(config)
                    ctx.jobs.start_council(ws, config, thread,
                                           rounds=max(int(plan.get("rounds") or 0), thread.latest_round()),
                                           synth_by=by, level_text=text)
                elif kind == "retry":
                    ctx.jobs.start_retry(ws, config, thread, level_text=text)
                else:
                    ctx.jobs.start_synthesis(ws, config, thread, by, level_text=text)
            except CouncilError as exc:
                ui.notify(str(exc), type="negative", multi_line=True, close_button=True)
                return
            ui.navigate.to("/run")

        def render() -> None:
            body.clear()
            with body:
                try:
                    ws = ctx.workspace()
                    config = ctx.config()
                    thread = ws.thread(tid)
                except CouncilError as exc:
                    page_header("Thread not found")
                    ui.label(str(exc)).classes("panel")
                    ui.link("Back to threads", "/")
                    return
                job = ctx.jobs.job
                running_here = bool(job and job.running and job.thread_id == thread.id)
                step = job.current_step() if running_here else None
                status, colour = h.thread_status(thread, thread.id if running_here else None,
                                                 step.title if step else "")
                labels = {k: (config.agents[k].label if k in config.agents else k)
                          for k in thread.participants}

                with ui.row().classes("crumbs items-center"):
                    ui.link("Threads", "/")
                    ui.label("/")
                    ui.label(thread.id).classes("mono")
                with ui.row().classes("w-full items-center justify-between gap-3"):
                    ui.label(thread.title).classes("h1")
                    badge(status, colour)

                unpushed = ws.unpushed_commits()
                remote = ws.remote_url()
                if remote is None:
                    sync = "on this computer only"
                elif unpushed == 0:
                    sync = "on GitHub"
                elif unpushed:
                    sync = f"{unpushed} change{'s' if unpushed != 1 else ''} not uploaded yet"
                else:
                    sync = "not uploaded yet"
                meta = [
                    h.nice_time(thread.manifest.get("created")),
                    ", ".join(labels.values()),
                    f"{thread.latest_round()} round{'s' if thread.latest_round() != 1 else ''}",
                    "client data" if thread.client_data else "no client data",
                    sync,
                ]
                ui.label(" · ".join(m for m in meta if m)).classes("sub")

                if running_here:
                    with ui.row().classes("panel items-center justify-between"):
                        ui.label(f"Running now: {step.title if step else 'starting'}").classes("font-medium")
                        ui.button("Watch live", icon="visibility", on_click=lambda: ui.navigate.to("/run"))

                # -- actions
                busy = ctx.jobs.busy
                latest = thread.latest_round()
                complete = latest > 0 and thread.round_complete(latest)
                missing = thread.missing_agents(latest) if latest else []
                plan = thread.manifest.get("plan") or {}
                planned_rounds = int(plan.get("rounds") or 0)
                planned_by = plan.get("synthesis_by")
                has_synth = bool(h.synth_entries(thread))
                resumable = (latest == 0 or complete) and (
                    latest < planned_rounds or (planned_by and not has_synth and latest >= 1)
                ) and planned_by in (None, *config.agents)
                with ui.row().classes("gap-2 items-center"):
                    if resumable and not running_here:
                        parts = []
                        if latest < planned_rounds:
                            parts.append(f"rounds {latest + 1}–{planned_rounds}"
                                         if planned_rounds - latest > 1 else f"round {planned_rounds}")
                        if planned_by and not has_synth:
                            parts.append(f"synthesis by {config.agents[planned_by].label}")
                        resume = ui.button("Continue: " + " and ".join(parts), icon="play_arrow",
                                           on_click=lambda: start("resume"))
                        resume.set_enabled(not busy)
                    more = ui.button("One more round", icon="forum", on_click=lambda: start("round"))
                    more.set_enabled(not busy and (latest == 0 or complete))
                    if resumable:
                        more.props("outline")
                    eligible = [
                        s for s in config.agents.values()
                        if not h.seat_problem(s) and (s.client_data or not thread.client_data)
                    ]
                    done_by = {e["by"] for e in h.synth_entries(thread)}
                    with ui.button("Synthesize with", icon="summarize").props("outline") as synth_btn:
                        with ui.menu():
                            for spec in eligible:
                                name = spec.label + (" (again)" if spec.key in done_by else "")
                                ui.menu_item(name, on_click=lambda k=spec.key: start("synth", k))
                    synth_btn.set_enabled(not busy and latest > 0 and complete and bool(eligible))
                    retry_label = (f"Retry failed ({', '.join(labels[m] for m in missing)})"
                                   if missing else "Retry failed (none failed)")
                    retry = ui.button(retry_label, icon="replay", on_click=lambda: start("retry")) \
                        .props("outline")
                    retry.set_enabled(not busy and bool(missing))

                    def open_folder(p: Path = thread.path) -> None:
                        error = h.open_in_system(p)
                        if error:
                            ui.notify(error, type="warning", multi_line=True)

                    ui.button("Open folder", icon="folder_open", on_click=open_folder).props("flat")
                if busy and not running_here:
                    ui.label("Another run is in progress, so these wait until it finishes.").classes("small")

                # -- question
                with ui.column().classes("panel"):
                    ui.label("Question").classes("h2")
                    try:
                        question_text = thread.question()
                    except CouncilError as exc:
                        ui.label(str(exc)).classes("small")
                    else:
                        if len(question_text) <= 900:
                            ui.markdown(question_text).classes("reply")
                        else:
                            ui.label(h.preview(question_text, 320)).classes("sub")
                            with ui.expansion("Show the full question").classes("w-full").props("dense"):
                                ui.markdown(question_text).classes("reply")
                    attachments = thread.attachment_paths()
                    if attachments:
                        with ui.row().classes("gap-2 items-center"):
                            ui.label("Attachments:").classes("small")
                            for path in attachments:
                                ui.chip(path.name, icon="attach_file", color="grey-3", text_color="dark",
                                        on_click=lambda p=path: h.open_in_system(p))

                # -- synthesis
                entries = h.synth_entries(thread)
                with ui.column().classes("panel"):
                    ui.label("Synthesis").classes("h2")
                    if not entries:
                        ui.label("No synthesis yet. Use Synthesize with to have one seat combine the "
                                 "rounds into a single answer that credits each position.").classes("sub")
                    else:
                        choice = {"by": entries[-1]["by"]}

                        @ui.refreshable
                        def synth_view() -> None:
                            entry = next(e for e in entries if e["by"] == choice["by"])
                            path = thread.path / entry["file"]
                            with ui.row().classes("items-center gap-3"):
                                ui.label(f"after round {entry.get('after_round')}").classes("small")
                                ui.button(entry["file"], icon="open_in_new",
                                          on_click=lambda p=path: h.open_in_system(p)) \
                                    .props("flat dense").classes("mono text-sm")
                            ui.markdown(unwrap_cli_bullets(path.read_text(encoding="utf-8"))
                                        if path.exists() else "_The file is missing._").classes("reply")

                        if len(entries) > 1:
                            ui.toggle({e["by"]: f"by {labels.get(e['by'], config.agents[e['by']].label if e['by'] in config.agents else e['by'])}"
                                       for e in entries},
                                      on_change=lambda _: synth_view.refresh()).props("no-caps").bind_value(choice, "by")
                        else:
                            by = entries[0]["by"]
                            ui.label(f"by {labels.get(by, by)}").classes("small")
                        synth_view()

                # -- rounds
                with ui.column().classes("panel"):
                    ui.label("Rounds").classes("h2")
                    if not thread.rounds:
                        ui.label("No rounds yet.").classes("sub")
                    else:
                        numbers = sorted(r["round"] for r in thread.rounds)
                        with ui.tabs().props("align=left no-caps active-color=primary indicator-color=primary") as tabs:
                            tab_items = {n: ui.tab(f"Round {n}") for n in numbers}
                        with ui.tab_panels(tabs, value=tab_items[numbers[-1]]).classes("w-full"):
                            for n in numbers:
                                record = thread.round_record(n)
                                texts = thread.responses(n)
                                with ui.tab_panel(tab_items[n]).classes("px-0 gap-4"):
                                    for agent in thread.participants:
                                        entry = record["agents"].get(agent)
                                        render_reply(thread, n, agent, labels[agent], entry, texts.get(agent))

                # -- audit trail
                with ui.column().classes("panel"):
                    ui.label("Audit trail").classes("h2")
                    commits = ws.log(thread.path, limit=30)
                    if not commits:
                        ui.label("No commits yet.").classes("sub")
                    for commit in commits:
                        with ui.row().classes("no-wrap gap-4 items-baseline"):
                            ui.label(commit["hash"]).classes("mono text-sm")
                            ui.label(commit["subject"]).classes("text-sm grow")
                            ui.label(h.nice_time(commit["date"])).classes("small")
                    if remote:
                        web = h.github_web_url(remote)
                        if unpushed == 0:
                            line = "All uploaded to GitHub."
                        elif unpushed:
                            line = f"{unpushed} commit(s) not uploaded yet. Settings > Upload now sends them."
                        else:
                            line = "Not uploaded yet."
                        with ui.row().classes("gap-2 items-center"):
                            ui.label(line).classes("small")
                            if web:
                                ui.link("Open on GitHub", f"{web}/tree/HEAD/threads/{thread.path.name}",
                                        new_tab=True).classes("text-sm")
                    ui.label("Every agent's exact prompt and saved answer is fingerprinted (sha256) "
                             "in thread.json.").classes("small")

        def render_reply(thread: Thread, n: int, agent: str, label: str, entry: dict | None,
                         text: str | None) -> None:
            text = unwrap_cli_bullets(text) if text is not None else None
            status = (entry or {}).get("status", "not run")
            colour = {"ok": "ok", "not run": "idle"}.get(status, "bad")
            word = {"ok": "Done", "failed": "Failed", "timeout": "Timed out",
                    "not_found": "Not installed", "error": "Could not start",
                    "cancelled": "Stopped", "not run": "Not run"}.get(status, status)
            with ui.column().classes("w-full gap-2 pt-2").style("border-top:1px solid var(--soft)"):
                with ui.row().classes("w-full items-center justify-between"):
                    with ui.row().classes("items-center gap-3"):
                        ui.label(label).classes("font-semibold text-base")
                        badge(word + (f" · {fmt_duration(entry.get('duration_s'))}"
                                      if entry and entry.get("duration_s") is not None else ""), colour)
                    if text is not None:
                        path = thread.response_path(n, agent)
                        ui.button(path.name, icon="open_in_new",
                                  on_click=lambda p=path: h.open_in_system(p)) \
                            .props("flat dense").classes("mono text-sm")
                if entry and entry.get("detail") and status != "ok":
                    ui.label(entry["detail"]).classes("small")
                failed_rerun = (entry or {}).get("last_rerun_failed")
                if failed_rerun:
                    ui.label(f"A later re-run failed ({failed_rerun.get('detail', '')}); "
                             "this saved answer was kept.").classes("small")
                if text is not None:
                    ui.label(h.preview(text)).classes("sub")
                    with ui.expansion("Read the full answer").classes("w-full").props("dense"):
                        ui.markdown(text).classes("reply")

        def tick() -> None:
            key = snapshot_key()
            if key != seen["key"]:
                seen["key"] = key
                render()

        tick()
        ui.timer(2.0, tick)

    # ------------------------------------------------------------- settings

    @ui.page("/settings", title="Council – Settings")
    def settings_page() -> None:
        main = frame(ctx, "settings")
        try:
            ws = ctx.workspace()
            config = ctx.config()
        except CouncilError as exc:
            with main:
                page_header("Settings")
                ui.label(str(exc)).classes("panel")
            return
        seats = list(config.agents.values())
        original = {
            "levels": {s.key: get_levels(s) for s in seats if h.has_levels(s)},
            "client": {s.key: s.client_data for s in seats},
            "timeout": round(config.timeout_s / 60, 2),
            "auto_push": config.auto_push,
        }
        pending = {
            "levels": {k: dict(v) for k, v in original["levels"].items()},
            "client": dict(original["client"]),
            "timeout": original["timeout"],
            "auto_push": original["auto_push"],
        }

        with main:
            page_header("Settings", "Changes are written to council.toml and apply to the next run.")

            # -- levels
            with ui.column().classes("panel"):
                ui.label("Model levels").classes("h2")
                ui.label("Higher levels give deeper answers, take longer, and use more of each plan's "
                         "limits.").classes("small")
                with ui.column().classes("w-full gap-0"):
                    for spec in seats:
                        with ui.row().classes("seat-row items-start"):
                            with ui.column().classes("gap-0 min-w-[150px]"):
                                ui.label(spec.label).classes("seat-name")
                                company, _ = h.provider(spec)
                                if company:
                                    ui.label(company).classes("small")
                                if not spec.enabled:
                                    ui.label("switched off in council.toml").classes("small")
                            with ui.column().classes("gap-2 grow"):
                                if h.has_levels(spec):
                                    with ui.row().classes("gap-3"):
                                        for knob, values in level_choices(spec).items():
                                            ui.select({v: h.choice_label(spec, knob, v) for v in values},
                                                      label=h.KNOB_LABELS.get(knob, knob)) \
                                                .props("outlined dense options-dense") \
                                                .classes("min-w-[260px]") \
                                                .bind_value(pending["levels"][spec.key], knob)
                                else:
                                    ui.label(h.describe_levels(spec) or "This seat has no level setting.") \
                                        .classes("sub")
                                    if spec.key == "kimi":
                                        ui.label("Your plan has one model. HighSpeed is the same model, "
                                                 "faster, on a higher plan.").classes("small")
                                for name in spec.env_refs():
                                    is_set = bool(os.environ.get(name))
                                    ui.label(f"{name} is set" if is_set else
                                             f"{name} is not set, so {spec.label} can't run until you "
                                             "set it and restart Council").classes("small") \
                                        .style("" if is_set else "color:#8C1D18")

            # -- time limit
            with ui.column().classes("panel"):
                ui.label("Time limit").classes("h2")
                ui.label("How long one agent may think before it is stopped. Raise it for deep effort "
                         "levels.").classes("small")
                ui.number("Minutes per agent", min=1, max=240, step=1, format="%.0f") \
                    .props("outlined dense").classes("w-48").bind_value(pending, "timeout")

            # -- connections
            with ui.column().classes("panel").props("id=connections"):
                with ui.row().classes("w-full items-center justify-between"):
                    ui.label("Connections").classes("h2")
                    test_btn = ui.button("Test connections", icon="wifi_tethering")

                @ui.refreshable
                def connections() -> None:
                    memory = ctx.state.get().get("last_test") or {}
                    results = memory.get("results", {})
                    job = ctx.jobs.job
                    live = job.steps[0].cards if job and job.kind == "ping" and job.running else {}
                    ui.label(f"Last tested {h.nice_time(memory.get('at'))}" if memory.get("at")
                             else "Not tested yet").classes("small")
                    with ui.element("table").classes("w-full text-left"):
                        with ui.element("tr"):
                            for head in ("Agent", "Result", "Reply time", "Program version"):
                                with ui.element("th").classes("small font-medium py-2 pr-4"):
                                    ui.label(head)
                        for spec in seats:
                            res = results.get(spec.key)
                            card = live.get(spec.key)
                            problem = h.seat_problem(spec)
                            if card and card.status in ("waiting", "thinking"):
                                result, colour, took = "Testing…", "running", fmt_duration(card.elapsed())
                            elif problem:
                                result, colour, took = problem, "idle", "–"
                            elif res is None:
                                result, colour, took = "Not tested yet", "idle", "–"
                            elif res.get("status") == "ok":
                                result, colour, took = "Connected", "ok", f"{res.get('duration_s', 0):.0f} s"
                            else:
                                result, colour = f"Failed: {res.get('detail') or res.get('status')}", "bad"
                                took = f"{res.get('duration_s', 0):.0f} s"
                            with ui.element("tr").style("border-top:1px solid var(--soft)"):
                                with ui.element("td").classes("py-2 pr-4 font-medium"):
                                    ui.label(spec.label)
                                with ui.element("td").classes("py-2 pr-4"):
                                    badge(result if len(result) < 90 else result[:87] + "…", colour)
                                with ui.element("td").classes("py-2 pr-4 mono text-sm"):
                                    ui.label(took)
                                with ui.element("td").classes("py-2 pr-4 mono text-sm"):
                                    version = (res or {}).get("version") or "–"
                                    if version != "–" and seat_kind(spec) == "glm":
                                        version += " via Z.ai"
                                    ui.label(version)

                connections()
                ui.label("Each seat gets a one-line prompt through the same path a real round uses, "
                         "using your subscriptions. Nothing is saved to any thread.").classes("small")

                def run_test() -> None:
                    try:
                        cfg = ctx.config()
                        keys = [s.key for s in cfg.agents.values() if h.testable(s)]
                        ctx.jobs.start_ping(ctx.workspace(), cfg, keys, level_text=ctx.level_text(cfg))
                    except CouncilError as exc:
                        ui.notify(str(exc), type="negative", multi_line=True)
                        return
                    ui.notify("Testing every seat; this takes up to a minute", type="info")
                    connections.refresh()

                test_btn.on_click(run_test)
                seen = {"key": None}

                def tick() -> None:
                    job = ctx.jobs.job
                    test_btn.set_enabled(not ctx.jobs.busy)
                    key = (job.id, job.version) if job and job.kind == "ping" else None
                    if key != seen["key"] or (job and job.kind == "ping" and job.running):
                        seen["key"] = key
                        connections.refresh()

                ui.timer(1.0, tick)

            # -- client data
            with ui.column().classes("panel"):
                ui.label("Client data").classes("h2")
                ui.label('Seats allowed on questions marked "contains client data". Many NDAs and '
                         "export rules restrict sending design data to China-based providers, so Kimi "
                         "and GLM start switched off. Check each client's terms, and each plan's "
                         "training and data-retention settings.").classes("small")
                with ui.column().classes("w-full gap-0"):
                    for spec in seats:
                        company, china = h.provider(spec)
                        with ui.row().classes("seat-row no-wrap"):
                            ui.switch().bind_value(pending["client"], spec.key)
                            with ui.column().classes("gap-0"):
                                ui.label(spec.label).classes("font-medium")
                                if company:
                                    ui.label(company).classes("small")

            # -- GitHub sync
            with ui.column().classes("panel"):
                ui.label("GitHub sync").classes("h2")
                remote = ws.remote_url()
                web = h.github_web_url(remote)

                @ui.refreshable
                def sync_state() -> None:
                    memory = ctx.state.get().get("last_push") or {}
                    unpushed = ws.unpushed_commits()
                    if remote is None:
                        ui.label("This workspace has no GitHub remote, so threads stay on this computer.") \
                            .classes("sub")
                        return
                    if web:
                        ui.link(web.replace("https://", ""), web, new_tab=True).classes("mono")
                    else:
                        ui.label(remote).classes("mono")
                    parts = []
                    if memory.get("at"):
                        result = memory.get("result", "")
                        parts.append(f"last upload {h.nice_time(memory['at'])}"
                                     if result == "pushed" else f"last upload {result}")
                    if unpushed == 0:
                        parts.append("everything is uploaded")
                    elif unpushed:
                        parts.append(f"{unpushed} commit(s) waiting to upload")
                    ui.label(" · ".join(parts).capitalize() if parts else "Not uploaded from this app yet") \
                        .classes("small")

                sync_state()
                with ui.row().classes("items-center gap-4"):
                    async def upload_now() -> None:
                        upload_btn.props("loading")
                        try:
                            result = await run.io_bound(ctx.workspace().push)
                        finally:
                            upload_btn.props(remove="loading")
                        ctx.state.record_push(result)
                        if result == "pushed":
                            ui.notify("Uploaded to GitHub", type="positive")
                        else:
                            ui.notify(f"Upload {result}", type="warning", multi_line=True)
                        sync_state.refresh()

                    upload_btn = ui.button("Upload now", icon="cloud_upload", on_click=upload_now) \
                        .props("outline")
                    upload_btn.set_enabled(remote is not None)
                    ui.switch("Upload after every round").bind_value(pending, "auto_push")
                ui.label("A failed upload never stops a run. Your desktop reads threads by pulling its "
                         "council-workspace copy.").classes("small")

            # -- workspace
            with ui.column().classes("panel"):
                ui.label("Workspace").classes("h2")
                ui.label(str(ws.root)).classes("mono")
                ui.label("Threads are plain files, committed to git after every round.").classes("small")

                def open_ws() -> None:
                    error = h.open_in_system(ws.root)
                    if error:
                        ui.notify(error, type="warning")

                ui.button("Open folder", icon="folder_open", on_click=open_ws).props("outline")

            # -- save
            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("Discard changes", on_click=lambda: ui.navigate.reload()).props("flat")

                async def save() -> None:
                    path = ws.config_path
                    changed = []
                    try:
                        for key, levels in pending["levels"].items():
                            if levels != original["levels"][key]:
                                set_levels(path, key, levels)
                                changed.append(f"{config.agents[key].label} level")
                        for key, value in pending["client"].items():
                            if value != original["client"][key]:
                                set_agent_value(path, key, "client_data", bool(value))
                                changed.append(f"{config.agents[key].label} client data")
                        minutes = pending["timeout"]
                        if minutes is None or float(minutes) < 1:
                            raise CouncilError("the time limit must be at least 1 minute")
                        if float(minutes) != float(original["timeout"]):
                            whole = float(minutes).is_integer()
                            set_default(path, "timeout_minutes", int(minutes) if whole else float(minutes))
                            changed.append("time limit")
                        if pending["auto_push"] != original["auto_push"]:
                            set_default(path, "auto_push", bool(pending["auto_push"]))
                            changed.append("GitHub upload")
                    except CouncilError as exc:
                        ui.notify(f"Not saved: {exc}", type="negative", multi_line=True, close_button=True)
                        return
                    if not changed:
                        ui.notify("Nothing changed")
                        return
                    ws_save = ctx.workspace()
                    ws_save.auto_push = load_config(path).auto_push
                    await run.io_bound(ws_save.commit, [path],
                                       "council.toml: " + ", ".join(changed) + " changed in the app")
                    ctx.state.record_push(ws_save.last_push)
                    original["levels"] = {k: dict(v) for k, v in pending["levels"].items()}
                    original["client"] = dict(pending["client"])
                    original["timeout"] = pending["timeout"]
                    original["auto_push"] = pending["auto_push"]
                    ui.notify("Saved to council.toml. The next run uses these settings.", type="positive")

                ui.button("Save settings", icon="save", on_click=save)


ICON_PATH = Path(__file__).resolve().parent / "assets" / "council.ico"


def serve(root: Path, *, port: int = 8090, show: bool = True, native: bool = False,
          state_path: Path | None = None) -> None:
    """Start Council. native=True opens its own window (pywebview); otherwise a browser tab."""
    ctx = Context(root, state_path)
    build(ctx)
    app.add_middleware(LocalOnly, port=port)
    app.on_shutdown(ctx.jobs.shutdown)
    url = f"http://127.0.0.1:{port}/"
    if native:
        app.native.window_args.update(
            maximized=True,
            min_size=(900, 600),
            text_select=True,    # replies can be selected and copied
            zoomable=True,       # Ctrl + and Ctrl - work, as in a browser
            confirm_close=True,  # closing the window stops a run, so ask first
            background_color="#EDEFEC",
        )
        app.native.start_args["localization"] = {
            "global.quitConfirmation": "Close Council? A run in progress stops; finished rounds stay saved.",
        }
        print(f"Council is open in its own window (served at {url}).")
    else:
        print(f"Council is running at {url}")
        print("Keep this window open while you use it. Close it (or press Ctrl+C) to quit;")
        print("that also stops any run in progress.")
    ui.run(
        host="127.0.0.1",
        port=port,
        title="Council",
        favicon=ICON_PATH,
        reload=False,
        native=native,
        window_size=(1360, 880) if native else None,
        show=("/" if show else False) if not native else False,
        show_welcome_message=False,
    )
