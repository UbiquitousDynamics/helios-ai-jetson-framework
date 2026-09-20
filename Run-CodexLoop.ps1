<#
.SYNOPSIS
    Unattended Codex loop runner for Windows, with a sandbox write gate.

.DESCRIPTION
    Runs `codex exec` repeatedly against a plan file, on an isolated git branch,
    with the test suite as a gate between iterations.

    Codex is invoked by explicit path, never by name. Resolving it through PATH
    proved unreliable: winget installs the binary as
    codex-x86_64-pc-windows-msvc.exe and only sometimes creates the `codex`
    alias, a shell inherits the PATH of whatever process launched it rather
    than the persisted one, and Task Scheduler may not see the user PATH at
    all. -CodexExe removes all of that.

    Before doing any real work the script verifies that the sandbox can
    actually write. On native Windows there is an open issue
    (openai/codex#34958) where `codex exec --sandbox workspace-write` starts
    normally and exits 0 while model-generated writes inside the working
    directory are rejected as read-only. An overnight run that "succeeds"
    without writing anything is the worst outcome: you lose the quota window
    and only find out in the morning. So: prove writes work, or refuse to
    start.

.EXAMPLE
    .\Run-CodexLoop.ps1 -MaxIterations 1

.EXAMPLE
    .\Run-CodexLoop.ps1 -MaxIterations 12 -MaxMinutes 300

.EXAMPLE
    .\Run-CodexLoop.ps1 -CodexExe "C:\path\to\codex-x86_64-pc-windows-msvc.exe"

.NOTES
    Verify the flags against your installed CLI version before the first
    unattended run; the CLI moves fast and flag names have changed between
    releases.
#>

[CmdletBinding()]
param(
    # Explicit path to the Codex executable. Leave empty to auto-detect.
    [string] $CodexExe = "",

    # Repository to work in.
    [string] $RepoPath = "C:\Users\Mimo\Documents\Ubiquitous_Dynamics\Emilia_PG\helios-ai-jetson-framework",

    # Plan file the agent works through, one step per iteration.
    [string] $Plan = "PLAN.md",

    # Safety valve. Each iteration consumes quota; do not leave this unbounded.
    [int] $MaxIterations = 10,

    # Stop cleanly before this many minutes have elapsed, so a run started at
    # night is not still holding the quota window in the morning.
    [int] $MaxMinutes = 240,

    # Branch to isolate the work on. Never run this on main.
    [string] $BranchPrefix = "codex/auto",

    # Command that must pass between iterations. Empty string disables the gate
    # (not recommended).
    [string] $TestCommand = "python -m pytest -q",

    # Sandbox policy passed to codex exec.
    [string] $Sandbox = "workspace-write",

    # Skip the sandbox write probe. Only for debugging the script itself.
    [switch] $SkipWriteProbe
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$stamp    = Get-Date -Format "yyyyMMdd-HHmmss"
$logDir   = Join-Path $RepoPath ".codex-runs"
$logFile  = Join-Path $logDir "run-$stamp.jsonl"
$summary  = Join-Path $logDir "run-$stamp.log"
$branch   = "$BranchPrefix/$stamp"
$deadline = (Get-Date).AddMinutes($MaxMinutes)

function Write-Log {
    param([string] $Message, [string] $Level = "INFO")
    $line = "[{0}] {1,-5} {2}" -f (Get-Date -Format "HH:mm:ss"), $Level, $Message
    Write-Host $line
    if (Test-Path $summary) { Add-Content -Path $summary -Value $line -Encoding utf8 }
    else                    { Set-Content -Path $summary -Value $line -Encoding utf8 }
}

function Resolve-CodexExecutable {
    <#
        Tries, in order: the caller's -CodexExe, the known winget portable
        layout, an npm shim, then anything named codex already on PATH.
        Returns a full path, or $null if nothing was found.
    #>
    param([string] $Explicit)

    if ($Explicit) {
        if (Test-Path $Explicit) { return (Resolve-Path $Explicit).Path }
        throw "Codex executable not found at the path given with -CodexExe: $Explicit"
    }

    $candidates = @(
        "$env:LOCALAPPDATA\Microsoft\WinGet\Packages\OpenAI.Codex_Microsoft.Winget.Source_8wekyb3d8bbwe\codex-x86_64-pc-windows-msvc.exe",
        "$env:LOCALAPPDATA\Microsoft\WinGet\Links\codex.exe",
        "$env:LOCALAPPDATA\Microsoft\WinGet\Links\codex.cmd",
        "$env:APPDATA\npm\codex.cmd",
        "$env:LOCALAPPDATA\Programs\codex\codex.exe"
    )
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path $candidate)) { return $candidate }
    }

    $onPath = Get-Command codex -ErrorAction SilentlyContinue
    if ($onPath) { return $onPath.Source }

    return $null
}

function Invoke-Codex {
    <#
        Single place where Codex is launched, so the executable path cannot
        drift between the probe and the loop.

        ErrorActionPreference is relaxed for the duration of the call. With
        "Stop" in effect, `2>&1` turns every stderr line from a native command
        into a terminating ErrorRecord -- and Codex writes progress and
        warnings to stderr as a matter of course, so the script would abort on
        output that is not an error at all. The exit code is what decides
        success here, not the presence of stderr.
    #>
    param([string[]] $CodexArgs)

    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $script:CodexExe @CodexArgs 2>&1
    }
    finally {
        $ErrorActionPreference = $previous
    }
}

function Invoke-Gate {
    <#
        Runs the test command with the same stderr relaxation, and returns its
        exit code. pytest and ruff both write to stderr on occasion.
    #>
    param([string] $Command)

    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        Invoke-Expression $Command 2>&1 | Out-Null
        return $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previous
    }
}

function Restore-StartBranch {
    param([string] $Original, [string] $Created)
    git switch $Original 2>&1 | Out-Null
    git branch -D $Created 2>&1 | Out-Null
}

# --------------------------------------------------------------------------
# Preflight
# --------------------------------------------------------------------------

if (-not (Test-Path $RepoPath)) { throw "Repository not found: $RepoPath" }
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
Set-Location $RepoPath

$CodexExe = Resolve-CodexExecutable -Explicit $CodexExe
if (-not $CodexExe) {
    throw @"
Could not locate the Codex executable.

Find it with:
  Get-ChildItem "`$env:LOCALAPPDATA\Microsoft\WinGet\Packages" -Recurse -Filter "codex*.exe"

Then pass it explicitly:
  .\Run-CodexLoop.ps1 -CodexExe "C:\full\path\to\codex-x86_64-pc-windows-msvc.exe"
"@
}

$versionOutput = (Invoke-Codex @("--version")) -join " "
Write-Log "codex exe     : $CodexExe"
Write-Log "codex version : $versionOutput"
Write-Log "repo          : $RepoPath"
Write-Log "branch        : $branch"
Write-Log "sandbox       : $Sandbox"
Write-Log "log           : $logFile"

# Refuse to start on a dirty tree. Otherwise you cannot tell the agent's
# changes from your own when reviewing in the morning. Note that
# `git status --porcelain` also reports untracked files.
$dirty = git status --porcelain
if ($dirty) {
    throw "Working tree is not clean. Commit or stash first:`n$dirty"
}

$startBranch = (git rev-parse --abbrev-ref HEAD).Trim()
if ($startBranch -in @("main", "master")) {
    Write-Log "Currently on '$startBranch'; the loop will branch off it." "WARN"
}

$planPath = Join-Path $RepoPath $Plan
if (-not (Test-Path $planPath)) { throw "Plan file not found: $planPath" }

git switch -c $branch | Out-Null
Write-Log "Created isolated branch from '$startBranch'."

# --------------------------------------------------------------------------
# Sandbox write probe  (openai/codex#34958)
# --------------------------------------------------------------------------

if (-not $SkipWriteProbe) {
    Write-Log "Probing whether the sandbox can actually write..."
    $probeName   = ".codex-write-probe-$stamp.txt"
    $probePath   = Join-Path $RepoPath $probeName
    $probePrompt = "Create a file named $probeName in the current working " +
                   "directory containing exactly the word OK. Do nothing else."

    $probeOut  = Invoke-Codex @("exec", "--sandbox", $Sandbox, $probePrompt)
    $probeExit = $LASTEXITCODE

    if (Test-Path $probePath) {
        Remove-Item $probePath -Force
        Write-Log "Write probe passed (exit $probeExit)."
    }
    else {
        Write-Log "Write probe FAILED: codex exited $probeExit but wrote nothing." "ERROR"
        Write-Log "This is the openai/codex#34958 failure mode on native Windows." "ERROR"
        Write-Log "Try, in order:" "ERROR"
        Write-Log "  1. Run the sandbox setup tool shipped alongside the CLI:" "ERROR"
        Write-Log "     codex-windows-sandbox-setup.exe (same folder as the codex binary)" "ERROR"
        Write-Log "  2. In ~\.codex\config.toml set:" "ERROR"
        Write-Log "       [features]" "ERROR"
        Write-Log "       experimental_windows_sandbox = false" "ERROR"
        Write-Log "  3. Run the loop inside WSL instead." "ERROR"
        Write-Log "Aborting rather than burning a quota window on a run that cannot write." "ERROR"
        Write-Log "codex output was:`n$($probeOut -join "`n")" "ERROR"
        Restore-StartBranch -Original $startBranch -Created $branch
        exit 2
    }
}

# --------------------------------------------------------------------------
# Baseline test run
# --------------------------------------------------------------------------

if ($TestCommand) {
    Write-Log "Establishing test baseline: $TestCommand"
    $gateExit = Invoke-Gate -Command $TestCommand
    if ($gateExit -ne 0) {
        Write-Log "Baseline tests already fail. Fix that before starting a loop." "ERROR"
        Restore-StartBranch -Original $startBranch -Created $branch
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

    # --output-last-message isolates the agent's final message. Matching the
    # markers against the full JSONL stream would be wrong: that stream echoes
    # the prompt, and the prompt names all three markers, so PLAN_COMPLETE
    # would match on the first iteration every time.
    $lastMessageFile = Join-Path $logDir "last-message-$stamp-$iteration.txt"

    $output = Invoke-Codex @(
        "exec",
        "--json",
        "--sandbox", $Sandbox,
        "-C", $RepoPath,
        "-o", $lastMessageFile,
        $prompt
    )
    $exit = $LASTEXITCODE
    Add-Content -Path $logFile -Value $output -Encoding utf8

    if ($exit -ne 0) {
        Write-Log "codex exec exited $exit; stopping." "ERROR"
        break
    }

    if (-not (Test-Path $lastMessageFile)) {
        Write-Log "No final message written; cannot tell what happened. Stopping." "WARN"
        break
    }
    $text = (Get-Content $lastMessageFile -Raw)

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
        $gateExit = Invoke-Gate -Command $TestCommand
        if ($gateExit -ne 0) {
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
