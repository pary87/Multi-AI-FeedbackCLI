"""python -m council_app [--home FOLDER] [--port 8090] [--no-browser]"""

from __future__ import annotations

import argparse
import importlib.util
import socket
import sys
import webbrowser
from pathlib import Path


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def main(argv: list[str] | None = None) -> int:
    if sys.version_info < (3, 11):
        print("Council needs Python 3.11 or newer. Install it from https://www.python.org/downloads/")
        return 2
    parser = argparse.ArgumentParser(prog="python -m council_app", description=__doc__)
    parser.add_argument("--home", help="workspace folder (default: COUNCIL_HOME, else ~/council)")
    parser.add_argument("--port", type=int, default=8090, help="local port (default 8090)")
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser tab")
    args = parser.parse_args(argv)

    if importlib.util.find_spec("nicegui") is None:
        print("The app needs NiceGUI, which is not installed yet. Install it with:")
        print("  python -m pip install -r requirements-app.txt")
        return 2

    from council_engine.store import CouncilError, Workspace, default_home

    root = Path(args.home).expanduser() if args.home else default_home()
    try:
        ws = Workspace.open(root)
    except CouncilError as exc:
        print(f"Council could not open its workspace: {exc}")
        return 2

    url = f"http://127.0.0.1:{args.port}/"
    if _port_in_use(args.port):
        print(f"Council is already running at {url} (or another program uses port {args.port}).")
        if not args.no_browser:
            webbrowser.open(url)
        return 0

    from .server import serve

    serve(ws.root, port=args.port, show=not args.no_browser)
    return 0


if __name__ in {"__main__", "__mp_main__"}:
    sys.exit(main())
