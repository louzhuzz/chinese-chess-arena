param([switch]$Dev, [switch]$SkipInstall)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectRoot

if (-not (Test-Path "$projectRoot\.venv")) { python -m venv "$projectRoot\.venv" }
if (-not $SkipInstall) { & "$projectRoot\.venv\Scripts\python.exe" -m pip install -q -r "$projectRoot\requirements.txt" }

if (-not (Test-Path "$projectRoot\config\models.yaml")) {
  Copy-Item "$projectRoot\config\models.example.yaml" "$projectRoot\config\models.yaml"
  Write-Host '已创建 config\models.yaml（含本地模拟预设，可直接试跑）'
}

Push-Location "$projectRoot\frontend"
try {
  if (-not (Test-Path node_modules)) { npm install }
  if (-not $Dev) { npm run build }
} finally { Pop-Location }

if ($Dev) {
  Start-Process npm -ArgumentList 'run','dev' -WorkingDirectory "$projectRoot\frontend" -WindowStyle Hidden
}
& "$projectRoot\.venv\Scripts\python.exe" -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000 $(if ($Dev) {'--reload'})
