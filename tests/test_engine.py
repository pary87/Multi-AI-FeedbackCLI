"""End-to-end tests of council_engine using stand-in agents (tests/fake_agent.py).

Run from the repo root:   python -m unittest discover -s tests -v

The fake agents stand in for Claude Code / Codex / Kimi and report exactly what
they could see, so these tests prove the protocol's guarantees -- round-1
blindness, isolation, hashes, retries, timeouts -- without any AI account.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from council_engine import protocol  # noqa: E402
from council_engine.agents import (  # noqa: E402
    DEFAULT_CONFIG_TOML,
    add_preset,
    check_batch_wrapper_args,
    clean_output,
    load_config,
)
from council_engine.runner import ping, run_round, run_synthesis  # noqa: E402
from council_engine.store import CouncilError, Workspace, sha256_file  # noqa: E402

FAKE = REPO / "tests" / "fake_agent.py"
q = json.dumps  # TOML basic strings accept JSON string escapes


def fake_config(timeout_minutes: float = 1, extra: str = "") -> str:
    py, fake = q(sys.executable), q(str(FAKE))
    return f"""
[defaults]
timeout_minutes = {timeout_minutes}

[agents.claude]
label = "Claude"
command = [{py}, {fake}, "--name", "claude", "--instruction", "{{instruction}}"]
capture = "stdout"
version_command = [{py}, "--version"]

[agents.chatgpt]
label = "ChatGPT"
command = [{py}, {fake}, "--name", "chatgpt", "--instruction", "{{instruction}}", "--out", "{{output_file}}"]
capture = "file"

[agents.kimi]
label = "Kimi"
command = [{py}, {fake}, "--name", "kimi", "--instruction", "{{instruction}}"]
capture = "stdout"
strip_regex = ['(?s)\\n*\\[session saved:.*\\Z']
{extra}
"""


def _alive(pid: int) -> bool:
    """True if `pid` is running. A killed-but-unreaped zombie counts as dead."""
    if os.name == "nt":
        # os.kill(pid, 0) on Windows sends CTRL_C_EVENT instead of probing, so ask tasklist.
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
            capture_output=True, text=True, stdin=subprocess.DEVNULL,
        ).stdout
        return f'"{pid}"' in out
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    stat = Path(f"/proc/{pid}/stat")
    if stat.exists():
        try:
            return stat.read_text().rsplit(")", 1)[1].split()[0] != "Z"
        except (OSError, IndexError):
            return False
    return True


def parse_report(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        if ": " in line:
            key, value = line.split(": ", 1)
            out[key] = value
    return out


class EngineTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name) / "council"
        self.ws, _ = Workspace.init(self.home)
        (self.home / "council.toml").write_text(fake_config(), encoding="utf-8")
        self.config = load_config(self.ws.config_path)
        self.attachment = Path(self._tmp.name) / "spec.txt"
        self.attachment.write_text("VIN range 4.5-5.5 V\n", encoding="utf-8")
        self._env_keys: list[str] = []

    def tearDown(self) -> None:
        for key in self._env_keys:
            os.environ.pop(key, None)
        self._tmp.cleanup()

    def mode(self, agent: str, value: str) -> None:
        key = f"FAKE_MODE_{agent.upper()}"
        os.environ[key] = value
        self._env_keys.append(key)

    def new_thread(self, attach: bool = False):
        return self.ws.create_thread(
            "LDO selection",
            "Which LDO should feed the 1.8 V rail?",
            ["claude", "chatgpt", "kimi"],
            [self.attachment] if attach else None,
        )

    def git_log(self) -> list[str]:
        return self.ws.git("log", "--format=%s").stdout.strip().splitlines()


class FullFlowTests(EngineTestCase):
    def test_two_rounds_then_synthesis(self) -> None:
        thread = self.new_thread(attach=True)
        events: list[dict] = []
        temp_before = {p.name for p in Path(tempfile.gettempdir()).glob("council-*")}

        r1 = run_round(thread, self.config, on_event=events.append)
        self.assertTrue(r1.complete)
        self.assertEqual(r1.round_no, 1)
        for agent in thread.participants:
            path = thread.response_path(1, agent)
            report = parse_report(path.read_text(encoding="utf-8"))
            self.assertEqual(report["seen"], "none", f"{agent} saw others in round 1")
            self.assertEqual(report["files"], "ROUND.md,attachments/spec.txt")
            self.assertEqual(report["instruction_mentions_round_md"], "True")
            self.assertIn("round 1", report["header"])
            # The hash recorded in the manifest is the hash of what the agent read.
            entry = thread.round_record(1)["agents"][agent]
            self.assertEqual(entry["prompt_sha256"], report["round_md_sha256"])
            self.assertEqual(entry["response_sha256"], sha256_file(path))
            self.assertIn("r\u00e9sum\u00e9 \u2014 \u2713", path.read_text(encoding="utf-8"))

        r2 = run_round(thread, self.config)
        self.assertTrue(r2.complete)
        claude2 = parse_report(thread.response_path(2, "claude").read_text(encoding="utf-8"))
        self.assertEqual(claude2["seen"], "Claude (you)@1,ChatGPT@1,Kimi@1")
        kimi2 = parse_report(thread.response_path(2, "kimi").read_text(encoding="utf-8"))
        self.assertEqual(kimi2["seen"], "Claude@1,ChatGPT@1,Kimi (you)@1")

        s = run_synthesis(thread, self.config, "chatgpt")
        self.assertTrue(s.complete)
        synth = parse_report(thread.synthesis_path("chatgpt").read_text(encoding="utf-8"))
        self.assertIn("synthesis", synth["header"])
        self.assertEqual(synth["seen"].count("@"), 6)

        self.assertEqual(
            self.git_log(),
            [
                "T-0001 synthesis by chatgpt: ok",
                "T-0001 round 2: claude ok, chatgpt ok, kimi ok",
                "T-0001 round 1: claude ok, chatgpt ok, kimi ok",
                'T-0001: new thread "LDO selection"',
                "council workspace initialised",
            ],
        )
        # Everything the runs produced is committed (council.toml was swapped by setUp).
        self.assertEqual(self.ws.git("status", "--porcelain", "--", "threads").stdout.strip(), "")
        temp_after = {p.name for p in Path(tempfile.gettempdir()).glob("council-*")}
        self.assertEqual(temp_after - temp_before, set(), "temp working folders were left behind")
        kinds = [e["type"] for e in events]
        self.assertEqual(kinds[0], "run_started")
        self.assertEqual(kinds[-1], "run_finished")
        self.assertEqual(kinds.count("agent_finished"), 3)

    def test_round_one_blind_even_when_others_finish_first(self) -> None:
        thread = self.new_thread()
        self.mode("kimi", "slow")  # Claude and ChatGPT finish while Kimi is still working
        run_round(thread, self.config)
        report = parse_report(thread.response_path(1, "kimi").read_text(encoding="utf-8"))
        self.assertEqual(report["files"], "ROUND.md")
        self.assertEqual(report["seen"], "none")


class FailureTests(EngineTestCase):
    def test_failed_agent_blocks_next_round_until_retried(self) -> None:
        thread = self.new_thread()
        self.mode("kimi", "fail")
        outcome = run_round(thread, self.config)
        self.assertFalse(outcome.complete)
        entry = thread.round_record(1)["agents"]["kimi"]
        self.assertEqual(entry["status"], "failed")
        self.assertIn("not logged in", entry["detail"])
        self.assertFalse(thread.response_path(1, "kimi").exists())
        self.assertTrue((thread.path / entry["stderr_log"]).exists())
        self.assertEqual(thread.missing_agents(1), ["kimi"])

        with self.assertRaises(CouncilError):
            run_round(thread, self.config)

        self.mode("kimi", "normal")
        retry = run_round(thread, self.config, retry=True)
        self.assertEqual(list(retry.results), ["kimi"])
        self.assertTrue(retry.complete)
        with self.assertRaises(CouncilError):
            run_round(thread, self.config, retry=True)  # nothing left to retry

    def test_allow_partial_marks_missing_response(self) -> None:
        thread = self.new_thread()
        self.mode("kimi", "fail")
        run_round(thread, self.config)
        os.environ.pop("FAKE_MODE_KIMI")
        run_round(thread, self.config, allow_partial=True)
        report = parse_report(thread.response_path(2, "claude").read_text(encoding="utf-8"))
        self.assertEqual(report["seen"], "Claude (you)@1,ChatGPT@1,Kimi@1")
        # Kimi's round-1 slot is present but flagged as missing in the transcript.
        self.assertEqual(report["missing"], "Kimi@1")
        self.assertTrue(thread.round_complete(2))

    def test_forced_rerun_that_fails_keeps_good_response(self) -> None:
        thread = self.new_thread()
        run_round(thread, self.config)
        good = thread.response_path(1, "claude").read_text(encoding="utf-8")
        self.mode("claude", "fail")
        with self.assertRaises(CouncilError):
            run_round(thread, self.config, retry=True, only=["claude"])  # needs --force
        run_round(thread, self.config, retry=True, only=["claude"], force=True)
        entry = thread.round_record(1)["agents"]["claude"]
        self.assertEqual(entry["status"], "ok")
        self.assertEqual(entry["last_rerun_failed"]["status"], "failed")
        self.assertEqual(thread.response_path(1, "claude").read_text(encoding="utf-8"), good)

    def test_empty_reply_is_a_failure(self) -> None:
        thread = self.new_thread()
        self.mode("chatgpt", "empty")
        run_round(thread, self.config)
        entry = thread.round_record(1)["agents"]["chatgpt"]
        self.assertEqual(entry["status"], "failed")
        self.assertIn("nothing", entry["detail"])

    def test_timeout_kills_whole_process_tree(self) -> None:
        thread = self.new_thread()
        pidfile = Path(self._tmp.name) / "pids.txt"
        os.environ["FAKE_PIDFILE"] = str(pidfile)
        self._env_keys.append("FAKE_PIDFILE")
        self.mode("chatgpt", "hang")
        started = time.monotonic()
        run_round(thread, self.config, timeout_s=3)
        self.assertLess(time.monotonic() - started, 30)
        entry = thread.round_record(1)["agents"]["chatgpt"]
        self.assertEqual(entry["status"], "timeout")
        for pid in map(int, pidfile.read_text().split()):
            deadline = time.monotonic() + 5
            while _alive(pid) and time.monotonic() < deadline:
                time.sleep(0.1)
            self.assertFalse(_alive(pid), f"pid {pid} survived the timeout")

    def test_agent_writes_stay_in_its_private_folder(self) -> None:
        thread = self.new_thread()
        self.mode("claude", "tamper")
        run_round(thread, self.config)
        entry = thread.round_record(1)["agents"]["claude"]
        self.assertEqual(entry["status"], "ok")
        self.assertTrue(any("planted.txt" in w for w in entry["warnings"]))
        self.assertEqual(list(self.home.rglob("planted.txt")), [])

    def test_footer_is_stripped(self) -> None:
        thread = self.new_thread()
        self.mode("kimi", "footer")
        run_round(thread, self.config)
        text = thread.response_path(1, "kimi").read_text(encoding="utf-8")
        self.assertNotIn("session saved", text)
        self.assertTrue(text.endswith("\u2713\n"))

    def test_missing_cli_reports_install_hint(self) -> None:
        extra = """
[agents.ghost]
label = "Ghost"
command = ["definitely-not-a-real-cli-xyz", "{instruction}"]
install_hint = "npm install -g ghost-cli"
"""
        (self.home / "council.toml").write_text(fake_config(extra=extra), encoding="utf-8")
        config = load_config(self.ws.config_path)
        thread = self.ws.create_thread("x", "y", ["claude", "ghost"])
        run_round(thread, config)
        entry = thread.round_record(1)["agents"]["ghost"]
        self.assertEqual(entry["status"], "not_found")
        self.assertIn("npm install -g ghost-cli", entry["detail"])

    def test_editing_question_after_start_is_refused(self) -> None:
        thread = self.new_thread()
        run_round(thread, self.config)
        thread.question_path.write_text("A different question?\n", encoding="utf-8")
        with self.assertRaises(CouncilError):
            run_round(thread, self.config)


class PingAndConfigTests(EngineTestCase):
    def test_ping(self) -> None:
        self.assertTrue(ping(self.ws, self.config).complete)
        self.mode("kimi", "badping")
        outcome = ping(self.ws, self.config)
        self.assertFalse(outcome.complete)
        self.assertEqual(outcome.results["kimi"].status, "failed")
        self.assertIn("unexpected reply", outcome.results["kimi"].detail)
        self.assertEqual(list(self.ws.threads_dir.iterdir()), [])

    def test_config_validation(self) -> None:
        bad = {
            "missing instruction": '[agents.a]\ncommand = ["x"]\n',
            "file capture without output_file": '[agents.a]\ncommand = ["x", "{instruction}"]\ncapture = "file"\n',
            "unknown placeholder": '[agents.a]\ncommand = ["x", "{instruction}", "{nope}"]\n',
            "bad capture": '[agents.a]\ncommand = ["x", "{instruction}"]\ncapture = "pipe"\n',
            "bad name": '[agents.Big]\ncommand = ["x", "{instruction}"]\n',
            "no agents": "[defaults]\ntimeout_minutes = 1\n",
        }
        for label, text in bad.items():
            path = Path(self._tmp.name) / "bad.toml"
            path.write_text(text, encoding="utf-8")
            with self.assertRaises(CouncilError, msg=label):
                load_config(path)

    def test_default_config_parses(self) -> None:
        path = Path(self._tmp.name) / "default.toml"
        from council_engine.agents import DEFAULT_CONFIG_TOML

        path.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
        config = load_config(path)
        self.assertEqual(list(config.agents), ["claude", "chatgpt", "kimi"])
        self.assertEqual(config.get("chatgpt").capture, "file")
        self.assertEqual(config.timeout_s, 1200)

    def test_windows_batch_wrapper_guard(self) -> None:
        check_batch_wrapper_args(["C:/npm/codex.cmd", protocol.INSTRUCTION], is_windows=True)
        check_batch_wrapper_args(["C:/bin/claude.exe", "a&b"], is_windows=True)
        check_batch_wrapper_args(["/usr/bin/codex", "a&b"], is_windows=False)
        with self.assertRaises(CouncilError):
            check_batch_wrapper_args(["C:/npm/codex.cmd", "C:/Users/R&D/x.md"], is_windows=True)

    def test_failure_reason_skips_harmless_notices(self) -> None:
        from council_engine.runner import _failure_reason

        noisy = (
            '"glm-5.3" isn\'t described by this version\'s model catalog; update Claude Code\n'
            '[claude-code:unrecognized_model] {"model":"glm-5.3"}\n'
        )
        self.assertEqual(_failure_reason(noisy), "")
        self.assertEqual(_failure_reason(noisy + "API Error: 401 invalid key\n"), "API Error: 401 invalid key")

    def test_clean_output(self) -> None:
        self.assertEqual(clean_output("\x1b[32mhi\x1b[0m\r\nthere\r\n\r\n"), "hi\nthere\n")
        self.assertEqual(clean_output("   \n"), "")


class CliTests(EngineTestCase):
    def run_cli(self, *args: str, expect: int = 0) -> str:
        proc = subprocess.run(
            [sys.executable, "-m", "council_engine", "--home", str(self.home), *args],
            cwd=REPO,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(proc.returncode, expect, proc.stdout + proc.stderr)
        return proc.stdout + proc.stderr

    def test_cli_flow(self) -> None:
        out = self.run_cli("doctor", "--ping")
        self.assertIn("all good", out)
        out = self.run_cli(
            "ask", "Pull-up value", "-q", "4.7k or 10k on I2C at 400 kHz?",
            "--attach", str(self.attachment), "--rounds", "2", "--synth", "claude",
        )
        self.assertIn("round 2 complete", out)
        self.assertIn("synthesis ->", out)
        out = self.run_cli("status")
        self.assertIn("T-0001  2 round(s) complete", out)
        out = self.run_cli("status", "1")
        self.assertIn("synthesis by claude after round 2: ok", out)
        self.run_cli("round", "T-9999", expect=2)

    def test_cli_synth_none_and_early_agent_check(self) -> None:
        out = self.run_cli("ask", "t", "-q", "q?", "--rounds", "1", "--synth", "none")
        self.assertIn("round 1 complete", out)
        self.assertNotIn("synthesis", out)
        # A misspelt synthesis agent fails before any round runs or thread is created.
        out = self.run_cli("ask", "t2", "-q", "q?", "--synth", "chatgtp", expect=2)
        self.assertIn("unknown agent 'chatgtp'", out)
        self.assertEqual(len(self.ws.thread_dirs()), 1)

    @unittest.skipIf(os.name == "nt", "sends SIGINT")
    def test_ctrl_c_stops_agents_and_saves_nothing(self) -> None:
        import signal

        self.run_cli("new", "t", "-q", "q?")
        pidfile = Path(self._tmp.name) / "pids.txt"
        os.environ["FAKE_PIDFILE"] = str(pidfile)
        self._env_keys.append("FAKE_PIDFILE")
        self.mode("kimi", "hang")
        proc = subprocess.Popen(
            [sys.executable, "-m", "council_engine", "--home", str(self.home), "round", "1"],
            cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        deadline = time.monotonic() + 20
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertTrue(pidfile.exists(), "hanging agent never started")
        proc.send_signal(signal.SIGINT)
        _out, err = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 130, err)
        self.assertIn("nothing was saved", err)
        for pid in map(int, pidfile.read_text().split()):
            end = time.monotonic() + 5
            while _alive(pid) and time.monotonic() < end:
                time.sleep(0.1)
            self.assertFalse(_alive(pid), f"pid {pid} survived Ctrl+C")
        thread = self.ws.thread("1")
        self.assertEqual(thread.rounds, [])
        self.assertEqual(len(self.git_log()), 2)  # init + new thread only

    def test_cli_incomplete_round_exit_code(self) -> None:
        self.mode("kimi", "fail")
        out = self.run_cli("ask", "t", "-q", "q?", "--rounds", "2", expect=1)
        self.assertIn("round 1 incomplete (no response from kimi)", out)
        self.assertIn("retry T-0001", out)
        os.environ.pop("FAKE_MODE_KIMI")
        self.run_cli("retry", "T-0001")
        out = self.run_cli("run", "T-0001", "--rounds", "2")
        self.assertIn("round 2 complete", out)


SECRET = "s3cr3t-token-0123456789"


def env_agents_toml() -> str:
    py, fake = q(sys.executable), q(str(FAKE))
    return f"""
[agents.zed]
label = "Zed"
command = [{py}, {fake}, "--name", "zed", "--instruction", "{{instruction}}",
           "--echo-env", "ZED_TOKEN", "--echo-env", "ZED_PROFILE", "--echo-env", "FAKE_MUST_VANISH"]
unset_env = ["FAKE_MUST_VANISH"]

[agents.zed.env]
ZED_TOKEN = "Bearer ${{COUNCIL_TEST_SECRET}}"
ZED_PROFILE = "{{home}}/.zed-profile"

[agents.yon]
label = "Yon"
command = [{py}, {fake}, "--name", "yon", "--instruction", "{{instruction}}", "--echo-env", "ZED_TOKEN"]
"""


class EnvTests(EngineTestCase):
    def setUp(self) -> None:
        super().setUp()
        (self.home / "council.toml").write_text(fake_config(extra=env_agents_toml()), encoding="utf-8")
        self.config = load_config(self.ws.config_path)

    def set_secret(self) -> None:
        os.environ["COUNCIL_TEST_SECRET"] = SECRET
        self._env_keys.append("COUNCIL_TEST_SECRET")

    def test_env_reaches_only_its_agent_and_secret_is_scrubbed(self) -> None:
        self.set_secret()
        os.environ["FAKE_MUST_VANISH"] = "an-anthropic-credential"
        self._env_keys.append("FAKE_MUST_VANISH")
        thread = self.ws.create_thread("env", "q?", ["zed", "yon"])
        outcome = run_round(thread, self.config)
        self.assertTrue(outcome.complete)
        zed = parse_report(thread.response_path(1, "zed").read_text(encoding="utf-8"))
        # The agent received the real value; what was saved has it scrubbed.
        self.assertEqual(zed["env_ZED_TOKEN"], "Bearer [redacted]")
        self.assertEqual(zed["env_ZED_PROFILE"], f"{Path.home()}/.zed-profile")
        self.assertEqual(zed["env_FAKE_MUST_VANISH"], "<unset>")  # unset_env removed it
        yon = parse_report(thread.response_path(1, "yon").read_text(encoding="utf-8"))
        self.assertEqual(yon["env_ZED_TOKEN"], "<unset>")
        for path in thread.path.rglob("*"):
            if path.is_file():
                self.assertNotIn(SECRET, path.read_text(encoding="utf-8"), f"secret leaked into {path}")
        self.assertNotIn(SECRET, self.ws.git("log", "-p").stdout)

    def test_missing_env_variable_fails_only_that_agent(self) -> None:
        os.environ.pop("COUNCIL_TEST_SECRET", None)
        thread = self.ws.create_thread("env", "q?", ["zed", "yon"])
        outcome = run_round(thread, self.config)
        self.assertEqual(outcome.results["zed"].status, "error")
        self.assertIn("COUNCIL_TEST_SECRET is not set", outcome.results["zed"].detail)
        self.assertEqual(outcome.results["yon"].status, "ok")

    def test_env_validation(self) -> None:
        bad = {
            "bad name": '[agents.a]\ncommand = ["x", "{instruction}"]\n[agents.a.env]\n"1BAD" = "v"\n',
            "not a string": '[agents.a]\ncommand = ["x", "{instruction}"]\n[agents.a.env]\nA = 5\n',
            "unknown placeholder": '[agents.a]\ncommand = ["x", "{instruction}"]\n[agents.a.env]\nA = "{nope}"\n',
        }
        for label, text in bad.items():
            path = Path(self._tmp.name) / "bad.toml"
            path.write_text(text, encoding="utf-8")
            with self.assertRaises(CouncilError, msg=label):
                load_config(path)

    def test_glm_preset(self) -> None:
        path = Path(self._tmp.name) / "c.toml"
        path.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
        self.assertEqual(add_preset(path, "glm"), "GLM")
        spec = load_config(path).get("glm")
        self.assertEqual(spec.env_refs(), ["ZAI_API_KEY"])
        self.assertEqual(spec.env["ANTHROPIC_BASE_URL"], "https://api.z.ai/api/anthropic")
        self.assertIn("glm-5.3", spec.command)
        self.assertIn("Read,Glob,Grep", spec.command)  # same read-only tools as the Claude seat
        self.assertTrue(spec.env["CLAUDE_CONFIG_DIR"].startswith("{home}"))  # never the main profile
        # Anthropic credentials are stripped so they can never be sent to Z.ai.
        self.assertEqual(sorted(spec.unset_env), ["ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"])
        with self.assertRaises(CouncilError):
            add_preset(path, "glm")
        with self.assertRaises(CouncilError):
            add_preset(path, "nope")


class SyncTests(EngineTestCase):
    def add_remote(self, url: str) -> None:
        self.ws.git("remote", "add", "origin", url)
        self.ws.auto_push = True

    def test_rounds_are_uploaded(self) -> None:
        bare = Path(self._tmp.name) / "remote.git"
        subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
        self.add_remote(str(bare))
        thread = self.new_thread()
        events: list[dict] = []
        run_round(thread, self.config, on_event=events.append)
        self.assertEqual(events[-1]["push"], "pushed")
        remote_log = subprocess.run(
            ["git", "--git-dir", str(bare), "log", "--all", "--format=%s"],
            capture_output=True, text=True, check=True,
        ).stdout
        self.assertIn("T-0001 round 1: claude ok, chatgpt ok, kimi ok", remote_log)
        self.assertIn('T-0001: new thread "LDO selection"', remote_log)

    def test_failed_upload_never_fails_the_run(self) -> None:
        self.add_remote(str(Path(self._tmp.name) / "does-not-exist.git"))
        thread = self.new_thread()
        events: list[dict] = []
        outcome = run_round(thread, self.config, on_event=events.append)
        self.assertTrue(outcome.complete)
        self.assertTrue(events[-1]["push"].startswith("failed"))
        self.assertEqual(self.git_log()[0], "T-0001 round 1: claude ok, chatgpt ok, kimi ok")

    def test_no_remote_means_no_upload(self) -> None:
        self.ws.auto_push = True
        thread = self.new_thread()
        events: list[dict] = []
        run_round(thread, self.config, on_event=events.append)
        self.assertEqual(events[-1]["push"], "no remote")

    def test_auto_push_setting(self) -> None:
        self.assertTrue(self.config.auto_push)
        path = Path(self._tmp.name) / "off.toml"
        path.write_text(fake_config().replace("timeout_minutes = 1", "timeout_minutes = 1\nauto_push = false"),
                        encoding="utf-8")
        self.assertFalse(load_config(path).auto_push)


class CliAdditionsTests(EngineTestCase):
    def run_cli(self, *args: str, expect: int = 0) -> str:
        proc = subprocess.run(
            [sys.executable, "-m", "council_engine", "--home", str(self.home), *args],
            cwd=REPO, capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(proc.returncode, expect, proc.stdout + proc.stderr)
        return proc.stdout + proc.stderr

    def test_add_agent_and_doctor_env_check(self) -> None:
        out = self.run_cli("add-agent", "glm")
        self.assertIn("added the GLM seat", out)
        self.assertIn("council.toml: add GLM seat", self.git_log()[0])
        self.run_cli("add-agent", "glm", expect=2)  # already there
        (self.home / "council.toml").write_text(fake_config(extra=env_agents_toml()), encoding="utf-8")
        os.environ.pop("COUNCIL_TEST_SECRET", None)
        out = self.run_cli("doctor", "--agents", "zed", expect=1)
        self.assertIn("MISSING environment variable: COUNCIL_TEST_SECRET", out)
        self.assertNotIn(SECRET, out)

    def test_cli_reports_upload(self) -> None:
        bare = Path(self._tmp.name) / "remote.git"
        subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
        self.ws.git("remote", "add", "origin", str(bare))
        out = self.run_cli("ask", "t", "-q", "q?", "--rounds", "1")
        self.assertIn("and uploaded to GitHub", out)
        out = self.run_cli("doctor")
        self.assertIn(f"sync: uploads to {bare}", out)


class ClientDataTests(EngineTestCase):
    def test_defaults_keep_china_based_seats_out(self) -> None:
        path = Path(self._tmp.name) / "c.toml"
        path.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
        add_preset(path, "glm")
        config = load_config(path)
        cleared = {key: spec.client_data for key, spec in config.agents.items()}
        self.assertEqual(cleared, {"claude": True, "chatgpt": True, "kimi": False, "glm": False})
        # An older council.toml with no client_data keys gets the same safe defaults.
        old = Path(self._tmp.name) / "old.toml"
        old.write_text(fake_config(), encoding="utf-8")
        self.assertFalse(load_config(old).get("kimi").client_data)
        self.assertTrue(load_config(old).get("claude").client_data)

    def test_engine_refuses_uncleared_seat_on_client_thread(self) -> None:
        thread = self.ws.create_thread("client", "q?", ["claude", "kimi"], client_data=True)
        with self.assertRaises(CouncilError) as caught:
            run_round(thread, self.config)
        self.assertIn("Kimi is not cleared for client data", str(caught.exception))
        self.assertEqual(thread.rounds, [])
        ok = self.ws.create_thread("client2", "q?", ["claude", "chatgpt"], client_data=True)
        self.assertTrue(run_round(ok, self.config).complete)
        with self.assertRaises(CouncilError):
            run_synthesis(ok, self.config, "kimi")

    def test_cli_client_data_flag(self) -> None:
        def cli(*args, expect=0):
            proc = subprocess.run(
                [sys.executable, "-m", "council_engine", "--home", str(self.home), *args],
                cwd=REPO, capture_output=True, text=True, encoding="utf-8",
            )
            self.assertEqual(proc.returncode, expect, proc.stdout + proc.stderr)
            return proc.stdout + proc.stderr

        out = cli("ask", "client q", "-q", "q?", "--client-data", "--rounds", "1")
        self.assertIn("client data: only claude, chatgpt take part", out)
        self.assertEqual(json.loads((self.ws.thread("1").path / "thread.json").read_text())["client_data"], True)
        out = cli("new", "x", "-q", "q?", "--client-data", "--agents", "claude,kimi", expect=2)
        self.assertIn("Kimi is not cleared for client data", out)
        out = cli("ask", "y", "-q", "q?", "--client-data", "--synth", "kimi", expect=2)
        self.assertIn("cannot write this synthesis", out)
        self.assertIn("client data:  yes", cli("status", "1"))


class StopTests(EngineTestCase):
    def test_stop_kills_agents_and_saves_nothing(self) -> None:
        import threading as _threading

        thread = self.new_thread()
        pidfile = Path(self._tmp.name) / "pids.txt"
        os.environ["FAKE_PIDFILE"] = str(pidfile)
        self._env_keys.append("FAKE_PIDFILE")
        self.mode("kimi", "hang")
        stop = _threading.Event()

        def press_stop() -> None:
            deadline = time.monotonic() + 20
            while not pidfile.exists() and time.monotonic() < deadline:
                time.sleep(0.1)
            stop.set()

        _threading.Thread(target=press_stop, daemon=True).start()
        from council_engine.runner import RunStopped

        started = time.monotonic()
        with self.assertRaises(RunStopped):
            run_round(thread, self.config, stop=stop)
        self.assertLess(time.monotonic() - started, 30)
        for pid in map(int, pidfile.read_text().split()):
            end = time.monotonic() + 5
            while _alive(pid) and time.monotonic() < end:
                time.sleep(0.1)
            self.assertFalse(_alive(pid), f"pid {pid} survived Stop")
        self.assertEqual(thread.rounds, [])
        self.assertFalse(thread.response_path(1, "claude").exists())


class ConfigEditTests(EngineTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.path = Path(self._tmp.name) / "council.toml"
        self.path.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")

    def test_defaults_keep_comments(self) -> None:
        from council_engine.config_edit import set_default

        set_default(self.path, "timeout_minutes", 40)
        set_default(self.path, "auto_push", False)
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("timeout_minutes = 40   # per agent, per round", text)
        self.assertIn("auto_push = false", text)
        config = load_config(self.path)
        self.assertEqual(config.timeout_s, 2400)
        self.assertFalse(config.auto_push)

    def test_client_data_toggle(self) -> None:
        from council_engine.config_edit import set_agent_value

        set_agent_value(self.path, "kimi", "client_data", True)
        self.assertTrue(load_config(self.path).get("kimi").client_data)
        self.assertIn("client_data = true    # China-based provider", self.path.read_text(encoding="utf-8"))
        set_agent_value(self.path, "kimi", "client_data", False)
        self.assertFalse(load_config(self.path).get("kimi").client_data)

    def test_levels_round_trip(self) -> None:
        from council_engine.config_edit import get_levels, set_levels

        set_levels(self.path, "claude", {"model": "best", "effort": "max"})
        set_levels(self.path, "chatgpt", {"reasoning": "high"})
        config = load_config(self.path)
        claude, codex = config.get("claude"), config.get("chatgpt")
        self.assertEqual(get_levels(claude), {"model": "best", "effort": "max"})
        self.assertEqual(get_levels(codex), {"reasoning": "high"})
        self.assertEqual(codex.command[:4], ["codex", "exec", "-c", "model_reasoning_effort=high"])
        self.assertEqual(codex.command[-1], "{instruction}")
        self.assertIn("--tools", claude.command)  # read-only flags untouched
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("# -p: non-interactive. --tools", text)  # comments kept
        set_levels(self.path, "claude", {"model": "default", "effort": "default"})
        set_levels(self.path, "chatgpt", {"reasoning": "default"})
        config = load_config(self.path)
        self.assertNotIn("--model", config.get("claude").command)
        self.assertNotIn("-c", config.get("chatgpt").command)
        self.assertEqual(config.get("claude").command, load_config(self._default_copy()).get("claude").command)

    def _default_copy(self) -> Path:
        p = Path(self._tmp.name) / "fresh.toml"
        p.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
        return p

    def test_bad_level_leaves_file_untouched(self) -> None:
        from council_engine.config_edit import set_levels

        before = self.path.read_text(encoding="utf-8")
        with self.assertRaises(CouncilError):
            set_levels(self.path, "claude", {"effort": "turbo"})
        with self.assertRaises(CouncilError):
            set_levels(self.path, "kimi", {"model": "x"})
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)

    def test_glm_model_switch(self) -> None:
        from council_engine.config_edit import get_levels, seat_kind, set_levels

        add_preset(self.path, "glm")
        set_levels(self.path, "glm", {"model": "glm-5.3-flash"})
        spec = load_config(self.path).get("glm")
        self.assertEqual(seat_kind(spec), "glm")
        self.assertEqual(get_levels(spec), {"model": "glm-5.3-flash"})
        self.assertEqual(spec.env["ANTHROPIC_AUTH_TOKEN"], "${ZAI_API_KEY}")  # env table intact


class AppSupportTests(EngineTestCase):
    """Engine pieces the app relies on."""

    def test_kimi_bullet_wrapping_is_removed(self) -> None:
        from council_engine.agents import unwrap_cli_bullets

        wrapped = "\u2022 # Title\n\n  ## Part\n  text\n  | a | b |\n"
        self.assertEqual(clean_output(wrapped), "# Title\n\n## Part\ntext\n| a | b |\n")
        # Ordinary Markdown and a plain list of bullets are left alone.
        for text in ("# Title\n\n  indented code?\n", "\u2022 one\n\u2022 two\n", "plain\n"):
            self.assertEqual(unwrap_cli_bullets(text), text)

    def test_plan_is_recorded_with_the_thread(self) -> None:
        thread = self.ws.create_thread("t", "q?", ["claude"], plan={"rounds": 3, "synthesis_by": None})
        on_disk = json.loads((thread.path / "thread.json").read_text(encoding="utf-8"))
        self.assertEqual(on_disk["plan"], {"rounds": 3, "synthesis_by": None})
        self.assertEqual(self.ws.git("status", "--porcelain", "--", "threads").stdout.strip(), "")  # committed

    def test_unpushed_count_and_log(self) -> None:
        self.assertIsNone(self.ws.unpushed_commits())  # no remote
        bare = Path(self._tmp.name) / "remote.git"
        subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
        self.ws.git("remote", "add", "origin", str(bare))
        thread = self.new_thread()
        self.assertIsNone(self.ws.unpushed_commits())  # branch never pushed
        self.assertEqual(self.ws.push(), "pushed")
        self.assertEqual(self.ws.unpushed_commits(), 0)
        run_round(thread, self.config)
        self.assertEqual(self.ws.unpushed_commits(), 1)
        subjects = [c["subject"] for c in self.ws.log(thread.path)]
        self.assertEqual(subjects, ["T-0001 round 1: claude ok, chatgpt ok, kimi ok",
                                    'T-0001: new thread "LDO selection"'])

    def test_question_survives_windows_line_endings(self) -> None:
        thread = self.ws.create_thread("t", "line one\nline two", ["claude"])
        thread.question_path.write_bytes(b"line one\r\nline two\r\n")  # as git checks out on Windows
        self.assertEqual(thread.question(), "line one\nline two\n")
        thread.question_path.write_text("edited\n", encoding="utf-8")
        with self.assertRaises(CouncilError):
            thread.question()

    def test_command_lines_stay_readable(self) -> None:
        from council_engine.config_edit import _format_command

        text = _format_command(["codex", "exec", "-o", "{output_file}", "{instruction}"])
        self.assertEqual(text.splitlines(), ["command = [", '  "codex", "exec",',
                                             '  "-o", "{output_file}",', '  "{instruction}",', "]"])


if __name__ == "__main__":
    unittest.main()
