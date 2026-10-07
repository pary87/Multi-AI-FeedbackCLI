"""Change council.toml in place, keeping its comments and layout.

Python's tomllib can read TOML but not write it, so these helpers change single
values and command arrays by careful text edits. Every edit is re-read with
load_config afterwards; if the result is not valid, the original file is put
back and the error is raised. The app's Settings screen is built on these.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .agents import AgentSpec, load_config
from .store import CouncilError, write_text

_HEADER_RE = re.compile(r"^\s*\[\[?\s*([^\]]+?)\s*\]\]?\s*(#.*)?$")

# The level knobs the app offers, per kind of seat. "default" removes the flag,
# so the CLI uses whatever your plan or its own settings default to.
CLAUDE_MODELS = ["default", "best", "opus", "sonnet"]
CLAUDE_EFFORTS = ["default", "low", "medium", "high", "xhigh", "max"]
CODEX_EFFORTS = ["default", "minimal", "low", "medium", "high", "xhigh"]
GLM_MODELS = ["glm-5.3", "glm-5.3-flash"]


# --------------------------------------------------------------- text surgery


def _table_span(lines: list[str], table: str) -> tuple[int, int] | None:
    """(header line, first line after the table) for [table], or None."""
    start = None
    for i, line in enumerate(lines):
        match = _HEADER_RE.match(line)
        if not match:
            continue
        if start is not None:
            return start, i
        if match.group(1) == table:
            start = i
    return (start, len(lines)) if start is not None else None


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)  # a valid TOML basic string
    raise CouncilError(f"cannot write {value!r} to council.toml")


def _split_value_and_comment(rest: str) -> tuple[str, str]:
    """Split what follows `key =` into (value, trailing comment incl. spaces)."""
    i, quote = 0, None
    while i < len(rest):
        ch = rest[i]
        if quote:
            if ch == "\\" and quote == '"':
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#":
            j = i
            while j > 0 and rest[j - 1] in " \t":
                j -= 1
            return rest[:j], rest[j:]
        i += 1
    return rest.rstrip("\r\n"), ""


def set_value_text(text: str, table: str, key: str, value) -> str:
    """Set `key = value` inside [table]; keep a trailing comment if there is one."""
    lines = text.splitlines(keepends=True)
    span = _table_span(lines, table)
    literal = _toml_value(value)
    if span is None:
        if table != "defaults":
            raise CouncilError(f"council.toml has no [{table}] table")
        return f"[defaults]\n{key} = {literal}\n\n" + text
    start, end = span
    key_re = re.compile(rf"^(\s*{re.escape(key)}\s*=\s*)(.*)$", re.S)
    for i in range(start + 1, end):
        match = key_re.match(lines[i].rstrip("\r\n"))
        if match:
            old_value, comment = _split_value_and_comment(match.group(2))
            if old_value.strip().startswith("[") and not old_value.strip().endswith("]"):
                raise CouncilError(f"[{table}] {key} spans several lines; edit it by hand")
            lines[i] = f"{match.group(1)}{literal}{comment}\n"
            return "".join(lines)
    lines.insert(start + 1, f"{key} = {literal}\n")
    return "".join(lines)


def _format_command(args: list[str]) -> str:
    """One line per flag and its value, like the hand-written defaults. These CLIs'
    flags take at most one value, so a second loose value (such as the trailing
    instruction) starts its own line."""
    groups: list[list[str]] = []
    for arg in args:
        if not groups or arg.startswith("-") or len(groups[-1]) >= 2:
            groups.append([arg])
        else:
            groups[-1].append(arg)
    body = "".join("  " + ", ".join(json.dumps(a, ensure_ascii=False) for a in g) + ",\n" for g in groups)
    return "command = [\n" + body + "]"


def set_command_text(text: str, agent: str, args: list[str]) -> str:
    """Replace the `command = [...]` array of [agents.<agent>]."""
    lines = text.splitlines(keepends=True)
    span = _table_span(lines, f"agents.{agent}")
    if span is None:
        raise CouncilError(f"council.toml has no [agents.{agent}] table")
    start, end = span
    offset = sum(len(line) for line in lines[: start + 1])
    region = "".join(lines[start + 1 : end])
    match = re.search(r"^\s*command\s*=\s*\[", region, flags=re.M)
    if not match:
        raise CouncilError(f"[agents.{agent}] has no command")
    begin = match.start() + len(match.group(0)) - len(match.group(0).lstrip())
    i, depth, quote = match.end(), 1, None
    while i < len(region) and depth:
        ch = region[i]
        if quote:
            if ch == "\\" and quote == '"':
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#":
            while i < len(region) and region[i] != "\n":
                i += 1
            continue
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        i += 1
    if depth:
        raise CouncilError(f"[agents.{agent}] command array is not closed")
    absolute_begin, absolute_end = offset + begin, offset + i
    return text[:absolute_begin] + _format_command(args) + text[absolute_end:]


def _write_checked(path: Path, new_text: str) -> None:
    original = path.read_text(encoding="utf-8")
    write_text(path, new_text)
    try:
        load_config(path)
    except CouncilError:
        write_text(path, original)
        raise


# ------------------------------------------------------------- public helpers


def set_default(path: Path, key: str, value) -> None:
    """Set a [defaults] value, e.g. timeout_minutes or auto_push."""
    _write_checked(path, set_value_text(path.read_text(encoding="utf-8"), "defaults", key, value))


def set_agent_value(path: Path, agent: str, key: str, value) -> None:
    """Set a simple value on one seat, e.g. client_data or enabled."""
    _write_checked(path, set_value_text(path.read_text(encoding="utf-8"), f"agents.{agent}", key, value))


def seat_kind(spec: AgentSpec) -> str:
    """'claude', 'glm' (Claude Code pointed elsewhere), 'codex', or 'other'."""
    exe = Path(spec.command[0]).name.lower()
    exe = re.sub(r"\.(exe|cmd|bat)$", "", exe)
    if exe == "claude":
        return "glm" if "ANTHROPIC_BASE_URL" in spec.env else "claude"
    if exe == "codex":
        return "codex"
    return "other"


def _flag_value(cmd: list[str], flag: str) -> str | None:
    for i, arg in enumerate(cmd[:-1]):
        if arg == flag:
            return cmd[i + 1]
    return None


def _without_flag(cmd: list[str], flag: str, value_prefix: str | None = None) -> list[str]:
    out, i = [], 0
    while i < len(cmd):
        if cmd[i] == flag and i + 1 < len(cmd) and (
            value_prefix is None or cmd[i + 1].startswith(value_prefix)
        ):
            i += 2
            continue
        out.append(cmd[i])
        i += 1
    return out


def get_levels(spec: AgentSpec) -> dict[str, str]:
    kind = seat_kind(spec)
    cmd = spec.command
    if kind == "claude":
        return {"model": _flag_value(cmd, "--model") or "default",
                "effort": _flag_value(cmd, "--effort") or "default"}
    if kind == "codex":
        for i, arg in enumerate(cmd[:-1]):
            if arg == "-c" and cmd[i + 1].startswith("model_reasoning_effort="):
                return {"reasoning": cmd[i + 1].split("=", 1)[1].strip('"')}
        return {"reasoning": "default"}
    if kind == "glm":
        return {"model": _flag_value(cmd, "--model") or GLM_MODELS[0]}
    return {}


def level_choices(spec: AgentSpec) -> dict[str, list[str]]:
    return {
        "claude": {"model": CLAUDE_MODELS, "effort": CLAUDE_EFFORTS},
        "codex": {"reasoning": CODEX_EFFORTS},
        "glm": {"model": GLM_MODELS},
    }.get(seat_kind(spec), {})


def apply_levels(spec: AgentSpec, levels: dict[str, str]) -> list[str]:
    """The seat's command with the requested levels applied."""
    choices = level_choices(spec)
    for knob, value in levels.items():
        if knob not in choices:
            raise CouncilError(f"{spec.label} has no '{knob}' setting")
        if value not in choices[knob]:
            raise CouncilError(f"{spec.label} {knob} must be one of: {', '.join(choices[knob])}")
    kind = seat_kind(spec)
    cmd = list(spec.command)
    if kind == "claude":
        current = get_levels(spec)
        model = levels.get("model", current["model"])
        effort = levels.get("effort", current["effort"])
        cmd = _without_flag(_without_flag(cmd, "--model"), "--effort")
        if model != "default":
            cmd += ["--model", model]
        if effort != "default":
            cmd += ["--effort", effort]
    elif kind == "codex":
        reasoning = levels.get("reasoning", get_levels(spec)["reasoning"])
        cmd = _without_flag(cmd, "-c", "model_reasoning_effort=")
        if reasoning != "default":
            at = cmd.index("exec") + 1 if "exec" in cmd else 1
            cmd[at:at] = ["-c", f"model_reasoning_effort={reasoning}"]
    elif kind == "glm":
        model = levels.get("model", get_levels(spec)["model"])
        if "--model" in cmd:
            cmd[cmd.index("--model") + 1] = model
        else:
            cmd += ["--model", model]
    return cmd


def set_levels(path: Path, agent: str, levels: dict[str, str]) -> None:
    spec = load_config(path).get(agent)
    new_cmd = apply_levels(spec, levels)
    if new_cmd == spec.command:
        return
    _write_checked(path, set_command_text(path.read_text(encoding="utf-8"), agent, new_cmd))
