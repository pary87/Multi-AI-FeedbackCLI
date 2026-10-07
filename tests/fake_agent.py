"""Stand-in for a vendor CLI, used only by the tests.

It reads ROUND.md from its working folder and replies with a report of exactly
what it could see, so tests can prove what each agent was (and was not) shown.

Behaviour per agent comes from the environment variable FAKE_MODE_<NAME>:
    normal   (default) reply with the report
    slow     wait 2 s, then list files and reply (proves round-1 blindness)
    fail     print an error to stderr and exit 3
    empty    exit 0 with no reply
    hang     start a grandchild, write both PIDs to $FAKE_PIDFILE, sleep 300 s
    tamper   plant a file in the working folder, then reply normally
    footer   reply normally plus a trailing session footer
    badping  answer a ping with the wrong text
"""

import argparse
import hashlib
import os
import re
import subprocess
import sys
import time
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--name", required=True)
parser.add_argument("--instruction", required=True)
parser.add_argument("--out", help="write the reply here instead of stdout")
args = parser.parse_args()
mode = os.environ.get(f"FAKE_MODE_{args.name.upper()}", "normal")


def emit(text: str) -> None:
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        sys.stdout.buffer.write(text.encode("utf-8"))
        sys.stdout.flush()


print(f"{args.name} fake 1.0 starting", file=sys.stderr)

if mode == "fail":
    print("error: not logged in. Run the login command first.", file=sys.stderr)
    sys.exit(3)

if mode == "hang":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
    pidfile = os.environ.get("FAKE_PIDFILE")
    if pidfile:
        Path(pidfile).write_text(f"{os.getpid()} {child.pid}")
    time.sleep(300)
    sys.exit(0)

round_md = Path("ROUND.md").read_text(encoding="utf-8")
first_line = round_md.splitlines()[0]

if "connectivity check" in first_line:
    emit("something else entirely\n" if mode == "badping" else "COUNCIL-OK\n")
    sys.exit(0)

if mode == "slow":
    time.sleep(2)

seen = re.findall(r'<response participant="([^"]+)" round="(\d+)"', round_md)
missing = re.findall(r'<response participant="([^"]+)" round="(\d+)" missing="true"', round_md)
files = sorted(p.relative_to(".").as_posix() for p in Path(".").rglob("*") if p.is_file())

if mode == "tamper":
    Path("planted.txt").write_text("this must never reach the thread", encoding="utf-8")

if mode == "empty":
    sys.exit(0)

report = "\n".join(
    [
        f"FAKE {args.name}",
        f"header: {first_line}",
        "seen: " + (",".join(f"{who}@{rnd}" for who, rnd in seen) or "none"),
        "missing: " + (",".join(f"{who}@{rnd}" for who, rnd in missing) or "none"),
        "files: " + ",".join(files),
        f"instruction_mentions_round_md: {'ROUND.md' in args.instruction}",
        f"round_md_sha256: {hashlib.sha256(round_md.encode('utf-8')).hexdigest()}",
        "unicode: résumé — ✓",
    ]
) + "\n"
if mode == "footer":
    report += "\n\n[session saved: fake-resume-123]\n"
emit(report)
