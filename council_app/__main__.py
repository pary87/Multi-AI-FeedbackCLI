"""python -m council_app [--home FOLDER] [--port 8090] [--browser] [--install-shortcuts]

By default Council opens in its own window (pywebview, which uses the Edge
engine built into Windows). With --browser, or if pywebview is not installed,
it opens as a tab in your browser instead.

Started with pythonw.exe (the Council shortcut does this) there is no console
window: messages go to a pop-up, and the log to ~/.council-app/council.log.
"""

from __future__ import annotations

import argparse
import importlib.util
import multiprocessing
import os
import socket
import sys
import webbrowser
from pathlib import Path

LOG_PATH = Path.home() / ".council-app" / "council.log"


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def has_console() -> bool:
    """False under pythonw.exe, where print() has nowhere to go."""
    return sys.stdout is not None and sys.stderr is not None


def send_output_to_log(path: Path = LOG_PATH) -> None:
    """Without a console, keep everything printed or logged in a file instead."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a", encoding="utf-8", buffering=1)  # noqa: SIM115 (kept open for the app's life)
    sys.stdout = sys.stderr = handle


def tell(message: str, *, error: bool = False) -> None:
    """Show a message: in the console if there is one, else in a Windows pop-up."""
    if has_console() or os.name != "nt":
        print(message)
        return
    import ctypes

    flags = 0x10 if error else 0x40  # MB_ICONERROR or MB_ICONINFORMATION
    ctypes.windll.user32.MessageBoxW(None, message, "Council", flags)  # type: ignore[attr-defined]
    print(message)


def main(argv: list[str] | None = None) -> int:
    if not has_console():
        send_output_to_log()
    if sys.version_info < (3, 11):
        tell("Council needs Python 3.11 or newer. Install it from https://www.python.org/downloads/",
             error=True)
        return 2
    parser = argparse.ArgumentParser(prog="python -m council_app", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--home", help="workspace folder (default: COUNCIL_HOME, else ~/council)")
    parser.add_argument("--port", type=int, default=8090, help="local port (default 8090)")
    parser.add_argument("--browser", action="store_true",
                        help="open in a browser tab instead of Council's own window")
    parser.add_argument("--no-browser", action="store_true",
                        help="start the server only; open nothing (for testing)")
    parser.add_argument("--install-shortcuts", action="store_true",
                        help="Windows: add Council to the Start menu and the desktop, then exit")
    args = parser.parse_args(argv)

    if args.install_shortcuts:
        from .shortcuts import install_shortcuts

        try:
            made = install_shortcuts()
        except RuntimeError as exc:
            tell(f"Could not make the shortcuts: {exc}", error=True)
            return 2
        tell("Council shortcuts made:\n" + "\n".join(f"  {p}" for p in made))
        return 0

    if importlib.util.find_spec("nicegui") is None:
        tell("Council needs NiceGUI, which is not installed yet. In PowerShell, in the "
             "Multi-AI-FeedbackCLI folder, run:\n\n  python -m pip install -r requirements-app.txt",
             error=True)
        return 2

    from council_engine.store import CouncilError, Workspace, default_home

    root = Path(args.home).expanduser() if args.home else default_home()
    try:
        ws = Workspace.open(root)
    except CouncilError as exc:
        tell(f"Council could not open its workspace.\n\n{exc}", error=True)
        return 2

    windowed = not args.browser and not args.no_browser and importlib.util.find_spec("webview") is not None
    url = f"http://127.0.0.1:{args.port}/"
    if _port_in_use(args.port):
        if windowed or not has_console():
            tell("Council is already open. Look for its window on the taskbar.\n\n"
                 f"(Or another program is using port {args.port}.)")
        else:
            print(f"Council is already running at {url} (or another program uses port {args.port}).")
            if not args.no_browser:
                webbrowser.open(url)
        return 0

    from .server import serve

    serve(ws.root, port=args.port, show=not args.no_browser, native=windowed)
    return 0


if __name__ == "__main__":
    # Only the process you started runs main(). The window runs in a helper process
    # that re-imports this module under another name, so it must not start a server.
    multiprocessing.freeze_support()
    sys.exit(main())
