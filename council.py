"""council -- turn a manual multi-chatbot critique loop into a guided workflow.

v0 is subscription-only: this tool never talks to any AI service. It writes
prompt files for you to paste into your chatbot tabs, and reads back the
replies you save. Transport is you.
"""

import argparse
import sys
from pathlib import Path

# The spec targets 3.12+, but nothing in v0 needs features newer than 3.11,
# so an older interpreter gets a warning instead of a refusal.
RECOMMENDED_PYTHON = (3, 12)

SELFTEST_FILE = Path(".council_selftest.txt")


def write_text_file(path: Path, text: str) -> None:
    """Write `text` to `path`, creating parent folders as needed.

    UTF-8 is forced explicitly because Windows otherwise defaults to a legacy
    encoding (cp1252) that silently mangles characters like arrows and
    em-dashes -- and chatbot output is full of those.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def read_text_file(path: Path) -> str:
    # TODO(pary): implement me -- this is M1's hands-on function.
    """Return the full text content of `path`.

    Inputs:  path -- a pathlib.Path to a text file that may or may not exist.
    Output:  the file's contents as one string, decoded as UTF-8.
    Errors:  if the file does not exist, raise SystemExit with a one-line
             human message naming the missing file, e.g.
                 raise SystemExit("council: file not found: draft.md")
             Never let a raw FileNotFoundError traceback reach the user.

    Hint 1: a Path object knows whether its file exists: path.exists()
    Hint 2: mirror write_text_file above -- reading is path.read_text(...),
            and it takes the same encoding argument.
    """
    raise NotImplementedError("read_text_file is Pary's M1 TODO")


def cmd_check(args: argparse.Namespace) -> int:
    """Environment self-test: interpreter version plus a file write/read loop."""
    print("hello from council")
    version = sys.version_info
    print(f"python: {version.major}.{version.minor}.{version.micro}")
    if version < RECOMMENDED_PYTHON:
        print("warning: spec recommends 3.12+; everything in v0 still works here")
    # Round-trip a scratch file: the run folder IS this tool's entire storage
    # model, so proving disk persistence works is the whole point of `check`.
    message = "council self-test: written and read back"
    write_text_file(SELFTEST_FILE, message)
    read_back = read_text_file(SELFTEST_FILE)
    SELFTEST_FILE.unlink()  # leave no litter behind in the project folder
    if read_back == message:
        print("file round-trip: OK")
        return 0
    print("file round-trip: FAILED (read something different than was written)")
    return 1


def build_parser() -> argparse.ArgumentParser:
    """Define the CLI surface. Each subcommand maps to one cmd_* function."""
    parser = argparse.ArgumentParser(
        prog="council",
        description="Multi-chatbot critique loop: you paste, it orchestrates.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="verify Python + file I/O work here")
    check.set_defaults(func=cmd_check)

    # Future subcommands land here milestone by milestone:
    #   new (M2)   critique (M2)   merge (M3)   gate (M4)   diff (M4)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
