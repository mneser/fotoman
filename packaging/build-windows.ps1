# Build Fotoman.exe (onedir) on Windows. Run from the repo root:
#   powershell -ExecutionPolicy Bypass -File packaging\build-windows.ps1
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

python -m pip install -r requirements.txt
python -m pip install pyinstaller
python -m PyInstaller --noconfirm --distpath dist --workpath build packaging\fotoman.spec

Write-Host "Built dist\Fotoman\Fotoman.exe"
Write-Host "Library data (db, thumbs, models) goes to %LOCALAPPDATA%\Fotoman"
