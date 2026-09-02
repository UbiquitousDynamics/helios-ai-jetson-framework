<#
.SYNOPSIS
    Unattended Codex loop runner for Windows, with a sandbox write gate.

.DESCRIPTION
    Runs `codex exec` repeatedly against a plan file, on an isolated git branch,
    with the test suite as a gate between iterations.

    The first thing it does is verify that the sandbox can actually write. On
    native Windows there is an open issue (openai/codex#34958) where
    `codex exec --sandbox workspace-write` starts normally and exits 0 while
    model-generated writes inside the working directory are rejected as
    read-only. An overnight run that "succeeds" without writing anything is the
    worst outcome: you lose the quota window and only find out in the morning.
    So: prove writes work, or refuse to start.

.EXAMPLE
    .\Run-CodexLoop.ps1 -Plan .\PLAN.md -MaxIterations 12

.NOTES
    Verify the flags against your installed Codex CLI version (`codex --help`);
    the CLI moves fast and flag names have changed between releases.
#>

[CmdletBinding()]
param(
    # Repository to work in.
    [string] $RepoPath = "C:\Users\Mimo\Documents\Ubiquitous_Dynamics\Emilia_PG\helios-ai-jetson-framework",

    # Plan file the agent works through, one step per iteration.
    [string] $Plan = "PLAN.md",

    # Safety valve. Each iteration consumes quota; do not leave this unbounded.
    [int] $MaxIterations = 10,

    # Stop cleanly before this many minutes have elapsed, so a run started at
    # night does not still be holding the quota window in the morning.
    [int] $MaxMinutes = 240,

    # Branch to isolate the work on. Never run this on main.
    [string] $BranchPrefix = "codex/auto",

    # Command that must pass between iterations. Empty string disables the gate
    # (not recommended).
    [string] $TestCommand = "python -m pytest -q",

    # Skip the sandbox write probe. Only for debugging the script itself.
    [switch] $SkipWriteProbe
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$stamp     = Get-Date -Format "yyyyMMdd-HHmmss"
$logDir    = Join-Path $RepoPath ".codex-runs"
$logFile   = Join-Path $logDir "run-$stamp.jsonl"
$summary   = Join-Path $logDir "run-$stamp.log"
$branch    = "$BranchPrefix/$stamp"
$deadline  = (Get-Date).AddMinutes($MaxMinutes)

function Write-Log {
    param([string] $Message, [string] $Level = "INFO")
    $line = "[{0}] {1,-5} {2}" -f (Get-Date -Format "HH:mm:ss"), $Level, $Message
    Write-Host $line
    Add-Content -Path $summary -Value $line -Encoding utf8
}

# --------------------------------------------------------------------------
# Preflight
# --------------------------------------------------------------------------

if (-not (Test-Path $RepoPath)) { throw "Repository not found: $RepoPath" }
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
Set-Location $RepoPath

if (-not (Get-Command codex -ErrorAction SilentlyContinue)) {
    throw "codex CLI not found on PATH."
}

Write-Log "codex version: $(codex --version 2>&1)"
Write-Log "repo:   $RepoPath"
Write-Log "branch: $branch"
Write-Log "log:    $logFile"

# Refuse to start on a dirty tree. Otherwise you cannot tell the agent's
# changes from your own when reviewing in the morning.
$dirty = git status --porcelain
if ($dirty) {
    throw "Working tree is not clean. Commit or stash first:`n$dirty"
}

$startBranch = (git rev-parse --abbrev-ref HEAD).Trim()
if ($startBranch -in @("main", "master")) {
    Write-Log "Currently on '$startBranch'; the loop will branch off it." "WARN"
}

git switch -c $branch | Out-Null
Write-Log "Created isolated branch from '$startBranch'."

$planPath = Join-Path $RepoPath $Plan
if (-not (Test-Path $planPath)) { throw "Plan file not found: $planPath" }

# --------------------------------------------------------------------------
# Sandbox write probe  (openai/codex#34958)
# --------------------------------------------------------------------------

if (-not $SkipWriteProbe) {
    Write-Log "Probing whether the sandbox can actually write..."
    $probeName = ".codex-write-probe-$stamp.txt"
    $probePath = Join-Path $RepoPath $probeName

    $probePrompt = "Create a file named $probeName in the current working " +
                   "directory containing exactly the word OK. Do nothing else."

    $probeOut = codex exec --sandbox workspace-write $probePrompt 2>&1
    $probeExit = $LASTEXITCODE

    if (Test-Path $probePath) {
        Remove-Item $probePath -Force
        Write-Log "Write probe passed (exit $probeExit)."
    }
    else {
        Write-Log "Write probe FAILED: codex exited $probeExit but wrote nothing." "ERROR"
        Write-Log "This is the openai/codex#34958 failure mode on native Windows." "ERROR"
        Write-Log "Try, in ~\.codex\config.toml:" "ERROR"
        Write-Log "  [features]" "ERROR"
        Write-Log "  experimental_windows_sandbox = false" "ERROR"
        Write-Log "or run the loop inside WSL instead. Aborting rather than" "ERROR"
        Write-Log "burning a quota window on a run that cannot write." "ERROR"
        Write-Log "codex output was:`n$probeOut" "ERROR"
        git switch $startBranch | Out-Null
        git branch -D $branch | Out-Null
        exit 2
    }
}

# --------------------------------------------------------------------------
# Baseline test run
# --------------------------------------------------------------------------

if ($TestCommand) {
    Write-Log "Establishing test baseline: $TestCommand"
    Invoke-Expression $TestCommand | Tee-Object -Variable baselineOut | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Log "Baseline tests already fail. Fix that before starting a loop." "ERROR"
        exit 3
    }
    Write-Log "Baseline is green."
}

# --------------------------------------------------------------------------
# The loop
# --------------------------------------------------------------------------

$iteration = 0
$completed = $false

while ($iteration -lt $MaxIterations -and (Get-Date) -lt $deadline) {
    $iteration++
    Write-Log "--- iteration $iteration / $MaxIterations ---"

    $prompt = @"
Read $Plan in the repository root. Work on the FIRST step that is not yet marked
done. Do only that one step.

When the step is complete:
  - run the test suite and make sure it passes
  - mark the step done in $Plan, with a one-line note on what you changed
  - commit with a message naming the step
  - print exactly PLAN_STEP_DONE on its own line

If every step in $Plan is already done, change nothing and print exactly
PLAN_COMPLETE on its own line.

If you are blocked and cannot complete the step, change nothing, explain why,
and print exactly PLAN_BLOCKED on its own line.

Follow AGENTS.md. Do not batch unrelated changes. Do not modify tests to make
something pass.
"@

    $output = codex exec --json --sandbox workspace-write $prompt 2>&1
    $exit = $LASTEXITCODE
    Add-Content -Path $logFile -Value $output -Encoding utf8

    if ($exit -ne 0) {
        Write-Log "codex exec exited $exit; stopping." "ERROR"
        break
    }

    $text = $output -join "`n"

    if ($text -match "PLAN_COMPLETE") {
        Write-Log "Plan reports complete."
        $completed = $true
        break
    }
    if ($text -match "PLAN_BLOCKED") {
        Write-Log "Agent reports it is blocked; stopping for human review." "WARN"
        break
    }
    if ($text -notmatch "PLAN_STEP_DONE") {
        Write-Log "No completion marker in output; stopping rather than looping blindly." "WARN"
        break
    }

    # Independent verification. Never trust the agent's own report that tests pass.
    if ($TestCommand) {
        Write-Log "Verifying: $TestCommand"
        Invoke-Expression $TestCommand | Out-Null
        if ($LASTEXITCODE -ne 0) {
            Write-Log "Tests FAIL after iteration $iteration. Stopping." "ERROR"
            Write-Log "Inspect with: git -C `"$RepoPath`" log --oneline $startBranch..$branch" "ERROR"
            break
        }
        Write-Log "Tests green."
    }

    $head = (git rev-parse --short HEAD).Trim()
    Write-Log "Iteration $iteration committed at $head."
}

# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------

if ((Get-Date) -ge $deadline) { Write-Log "Stopped: time budget reached." "WARN" }

$commits = git log --oneline "$startBranch..$branch" 2>$null
Write-Log "--- summary ---"
Write-Log "iterations run : $iteration"
Write-Log "plan complete  : $completed"
Write-Log "branch         : $branch"
Write-Log "commits:"
if ($commits) { $commits | ForEach-Object { Write-Log "  $_" } }
else          { Write-Log "  (none)" }
Write-Log "Review with: git -C `"$RepoPath`" diff $startBranch..$branch"

exit 0
