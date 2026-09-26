' Launches start.bat completely hidden - no console window at all, not even a
' flash. This is what the desktop/Start Menu shortcuts point at (see
' installer.iss) instead of start.bat directly, because Windows always shows a
' visible console the instant a .bat file itself is run, no matter what that
' .bat then does internally. wscript.exe (the host for this script) has no
' window of its own, and WScript.Shell.Run's windowStyle=0 hides the cmd.exe
' window it spawns for start.bat too - so nothing is ever visible except the
' browser start.bat opens.
'
' Double-clicking start.bat directly still flashes a console for an instant
' (unavoidable for a .bat run that way) - use this file, or the desktop
' shortcut, to avoid that entirely.

Dim shell, scriptDir
Set shell = CreateObject("WScript.Shell")
scriptDir = Left(WScript.ScriptFullName, Len(WScript.ScriptFullName) - Len(WScript.ScriptName))
shell.Run Chr(34) & scriptDir & "start.bat" & Chr(34), 0, False
