' FlowShift runtime launcher (TEMPLATE — copy to start_flowshift.vbs and edit paths).
'
' This starts the FlowShift runtime (tray.py --tray) with pythonw so no console
' window appears. Prefer the installer's Scheduled Task autostart instead of a
' hand-edited VBS; this template exists only for manual/dev use.
'
' Replace the two paths below with your local pythonw.exe and tray.py locations.
' Note: start_flowshift.vbs is git-ignored so your private paths never get committed.

Set WshShell = CreateObject("WScript.Shell")
WshShell.Run """C:\Path\To\pythonw.exe"" ""C:\Path\To\flowshift\src\python\tray.py"" --tray", 0, False
