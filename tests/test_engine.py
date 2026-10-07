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


if __name__ == "__main__":
    unittest.main()
