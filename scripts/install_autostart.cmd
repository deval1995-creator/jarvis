@echo off
rem Start Jarvis (Core + voice, kept running) whenever you log in to Windows,
rem with a shortcut in your Startup folder (no admin rights needed).
rem Undo: delete "Jarvis" from shell:startup
powershell -NoProfile -Command ^
  "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Startup')+'\Jarvis.lnk');" ^
  "$s.TargetPath='%~dp0..\.venv\Scripts\pythonw.exe'; $s.Arguments='\"%~dp0jarvis_service.py\"';" ^
  "$s.WorkingDirectory='%~dp0..'; $s.WindowStyle=7; $s.Save()"
start "" "%~dp0..\.venv\Scripts\pythonw.exe" "%~dp0jarvis_service.py"
