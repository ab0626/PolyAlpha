"""Best-effort desktop notifications for long-running jobs.

Windows-only: pops a transient NotifyIcon balloon via PowerShell. Every call
fails silently on non-Windows or headless sessions, so a notification can
never break the job that requests it.
"""

from __future__ import annotations

import subprocess
import sys
import time


def toast(title: str, message: str, kind: str = "info", timeout_ms: int = 8000) -> None:
    """Show a Windows desktop toast (NotifyIcon balloon), non-blocking.

    The PowerShell process is detached and self-terminates after keeping the
    balloon visible, so callers never wait on it.
    """
    if sys.platform != "win32":
        return
    if kind == "warning":
        icon, tip = "Warning", "Warning"
    else:
        icon, tip = "Information", "Info"
    # Balloon tips render single-line; escape the one char that breaks quoting.
    safe_title = title.replace("'", "`'")
    safe_message = message.replace("'", "`'")
    script = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "Add-Type -AssemblyName System.Drawing; "
        "$n = New-Object System.Windows.Forms.NotifyIcon; "
        f"$n.Icon = [System.Drawing.SystemIcons]::{icon}; "
        "$n.Visible = $true; "
        f"$n.ShowBalloonTip({timeout_ms}, '{safe_title}', '{safe_message}', "
        f"[System.Windows.Forms.ToolTipIcon]::{tip}); "
        "Start-Sleep -Seconds 10; $n.Dispose()"
    )
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-Command", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:  # noqa: BLE001 — notification is best-effort
        pass


class ThrottledToaster:
    """Emit toasts at most every ``interval_seconds``, plus a forced final one.

    Decouples "how fast progress changes" from "how often the user is poked".
    """

    def __init__(self, interval_seconds: float = 300.0):
        self.interval_seconds = interval_seconds
        self._last_at = 0.0

    def maybe(self, title: str, message: str, kind: str = "info", force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_at < self.interval_seconds:
            return
        self._last_at = now
        toast(title, message, kind=kind)
