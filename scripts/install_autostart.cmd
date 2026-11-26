@echo off
rem Start Jarvis (Core + voice, kept running) whenever you log in to Windows.
rem Undo with:  schtasks /Delete /TN Jarvis /F
schtasks /Create /TN "Jarvis" /SC ONLOGON /RL LIMITED /F ^
  /TR "\"%~dp0..\.venv\Scripts\pythonw.exe\" \"%~dp0jarvis_service.py\""
schtasks /Run /TN "Jarvis"
