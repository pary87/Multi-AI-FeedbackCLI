# Multi-AI-FeedbackCLI

Two tools live in this repo:

| Path | What it is |
|---|---|
| `council.py` | The v0 manual flow: it writes prompt files and you paste them into chatbot tabs. Milestones M1–M4. This branch does not change it, and its M1 TODO (`read_text_file`) is still yours to write. |
| `council_engine/` | **The terminal council.** Claude Code, Codex CLI (ChatGPT), Kimi Code CLI and optionally GLM-5.3 answer the same question in rounds, and each round reads the one before. No copy-paste. |

The rest of this README covers `council_engine`.

## How a thread runs

```
00-question.md ─┬─► Claude  ─┐
  (+attachments)├─► ChatGPT ─┼─► r1-*.md ── commit
                └─► Kimi    ─┘      │
                                    ▼
  round 2: each agent reads the question + every r1 reply, answers the others by name
                                    │
                                    ▼
                          r2-*.md ── commit ──► (optional) synthesis-<agent>.md
```

Each agent is launched through its vendor's own CLI, logged in with your subscription. No API keys are involved. Every agent gets the same one-line instruction ("read ROUND.md and follow it"). Everything else is in `ROUND.md`: the rules, the question, and every earlier reply.

### Guarantees, and how each is enforced

- **Round 1 is blind.** Each agent runs in its own temporary folder, which holds only `ROUND.md` and the attachments. Replies are copied into the thread only after *every* agent in the round has finished. An agent can't see another's answer even by listing files. A test covers this, with one agent finishing late on purpose.
- **Agents are read-only.**
  - Claude runs with `--tools Read,Glob,Grep` and `--strict-mcp-config`.
  - Codex runs with `--sandbox read-only`.
  - Kimi's `-p` mode auto-approves every action, so it gets an `--agent-file` profile whose only tools are `Read, Glob, Grep`.
  - Anything an agent writes in its temp folder is thrown away, and the round reports it as a warning.
- **Auditable.** Each thread's `thread.json` records, per agent per round:
  - the CLI version, exit code and duration;
  - the **sha256 of the exact `ROUND.md` that agent was shown**;
  - the sha256 of its reply.

  Every round is one git commit in the workspace.
- **The question is locked.** If `00-question.md` is edited after round 1, the next run is refused, because earlier rounds answered the original text.
- **Failures are contained.**
  - A failed or timed-out agent leaves its slot empty, and its stderr is saved to `logs/`.
  - The next round won't start until you either `retry` the agent or pass `--allow-partial`.
  - Timeouts and Ctrl+C kill each CLI's whole process tree. Ctrl+C saves nothing.

## One-time setup (Windows)

1. **Python 3.11 or newer, and git.** Check both with `python --version` and `git --version`.
2. **Claude Code**, logged in with your Max plan. You already have this.
3. **Codex CLI** (ChatGPT plan):
   ```
   npm install -g @openai/codex
   codex login            (choose "Sign in with ChatGPT")
   codex login status
   ```
4. **Kimi Code CLI.** This needs a Kimi membership that includes Kimi Code; free kimi.com does not cover the CLI.
   ```
   npm install -g @moonshot-ai/kimi-code
   kimi login --region global
   ```
5. **Make the engine runnable from any folder** (optional). Run this once, then open a new terminal:
   ```
   setx PYTHONPATH "C:\path\to\Multi-AI-FeedbackCLI"
   ```
   Without it, run the commands below from the repo folder.
6. **Create the workspace and test all three logins:**
   ```
   python -m council_engine init
   python -m council_engine doctor --ping
   ```
   `doctor --ping` sends each agent a one-word test prompt through the exact same launch path a real round uses. When it prints `all good`, the setup works.

The workspace is the folder named by the `COUNCIL_HOME` environment variable (on Pary's laptop: `Documents\AI-Apps\Council\workspace`). Without it, it defaults to `%USERPROFILE%\council`. `--home <folder>` before the command overrides both.

### Optional: add GLM-5.3 as a fourth seat

GLM-5.3 runs on the Z.ai GLM Coding Plan (a flat monthly subscription) through Claude Code pointed at Z.ai, which is the setup Z.ai documents. It uses its own Claude Code profile folder (`%USERPROFILE%\.claude-glm`), so your normal Claude seat and Anthropic login are untouched. The seat also strips `ANTHROPIC_API_KEY` and `CLAUDE_CODE_OAUTH_TOKEN` from its environment, so no Anthropic credential is ever sent to Z.ai. A test proved this against a stand-in server.

Do **not** run Z.ai's own setup helper or edit your main `.claude\settings.json`; those would switch every Claude session to GLM.

1. Subscribe to a GLM Coding Plan and create an API key at https://z.ai/manage-apikey/apikey-list.
2. Store the key in Windows (replace the placeholder), then fully restart VS Code:
   ```
   [Environment]::SetEnvironmentVariable('ZAI_API_KEY', 'PASTE-KEY-HERE', 'User')
   ```
3. Add the seat and test it:
   ```
   python -m council_engine add-agent glm
   python -m council_engine doctor --ping --agents glm
   ```
New threads then include GLM. Leave it out of one question with `--agents claude,chatgpt,kimi`.

### Automatic upload to GitHub

If the workspace git repo has an `origin` remote (for example a private `council-workspace` repo), every new thread and every finished round is pushed automatically. A failed upload never fails a run: the threads stay safe locally and the next successful upload carries everything. `doctor` shows the sync status. To turn it off, set `auto_push = false` under `[defaults]` in `council.toml`.

## Daily use

```
python -m council_engine ask "1V8 rail LDO" -f question.md --attach "C:\docs\TPS7A20.pdf" --rounds 2 --synth chatgpt
python -m council_engine status                 list threads
python -m council_engine status T-0003          one thread, round by round
python -m council_engine retry T-0003           re-run whoever failed in the latest round
python -m council_engine round T-0003           add one more round
python -m council_engine synth T-0003 --by claude
```

| Command | Purpose |
|---|---|
| `add-agent glm` | Add a ready-made seat to `council.toml` (currently: `glm`). |
| `ask TITLE -q TEXT \| -f FILE [--attach F]... [--agents a,b] [--rounds N] [--synth AGENT]` | Create a thread and run it. |
| `new` (same question options) | Create a thread without running it. |
| `round THREAD [--allow-partial]` | Run the next round. |
| `run THREAD --rounds N [--synth AGENT]` | Run until the thread has N complete rounds. |
| `retry THREAD [--only a,b] [--force]` | Re-run agents in the latest round. `--force` replaces replies that succeeded; if the forced re-run fails, the old reply is kept. |
| `synth THREAD --by AGENT` | One agent writes the synthesis: answer, consensus, disagreements, corrections, and decisions left to you. |
| `status [THREAD]` | Show state. |
| `doctor [--ping]` | Check installs, versions and logins. |

`THREAD` can be `T-0003`, `3`, or the folder name. Every run command also takes `--timeout-min` and `--keep-views`; the second keeps each agent's temp folder for debugging.

Results land in `<workspace>\threads\T-0003-...\`. That folder holds `r1-claude.md`, `r1-chatgpt.md`, `r1-kimi.md`, `r2-...`, `synthesis-...`, and `thread.json`.

## Using it from VS Code

Open the repo folder in VS Code and install the recommended extensions when prompted. Python is required, because the tasks run with the interpreter you select there. Claude Code and Codex are optional for the council itself.

Then use **Terminal > Run Task...** (or Ctrl+Shift+P, then "Run Task"). The task list includes:

| Task | What it does |
|---|---|
| Council: ask about the open file | The file open in the editor is the question. It asks for a title, the number of rounds and an optional synthesis agent, then runs everything. |
| Council: next round / retry failed agents / synthesis | Asks for the thread ID. |
| Council: status of all threads / one thread | Shows the state of the threads. |
| Council: doctor (check installs and logins) | The same as `doctor --ping`. |
| Council: create workspace (first time only) | The same as `init`. |
| Council: add the GLM seat (first time only) | The same as `add-agent glm`. |
| Council: add the threads folder to this window | Puts the workspace (`COUNCIL_HOME`) in the Explorer, so replies open as Markdown beside the code. |
| Council: run tests | The default test task. The Testing panel also discovers the tests. |

The workspace settings put the repo on `PYTHONPATH` in VS Code terminals, so `python -m council_engine ...` works from any folder there. Attachments still go through the terminal: `--attach "C:\path\file.pdf"`.

## When a CLI update breaks something

Each CLI's launch details (flags, read-only switches, capture mode) are stored as data in the workspace's `council.toml`, not in code. If a vendor renames a flag, edit that line and re-run `doctor --ping`. The defaults were checked against the `--help` output of Claude Code 2.1.292, Codex CLI 0.160.1 and Kimi Code CLI 2.1.1.

To add another CLI, add an `[agents.<name>]` table to `council.toml`. No code change is needed. A seat can also carry an `[agents.<name>.env]` table: values may use `${OS_VARIABLE}` (read at launch, never saved, and scrubbed from replies and logs), `{home}` and `{council_dir}`. `unset_env = [...]` removes variables from that seat's process.

## Vendor terms

Each vendor's model is reached only through that vendor's official CLI with its normal subscription login. The GLM seat uses Z.ai's documented Claude Code setup with a Z.ai key, in a separate Claude Code profile. The Claude subscription is never routed through a third-party harness. This setup is for personal use. If it becomes a product or service you sell, move Claude to API-key authentication.

## Path to an app

The engine was built so a GUI can sit on top without touching the logic:

- **`runner.py`** exposes `run_round`, `run_synthesis` and `ping`. It never prints. Progress arrives through an `on_event` callback, one dict per event (`run_started`, `agent_started`, `agent_finished`, `run_finished`). The CLI is just one consumer of those events.
- **`store.py`** makes the data model plain files plus `thread.json`. Any front end (a Streamlit page, a local web app, a desktop shell) can list threads, render rounds side by side, and call the same three functions.
- **`council.toml`** stays the single source of truth for which agents exist.

## Tests

```
python -m unittest discover -s tests -v
```

There are 31 tests. They use `tests/fake_agent.py` in place of the real CLIs. It reports exactly what it was shown, which lets the tests prove:

- round-1 blindness;
- the round-2 transcript;
- that recorded hashes equal what each agent read;
- retries and forced re-runs;
- that `--allow-partial` marks the missing reply;
- timeout and Ctrl+C process-tree kills;
- isolation of agent writes;
- footer stripping;
- config validation;
- the Windows `.cmd` argument guard;
- per-seat environment variables, missing-variable errors, and secrets never reaching saved files or git history;
- stripping of credentials a seat must not see;
- automatic upload to a remote, and that a failed upload never fails a run;
- the full CLI flow.
