"""Tests of the app's run manager, helpers and local-only guard.

These need no browser and no NiceGUI: the pages are thin and were checked by
clicking through them in a real browser; everything they rely on is tested here.

Run from the repo root:   python -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from test_engine import EngineTestCase, _alive  # noqa: E402  (tests/ is on sys.path)

from council_app import helpers as h  # noqa: E402
from council_app.jobs import AppState, JobManager, config_with_levels  # noqa: E402
from council_app.security import LocalOnly  # noqa: E402
from council_engine.agents import DEFAULT_CONFIG_TOML, load_config  # noqa: E402
from council_engine.config_edit import get_levels  # noqa: E402
from council_engine.store import CouncilError  # noqa: E402


class JobTestCase(EngineTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.state = AppState(Path(self._tmp.name) / "state.json", self.home)
        self.jobs = JobManager(self.state)

    def tearDown(self) -> None:
        self.jobs.shutdown()
        super().tearDown()

    def finish(self, timeout: float = 60) -> None:
        self.assertTrue(self.jobs.wait(timeout), "the run did not finish in time")


class RunManagerTests(JobTestCase):
    def test_council_run_rounds_then_synthesis(self) -> None:
        thread = self.ws.create_thread("t", "q?", ["claude", "chatgpt", "kimi"],
                                       plan={"rounds": 2, "synthesis_by": "chatgpt"})
        job = self.jobs.start_council(self.ws, self.config, thread, rounds=2, synth_by="chatgpt")
        self.assertTrue(self.jobs.busy)
        self.assertEqual(self.jobs.running_thread_id(), thread.id)
        self.finish()
        self.assertEqual(job.state, "done", job.message)
        self.assertEqual([s.state for s in job.steps], ["done", "done", "done"])
        self.assertEqual([s.title for s in job.steps], ["Round 1", "Round 2", "Synthesis by ChatGPT"])
        self.assertTrue(all(c.status == "ok" for s in job.steps for c in s.cards.values()))
        self.assertTrue(thread.round_complete(2))
        self.assertTrue(thread.synthesis_path("chatgpt").exists())
        log = "\n".join(text for _, text in job.log)
        self.assertIn("Round 1 started: Claude, ChatGPT, Kimi", log)
        self.assertIn("Saved and committed", log)
        self.assertFalse(self.jobs.busy)

    def test_only_one_run_at_a_time(self) -> None:
        self.mode("claude", "slow")
        thread = self.new_thread()
        self.jobs.start_council(self.ws, self.config, thread, rounds=1, synth_by=None)
        with self.assertRaises(CouncilError):
            self.jobs.start_ping(self.ws, self.config, ["kimi"])
        self.finish()

    def test_failed_seat_stops_the_plan_then_retry_fills_the_gap(self) -> None:
        self.mode("kimi", "fail")
        thread = self.new_thread()
        job = self.jobs.start_council(self.ws, self.config, thread, rounds=2, synth_by="claude")
        self.finish()
        self.assertEqual(job.state, "incomplete")
        self.assertIn("Round 1 finished without Kimi", job.message)
        self.assertEqual([s.state for s in job.steps], ["incomplete", "skipped", "skipped"])
        self.assertEqual(thread.missing_agents(1), ["kimi"])

        os.environ["FAKE_MODE_KIMI"] = "normal"
        retry = self.jobs.start_retry(self.ws, self.config, thread)
        self.assertEqual(retry.steps[0].agents, ["kimi"])
        self.finish()
        self.assertEqual(retry.state, "done", retry.message)
        self.assertTrue(thread.round_complete(1))
        with self.assertRaises(CouncilError):  # nothing left to retry
            self.jobs.start_retry(self.ws, self.config, thread)

    def test_stop_closes_every_agent_and_saves_nothing(self) -> None:
        pidfile = Path(self._tmp.name) / "pids.txt"
        os.environ["FAKE_PIDFILE"] = str(pidfile)
        self._env_keys.append("FAKE_PIDFILE")
        self.mode("kimi", "hang")
        thread = self.new_thread()
        job = self.jobs.start_council(self.ws, self.config, thread, rounds=2, synth_by="claude")
        deadline = time.monotonic() + 20
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertTrue(self.jobs.stop())
        self.finish(30)
        self.assertEqual(job.state, "stopped")
        self.assertEqual([s.state for s in job.steps], ["stopped", "skipped", "skipped"])
        for pid in map(int, pidfile.read_text().split()):
            end = time.monotonic() + 5
            while _alive(pid) and time.monotonic() < end:
                time.sleep(0.1)
            self.assertFalse(_alive(pid), f"pid {pid} survived Stop")
        reloaded = self.ws.thread(thread.id)
        self.assertEqual(reloaded.rounds, [])
        self.assertFalse(reloaded.response_path(1, "claude").exists())
        self.assertFalse(self.jobs.stop())  # nothing running any more

    def test_client_data_guard_holds_in_the_app(self) -> None:
        thread = self.ws.create_thread("client", "q?", ["claude", "chatgpt"], client_data=True)
        self.jobs.start_council(self.ws, self.config, thread, rounds=1, synth_by=None)
        self.finish()
        job = self.jobs.start_synthesis(self.ws, self.config, thread, "kimi")
        self.finish()
        self.assertEqual(job.state, "error")
        self.assertIn("not cleared for client data", job.message)

    def test_connection_test_is_remembered(self) -> None:
        self.mode("kimi", "badping")
        job = self.jobs.start_ping(self.ws, self.config, ["claude", "kimi"])
        self.finish()
        self.assertEqual(job.state, "incomplete")
        results = self.state.get()["last_test"]["results"]
        self.assertEqual(results["claude"]["status"], "ok")
        self.assertEqual(results["kimi"]["status"], "failed")
        self.assertIn("at", self.state.get()["last_test"])

    def test_upload_result_is_remembered(self) -> None:
        import subprocess

        bare = Path(self._tmp.name) / "remote.git"
        subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
        self.ws.git("remote", "add", "origin", str(bare))
        thread = self.new_thread()
        job = self.jobs.start_council(self.ws, self.config, thread, rounds=1, synth_by=None)
        self.finish()
        self.assertEqual(job.push, "pushed")
        self.assertEqual(self.state.get()["last_push"]["result"], "pushed")


class LevelOverrideTests(EngineTestCase):
    def test_levels_for_one_run_leave_the_file_alone(self) -> None:
        path = Path(self._tmp.name) / "real.toml"
        path.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
        before = path.read_text(encoding="utf-8")
        config = load_config(path)
        run = config_with_levels(config, {"claude": {"model": "best", "effort": "max"},
                                          "chatgpt": {"reasoning": "high"}})
        self.assertEqual(get_levels(run.get("claude")), {"model": "best", "effort": "max"})
        self.assertEqual(get_levels(run.get("chatgpt")), {"reasoning": "high"})
        self.assertEqual(get_levels(config.get("claude")), {"model": "default", "effort": "default"})
        self.assertEqual(path.read_text(encoding="utf-8"), before)
        with self.assertRaises(CouncilError):
            config_with_levels(config, {"claude": {"effort": "turbo"}})


class HelperTests(EngineTestCase):
    def test_thread_status_words(self) -> None:
        thread = self.ws.create_thread("t", "q?", ["claude", "chatgpt", "kimi"], plan={"rounds": 2})
        self.assertEqual(h.thread_status(thread, None), ("No rounds yet", "idle"))
        self.assertEqual(h.thread_status(thread, thread.id, "Round 1")[1], "running")
        self.mode("kimi", "fail")
        from council_engine.runner import run_round

        run_round(thread, self.config)
        self.assertEqual(h.thread_status(thread, None), ("Round 1 incomplete", "warn"))
        os.environ["FAKE_MODE_KIMI"] = "normal"
        run_round(thread, self.config, retry=True)
        self.assertEqual(h.thread_status(thread, None), ("1 of 2 rounds", "warn"))
        run_round(thread, self.config)
        self.assertEqual(h.thread_status(thread, None), ("Complete", "ok"))
        row = h.thread_row(thread, self.config, None)
        self.assertEqual(row["rounds"], "2 of 2")
        self.assertEqual(row["synthesis"], "–")

    def test_preview_and_names(self) -> None:
        self.assertEqual(h.preview("# Title\n## Part\nThe **answer** is [A](http://x).\n"),
                         "The answer is A.")
        self.assertTrue(h.preview("word " * 200, 50).endswith("…"))
        self.assertEqual(h.safe_filename("../../evil/..\\run.bat"), "run.bat")
        self.assertEqual(h.safe_filename("a<b>|c.pdf"), "a_b_c.pdf")
        self.assertEqual(h.safe_filename("..."), "attachment")

    def test_github_link(self) -> None:
        self.assertEqual(h.github_web_url("https://github.com/pary87/council-workspace.git"),
                         "https://github.com/pary87/council-workspace")
        self.assertEqual(h.github_web_url("git@github.com:pary87/council-workspace.git"),
                         "https://github.com/pary87/council-workspace")
        self.assertIsNone(h.github_web_url("/srv/git/council.git"))

    def test_nice_time(self) -> None:
        now = datetime(2026, 10, 7, 18, 0).astimezone()
        self.assertEqual(h.nice_time((now - timedelta(hours=1)).isoformat(), now), "Today, 5:00 PM")
        self.assertEqual(h.nice_time((now - timedelta(days=1)).isoformat(), now), "Yesterday, 6:00 PM")
        self.assertEqual(h.nice_time(datetime(2026, 1, 3, 0, 5).astimezone().isoformat(), now),
                         "Jan 3, 2026, 12:05 AM")
        self.assertEqual(h.nice_time(None), "")

    def test_level_wording(self) -> None:
        path = Path(self._tmp.name) / "real.toml"
        path.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
        config = load_config(path)
        self.assertEqual(h.describe_levels(config.get("claude")), "Default model · default effort")
        self.assertEqual(h.describe_levels(config.get("claude"), {"model": "best", "effort": "high"}),
                         "Best available · high effort")
        self.assertEqual(h.describe_levels(config.get("chatgpt"), {"reasoning": "xhigh"}),
                         "Default model · xhigh reasoning")
        self.assertEqual(h.provider(config.get("kimi")), ("Moonshot AI, China-based", True))


class LocalOnlyTests(unittest.TestCase):
    def call(self, scope_type: str, headers: dict[str, str]) -> tuple[list[dict], bool]:
        reached = []

        async def inner(scope, receive, send):
            reached.append(True)

        sent: list[dict] = []

        async def receive():
            return {"type": "websocket.connect"}

        async def send(message):
            sent.append(message)

        scope = {"type": scope_type,
                 "headers": [(k.encode(), v.encode()) for k, v in headers.items()]}
        asyncio.run(LocalOnly(inner, 8090)(scope, receive, send))
        return sent, bool(reached)

    def test_own_browser_is_let_in(self) -> None:
        for host in ("127.0.0.1:8090", "localhost:8090"):
            _, reached = self.call("http", {"host": host})
            self.assertTrue(reached)
        _, reached = self.call("websocket", {"host": "127.0.0.1:8090", "origin": "http://127.0.0.1:8090"})
        self.assertTrue(reached)

    def test_foreign_host_or_origin_is_refused(self) -> None:
        sent, reached = self.call("http", {"host": "evil.example:8090"})
        self.assertFalse(reached)
        self.assertEqual(sent[0]["status"], 403)
        sent, reached = self.call("http", {"host": "127.0.0.1:8090", "origin": "https://evil.example"})
        self.assertFalse(reached)
        sent, reached = self.call("websocket", {"host": "127.0.0.1:8090", "origin": "https://evil.example"})
        self.assertFalse(reached)
        self.assertEqual(sent[0]["type"], "websocket.close")
        sent, reached = self.call("http", {"host": "127.0.0.1:9999"})  # another port's page
        self.assertFalse(reached)


@unittest.skipUnless(importlib.util.find_spec("nicegui"), "NiceGUI is not installed")
class PagesImportTests(unittest.TestCase):
    def test_pages_module_loads(self) -> None:
        from council_app import server

        import inspect

        self.assertTrue(callable(server.build) and callable(server.serve))
        self.assertIn("native", inspect.signature(server.serve).parameters)
        self.assertTrue(server.ICON_PATH.is_file())


class AppStateTests(unittest.TestCase):
    def test_state_is_per_workspace_and_survives_bad_files(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sub" / "state.json"
            a, b = AppState(path, Path(tmp) / "a"), AppState(path, Path(tmp) / "b")
            a.record_push("pushed")
            self.assertEqual(a.get()["last_push"]["result"], "pushed")
            self.assertEqual(b.get(), {})
            path.write_text("{not json", encoding="utf-8")
            self.assertEqual(a.get(), {})
            a.record_test({"claude": {"status": "ok"}})
            self.assertEqual(json.loads(path.read_text())["workspaces"][str(Path(tmp) / "a")]
                             ["last_test"]["results"]["claude"]["status"], "ok")


class LauncherTests(unittest.TestCase):
    """The own-window launcher: no console, pop-up messages, shortcuts, the icon."""

    def test_helper_process_never_starts_a_second_server(self) -> None:
        import runpy
        from unittest import mock

        import warnings

        with mock.patch("council_app.__main__.main") as fake_main, warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)  # runpy notes the module is already loaded
            # The window's helper process may import this module under another name.
            runpy.run_module("council_app.__main__", run_name="__mp_main__")
        fake_main.assert_not_called()

    def test_output_goes_to_a_log_without_a_console(self) -> None:
        import sys
        import tempfile

        from council_app import __main__ as launcher

        saved = sys.stdout, sys.stderr
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "logs" / "council.log"
            try:
                launcher.send_output_to_log(log)
                print("hello from the app")
                sys.stdout.flush()
            finally:
                sys.stdout.close()
                sys.stdout, sys.stderr = saved
            self.assertIn("hello from the app", log.read_text(encoding="utf-8"))
        self.assertTrue(launcher.has_console())

    def test_shortcuts_point_at_windowless_python(self) -> None:
        import tempfile

        from council_app import shortcuts

        with tempfile.TemporaryDirectory() as tmp:
            python = Path(tmp) / "python.exe"
            python.write_text("")
            self.assertEqual(shortcuts.windowless_python(str(python)), python)  # no pythonw: fall back
            (Path(tmp) / "pythonw.exe").write_text("")
            self.assertEqual(shortcuts.windowless_python(str(python)), Path(tmp) / "pythonw.exe")
            env = shortcuts.shortcut_env(str(python))
        self.assertTrue(env["COUNCIL_LNK_TARGET"].endswith("pythonw.exe"))
        self.assertEqual(Path(env["COUNCIL_LNK_DIR"]), Path(__file__).resolve().parent.parent)
        self.assertTrue(Path(env["COUNCIL_LNK_ICON"]).is_file())
        if os.name != "nt":
            with self.assertRaises(RuntimeError):
                shortcuts.install_shortcuts()

    def test_icon_is_a_real_windows_icon(self) -> None:
        icon = Path(__file__).resolve().parent.parent / "council_app" / "assets" / "council.ico"
        data = icon.read_bytes()
        self.assertEqual(data[:4], b"\x00\x00\x01\x00")  # ICO header
        self.assertGreaterEqual(int.from_bytes(data[4:6], "little"), 4)  # several sizes


if __name__ == "__main__":
    unittest.main()
