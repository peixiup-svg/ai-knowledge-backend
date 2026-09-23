param(
    [string]$Python = "python"
)
$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Push-Location -LiteralPath $projectRoot
try {
    $venvPython = Join-Path $projectRoot ".venv/Scripts/python.exe"
    if (-not (Test-Path -LiteralPath $venvPython)) {
        & $Python -c "import sys; assert (3, 11) <= sys.version_info < (3, 15), 'Python 3.11-3.14 required'"
        if ($LASTEXITCODE -ne 0) { throw "Python version check failed." }
        & $Python -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw "Failed to create .venv." }
    }
    & $venvPython -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw "pip update failed; check network access." }
    & $venvPython -m pip install -c constraints.txt -e ".[dev]"
    if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed." }
    if (-not (Test-Path -LiteralPath ".env")) {
        Copy-Item -LiteralPath ".env.example" -Destination ".env"
        & $venvPython -c "from pathlib import Path; import secrets; p=Path('.env'); s=p.read_text(encoding='utf-8'); p.write_text(s.replace('local-demo-only-change-before-deployment-32chars', secrets.token_urlsafe(48)), encoding='utf-8')"
        if ($LASTEXITCODE -ne 0) { throw "Failed to generate local JWT secret." }
        Write-Host "Created .env with offline local defaults. Existing .env files are never overwritten."
    }
    New-Item -ItemType Directory -Force -Path "data", "reports" | Out-Null
    & $venvPython -m app.manage init-db
    if ($LASTEXITCODE -ne 0) { throw "Database initialization failed; check .env." }
    Write-Host "Setup complete. Select .venv in VS Code, then press F5."
    Write-Host "Or run: .\.venv\Scripts\python.exe -m uvicorn app.main:app --reload"
}
finally {
    Pop-Location
}
