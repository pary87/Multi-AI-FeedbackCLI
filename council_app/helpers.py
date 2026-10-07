"""Small pure helpers the pages share: wording, dates, thread summaries."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from council_engine.agents import AgentSpec, Config
from council_engine.config_edit import get_levels, level_choices, seat_kind
from council_engine.store import Thread

# Who runs each kind of seat, for the client-data screen.
PROVIDERS = {
    "claude": ("Anthropic", False),
    "chatgpt": ("OpenAI", False),
    "codex": ("OpenAI", False),
    "kimi": ("Moonshot AI, China-based", True),
    "glm": ("Z.ai (Zhipu), China-based", True),
}

CLAUDE_MODEL_NAMES = {
    "default": "Default model",
    "best": "Best available",
    "opus": "Opus",
    "sonnet": "Sonnet",
}
KNOB_LABELS = {"model": "Model", "effort": "Thinking effort", "reasoning": "Reasoning"}


def provider(spec: AgentSpec) -> tuple[str, bool]:
    """(company, china_based) for a seat, by its name or the CLI it runs."""
    if spec.key in PROVIDERS:
        return PROVIDERS[spec.key]
    kind = seat_kind(spec)
    if kind in PROVIDERS:
        return PROVIDERS[kind]
    return ("", False)


def choice_label(spec: AgentSpec, knob: str, value: str) -> str:
    kind = seat_kind(spec)
    if knob == "model" and kind == "claude":
        return {
            "default": "Default (what your plan picks)",
            "best": "Best available (Fable if your plan has it, else Opus)",
            "opus": "Opus",
            "sonnet": "Sonnet (faster)",
        }.get(value, value)
    if knob == "model" and kind == "glm":
        return {"glm-5.3": "GLM-5.3", "glm-5.3-flash": "GLM-5.3-Flash (faster)"}.get(value, value)
    if value == "default":
        return "Default"
    if value == "max":
        return "max (slowest)"
    if value == "xhigh" and kind == "codex":
        return "xhigh (some models)"
    return value


def describe_levels(spec: AgentSpec, levels: dict[str, str] | None = None) -> str:
    """One line such as 'Best available · high effort'."""
    kind = seat_kind(spec)
    levels = levels if levels is not None else get_levels(spec)
    if kind == "claude":
        model = CLAUDE_MODEL_NAMES.get(levels.get("model", "default"), levels.get("model"))
        effort = levels.get("effort", "default")
        return f"{model} · {'default' if effort == 'default' else effort} effort"
    if kind == "codex":
        reasoning = levels.get("reasoning", "default")
        return f"Default model · {'default' if reasoning == 'default' else reasoning} reasoning"
    if kind == "glm":
        return choice_label(spec, "model", levels.get("model", "glm-5.3")).replace(" (faster)", "")
    if spec.key == "kimi" or Path(spec.command[0]).stem.lower() == "kimi":
        return "Kimi Code · one level on your plan"
    return ""


def has_levels(spec: AgentSpec) -> bool:
    return bool(level_choices(spec))


def missing_env(spec: AgentSpec) -> list[str]:
    return [name for name in spec.env_refs() if not os.environ.get(name)]


def installed(spec: AgentSpec) -> bool:
    return shutil.which(spec.command[0]) is not None


def testable(spec: AgentSpec) -> bool:
    return spec.enabled and installed(spec) and not missing_env(spec)


def seat_problem(spec: AgentSpec) -> str:
    """Why a seat cannot run right now, or ''."""
    if not spec.enabled:
        return "switched off in council.toml"
    if not installed(spec):
        return f"'{spec.command[0]}' is not installed or not on PATH"
    missing = missing_env(spec)
    if missing:
        return f"{', '.join(missing)} is not set"
    return ""


# ----------------------------------------------------------------------- dates


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).astimezone()
    except ValueError:
        return None


def _hm(dt: datetime) -> str:
    hour = dt.hour % 12 or 12
    return f"{hour}:{dt.minute:02d} {'AM' if dt.hour < 12 else 'PM'}"


def nice_time(value: str | None, now: datetime | None = None) -> str:
    """'Today, 5:40 PM', 'Yesterday, 9:02 AM' or 'Oct 7, 2026, 3:20 AM'."""
    dt = parse_iso(value)
    if dt is None:
        return ""
    now = now or datetime.now().astimezone()
    days = (now.date() - dt.date()).days
    if days == 0:
        return f"Today, {_hm(dt)}"
    if days == 1:
        return f"Yesterday, {_hm(dt)}"
    return f"{dt.strftime('%b')} {dt.day}, {dt.year}, {_hm(dt)}"


# --------------------------------------------------------------------- threads


def synth_entries(thread: Thread) -> list[dict]:
    """Successful syntheses, newest per writer, in the order they were written."""
    latest: dict[str, dict] = {}
    for entry in thread.manifest.get("syntheses", []):
        if entry.get("status") == "ok" and entry.get("file"):
            latest[entry["by"]] = entry
    return sorted(latest.values(), key=lambda e: e.get("finished", ""))


def thread_status(thread: Thread, running_id: str | None, running_step: str = "") -> tuple[str, str]:
    """(text, colour name) for a thread's status badge."""
    if running_id == thread.id:
        return (f"Running · {running_step.lower()}" if running_step else "Running", "running")
    latest = thread.latest_round()
    if latest == 0:
        return ("No rounds yet", "idle")
    if not thread.round_complete(latest):
        return (f"Round {latest} incomplete", "warn")
    planned = (thread.manifest.get("plan") or {}).get("rounds")
    if planned and latest < planned:
        return (f"{latest} of {planned} rounds", "warn")
    return ("Complete", "ok")


def thread_row(thread: Thread, config: Config, running_id: str | None, running_step: str = "") -> dict:
    status, colour = thread_status(thread, running_id, running_step)
    planned = (thread.manifest.get("plan") or {}).get("rounds")
    done = sum(1 for r in thread.rounds if thread.round_complete(r["round"]))
    labels = [_label(config, e["by"]) for e in synth_entries(thread)]
    plan_by = (thread.manifest.get("plan") or {}).get("synthesis_by")
    if not labels and plan_by and running_id == thread.id:
        labels = [f"{_label(config, plan_by)}, pending"]
    created = thread.manifest.get("created", "")
    return {
        "id": thread.id,
        "title": thread.title,
        "status": status,
        "colour": colour,
        "rounds": f"{done} of {planned}" if planned else str(done),
        "synthesis": ", ".join(labels) or "–",
        "started": nice_time(created),
        "sort": created,
        "client": "Yes" if thread.client_data else "",
    }


def _label(config: Config, key: str) -> str:
    return config.agents[key].label if key in config.agents else key


def preview(text: str | None, length: int = 260) -> str:
    """The first real sentence or two of a reply, as plain text."""
    if not text:
        return ""
    for block in re.split(r"\n\s*\n", text):
        if block.strip().startswith("```"):
            continue
        lines = [line for line in block.strip().splitlines()
                 if line.strip() and not line.lstrip().startswith(("#", "|", "---", "```"))]
        if not lines:
            continue
        plain = re.sub(r"[*_`>#]+", "", " ".join(lines))
        plain = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", plain)
        plain = " ".join(plain.split())
        return plain if len(plain) <= length else plain[: length - 1].rstrip() + "…"
    return ""


def safe_filename(name: str) -> str:
    """A plain file name from an uploaded one: no folders, no odd characters."""
    name = Path(name.replace("\\", "/")).name
    name = re.sub(r"[^A-Za-z0-9._ ()-]+", "_", name).strip(" .")
    return name[:120] or "attachment"


# ------------------------------------------------------------------- the desktop


def open_in_system(path: Path) -> str:
    """Open a folder or file the way double-clicking it would. Returns '' or an error."""
    try:
        if os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        elif shutil.which("xdg-open"):
            subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        else:
            return f"no file browser found; the path is {path}"
    except OSError as exc:
        return str(exc)
    return ""


def github_web_url(remote: str | None) -> str | None:
    """https://github.com/owner/repo for an https or ssh GitHub remote, else None."""
    if not remote:
        return None
    match = re.match(r"^(?:https://(?:[^@/]+@)?github\.com/|git@github\.com:)([^/]+)/(.+?)(?:\.git)?/?$",
                     remote)
    return f"https://github.com/{match.group(1)}/{match.group(2)}" if match else None
