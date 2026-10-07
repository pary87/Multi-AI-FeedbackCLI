import sys

# tomllib (used to read council.toml) arrived in Python 3.11.
if sys.version_info < (3, 11):
    sys.exit(f"council_engine needs Python 3.11 or newer; this is {sys.version.split()[0]}")

from .cli import main  # noqa: E402  (import after the version check on purpose)

raise SystemExit(main())
