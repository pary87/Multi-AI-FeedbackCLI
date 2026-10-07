"""Workspace and thread storage: plain files plus a JSON manifest, versioned by git.

A workspace is one folder (default ~/council) that holds every thread:

    ~/council/
      council.toml                 which CLIs take part and how each is launched
      .council/kimi-council-member.md   read-only agent profile handed to Kimi
      threads/
        T-0001-ldo-selection/
          00-question.md           the question, written once, never edited by agents
          attachments/             optional files every agent may read
          r1-claude.md  r1-chatgpt.md  r1-kimi.md
          r2-claude.md  ...
          synthesis-chatgpt.md     optional
          logs/                    stderr of any agent that printed some
          thread.json              manifest: who ran, when, versions, sha256 of everything

Every completed round is one git commit, so `git log` is the audit trail.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

THREADS_DIR = "threads"
COUNCIL_DIR = ".council"
CONFIG_FILE = "council.toml"
QUESTION_FILE = "00-question.md"
MANIFEST_FILE = "thread.json"
ATTACHMENTS_DIR = "attachments"
LOGS_DIR = "logs"
KIMI_AGENT_FILE = "kimi-council-member.md"

THREAD_ID_RE = re.compile(r"^T-(\d{4,})")


class CouncilError(Exception):
    """A problem the user can fix. The CLI prints the message without a traceback."""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def write_text(path: Path, text: str) -> None:
    """Write UTF-8 with LF line endings on every OS.

    Forcing LF keeps the sha256 of a file identical on Windows and Linux, which
    matters because the manifest records hashes for audit.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def read_text(path: Path) -> str:
    if not path.exists():
        raise CouncilError(f"file not found: {path}")
    return path.read_text(encoding="utf-8")


def slugify(title: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug[:max_len].rstrip("-") or "thread"


def default_home() -> Path:
    env = os.environ.get("COUNCIL_HOME")
    return Path(env).expanduser() if env else Path.home() / "council"


# --------------------------------------------------------------------------- git


def git_available() -> bool:
    return shutil.which("git") is not None


@dataclass
class Workspace:
    root: Path
    # When true and the workspace has an "origin" remote, every commit is pushed.
    auto_push: bool = False
    # Outcome of the most recent push: "pushed", "no remote", "failed: ...", or None.
    last_push: str | None = None

    @property
    def threads_dir(self) -> Path:
        return self.root / THREADS_DIR

    @property
    def council_dir(self) -> Path:
        return self.root / COUNCIL_DIR

    @property
    def config_path(self) -> Path:
        return self.root / CONFIG_FILE

    @property
    def kimi_agent_path(self) -> Path:
        return self.council_dir / KIMI_AGENT_FILE

    # -- lifecycle -----------------------------------------------------------

    @classmethod
    def open(cls, root: Path) -> "Workspace":
        root = root.expanduser().resolve()
        ws = cls(root)
        if not ws.config_path.exists():
            raise CouncilError(
                f"no council workspace at {root} (missing {CONFIG_FILE}). "
                f"Create one with: python -m council_engine --home \"{root}\" init"
            )
        ws.ensure_support_files()
        return ws

    @classmethod
    def init(cls, root: Path) -> tuple["Workspace", list[str]]:
        """Create (or repair) a workspace. Returns it plus a list of what was done."""
        from . import agents, protocol  # local import: avoids a cycle at module load

        root = root.expanduser().resolve()
        ws = cls(root)
        done: list[str] = []
        ws.threads_dir.mkdir(parents=True, exist_ok=True)
        if not ws.config_path.exists():
            write_text(ws.config_path, agents.DEFAULT_CONFIG_TOML)
            done.append(f"wrote {ws.config_path}")
        else:
            done.append(f"kept existing {ws.config_path}")
        if not ws.kimi_agent_path.exists():
            write_text(ws.kimi_agent_path, protocol.KIMI_AGENT_PROFILE)
            done.append(f"wrote {ws.kimi_agent_path}")
        if git_available():
            if not (root / ".git").exists():
                ws.git("init", "-q")
                done.append("initialised git repository")
            if ws.commit([ws.config_path, ws.kimi_agent_path], "council workspace initialised"):
                done.append("committed workspace files")
        else:
            done.append("git not found: threads will work but rounds will not be committed")
        return ws, done

    def ensure_support_files(self) -> None:
        """Recreate generated support files if someone deleted them."""
        from . import protocol

        if not self.kimi_agent_path.exists():
            write_text(self.kimi_agent_path, protocol.KIMI_AGENT_PROFILE)

    # -- git -----------------------------------------------------------------

    def git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(self.root), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=check,
            stdin=subprocess.DEVNULL,
        )

    def is_git_repo(self) -> bool:
        return git_available() and (self.root / ".git").exists()

    def commit(self, paths: list[Path], message: str) -> str | None:
        """Stage `paths` and commit. Returns the short hash, or None if nothing changed.

        Uses your own git identity when one is configured; otherwise commits as
        "council" so a fresh machine never fails a round over missing git config.
        """
        self.last_push = None
        if not self.is_git_repo():
            return None
        rel = [str(p.resolve().relative_to(self.root)) for p in paths if p.exists()]
        if not rel:
            return None
        self.git("add", "--", *rel)
        if self.git("diff", "--cached", "--quiet", check=False).returncode == 0:
            return None
        identity: list[str] = []
        if self.git("config", "user.email", check=False).returncode != 0:
            identity = ["-c", "user.name=council", "-c", "user.email=council@localhost"]
        self.git(*identity, "commit", "-q", "-m", message)
        if self.auto_push:
            self.last_push = self.push()
        return self.git("rev-parse", "--short", "HEAD").stdout.strip()

    def remote_url(self) -> str | None:
        if not self.is_git_repo():
            return None
        proc = self.git("remote", "get-url", "origin", check=False)
        return proc.stdout.strip() or None if proc.returncode == 0 else None

    def push(self) -> str:
        """Push the current branch to origin. Never raises and never waits for a
        password prompt: a failure is reported and the threads stay safe locally."""
        if self.remote_url() is None:
            return "no remote"
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never")
        try:
            proc = subprocess.run(
                ["git", "-C", str(self.root), "push", "--quiet", "origin", "HEAD"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdin=subprocess.DEVNULL,
                env=env,
                timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"failed: {exc.__class__.__name__}"
        if proc.returncode == 0:
            return "pushed"
        lines = [line.strip() for line in (proc.stderr or proc.stdout).splitlines() if line.strip()]
        return "failed: " + (lines[-1][:200] if lines else f"exit code {proc.returncode}")

    def unpushed_commits(self) -> int | None:
        """How many commits on this branch are not on origin yet. None when that
        is unknown: no remote, a detached HEAD, or a branch never pushed."""
        if self.remote_url() is None:
            return None
        branch = self.git("rev-parse", "--abbrev-ref", "HEAD", check=False).stdout.strip()
        if not branch or branch == "HEAD":
            return None
        proc = self.git("rev-list", "--count", f"origin/{branch}..HEAD", check=False)
        if proc.returncode != 0:
            return None
        return int(proc.stdout.strip() or 0)

    def log(self, path: Path | None = None, limit: int = 50) -> list[dict]:
        """Recent commits (newest first), optionally only those touching `path`."""
        if not self.is_git_repo():
            return []
        args = ["log", f"-{limit}", "--format=%h%x09%cI%x09%s"]
        if path is not None:
            args += ["--", str(path.resolve().relative_to(self.root))]
        proc = self.git(*args, check=False)
        out = []
        for line in proc.stdout.splitlines():
            parts = line.split("\t", 2)
            if len(parts) == 3:
                out.append({"hash": parts[0], "date": parts[1], "subject": parts[2]})
        return out

    # -- threads -------------------------------------------------------------

    def thread_dirs(self) -> list[Path]:
        if not self.threads_dir.exists():
            return []
        return sorted(
            p for p in self.threads_dir.iterdir() if p.is_dir() and THREAD_ID_RE.match(p.name)
        )

    def next_thread_id(self) -> str:
        numbers = [int(THREAD_ID_RE.match(p.name).group(1)) for p in self.thread_dirs()]
        return f"T-{(max(numbers, default=0) + 1):04d}"

    def thread(self, ref: str) -> "Thread":
        """Find a thread by 'T-0003', '3', '0003' or its full folder name."""
        ref = ref.strip()
        if ref.isdigit():
            ref = f"T-{int(ref):04d}"
        matches = [p for p in self.thread_dirs() if p.name == ref or p.name.startswith(ref + "-")]
        if not matches:
            raise CouncilError(f"no thread matching '{ref}' in {self.threads_dir}")
        if len(matches) > 1:
            names = ", ".join(p.name for p in matches)
            raise CouncilError(f"'{ref}' matches more than one thread: {names}")
        return Thread.load(self, matches[0])

    def create_thread(
        self,
        title: str,
        question: str,
        participants: list[str],
        attachments: list[Path] | None = None,
        client_data: bool = False,
        plan: dict | None = None,
    ) -> "Thread":
        """Create a thread folder and commit it. `plan` (optional) records what the
        caller intends to run, e.g. {"rounds": 2, "synthesis_by": "claude"}."""
        if not question.strip():
            raise CouncilError("the question is empty")
        if not participants:
            raise CouncilError("a thread needs at least one participant")
        thread_id = self.next_thread_id()
        path = self.threads_dir / f"{thread_id}-{slugify(title)}"
        path.mkdir(parents=True)
        question_path = path / QUESTION_FILE
        write_text(question_path, question.rstrip() + "\n")
        attached: list[dict] = []
        for src in attachments or []:
            src = Path(src).expanduser()
            if not src.is_file():
                raise CouncilError(f"attachment not found: {src}")
            dest = path / ATTACHMENTS_DIR / src.name
            if dest.exists():
                raise CouncilError(f"two attachments share the name {src.name}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            attached.append({"name": src.name, "sha256": sha256_file(dest)})
        manifest = {
            "id": thread_id,
            "title": title,
            "created": now_iso(),
            "participants": participants,
            "client_data": bool(client_data),
            "question_sha256": sha256_file(question_path),
            "attachments": attached,
            "rounds": [],
            "syntheses": [],
        }
        if plan:
            manifest["plan"] = dict(plan)
        thread = Thread(self, path, manifest)
        thread.save()
        self.commit([path], f"{thread_id}: new thread \"{title}\"")
        return thread


@dataclass
class Thread:
    workspace: Workspace
    path: Path
    manifest: dict = field(default_factory=dict)

    @classmethod
    def load(cls, workspace: Workspace, path: Path) -> "Thread":
        manifest_path = path / MANIFEST_FILE
        if not manifest_path.exists():
            raise CouncilError(f"{path.name} has no {MANIFEST_FILE}; it is not a council thread")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        return cls(workspace, path, manifest)

    def save(self) -> None:
        write_text(self.path / MANIFEST_FILE, json.dumps(self.manifest, indent=2) + "\n")

    @property
    def id(self) -> str:
        return self.manifest["id"]

    @property
    def title(self) -> str:
        return self.manifest["title"]

    @property
    def participants(self) -> list[str]:
        return list(self.manifest["participants"])

    @property
    def client_data(self) -> bool:
        return bool(self.manifest.get("client_data", False))

    @property
    def question_path(self) -> Path:
        return self.path / QUESTION_FILE

    def question(self) -> str:
        text = read_text(self.question_path)
        if sha256_text(text) != self.manifest["question_sha256"] and "\r\n" in text:
            # Git on Windows may check files out with CRLF line endings (core.autocrlf).
            # The engine always writes LF, so compare the LF form before calling it edited.
            text = text.replace("\r\n", "\n")
        if sha256_text(text) != self.manifest["question_sha256"]:
            raise CouncilError(
                f"{self.id}: {QUESTION_FILE} was edited after the thread started. "
                "Earlier rounds answered the original text, so start a new thread instead."
            )
        return text

    def attachment_paths(self) -> list[Path]:
        folder = self.path / ATTACHMENTS_DIR
        return sorted(p for p in folder.iterdir() if p.is_file()) if folder.exists() else []

    def response_path(self, round_no: int, agent: str) -> Path:
        return self.path / f"r{round_no}-{agent}.md"

    def synthesis_path(self, agent: str) -> Path:
        return self.path / f"synthesis-{agent}.md"

    # -- round bookkeeping ---------------------------------------------------

    @property
    def rounds(self) -> list[dict]:
        return self.manifest["rounds"]

    def round_record(self, round_no: int) -> dict | None:
        for record in self.rounds:
            if record["round"] == round_no:
                return record
        return None

    def round_complete(self, round_no: int) -> bool:
        record = self.round_record(round_no)
        if record is None:
            return False
        return all(
            record["agents"].get(agent, {}).get("status") == "ok" for agent in self.participants
        )

    def missing_agents(self, round_no: int) -> list[str]:
        record = self.round_record(round_no) or {"agents": {}}
        return [a for a in self.participants if record["agents"].get(a, {}).get("status") != "ok"]

    def latest_round(self) -> int:
        return max((r["round"] for r in self.rounds), default=0)

    def responses(self, round_no: int) -> dict[str, str | None]:
        """Each participant's saved text for a round, or None if it has none."""
        out: dict[str, str | None] = {}
        for agent in self.participants:
            path = self.response_path(round_no, agent)
            out[agent] = read_text(path) if path.exists() else None
        return out

    def state(self) -> str:
        latest = self.latest_round()
        if latest == 0:
            return "no rounds yet"
        if self.round_complete(latest):
            return f"{latest} round(s) complete"
        missing = ", ".join(self.missing_agents(latest))
        return f"round {latest} incomplete (missing: {missing})"
