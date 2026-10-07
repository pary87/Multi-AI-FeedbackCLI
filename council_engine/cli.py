"""Command-line front end. Every command is a thin wrapper over runner/store calls,
so a GUI can offer the same operations by calling those functions directly."""

from __future__ import annotations

import argparse
import shutil
import sys
import threading
from pathlib import Path

from . import __version__
from .agents import Config, load_config, probe_version
from .runner import RunOutcome, ping, run_round, run_synthesis
from .store import CouncilError, Thread, Workspace, default_home, git_available

EXIT_OK, EXIT_INCOMPLETE, EXIT_USAGE, EXIT_INTERRUPTED = 0, 1, 2, 130


# ---------------------------------------------------------------- output helpers


def _printer():
    """Event handler that prints one line per event. Agents finish on worker
    threads, so printing is serialised with a lock."""
    lock = threading.Lock()

    def on_event(event: dict) -> None:
        kind = event.get("kind")
        if kind == "ping":
            tag = "[ping]"
        elif kind == "synthesis":
            tag = f"[{event['thread']} synthesis]"
        else:
            tag = f"[{event['thread']} round {event['round']}]"
        etype = event["type"]
        if etype == "run_started":
            line = f"{tag} running: {', '.join(event['agents'])}"
        elif etype == "agent_started":
            line = f"{tag} {event['label']:<8} started"
        elif etype == "agent_finished":
            line = f"{tag} {event['label']:<8} {event['status']} ({event['duration_s']:.0f}s)"
            if event.get("detail"):
                line += f" - {event['detail']}"
            for warning in event.get("warnings", []):
                line += f"\n{tag} {'':<8} warning: {warning}"
        elif etype == "run_finished":
            if event.get("commit"):
                line = f"{tag} saved and committed ({event['commit']})"
            else:
                line = f"{tag} saved"
        else:
            return
        with lock:
            print(line, flush=True)

    return on_event


def _open(args) -> tuple[Workspace, Config]:
    ws = Workspace.open(Path(args.home))
    return ws, load_config(ws.config_path)


def _read_question(args) -> str:
    if args.question and args.question_file:
        raise CouncilError("give the question with -q or -f, not both")
    if args.question:
        return args.question
    if args.question_file == "-":
        return sys.stdin.read()
    if args.question_file:
        path = Path(args.question_file).expanduser()
        if not path.is_file():
            raise CouncilError(f"question file not found: {path}")
        return path.read_text(encoding="utf-8")
    raise CouncilError('no question given: use -q "text" or -f question.md (or -f - for stdin)')


def _participants(config: Config, agents_arg: str | None) -> list[str]:
    if agents_arg:
        keys = [a.strip() for a in agents_arg.split(",") if a.strip()]
        for key in keys:
            if not config.get(key).enabled:
                raise CouncilError(f"agent '{key}' is disabled in council.toml")
        return keys
    keys = [spec.key for spec in config.enabled()]
    if not keys:
        raise CouncilError("every agent is disabled in council.toml")
    return keys


def _timeout(args) -> float | None:
    minutes = getattr(args, "timeout_min", None)
    return minutes * 60 if minutes else None


def _report_round(thread: Thread, outcome: RunOutcome) -> int:
    if outcome.complete:
        print(f"{thread.id}: round {outcome.round_no} complete -> {thread.path}")
        return EXIT_OK
    missing = ", ".join(thread.missing_agents(outcome.round_no))
    print(
        f"{thread.id}: round {outcome.round_no} incomplete (no response from {missing}).\n"
        f"  retry them:        python -m council_engine retry {thread.id}\n"
        f"  or move on:        python -m council_engine round {thread.id} --allow-partial"
    )
    return EXIT_INCOMPLETE


def _run_rounds(thread: Thread, config: Config, target: int, args, on_event) -> int:
    """Run rounds until `target` complete rounds exist; stop at the first gap."""
    if target < 1:
        raise CouncilError("--rounds must be at least 1")
    while True:
        latest = thread.latest_round()
        if latest and not thread.round_complete(latest):
            missing = ", ".join(thread.missing_agents(latest))
            print(f"{thread.id}: round {latest} is incomplete (no response from {missing}); "
                  f"run `python -m council_engine retry {thread.id}` first")
            return EXIT_INCOMPLETE
        if latest >= target:
            return EXIT_OK
        outcome = run_round(
            thread,
            config,
            timeout_s=_timeout(args),
            keep_views=args.keep_views,
            on_event=on_event,
        )
        code = _report_round(thread, outcome)
        if code != EXIT_OK:
            return code


# ---------------------------------------------------------------------- commands


def cmd_init(args) -> int:
    ws, done = Workspace.init(Path(args.home))
    for line in done:
        print(f"  {line}")
    print(f"workspace ready: {ws.root}")
    print("next: python -m council_engine doctor --ping")
    return EXIT_OK


def cmd_new(args) -> int:
    ws, config = _open(args)
    thread = ws.create_thread(
        args.title,
        _read_question(args),
        _participants(config, args.agents),
        [Path(p) for p in args.attach or []],
    )
    print(f"created {thread.id}: {thread.path}")
    print(f"participants: {', '.join(thread.participants)}")
    print(f"next: python -m council_engine round {thread.id}")
    return EXIT_OK


def cmd_ask(args) -> int:
    ws, config = _open(args)
    thread = ws.create_thread(
        args.title,
        _read_question(args),
        _participants(config, args.agents),
        [Path(p) for p in args.attach or []],
    )
    print(f"created {thread.id}: {thread.path}")
    on_event = _printer()
    code = _run_rounds(thread, config, args.rounds, args, on_event)
    if code == EXIT_OK and args.synth:
        code = _synth(thread, config, args.synth, args, on_event)
    return code


def cmd_round(args) -> int:
    ws, config = _open(args)
    thread = ws.thread(args.thread)
    outcome = run_round(
        thread,
        config,
        allow_partial=args.allow_partial,
        timeout_s=_timeout(args),
        keep_views=args.keep_views,
        on_event=_printer(),
    )
    return _report_round(thread, outcome)


def cmd_run(args) -> int:
    ws, config = _open(args)
    thread = ws.thread(args.thread)
    on_event = _printer()
    code = _run_rounds(thread, config, args.rounds, args, on_event)
    if code == EXIT_OK and args.synth:
        code = _synth(thread, config, args.synth, args, on_event)
    return code


def cmd_retry(args) -> int:
    ws, config = _open(args)
    thread = ws.thread(args.thread)
    only = [a.strip() for a in args.only.split(",")] if args.only else None
    outcome = run_round(
        thread,
        config,
        retry=True,
        only=only,
        force=args.force,
        timeout_s=_timeout(args),
        keep_views=args.keep_views,
        on_event=_printer(),
    )
    return _report_round(thread, outcome)


def _synth(thread: Thread, config: Config, by: str, args, on_event) -> int:
    outcome = run_synthesis(
        thread,
        config,
        by,
        allow_partial=getattr(args, "allow_partial", False),
        timeout_s=_timeout(args),
        keep_views=args.keep_views,
        on_event=on_event,
    )
    if outcome.complete:
        print(f"{thread.id}: synthesis -> {thread.synthesis_path(by)}")
        return EXIT_OK
    print(f"{thread.id}: synthesis by {by} failed: {outcome.results[by].detail}")
    return EXIT_INCOMPLETE


def cmd_synth(args) -> int:
    ws, config = _open(args)
    return _synth(ws.thread(args.thread), config, args.by, args, _printer())


def cmd_status(args) -> int:
    ws, _config = _open(args)
    if not args.thread:
        dirs = ws.thread_dirs()
        if not dirs:
            print(f"no threads yet in {ws.threads_dir}")
            return EXIT_OK
        for path in dirs:
            thread = Thread.load(ws, path)
            print(f"{thread.id}  {thread.state():<34}  {thread.title}")
        return EXIT_OK
    thread = ws.thread(args.thread)
    print(f"{thread.id}: {thread.title}")
    print(f"folder:       {thread.path}")
    print(f"participants: {', '.join(thread.participants)}")
    print(f"state:        {thread.state()}")
    for record in thread.rounds:
        print(f"round {record['round']}:")
        for agent in thread.participants:
            entry = record["agents"].get(agent)
            if entry is None:
                print(f"  {agent:<8} not run")
                continue
            line = f"  {agent:<8} {entry['status']:<9} {entry.get('file', '')}"
            if entry.get("detail"):
                line += f"  ({entry['detail']})"
            print(line)
    for synth in thread.manifest.get("syntheses", []):
        print(f"synthesis by {synth['by']} after round {synth['after_round']}: "
              f"{synth['status']} {synth.get('file', '')}")
    return EXIT_OK


def cmd_doctor(args) -> int:
    ws, config = _open(args)
    print(f"council_engine {__version__}, python {sys.version.split()[0]}")
    git = "yes" if ws.is_git_repo() else ("installed, workspace not a repo" if git_available() else "not found")
    print(f"workspace: {ws.root}  (git: {git})")
    problems = 0
    selected = [s.strip() for s in args.agents.split(",")] if args.agents else None
    specs = [config.get(a) for a in selected] if selected else list(config.agents.values())
    for spec in specs:
        exe = shutil.which(spec.command[0])
        if not spec.enabled:
            print(f"  {spec.key:<8} disabled in council.toml")
            continue
        if exe is None:
            problems += 1
            print(f"  {spec.key:<8} NOT FOUND: '{spec.command[0]}' is not on PATH")
            if spec.install_hint:
                print(f"  {'':<8} install: {spec.install_hint}")
            continue
        version = probe_version(spec) or "version unknown"
        print(f"  {spec.key:<8} {version}  [{exe}]")
    if args.ping:
        print("pinging each agent through the real launch path (may take a minute)...")
        enabled = [s.key for s in specs if s.enabled and shutil.which(s.command[0])]
        if enabled:
            outcome = ping(ws, config, enabled, on_event=_printer())
            problems += sum(1 for r in outcome.results.values() if not r.ok)
    if problems:
        print(f"{problems} problem(s) found")
        return EXIT_INCOMPLETE
    print("all good" if args.ping else "all agents found (add --ping to test logins)")
    return EXIT_OK


# ------------------------------------------------------------------------ parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m council_engine",
        description="Put one question to several AI CLIs in rounds; each round reads the last.",
    )
    parser.add_argument(
        "--home",
        default=str(default_home()),
        help="workspace folder (default: $COUNCIL_HOME or ~/council)",
    )
    parser.add_argument("--version", action="version", version=f"council_engine {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def run_opts(p):
        p.add_argument("--timeout-min", type=float, help="per-agent timeout in minutes")
        p.add_argument("--keep-views", action="store_true",
                       help="keep each agent's temp working folder for debugging")

    def question_opts(p):
        p.add_argument("title", help="short title, used in the folder name")
        p.add_argument("-q", "--question", help="the question text")
        p.add_argument("-f", "--question-file", help="file holding the question ('-' = stdin)")
        p.add_argument("--attach", action="append", metavar="FILE",
                       help="file every agent may read (repeatable)")
        p.add_argument("--agents", help="comma list of participants (default: all enabled)")

    p = sub.add_parser("init", help="create the workspace, config and git repo")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("doctor", help="check each CLI is installed; --ping also tests logins")
    p.add_argument("--ping", action="store_true", help="send each agent a one-word test prompt")
    p.add_argument("--agents", help="comma list (default: all configured)")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("new", help="create a thread without running it")
    question_opts(p)
    p.set_defaults(func=cmd_new)

    p = sub.add_parser("ask", help="create a thread and run it (default: 2 rounds)")
    question_opts(p)
    p.add_argument("--rounds", type=int, default=2, help="rounds to run (default 2)")
    p.add_argument("--synth", metavar="AGENT", help="then have AGENT write the synthesis")
    run_opts(p)
    p.set_defaults(func=cmd_ask)

    p = sub.add_parser("round", help="run the next round of a thread")
    p.add_argument("thread", help="T-0001, 1, or the folder name")
    p.add_argument("--allow-partial", action="store_true",
                   help="proceed even if the previous round is missing responses")
    run_opts(p)
    p.set_defaults(func=cmd_round)

    p = sub.add_parser("run", help="run rounds until the thread has N complete rounds")
    p.add_argument("thread")
    p.add_argument("--rounds", type=int, required=True)
    p.add_argument("--synth", metavar="AGENT", help="then have AGENT write the synthesis")
    run_opts(p)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("retry", help="re-run agents missing from the latest round")
    p.add_argument("thread")
    p.add_argument("--only", help="comma list of agents to re-run")
    p.add_argument("--force", action="store_true", help="also replace responses that succeeded")
    run_opts(p)
    p.set_defaults(func=cmd_retry)

    p = sub.add_parser("synth", help="have one agent write the synthesis of the thread")
    p.add_argument("thread")
    p.add_argument("--by", required=True, metavar="AGENT")
    p.add_argument("--allow-partial", action="store_true")
    run_opts(p)
    p.set_defaults(func=cmd_synth)

    p = sub.add_parser("status", help="list threads, or show one thread's rounds")
    p.add_argument("thread", nargs="?")
    p.set_defaults(func=cmd_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    # Windows consoles default to a legacy code page; never crash printing a reply.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except CouncilError as exc:
        print(f"council: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except KeyboardInterrupt:
        print("\ncouncil: interrupted; running agents were stopped and nothing was saved",
              file=sys.stderr)
        return EXIT_INTERRUPTED
