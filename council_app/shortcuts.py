"""Windows shortcuts: put Council on the desktop and in the Start menu.

Each shortcut runs `pythonw.exe -m council_app` in this repo folder, so Council
opens in its own window with no black console window, and with its own icon.
Because the shortcut runs the code in this folder, `git pull` updates the app;
the shortcuts never need to be made again.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ICON = Path(__file__).resolve().parent / "assets" / "council.ico"

# Paths reach PowerShell through environment variables, so no quoting can break them.
_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$shell = New-Object -ComObject WScript.Shell
foreach ($folder in @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))) {
    $path = Join-Path $folder 'Council.lnk'
    $link = $shell.CreateShortcut($path)
    $link.TargetPath = $env:COUNCIL_LNK_TARGET
    $link.Arguments = '-m council_app'
    $link.WorkingDirectory = $env:COUNCIL_LNK_DIR
    $link.IconLocation = $env:COUNCIL_LNK_ICON + ',0'
    $link.Description = 'Council: ask Claude, ChatGPT and Kimi together'
    $link.Save()
    Write-Output $path
}
"""


def windowless_python(executable: str | None = None) -> Path:
    """pythonw.exe next to the running python.exe (no console window), if it exists."""
    exe = Path(executable or sys.executable)
    candidate = exe.with_name("pythonw.exe")
    return candidate if candidate.exists() else exe


def shortcut_env(executable: str | None = None) -> dict[str, str]:
    return {
        "COUNCIL_LNK_TARGET": str(windowless_python(executable)),
        "COUNCIL_LNK_DIR": str(REPO),
        "COUNCIL_LNK_ICON": str(ICON),
    }


def install_shortcuts() -> list[str]:
    if os.name != "nt":
        raise RuntimeError("shortcuts are for Windows; on this system run: python -m council_app")
    env = dict(os.environ, **shortcut_env())
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", _SCRIPT],
        capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL,
    )
    if proc.returncode != 0:
        lines = [line for line in (proc.stderr or proc.stdout).splitlines() if line.strip()]
        raise RuntimeError(lines[0] if lines else f"PowerShell exit code {proc.returncode}")
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]
