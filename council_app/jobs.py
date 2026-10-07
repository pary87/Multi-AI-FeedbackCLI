"""Background runs for the app.

One run at a time (a council run, one extra round, a retry, a synthesis, or a
connection test). The run happens on a worker thread; the pages poll the Job
object for live state. Stop sets an event the engine checks every half second;
the engine then kills every agent process it started and saves nothing from the
unfinished step.

This module has no NiceGUI code, so it can be tested on its own.
"""

from __future__ import annotations

import itertools
import json
import threading
import time
import traceback
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

from council_engine.agents import Config
from council_engine.config_edit import apply_levels
from council_engine.runner import RunStopped, ping, run_round, run_synthesis
from council_engine.store import CouncilError, Thread, Workspace, now_iso, write_text

FINISHED = {"ok", "failed", "timeout", "not_found", "error", "cancelled"}


def _clock() -> str:
    return datetime.now().strftime("%H:%M:%S")


def fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return ""
    seconds = int(round(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


# ------------------------------------------------------------------ app state


class AppState:
    """Small per-computer memory: the last connection test and the last upload.

    Kept outside the workspace (in ~/.council-app/state.json), so it is never
    committed or uploaded, and keyed by workspace folder.
    """

    def __init__(self, path: Path, workspace_root: Path):
        self.path = path
        self.key = str(workspace_root)
        self._lock = threading.Lock()

    def _read_all(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def get(self) -> dict:
        with self._lock:
            return dict(self._read_all().get("workspaces", {}).get(self.key, {}))

    def update(self, **values) -> None:
        with self._lock:
            data = self._read_all()
            mine = data.setdefault("workspaces", {}).setdefault(self.key, {})
            mine.update(values)
            try:
                write_text(self.path, json.dumps(data, indent=2) + "\n")
            except OSError:
                pass  # memory only; never let this break a run

    def record_push(self, result: str | None) -> None:
        if result:
            self.update(last_push={"at": now_iso(), "result": result})

    def record_test(self, results: dict) -> None:
        merged = dict(self.get().get("last_test", {}).get("results", {}))
        merged.update(results)
        self.update(last_test={"at": now_iso(), "results": merged})


# ----------------------------------------------------------------- live state


@dataclass
class Card:
    """One agent's box on the live-run screen."""

    key: str
    label: str
    level: str = ""
    status: str = "waiting"  # waiting | thinking | ok | failed | timeout | not_found | error | cancelled
    started: float | None = None
    duration_s: float | None = None
    detail: str = ""
    warnings: list[str] = field(default_factory=list)

    def elapsed(self) -> float:
        if self.duration_s is not None:
            return self.duration_s
        if self.started is not None:
            return time.monotonic() - self.started
        return 0.0


@dataclass
class Step:
    kind: str  # round | retry | synthesis | ping
    title: str
    agents: list[str]
    round_no: int | None = None
    by: str | None = None
    state: str = "waiting"  # waiting | running | done | incomplete | stopped | skipped | error
    cards: dict[str, Card] = field(default_factory=dict)


@dataclass
class Job:
    id: int
    kind: str  # council | ping
    title: str
    thread_id: str | None
    steps: list[Step]
    timeout_s: float
    started_at: str = field(default_factory=now_iso)
    state: str = "running"  # running | done | incomplete | stopped | error
    message: str = ""
    log: list[tuple[str, str]] = field(default_factory=list)
    version: int = 0
    push: str | None = None
    finished_at: str | None = None

    @property
    def running(self) -> bool:
        return self.state == "running"

    def current_step(self) -> Step | None:
        for step in self.steps:
            if step.state == "running":
                return step
        return None

    def cards(self) -> list[Card]:
        """Cards of the step that is running, else of the last step that ran."""
        step = self.current_step()
        if step is None:
            ran = [s for s in self.steps if s.state not in ("waiting", "skipped")]
            step = ran[-1] if ran else (self.steps[0] if self.steps else None)
        return list(step.cards.values()) if step else []


def config_with_levels(config: Config, levels: dict[str, dict[str, str]]) -> Config:
    """A copy of `config` with model levels changed for one run only."""
    agents = dict(config.agents)
    for key, wanted in levels.items():
        if key in agents and wanted:
            agents[key] = replace(agents[key], command=apply_levels(agents[key], wanted))
    return replace(config, agents=agents)


class JobManager:
    def __init__(self, state: AppState):
        self.state = state
        self._lock = threading.RLock()
        self._ids = itertools.count(1)
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()
        self.job: Job | None = None  # the running job, or the most recent one

    # -- queries ---------------------------------------------------------

    @property
    def busy(self) -> bool:
        with self._lock:
            return self.job is not None and self.job.running

    def running_thread_id(self) -> str | None:
        with self._lock:
            if self.job and self.job.running:
                return self.job.thread_id
            return None

    # -- starting runs ---------------------------------------------------

    def _check_free(self) -> None:
        if self.busy:
            raise CouncilError("a run is already in progress; wait for it or stop it first")

    def _cards(self, config: Config, keys: list[str], levels: dict[str, str]) -> dict[str, Card]:
        return {
            key: Card(key=key, label=config.get(key).label, level=levels.get(key, ""))
            for key in keys
        }

    def start_council(
        self,
        ws: Workspace,
        config: Config,
        thread: Thread,
        *,
        rounds: int,
        synth_by: str | None,
        level_text: dict[str, str] | None = None,
    ) -> Job:
        """Run rounds until `rounds` complete rounds exist, then the synthesis."""
        with self._lock:
            self._check_free()
            level_text = level_text or {}
            latest = thread.latest_round()
            steps = [
                Step("round", f"Round {n}", thread.participants, round_no=n,
                     cards=self._cards(config, thread.participants, level_text))
                for n in range(latest + 1, rounds + 1)
            ]
            if synth_by:
                steps.append(Step("synthesis", f"Synthesis by {config.get(synth_by).label}",
                                  [synth_by], by=synth_by,
                                  cards=self._cards(config, [synth_by], level_text)))
            if not steps:
                raise CouncilError(f"{thread.id} already has {latest} round(s); nothing to run")
            return self._launch(ws, config, thread, "council", thread.title, steps)

    def start_retry(self, ws: Workspace, config: Config, thread: Thread,
                    level_text: dict[str, str] | None = None) -> Job:
        with self._lock:
            self._check_free()
            latest = thread.latest_round()
            missing = thread.missing_agents(latest) if latest else []
            if not missing:
                raise CouncilError(f"{thread.id}: nothing failed, so there is nothing to retry")
            step = Step("retry", f"Retry round {latest}", missing, round_no=latest,
                        cards=self._cards(config, missing, level_text or {}))
            return self._launch(ws, config, thread, "council", thread.title, [step])

    def start_synthesis(self, ws: Workspace, config: Config, thread: Thread, by: str,
                        level_text: dict[str, str] | None = None) -> Job:
        with self._lock:
            self._check_free()
            step = Step("synthesis", f"Synthesis by {config.get(by).label}", [by], by=by,
                        cards=self._cards(config, [by], level_text or {}))
            return self._launch(ws, config, thread, "council", thread.title, [step])

    def start_ping(self, ws: Workspace, config: Config, keys: list[str],
                   level_text: dict[str, str] | None = None) -> Job:
        with self._lock:
            self._check_free()
            if not keys:
                raise CouncilError("no seat can be tested: none is enabled, installed and set up")
            step = Step("ping", "Connection test", keys,
                        cards=self._cards(config, keys, level_text or {}))
            return self._launch(ws, config, None, "ping", "Connection test", [step])

    def _launch(self, ws: Workspace, config: Config, thread: Thread | None,
                kind: str, title: str, steps: list[Step]) -> Job:
        ws.auto_push = config.auto_push
        job = Job(
            id=next(self._ids),
            kind=kind,
            title=title,
            thread_id=thread.id if thread else None,
            steps=steps,
            timeout_s=config.timeout_s,
        )
        self._stop = threading.Event()
        self.job = job
        self._worker = threading.Thread(
            target=self._work, args=(job, ws, config, thread, self._stop),
            name=f"council-job-{job.id}", daemon=True,
        )
        self._worker.start()
        return job

    # -- stopping --------------------------------------------------------

    def stop(self) -> bool:
        """Ask the running job to stop. Returns False if nothing was running."""
        with self._lock:
            if not self.busy:
                return False
            self._stop.set()
            self._note(self.job, "Stop requested: closing every running agent")
            return True

    def wait(self, timeout: float | None = None) -> bool:
        worker = self._worker
        if worker is None:
            return True
        worker.join(timeout)
        return not worker.is_alive()

    def shutdown(self) -> None:
        """Called when the app closes: stop the run and give it time to clean up."""
        self.stop()
        self.wait(30)

    # -- the worker ------------------------------------------------------

    def _note(self, job: Job, text: str) -> None:
        with self._lock:
            job.log.append((_clock(), text))
            job.version += 1

    def _on_event(self, job: Job, step: Step, event: dict) -> None:
        with self._lock:
            etype = event.get("type")
            key = event.get("agent")
            card = step.cards.get(key) if key else None
            if key and card is None:
                card = step.cards[key] = Card(key=key, label=event.get("label", key))
            if etype == "run_started":
                if event.get("round"):
                    step.round_no = event["round"]
                names = ", ".join(step.cards[k].label if k in step.cards else k
                                  for k in event.get("agents", []))
                self._note(job, f"{step.title} started: {names}")
            elif etype == "agent_started":
                card.status, card.started = "thinking", time.monotonic()
            elif etype == "agent_finished":
                card.status = event.get("status", "error")
                card.duration_s = event.get("duration_s")
                card.detail = event.get("detail", "")
                card.warnings = list(event.get("warnings", []))
                if card.status == "ok":
                    self._note(job, f"{card.label} finished in {fmt_duration(card.duration_s)}")
                elif card.status != "cancelled":
                    self._note(job, f"{card.label} {card.status}: {card.detail}")
                for warning in card.warnings:
                    self._note(job, f"{card.label} warning: {warning}")
            elif etype == "run_finished":
                push = event.get("push")
                line = "Saved"
                if event.get("commit"):
                    line += f" and committed ({event['commit']})"
                if push == "pushed":
                    line += ", uploaded to GitHub"
                elif push and push.startswith("failed"):
                    line += (f". Upload to GitHub {push}; the threads are safe on this computer"
                             " and the next successful upload sends everything")
                self._note(job, line)
                if push:
                    job.push = push
                    self.state.record_push(push)
            job.version += 1

    def _work(self, job: Job, ws: Workspace, config: Config, thread: Thread | None,
              stop: threading.Event) -> None:
        step: Step | None = None
        try:
            for step in job.steps:
                if stop.is_set():
                    raise RunStopped("stopped before this step started")
                with self._lock:
                    step.state = "running"
                    job.version += 1

                def on_event(event: dict, _step: Step = step) -> None:
                    self._on_event(job, _step, event)

                if step.kind == "round":
                    outcome = run_round(thread, config, on_event=on_event, stop=stop)
                elif step.kind == "retry":
                    outcome = run_round(thread, config, retry=True, on_event=on_event, stop=stop)
                elif step.kind == "synthesis":
                    outcome = run_synthesis(thread, config, step.by, on_event=on_event, stop=stop)
                else:
                    outcome = ping(ws, config, step.agents, on_event=on_event, stop=stop,
                                   timeout_s=min(config.timeout_s, 300))
                    self.state.record_test({
                        key: {
                            "status": res.status,
                            "duration_s": res.duration_s,
                            "version": res.version,
                            "detail": res.detail,
                        }
                        for key, res in outcome.results.items()
                    })
                with self._lock:
                    step.state = "done" if outcome.complete else "incomplete"
                    job.version += 1
                if not outcome.complete and step.kind != "ping":
                    failed = [card.label for card in step.cards.values() if card.status != "ok"]
                    raise _Incomplete(
                        f"{step.title} finished without {', '.join(failed) or 'every agent'}. "
                        "What did finish is saved. Use Retry failed on the thread page."
                    )
            with self._lock:
                if job.kind == "ping":
                    bad = [c.label for c in job.steps[0].cards.values() if c.status != "ok"]
                    job.state = "incomplete" if bad else "done"
                    job.message = (f"Not connected: {', '.join(bad)}" if bad
                                   else "Every seat answered")
                else:
                    job.state, job.message = "done", "Finished"
        except _Incomplete as exc:
            self._finish(job, step, "incomplete", "incomplete", str(exc))
        except RunStopped:
            self._finish(job, step, "stopped", "stopped",
                         "Stopped. Every agent was closed and nothing from the unfinished step was saved.")
        except CouncilError as exc:
            self._finish(job, step, "error", "error", str(exc))
        except Exception as exc:  # never let the worker die silently
            self._note(job, traceback.format_exc(limit=3).strip().splitlines()[-1])
            self._finish(job, step, "error", "error", f"unexpected error: {exc}")
        finally:
            with self._lock:
                for later in job.steps:
                    if later.state == "waiting":
                        later.state = "skipped"
                job.finished_at = now_iso()
                if job.message:
                    self._note(job, job.message)
                job.version += 1

    def _finish(self, job: Job, step: Step | None, step_state: str, job_state: str,
                message: str) -> None:
        with self._lock:
            if step is not None and step.state in ("running", "incomplete"):
                step.state = step_state if step.state == "running" else step.state
            for card in (step.cards.values() if step else []):
                if card.status in ("waiting", "thinking") and job_state == "stopped":
                    card.status = "cancelled"
                    card.duration_s = card.elapsed() if card.started else None
            job.state, job.message = job_state, message
            job.version += 1


class _Incomplete(Exception):
    pass
