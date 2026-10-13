"""Windows desktop notifications (toasts), sent through the PowerShell that ships with Windows.

The title and message travel in environment variables, never inside the script text, so
nothing in them can be interpreted as code.
"""
from __future__ import annotations

import os
import subprocess
import sys

from config import NO_CONSOLE

POWERSHELL_APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"
SCRIPT = f"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$title = [System.Security.SecurityElement]::Escape($env:RECAPPER_TOAST_TITLE)
$message = [System.Security.SecurityElement]::Escape($env:RECAPPER_TOAST_MESSAGE)
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml("<toast><visual><binding template='ToastGeneric'><text>$title</text><text>$message</text></binding></visual></toast>")
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{POWERSHELL_APP_ID}').Show($toast)
"""


def toast(title: str, message: str) -> None:
    """Show a notification without waiting for it; does nothing off Windows."""
    if sys.platform != "win32":
        return
    subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-Command", SCRIPT],
                     env={**os.environ, "RECAPPER_TOAST_TITLE": title, "RECAPPER_TOAST_MESSAGE": message},
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, **NO_CONSOLE)
