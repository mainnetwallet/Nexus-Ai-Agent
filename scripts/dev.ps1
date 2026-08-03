<#
    Nexus-Agent -- run backend + frontend with a single command (Windows).

    Usage (from the repo root):
        .\scripts\dev.ps1

    What it does:
      1. Starts the FastAPI backend (uvicorn) and the Vite frontend dev
         server as background jobs, both in THIS window (no second
         window is opened).
      2. Only frontend (Vite) output is printed to this console. Backend
         output is written to logs\backend.log instead, so the terminal
         stays focused on the frontend log.
      3. On Ctrl+C (or any exit), stops both jobs, so nothing is left
         running in the background.

    Assumes you've already completed the one-time setup from the README
    (a .venv with requirements.txt installed, and `npm install` run inside
    frontend/). This script only launches the two dev servers -- it does
    not install anything.
#>

$ErrorActionPreference = "Stop"

# Resolve the repo root as the parent of this script's folder, so the
# script works no matter where the caller's shell is currently located.
$RepoRoot = Split-Path -Parent $PSScriptRoot
$LogDir = Join-Path $RepoRoot "logs"
if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir | Out-Null
}
$BackendLog = Join-Path $LogDir "backend.log"

# Clear out any orphaned backend from a previous run (e.g. left behind by
# uvicorn --reload's child watcher process) before starting a fresh one.
Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -like "*uvicorn*backend.main:app*" } |
    ForEach-Object {
        Write-Host "Found leftover backend from a previous run (pid $($_.ProcessId)) -- stopping it first..." -ForegroundColor Yellow
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }

$BackendJob = Start-Job -ScriptBlock {
    param($RepoRoot)
    Set-Location $RepoRoot
    if (Test-Path '.venv\Scripts\Activate.ps1') {
        . '.venv\Scripts\Activate.ps1'
    }
    # NOTE: --reload is intentionally NOT used here. On Windows, uvicorn's
    # --reload worker subprocess forces WindowsSelectorEventLoopPolicy
    # (uvicorn/loops/asyncio.py), which breaks Playwright's
    # asyncio.create_subprocess_exec (raises NotImplementedError) since
    # SelectorEventLoop has no subprocess support. This happens before the
    # app even loads, so nothing in backend/main.py can undo it -- the only
    # fix is to not use --reload on Windows. Restart the script manually
    # after backend code changes.
    python -m uvicorn backend.main:app 2>&1
} -ArgumentList $RepoRoot

$FrontendJob = Start-Job -ScriptBlock {
    param($RepoRoot)
    Set-Location (Join-Path $RepoRoot "frontend")
    npm run dev 2>&1
} -ArgumentList $RepoRoot

function Cleanup {
    Write-Host ""
    Write-Host "Stopping backend + frontend..." -ForegroundColor Cyan
    Stop-Job $BackendJob, $FrontendJob -ErrorAction SilentlyContinue
    Remove-Job $BackendJob, $FrontendJob -Force -ErrorAction SilentlyContinue

    # uvicorn --reload spawns a child watcher process that Stop-Job does not
    # always reach, leaving an orphaned backend running in the background --
    # its Telegram poller then fights the next run with a Conflict error.
    # Force-kill any leftover uvicorn process for this project by command line.
    Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -like "*uvicorn*backend.main:app*" } |
        ForEach-Object {
            Write-Host "Killing leftover backend process (pid $($_.ProcessId))..." -ForegroundColor Yellow
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        }
}

Write-Host "Starting backend (uvicorn) + frontend (Vite)..." -ForegroundColor Cyan
Write-Host "Backend log is hidden here -- full output goes to logs\backend.log. Ctrl+C stops both." -ForegroundColor Cyan

try {
    while ($true) {
        # Backend output goes only to the log file, not this console.
        Receive-Job -Job $BackendJob | Out-File -FilePath $BackendLog -Append -Encoding utf8

        Receive-Job -Job $FrontendJob | ForEach-Object { Write-Host "[frontend] $_" }

        # If the backend crashes, say so once instead of staying silent.
        if ($BackendJob.State -in 'Completed', 'Failed', 'Stopped') {
            Write-Host "[backend] stopped -- check logs\backend.log for details" -ForegroundColor Yellow
            break
        }

        if ($FrontendJob.State -in 'Completed', 'Failed', 'Stopped') {
            Write-Host "Frontend process exited." -ForegroundColor Yellow
            break
        }

        Start-Sleep -Milliseconds 300
    }
}
finally {
    Cleanup
}
