"""The engine: run one round, a synthesis, or a connectivity ping across the agents.

Isolation, per agent per run:
    view folder     a fresh temp folder holding only ROUND.md (+ attachments/).
                    It is the agent's working directory and is deleted afterwards.
    staging folder  one temp folder outside every view, where capture="file"
                    agents (Codex) write their reply.
Replies are copied into the thread only after every agent in the run has
finished. So in round 1 no agent can see another's answer, even by listing
files -- blindness is enforced by the file system, not just requested in text.

The engine never prints. It reports progress through an `on_event` callback
(a dict per event), which the CLI prints and a future GUI can render.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import protocol
from .agents import (
    AgentNotFound,
    AgentSpec,
    Config,
    check_batch_wrapper_args,
    child_env,
    clean_output,
    probe_version,
    redact,
    render_command,
    resolve_env,
    resolve_executable,
)
from .store import (
    LOGS_DIR,
    CouncilError,
    Thread,
    Workspace,
    now_iso,
    sha256_file,
    sha256_text,
    write_text,
)

EventHandler = Callable[[dict], None]


class RunStopped(CouncilError):
    """Raised when a run is stopped from outside (the app's Stop button)."""


def _no_events(_event: dict) -> None:
    pass


@dataclass
class AgentResult:
    agent: str
    status: str = "error"  # ok | failed | timeout | not_found | error | cancelled
    duration_s: float = 0.0
    exit_code: int | None = None
    output: str | None = None
    stderr: str = ""
    detail: str = ""
    version: str | None = None
    prompt_sha256: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def record(self) -> dict:
        entry = {
            "status": self.status,
            "finished": now_iso(),
            "duration_s": self.duration_s,
            "exit_code": self.exit_code,
            "cli_version": self.version,
            "prompt_sha256": self.prompt_sha256,
        }
        if self.detail:
            entry["detail"] = self.detail
        if self.warnings:
            entry["warnings"] = list(self.warnings)
        return entry


@dataclass
class RunOutcome:
    kind: str  # "round" | "synthesis" | "ping"
    round_no: int | None
    results: dict[str, AgentResult]
    commit: str | None = None
    complete: bool = False


# ------------------------------------------------------------- process control


class _ProcessRegistry:
    """Tracks live child processes so Ctrl+C can kill every one of them."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._procs: set[subprocess.Popen] = set()

    def add(self, proc: subprocess.Popen) -> None:
        with self._lock:
            self._procs.add(proc)

    def discard(self, proc: subprocess.Popen) -> None:
        with self._lock:
            self._procs.discard(proc)

    def kill_all(self) -> None:
        with self._lock:
            procs = list(self._procs)
        for proc in procs:
            _kill_tree(proc)


def _popen(
    argv: list[str],
    cwd: Path,
    extra_env: dict[str, str] | None = None,
    unset_env: list[str] | None = None,
) -> subprocess.Popen:
    kwargs: dict = dict(
        cwd=str(cwd),
        stdin=subprocess.DEVNULL,  # Codex appends piped stdin to the prompt; give it nothing
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=child_env(extra_env, unset_env),
    )
    # Own process group, so a timeout or Ctrl+C can kill the whole tree (the CLI
    # plus any node/shell processes it spawned), not just the top wrapper.
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(argv, **kwargs)


def _kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            # Killing a .cmd wrapper alone would orphan the node process behind it.
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                capture_output=True,
                stdin=subprocess.DEVNULL,
            )
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


def _snapshot(folder: Path) -> dict[str, str]:
    return {
        str(p.relative_to(folder)): sha256_file(p)
        for p in sorted(folder.rglob("*"))
        if p.is_file()
    }


_ERROR_LINE_RE = re.compile(r"error|fail|denied|login|log in|unauthori[sz]ed|quota|limit", re.I)
# Harmless notices that must never be reported as the reason a run failed.
_NOISE_LINE_RE = re.compile(
    r"isn't described by this version's model catalog|\[claude-code:unrecognized_model\]"
)


def _failure_reason(text: str) -> str:
    """The most informative line of an error stream: the last one that looks like
    an error, else the last line. (Kimi, for one, ends with a 'See log:' line.)"""
    lines = [
        line.strip() for line in text.splitlines()
        if line.strip() and not _NOISE_LINE_RE.search(line)
    ]
    if not lines:
        return ""
    errorish = [line for line in lines if _ERROR_LINE_RE.search(line)]
    return (errorish or lines)[-1][:300]


# ---------------------------------------------------------------- running agents


def _run_one(
    spec: AgentSpec,
    prompt_text: str,
    *,
    timeout_s: float,
    council_dir: Path,
    staging: Path,
    attachments: list[Path],
    registry: _ProcessRegistry,
    cancel: threading.Event,
    keep_views: bool,
    on_event: EventHandler,
    context: dict,
    validator: Callable[[AgentResult], None] | None = None,
) -> AgentResult:
    result = AgentResult(agent=spec.key, prompt_sha256=sha256_text(prompt_text))
    started = time.monotonic()
    view = Path(tempfile.mkdtemp(prefix=f"council-{spec.key}-"))
    on_event({"type": "agent_started", "agent": spec.key, "label": spec.label, **context})
    try:
        write_text(view / "ROUND.md", prompt_text)
        for src in attachments:
            dest = view / "attachments" / src.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
        before = _snapshot(view)
        out_file = staging / f"{spec.key}.md"
        argv = render_command(
            spec,
            {
                "instruction": protocol.INSTRUCTION,
                "output_file": str(out_file),
                "view_dir": str(view),
                "council_dir": str(council_dir),
            },
        )
        argv = resolve_executable(argv, spec)
        check_batch_wrapper_args(argv)
        extra_env, secrets = resolve_env(spec, council_dir)
        result.version = probe_version(spec)
        if cancel.is_set():
            result.status = "cancelled"
            return result

        proc = _popen(argv, view, extra_env, spec.unset_env)
        registry.add(proc)
        if cancel.is_set():  # Ctrl+C landed between the check above and registration
            _kill_tree(proc)
        timed_out = False
        try:
            stdout, stderr = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_tree(proc)
            try:
                stdout, stderr = proc.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                stdout, stderr = b"", b""
        finally:
            registry.discard(proc)

        result.exit_code = proc.returncode
        # Secrets passed via ${...} never reach a saved reply, log or event.
        result.stderr = redact(stderr.decode("utf-8", errors="replace"), secrets)
        if spec.capture == "file":
            raw = out_file.read_text(encoding="utf-8", errors="replace") if out_file.exists() else ""
        else:
            raw = stdout.decode("utf-8", errors="replace")
        text = clean_output(redact(raw, secrets), spec.strip_regex)

        after = _snapshot(view)
        changed = sorted(set(before.items()) ^ set(after.items()))
        if changed:
            names = sorted({name for name, _ in changed})
            result.warnings.append(
                "agent changed files in its private working folder (discarded): "
                + ", ".join(names)
            )

        if cancel.is_set():
            result.status, result.detail = "cancelled", "cancelled by user"
        elif timed_out:
            result.status = "timeout"
            result.detail = f"no reply within {timeout_s / 60:g} min; process tree killed"
        elif proc.returncode != 0:
            result.status = "failed"
            reason = _failure_reason(clean_output(result.stderr)) or _failure_reason(text)
            result.detail = f"exit code {proc.returncode}: " + (
                reason or "no error message (check the network and this agent's login)"
            )
        elif not text:
            result.status, result.detail = "failed", "exited normally but replied with nothing"
        else:
            result.status, result.output = "ok", text
            if validator is not None:
                validator(result)
    except AgentNotFound as exc:
        result.status, result.detail = "not_found", str(exc)
    except CouncilError as exc:
        result.status, result.detail = "error", str(exc)
    except OSError as exc:
        result.status, result.detail = "error", f"could not start: {exc}"
    finally:
        result.duration_s = round(time.monotonic() - started, 1)
        if keep_views:
            result.warnings.append(f"working folder kept at {view}")
        else:
            shutil.rmtree(view, ignore_errors=True)
        on_event(
            {
                "type": "agent_finished",
                "agent": spec.key,
                "label": spec.label,
                "status": result.status,
                "duration_s": result.duration_s,
                "detail": result.detail,
                "warnings": list(result.warnings),
                **context,
            }
        )
    return result


def run_agents(
    specs: list[AgentSpec],
    prompts: dict[str, str],
    *,
    config: Config,
    council_dir: Path,
    attachments: list[Path] | None = None,
    timeout_s: float | None = None,
    keep_views: bool = False,
    on_event: EventHandler = _no_events,
    context: dict | None = None,
    validator: Callable[[AgentResult], None] | None = None,
    stop: threading.Event | None = None,
) -> dict[str, AgentResult]:
    """Run every agent in parallel, each in its own view. Ctrl+C kills them all,
    and so does setting `stop` (raises RunStopped). Either way nothing is saved.

    `validator` may downgrade an ok result (e.g. the ping checks the reply text).
    """
    context = dict(context or {})
    staging = Path(tempfile.mkdtemp(prefix="council-staging-"))
    registry = _ProcessRegistry()
    cancel = threading.Event()
    pool = ThreadPoolExecutor(max_workers=max(1, len(specs)))
    futures = {
        pool.submit(
            _run_one,
            spec,
            prompts[spec.key],
            timeout_s=timeout_s if timeout_s is not None else config.timeout_for(spec),
            council_dir=council_dir,
            staging=staging,
            attachments=list(attachments or []),
            registry=registry,
            cancel=cancel,
            keep_views=keep_views,
            on_event=on_event,
            context=context,
            validator=validator,
        ): spec.key
        for spec in specs
    }
    results: dict[str, AgentResult] = {}
    try:
        pending = set(futures)
        while pending:
            if stop is not None and stop.is_set():
                raise RunStopped("stopped: the running agents were closed and nothing from this run was saved")
            # Short waits keep Ctrl+C responsive on Windows, where an untimed wait is not.
            done, pending = wait(pending, timeout=0.5, return_when=FIRST_COMPLETED)
            for fut in done:
                results[futures[fut]] = fut.result()
    except (KeyboardInterrupt, RunStopped):
        cancel.set()
        registry.kill_all()
        pool.shutdown(wait=True, cancel_futures=True)
        shutil.rmtree(staging, ignore_errors=True)
        raise
    pool.shutdown(wait=True)
    shutil.rmtree(staging, ignore_errors=True)
    # Report in configured order, not completion order.
    return {spec.key: results[spec.key] for spec in specs}


# ------------------------------------------------------------------- operations


def _participant_specs(thread: Thread, config: Config, keys: list[str]) -> list[AgentSpec]:
    specs = []
    for key in keys:
        spec = config.get(key)
        if not spec.enabled:
            raise CouncilError(
                f"{thread.id}: participant '{key}' is disabled in council.toml; "
                "enable it or start a new thread without it"
            )
        specs.append(spec)
    return specs


def _check_client_data(thread: Thread, specs: list[AgentSpec]) -> None:
    if not thread.client_data:
        return
    blocked = [spec.label for spec in specs if not spec.client_data]
    if blocked:
        raise CouncilError(
            f"{thread.id} contains client data, and {', '.join(blocked)} "
            f"{'is' if len(blocked) == 1 else 'are'} not cleared for client data "
            "(client_data = false in council.toml)"
        )


def _labels(thread: Thread, config: Config) -> dict[str, str]:
    return {key: config.get(key).label for key in thread.participants}


def _write_stderr_log(thread: Thread, name: str, result: AgentResult) -> str | None:
    if not result.stderr.strip():
        return None
    path = thread.path / LOGS_DIR / name
    write_text(path, clean_output(result.stderr))
    return f"{LOGS_DIR}/{name}"


def run_round(
    thread: Thread,
    config: Config,
    *,
    retry: bool = False,
    only: list[str] | None = None,
    force: bool = False,
    allow_partial: bool = False,
    timeout_s: float | None = None,
    keep_views: bool = False,
    on_event: EventHandler = _no_events,
    stop: threading.Event | None = None,
) -> RunOutcome:
    """Run the next round, or (retry=True) re-run agents in the latest round.

    Integrity rules:
      * a new round needs the previous round complete, unless allow_partial;
      * only the latest round can be retried, because later rounds were built on it;
      * a forced re-run that fails keeps the earlier good response.
    """
    latest = thread.latest_round()
    participants = thread.participants
    if only:
        unknown = [a for a in only if a not in participants]
        if unknown:
            raise CouncilError(f"{thread.id}: {', '.join(unknown)} not a participant of this thread")

    if retry:
        if latest == 0:
            raise CouncilError(f"{thread.id}: no round to retry yet; run a round first")
        round_no = latest
        if force:
            agents = only or participants
        else:
            missing = thread.missing_agents(round_no)
            agents = only or missing
            already = [a for a in agents if a not in missing]
            if already:
                raise CouncilError(
                    f"round {round_no} already has a response from {', '.join(already)}; "
                    "add --force to replace it"
                )
            if not agents:
                raise CouncilError(f"round {round_no} is already complete; nothing to retry")
    else:
        if only:
            raise CouncilError("--only applies to retries; a new round always asks every participant")
        if latest and not thread.round_complete(latest) and not allow_partial:
            missing = ", ".join(thread.missing_agents(latest))
            raise CouncilError(
                f"round {latest} is incomplete (no response from {missing}). Retry with "
                f"`council retry {thread.id}`, or continue without them using --allow-partial"
            )
        round_no = latest + 1
        agents = participants

    question = thread.question()
    labels = _labels(thread, config)
    specs = _participant_specs(thread, config, agents)
    _check_client_data(thread, specs)
    attachments = thread.attachment_paths()
    prior = [(n, thread.responses(n)) for n in range(1, round_no)]
    prompts = {
        spec.key: protocol.build_round_prompt(
            thread_id=thread.id,
            title=thread.title,
            question=question,
            round_no=round_no,
            me_key=spec.key,
            labels=labels,
            prior=prior,
            attachment_names=[p.name for p in attachments],
        )
        for spec in specs
    }
    context = {"thread": thread.id, "round": round_no, "kind": "round"}
    on_event({"type": "run_started", "agents": [s.key for s in specs], **context})
    results = run_agents(
        specs,
        prompts,
        config=config,
        council_dir=thread.workspace.council_dir,
        attachments=attachments,
        timeout_s=timeout_s,
        keep_views=keep_views,
        on_event=on_event,
        context=context,
        stop=stop,
    )

    record = thread.round_record(round_no)
    if record is None:
        record = {"round": round_no, "started": now_iso(), "agents": {}}
        thread.rounds.append(record)
    for key, res in results.items():
        previous = record["agents"].get(key)
        entry = res.record()
        log = _write_stderr_log(thread, f"r{round_no}-{key}.stderr.txt", res)
        if log:
            entry["stderr_log"] = log
        if res.ok:
            path = thread.response_path(round_no, key)
            write_text(path, res.output)
            entry["file"] = path.name
            entry["response_sha256"] = sha256_file(path)
            record["agents"][key] = entry
        elif previous and previous.get("status") == "ok":
            previous["last_rerun_failed"] = {
                "status": res.status,
                "detail": res.detail,
                "at": entry["finished"],
            }
        else:
            record["agents"][key] = entry
    record["finished"] = now_iso()
    thread.save()

    summary = ", ".join(f"{k} {r.status}" for k, r in results.items())
    verb = "retry" if retry else "round"
    commit = thread.workspace.commit([thread.path], f"{thread.id} {verb} {round_no}: {summary}")
    outcome = RunOutcome(
        "round", round_no, results, commit=commit, complete=thread.round_complete(round_no)
    )
    on_event({"type": "run_finished", "commit": commit, "complete": outcome.complete,
              "push": thread.workspace.last_push if commit else None, **context})
    return outcome


def run_synthesis(
    thread: Thread,
    config: Config,
    by: str,
    *,
    allow_partial: bool = False,
    timeout_s: float | None = None,
    keep_views: bool = False,
    on_event: EventHandler = _no_events,
    stop: threading.Event | None = None,
) -> RunOutcome:
    latest = thread.latest_round()
    if latest == 0:
        raise CouncilError(f"{thread.id}: nothing to synthesise yet; run at least one round")
    if not thread.round_complete(latest) and not allow_partial:
        missing = ", ".join(thread.missing_agents(latest))
        raise CouncilError(
            f"round {latest} is incomplete (no response from {missing}); retry it first "
            "or pass --allow-partial"
        )
    spec = config.get(by)
    if not spec.enabled:
        raise CouncilError(f"agent '{by}' is disabled in council.toml")
    _check_client_data(thread, [spec])
    labels = _labels(thread, config)
    if by not in labels:
        labels[by] = spec.label  # an outside agent may synthesise a thread it did not join
    attachments = thread.attachment_paths()
    prompt = protocol.build_synthesis_prompt(
        thread_id=thread.id,
        title=thread.title,
        question=thread.question(),
        me_key=by,
        labels=labels,
        prior=[(n, thread.responses(n)) for n in range(1, latest + 1)],
        attachment_names=[p.name for p in attachments],
    )
    context = {"thread": thread.id, "round": None, "kind": "synthesis"}
    on_event({"type": "run_started", "agents": [by], **context})
    result = run_agents(
        [spec],
        {by: prompt},
        config=config,
        council_dir=thread.workspace.council_dir,
        attachments=attachments,
        timeout_s=timeout_s,
        keep_views=keep_views,
        on_event=on_event,
        context=context,
        stop=stop,
    )[by]
    entry = result.record()
    entry.update({"by": by, "after_round": latest})
    log = _write_stderr_log(thread, f"synthesis-{by}.stderr.txt", result)
    if log:
        entry["stderr_log"] = log
    if result.ok:
        path = thread.synthesis_path(by)
        write_text(path, result.output)
        entry["file"] = path.name
        entry["response_sha256"] = sha256_file(path)
    thread.manifest.setdefault("syntheses", []).append(entry)
    thread.save()
    commit = thread.workspace.commit(
        [thread.path], f"{thread.id} synthesis by {by}: {result.status}"
    )
    outcome = RunOutcome("synthesis", None, {by: result}, commit=commit, complete=result.ok)
    on_event({"type": "run_finished", "commit": commit, "complete": result.ok,
              "push": thread.workspace.last_push if commit else None, **context})
    return outcome


def ping(
    workspace: Workspace,
    config: Config,
    agents: list[str] | None = None,
    *,
    timeout_s: float = 300,
    on_event: EventHandler = _no_events,
    stop: threading.Event | None = None,
) -> RunOutcome:
    """Send each agent a trivial prompt through the real launch path.

    This proves install + login + flags in one go, using the same isolation as
    a real round. Nothing is written to any thread.
    """
    specs = [config.get(a) for a in agents] if agents else config.enabled()
    if not specs:
        raise CouncilError("no agents to ping: every agent is disabled in council.toml")
    prompt = protocol.build_ping_prompt()
    context = {"thread": None, "round": None, "kind": "ping"}

    def expect_token(res: AgentResult) -> None:
        if protocol.PING_TOKEN not in (res.output or ""):
            res.status = "failed"
            res.detail = f"unexpected reply: {(res.output or '').strip()[:200]!r}"

    results = run_agents(
        specs,
        {s.key: prompt for s in specs},
        config=config,
        council_dir=workspace.council_dir,
        timeout_s=timeout_s,
        on_event=on_event,
        context=context,
        validator=expect_token,
        stop=stop,
    )
    return RunOutcome(
        "ping", None, results, complete=all(r.ok for r in results.values())
    )
