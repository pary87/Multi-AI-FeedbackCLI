"""council_engine -- put one question to several AI command-line agents, in rounds.

The engine never talks to an AI service itself. It launches each vendor's own
CLI (Claude Code, Codex CLI, Kimi Code CLI) as a subprocess -- each one logged
in with your subscription -- and moves text between them through files on disk.

Layering, so a GUI can sit on top later without touching the logic:
    store.py     -- workspace, threads, manifest, git commits   (the data model)
    agents.py    -- council.toml, how each CLI is launched       (the adapters)
    protocol.py  -- the text each agent is given each round      (the rules)
    runner.py    -- run a round / synthesis / ping, emit events  (the engine)
    cli.py       -- argparse front end that prints those events  (one UI of many)
"""

__version__ = "0.1.0"
