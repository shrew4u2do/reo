# Launch reo: backend (FastAPI + go2rtc) and frontend (Vite dev server).
# Usage:  powershell -ExecutionPolicy Bypass -File start.ps1
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot

# Make sure winget-installed tools (node, ffmpeg) are on PATH for child procs.
$env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
            [System.Environment]::GetEnvironmentVariable("Path", "User")

Write-Host "Starting backend (http://localhost:8000) + go2rtc (http://localhost:1984) ..."
$backend = Start-Process -PassThru -WorkingDirectory "$root\backend" `
  -FilePath "$root\backend\.venv\Scripts\python.exe" `
  -ArgumentList @('-m', 'uvicorn', 'app.main:app', '--host', '0.0.0.0', '--port', '8000')

Write-Host "Starting frontend (http://localhost:5173) ..."
$frontend = Start-Process -PassThru -WorkingDirectory "$root\frontend" `
  -FilePath "npm.cmd" -ArgumentList @('run', 'dev')

Write-Host ""
Write-Host "reo is starting up."
Write-Host "  On this PC:   http://localhost:5173"
Write-Host "  On your phone (same Wi-Fi): http://<this-pc-LAN-IP>:5173"
Write-Host ""
Write-Host "Press Ctrl+C to stop, then close the two spawned windows."
Wait-Process -Id $backend.Id, $frontend.Id
