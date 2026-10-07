"""Agent adapters: how each vendor CLI is launched, read from council.toml.

Nothing here is specific to one vendor. Every difference between Claude Code,
Codex CLI and Kimi Code CLI lives in the config file as data, so a CLI update
that renames a flag is a one-line edit to council.toml, not a code change.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .store import CouncilError

# The flags below were checked against the --help output of
# Claude Code 2.1.292, Codex CLI 0.160.1 and Kimi Code CLI 2.1.1 (2026-10-06).
DEFAULT_CONFIG_TOML = r'''# council.toml -- which AI command-line agents take part, and exactly how each is launched.
#
# Each agent runs in its own throwaway folder containing only ROUND.md (plus any
# thread attachments). Every agent gets the same one-line instruction -- "read
# ROUND.md and follow it" -- and its final reply is saved as its response.
#
# Placeholders usable inside `command`:
#   {instruction}   the fixed one-line instruction (required)
#   {output_file}   where the agent must write its reply (required when capture = "file")
#   {view_dir}      the agent's throwaway working folder
#   {council_dir}   this workspace's .council folder
#
# capture = "stdout"  -> whatever the agent prints is its reply
# capture = "file"    -> the agent writes its reply to {output_file}
#
# Flags checked against Claude Code 2.1.292, Codex CLI 0.160.1, Kimi Code CLI 2.1.1.
# If an update renames a flag, fix it here; no code change is needed.

[defaults]
timeout_minutes = 20   # per agent, per round
auto_push = true       # after every round, upload threads to GitHub (only if the workspace has an "origin" remote)

# More ready-made seats: python -m council_engine add-agent glm   (GLM-5.3 via the Z.ai GLM Coding Plan)

[agents.claude]
label = "Claude"
enabled = true
client_data = true     # may take part in questions marked "contains client data"
# -p: non-interactive. --tools: only read/search tools exist, so it cannot write.
# --strict-mcp-config: your MCP servers stay out of council rounds.
command = [
  "claude", "-p", "{instruction}",
  "--output-format", "text",
  "--tools", "Read,Glob,Grep",
  "--permission-prompts", "none",
  "--strict-mcp-config",
  "--no-session-persistence",
]
capture = "stdout"
version_command = ["claude", "--version"]
install_hint = "Install Claude Code and log in with your Claude plan: https://docs.claude.com/en/docs/claude-code"

[agents.chatgpt]
label = "ChatGPT"
enabled = true
client_data = true
# exec: non-interactive. read-only sandbox. -o: write only the final message.
command = [
  "codex", "exec",
  "--sandbox", "read-only",
  "--skip-git-repo-check",
  "--ephemeral",
  "--color", "never",
  "-o", "{output_file}",
  "{instruction}",
]
capture = "file"
version_command = ["codex", "--version"]
install_hint = "npm install -g @openai/codex   then: codex login   (choose Sign in with ChatGPT)"

[agents.kimi]
label = "Kimi"
enabled = true
client_data = false    # China-based provider: off for client data unless you decide otherwise
# -p: non-interactive. Kimi has no read-only switch in -p mode, so --agent-file
# gives it a profile whose only tools are Read, Glob and Grep.
command = [
  "kimi", "-p", "{instruction}",
  "--output-format", "text",
  "--agent-file", "{council_dir}/kimi-council-member.md",
]
capture = "stdout"
version_command = ["kimi", "--version"]
install_hint = "npm install -g @moonshot-ai/kimi-code   then: kimi login --region global"
# strip_regex = ['(?s)\n+SOME FOOTER TEXT.*\Z']   # regexes removed from the reply, if a CLI adds noise
'''

# Ready-made seats that `python -m council_engine add-agent <name>` appends to council.toml.
PRESETS: dict[str, str] = {
    "glm": r'''
[agents.glm]
label = "GLM"
enabled = true
client_data = false    # China-based provider: off for client data unless you decide otherwise
# GLM-5.3 on the Z.ai GLM Coding Plan, run through Claude Code pointed at Z.ai
# (the setup Z.ai documents). It gets its own Claude Code profile folder, so your
# normal Claude seat and your Anthropic login are never touched or sent to Z.ai.
# Needs the user environment variable ZAI_API_KEY holding your Z.ai API key.
command = [
  "claude", "-p", "{instruction}",
  "--model", "glm-5.3",
  "--output-format", "text",
  "--tools", "Read,Glob,Grep",
  "--permission-prompts", "none",
  "--strict-mcp-config",
  "--no-session-persistence",
]
capture = "stdout"
version_command = ["claude", "--version"]
install_hint = "Uses Claude Code (already installed). Subscribe to a Z.ai GLM Coding Plan and set ZAI_API_KEY."
# Never let an Anthropic credential ride along to Z.ai: with ANTHROPIC_API_KEY set,
# Claude Code would send it as an extra header even when pointed at another server.
unset_env = ["ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"]

[agents.glm.env]
CLAUDE_CONFIG_DIR = "{home}/.claude-glm"
ANTHROPIC_BASE_URL = "https://api.z.ai/api/anthropic"
ANTHROPIC_AUTH_TOKEN = "${ZAI_API_KEY}"
ANTHROPIC_DEFAULT_OPUS_MODEL = "glm-5.3"
ANTHROPIC_DEFAULT_SONNET_MODEL = "glm-5.3"
ANTHROPIC_DEFAULT_HAIKU_MODEL = "glm-5.3-flash"
API_TIMEOUT_MS = "3000000"
''',
}

# Seats whose providers are China-based stay out of client-data threads unless
# council.toml explicitly says `client_data = true` for them.
CLIENT_DATA_OFF_BY_DEFAULT = {"kimi", "glm"}

ALLOWED_PLACEHOLDERS = {"instruction", "output_file", "view_dir", "council_dir"}
ENV_PLACEHOLDERS = {"home", "council_dir"}
PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_]+)\}")
ENV_REF_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
AGENT_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")

# Characters cmd.exe reinterprets even inside quotes when Windows launches a
# .cmd/.bat wrapper (npm-installed CLIs are such wrappers on Windows).
CMD_METACHARS = set('%^&|<>"!')


class AgentNotFound(CouncilError):
    pass


class AgentEnvMissing(CouncilError):
    pass


@dataclass
class AgentSpec:
    key: str
    label: str
    command: list[str]
    capture: str = "stdout"
    enabled: bool = True
    timeout_s: float | None = None
    version_command: list[str] | None = None
    strip_regex: list[str] = field(default_factory=list)
    install_hint: str = ""
    # Extra environment variables for this agent's process only. Values may use
    # ${NAME} (taken from your OS environment at launch) and {home}/{council_dir}.
    env: dict[str, str] = field(default_factory=dict)
    # Variables removed from this agent's process (e.g. credentials it must not see).
    unset_env: list[str] = field(default_factory=list)
    # May this seat take part in threads marked as containing client data?
    client_data: bool = True

    def env_refs(self) -> list[str]:
        """Names of the OS environment variables this agent's env table needs."""
        return sorted({n for v in self.env.values() for n in ENV_REF_RE.findall(v)})


@dataclass
class Config:
    agents: dict[str, AgentSpec]
    timeout_s: float
    auto_push: bool = True

    def enabled(self) -> list[AgentSpec]:
        return [a for a in self.agents.values() if a.enabled]

    def get(self, key: str) -> AgentSpec:
        if key not in self.agents:
            known = ", ".join(self.agents) or "none"
            raise CouncilError(f"unknown agent '{key}' (configured: {known})")
        return self.agents[key]

    def timeout_for(self, spec: AgentSpec) -> float:
        return spec.timeout_s if spec.timeout_s is not None else self.timeout_s


def _str_list(value, where: str) -> list[str]:
    if not isinstance(value, list) or not value or not all(isinstance(v, str) for v in value):
        raise CouncilError(f"{where} must be a non-empty list of strings")
    return list(value)


def load_config(path: Path) -> Config:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise CouncilError(f"config not found: {path}") from None
    except tomllib.TOMLDecodeError as exc:
        raise CouncilError(f"{path.name} is not valid TOML: {exc}") from None

    defaults = data.get("defaults", {})
    timeout_s = float(defaults.get("timeout_minutes", 20)) * 60
    table = data.get("agents")
    if not isinstance(table, dict) or not table:
        raise CouncilError(f"{path.name} defines no [agents.<name>] tables")

    agents: dict[str, AgentSpec] = {}
    for key, raw in table.items():
        where = f"[agents.{key}]"
        if not AGENT_KEY_RE.match(key):
            raise CouncilError(f"{where}: names must be lowercase letters, digits and dashes")
        command = _str_list(raw.get("command"), f"{where} command")
        capture = raw.get("capture", "stdout")
        if capture not in ("stdout", "file"):
            raise CouncilError(f"{where} capture must be \"stdout\" or \"file\"")
        used = {name for arg in command for name in PLACEHOLDER_RE.findall(arg)}
        unknown = used - ALLOWED_PLACEHOLDERS
        if unknown:
            raise CouncilError(f"{where} command uses unknown placeholder(s): {sorted(unknown)}")
        if "instruction" not in used:
            raise CouncilError(f"{where} command must pass {{instruction}} to the agent")
        if capture == "file" and "output_file" not in used:
            raise CouncilError(f"{where} uses capture = \"file\" but never passes {{output_file}}")
        for pattern in raw.get("strip_regex", []):
            try:
                re.compile(pattern)
            except re.error as exc:
                raise CouncilError(f"{where} strip_regex {pattern!r} is invalid: {exc}") from None
        env = _parse_env(raw.get("env", {}), where)
        client_data = raw.get("client_data", key not in CLIENT_DATA_OFF_BY_DEFAULT)
        if not isinstance(client_data, bool):
            raise CouncilError(f"{where} client_data must be true or false")
        unset_env = raw.get("unset_env", [])
        if not isinstance(unset_env, list) or not all(
            isinstance(n, str) and ENV_NAME_RE.match(n) for n in unset_env
        ):
            raise CouncilError(f"{where} unset_env must be a list of environment variable names")
        minutes = raw.get("timeout_minutes")
        agents[key] = AgentSpec(
            key=key,
            label=str(raw.get("label", key)),
            command=command,
            capture=capture,
            enabled=bool(raw.get("enabled", True)),
            timeout_s=float(minutes) * 60 if minutes is not None else None,
            version_command=(
                _str_list(raw["version_command"], f"{where} version_command")
                if "version_command" in raw
                else None
            ),
            strip_regex=list(raw.get("strip_regex", [])),
            install_hint=str(raw.get("install_hint", "")),
            env=env,
            unset_env=list(unset_env),
            client_data=client_data,
        )
    auto_push = defaults.get("auto_push", True)
    if not isinstance(auto_push, bool):
        raise CouncilError("[defaults] auto_push must be true or false")
    return Config(agents=agents, timeout_s=timeout_s, auto_push=auto_push)


def _parse_env(raw, where: str) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise CouncilError(f"{where} env must be a table of NAME = \"value\" lines")
    env: dict[str, str] = {}
    for name, value in raw.items():
        if not ENV_NAME_RE.match(name):
            raise CouncilError(f"{where} env: {name!r} is not a valid environment variable name")
        if not isinstance(value, str):
            raise CouncilError(f"{where} env: {name} must be a quoted string")
        unknown = set(PLACEHOLDER_RE.findall(ENV_REF_RE.sub("", value))) - ENV_PLACEHOLDERS
        if unknown:
            raise CouncilError(
                f"{where} env: {name} uses unknown placeholder(s) {sorted(unknown)}; "
                "use {home}, {council_dir} or ${OS_VARIABLE}"
            )
        env[name] = value
    return env


def resolve_env(spec: AgentSpec, council_dir: Path) -> tuple[dict[str, str], list[str]]:
    """Expand an agent's env table at launch.

    Returns (variables, secrets): the expanded variables, and the values that came
    from ${...} references, so callers can scrub them from anything they save.
    Raises AgentEnvMissing naming (never revealing) any referenced variable that
    is not set.
    """
    missing = [name for name in spec.env_refs() if not os.environ.get(name)]
    if missing:
        raise AgentEnvMissing(
            f"environment variable {', '.join(missing)} is not set "
            f"(needed by [agents.{spec.key}] in council.toml). Set it, then reopen the terminal."
        )
    secrets = [os.environ[name] for name in spec.env_refs()]
    out: dict[str, str] = {}
    for name, value in spec.env.items():
        value = ENV_REF_RE.sub(lambda m: os.environ[m.group(1)], value)
        value = value.replace("{home}", str(Path.home())).replace("{council_dir}", str(council_dir))
        out[name] = value
    return out, secrets


def redact(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        if secret and len(secret) >= 6:
            text = text.replace(secret, "[redacted]")
    return text


def add_preset(config_path: Path, name: str) -> str:
    """Append a ready-made seat to council.toml. Returns the agent's label."""
    if name not in PRESETS:
        raise CouncilError(f"no ready-made seat called '{name}' (available: {', '.join(PRESETS)})")
    original = config_path.read_text(encoding="utf-8")
    if re.search(rf"^\[agents\.{re.escape(name)}\]", original, flags=re.M):
        raise CouncilError(f"council.toml already has an [agents.{name}] seat")
    updated = original.rstrip("\n") + "\n" + PRESETS[name]
    with open(config_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(updated)
    try:
        return load_config(config_path).get(name).label
    except CouncilError:
        with open(config_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(original)
        raise


def render_command(spec: AgentSpec, values: dict[str, str]) -> list[str]:
    out = []
    for arg in spec.command:
        for name, value in values.items():
            arg = arg.replace("{" + name + "}", value)
        out.append(arg)
    return out


def resolve_executable(argv: list[str], spec: AgentSpec) -> list[str]:
    """Replace argv[0] with its full path. shutil.which honours PATHEXT, so on
    Windows 'codex' finds codex.cmd / codex.exe exactly as a terminal would."""
    found = shutil.which(argv[0])
    if found is None:
        hint = f" Install: {spec.install_hint}" if spec.install_hint else ""
        raise AgentNotFound(f"'{argv[0]}' is not on PATH.{hint}")
    return [found, *argv[1:]]


def check_batch_wrapper_args(argv: list[str], is_windows: bool | None = None) -> None:
    """Refuse arguments that cmd.exe would mangle when launching a .cmd/.bat shim.

    The instruction text is fixed and contains none of these characters; this
    guard is for paths (e.g. a Windows user name containing '&').
    """
    if is_windows is None:
        is_windows = os.name == "nt"
    if not is_windows or not argv[0].lower().endswith((".cmd", ".bat")):
        return
    for arg in argv[1:]:
        bad = sorted(set(arg) & CMD_METACHARS)
        if bad:
            raise CouncilError(
                f"{Path(argv[0]).name} is a batch-file wrapper, and cmd.exe would reinterpret "
                f"{' '.join(bad)} in the argument {arg!r}. Move the workspace to a path "
                "without those characters (COUNCIL_HOME or --home)."
            )


def unwrap_cli_bullets(text: str) -> str:
    """Undo terminal list formatting around a Markdown reply.

    Kimi's text output prints each message as a list item: "• " before the first
    line and two spaces before every later line, which turns headings and tables
    into literal text. When the whole reply has exactly that shape, strip it so
    the reply is plain Markdown again; any other text is returned unchanged.
    """
    lines = text.split("\n")
    if not lines[0].startswith("\u2022 "):
        return text
    if not all(not l.strip() or l.startswith(("\u2022 ", "  ")) for l in lines):
        return text
    if not any(l.startswith("  ") and l.strip() for l in lines):
        return text  # a plain list of bullets, not a wrapped reply
    out: list[str] = []
    for line in lines:
        if line.startswith("\u2022 "):
            if out and out[-1].strip():
                out.append("")
            out.append(line[2:])
        else:
            out.append(line[2:] if line.startswith("  ") else "")
    return "\n".join(out)


def clean_output(text: str, strip_regex: list[str] | None = None) -> str:
    """Normalise a reply: LF endings, no terminal colour codes, configured noise removed."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = ANSI_RE.sub("", text)
    text = unwrap_cli_bullets(text)
    for pattern in strip_regex or []:
        text = re.sub(pattern, "", text)
    text = text.strip()
    return text + "\n" if text else ""


def child_env(extra: dict[str, str] | None = None, unset: list[str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env["NO_COLOR"] = "1"
    for name in unset or []:
        # Windows variable names are case-insensitive; match them that way there.
        for key in [k for k in env if k == name or (os.name == "nt" and k.upper() == name.upper())]:
            del env[key]
    env.update(extra or {})
    return env


def probe_version(spec: AgentSpec, timeout: float = 30) -> str | None:
    if not spec.version_command:
        return None
    try:
        argv = resolve_executable(list(spec.version_command), spec)
        proc = subprocess.run(
            argv,
            capture_output=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            env=child_env(),
        )
    except (CouncilError, OSError, subprocess.TimeoutExpired):
        return None
    text = (proc.stdout or proc.stderr).decode("utf-8", errors="replace")
    lines = [line.strip() for line in clean_output(text).splitlines() if line.strip()]
    return lines[0] if lines else None
