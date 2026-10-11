param([switch]$CheckOnly)
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
$python = Join-Path $projectRoot '.local/venv/Scripts/python.exe'
$upstream = Join-Path $projectRoot '.local/SWE-bench-Live'
$task = Join-Path $projectRoot '.local/multilang-smoke/task.private.jsonl'
if (-not (Test-Path -LiteralPath $python)) { throw 'Missing .local/venv. See docs/swe-live-multilang-pc.md.' }
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
# A running terminal keeps its old PATH after Docker Desktop is installed.
$oldPath = $env:PATH
$dockerBins = @(
    "$env:ProgramFiles\Docker\Docker\resources\bin",
    "$env:LOCALAPPDATA\Programs\DockerDesktop\resources\bin"
)
foreach ($bin in $dockerBins) {
    if (Test-Path -LiteralPath (Join-Path $bin 'docker-credential-desktop.exe')) {
        $env:PATH = "$bin;$env:PATH"
        break
    }
}
Push-Location $upstream
try {
    & $python -c "import sys; sys.path.insert(0, 'launch'); from launch.core.platforms.linux import LinuxRuntime; import docker; c=docker.from_env(); i=c.info(); print('Docker:',i['OSType'],'RAM GiB:',round(i['MemTotal']/2**30,1)); assert i['OSType']=='linux', 'Switch Docker Desktop to Linux containers'; c.close()"
    if ($LASTEXITCODE -ne 0) { throw 'Docker/import check failed. Start Docker Desktop and wait for the engine.' }
    if ($CheckOnly) { return }
    if (-not (Test-Path -LiteralPath $task)) { throw 'Run scripts/swe_live_multilang/prepare_smoke.py first.' }
    $manifestPath = Join-Path $projectRoot '.local/multilang-smoke/manifest.json'
    $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    if ((Get-FileHash -LiteralPath $task -Algorithm SHA256).Hash.ToLowerInvariant() -ne $manifest.task_sha256) {
        throw 'The task file has changed since preparation. Prepare it again before evaluation.'
    }
    $upstreamCommit = & git -C $upstream rev-parse HEAD
    if ($LASTEXITCODE -ne 0 -or $upstreamCommit -ne $manifest.upstream_commit) { throw 'Upstream commit differs from the manifest.' }
    $launchCommit = & git -C (Join-Path $upstream 'launch') rev-parse HEAD
    if ($LASTEXITCODE -ne 0 -or $launchCommit -ne $manifest.repolaunch_commit) { throw 'RepoLaunch commit differs from the manifest.' }
    $runId = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
    $output = Join-Path $projectRoot ".local/multilang-smoke/runs/$runId"
    & $python (Join-Path $PSScriptRoot 'run_evaluation.py') --dataset $task --platform linux --patch_dir gold --output_dir $output --workers 1 --overwrite 0
    if ($LASTEXITCODE -ne 0) { throw "Evaluator process failed; inspect $output" }
    $passedFile = Join-Path $output 'gold_patch_evaluated_instances.jsonl'
    if (-not (Test-Path -LiteralPath $passedFile)) { throw "No passing-task report; inspect $output" }
    $rows = @(Get-Content -LiteralPath $passedFile | Where-Object { $_.Trim() })
    if ($rows.Count -ne 1) { throw "Gold task did not pass; inspect $output" }
    Write-Output "Gold environment smoke test passed. Results: $output"
} finally {
    Pop-Location
    $env:PATH = $oldPath
}
