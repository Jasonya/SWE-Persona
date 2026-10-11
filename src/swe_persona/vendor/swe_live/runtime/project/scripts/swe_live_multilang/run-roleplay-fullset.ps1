param(
    [switch]$Run,
    [switch]$CheckOnly,
    [string]$StudyDir = '',
    [ValidateSet('RandomPersona', 'LegacyPairs')][string]$Mode = 'RandomPersona',
    [ValidateRange(5, 600)][int]$DockerWaitSeconds = 120
)
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
Set-Location $repo
if (-not $StudyDir) {
    $StudyDir = if ($Mode -eq 'LegacyPairs') { '.local\multilang-roleplay\fullset-once-v3' } else { '.local\multilang-roleplay\task-rotation-tb3-v1' }
}
$dockerBin = 'C:\Program Files\Docker\Docker\resources\bin'
if (Test-Path $dockerBin) { $env:PATH = "$dockerBin;$env:PATH" }
$python = Join-Path $repo '.local\venv\Scripts\python.exe'
if (-not (Test-Path $python)) { throw 'Missing local Python environment' }
# Use the same Python SDK and environment as the collector. A CLI context can
# point at a different daemon, so a successful `docker info` alone is not enough.
$dockerProbe = @'
import json
import docker
client = None
try:
    client = docker.from_env(timeout=4)
    info = client.info()
    result = {'ready': True, 'os': info['OSType'], 'memory_gib': info['MemTotal'] / 1024**3}
except Exception as error:
    result = {'ready': False, 'error_type': type(error).__name__}
finally:
    if client is not None:
        client.close()
print(json.dumps(result))
'@
function Get-BenchmarkDockerStatus {
    $probeText = (& $python -c $dockerProbe) -join [Environment]::NewLine
    if ($LASTEXITCODE -ne 0) { throw 'The local Docker SDK check failed. Check the Python environment.' }
    return ($probeText | ConvertFrom-Json)
}
if ($Run -or $CheckOnly) {
    $dockerStatus = Get-BenchmarkDockerStatus
    if (-not $dockerStatus.ready) {
        $desktopExe = 'C:\Program Files\Docker\Docker\Docker Desktop.exe'
        if (-not (Test-Path -LiteralPath $desktopExe)) {
            throw 'Docker engine unavailable and Docker Desktop was not found. Start Docker Desktop, then rerun this command.'
        }
        if (-not (Get-Process -Name 'Docker Desktop' -ErrorAction SilentlyContinue)) {
            Write-Host 'Starting Docker Desktop...'
            Start-Process -FilePath $desktopExe -WindowStyle Hidden
        }
        Write-Host "Waiting up to $DockerWaitSeconds seconds for the Docker engine..."
        $dockerDeadline = [DateTime]::UtcNow.AddSeconds($DockerWaitSeconds)
        $nextNotice = [DateTime]::UtcNow.AddSeconds(15)
        while (-not $dockerStatus.ready -and [DateTime]::UtcNow -lt $dockerDeadline) {
            Start-Sleep -Seconds 3
            $dockerStatus = Get-BenchmarkDockerStatus
            if (-not $dockerStatus.ready -and [DateTime]::UtcNow -ge $nextNotice) {
                Write-Host 'Docker is still starting; no benchmark or model calls have started.'
                $nextNotice = [DateTime]::UtcNow.AddSeconds(15)
            }
        }
        if (-not $dockerStatus.ready) {
            throw 'Docker did not become ready. Open Docker Desktop and resolve its startup message, then rerun. Existing results are preserved.'
        }
    }
    if ($dockerStatus.os -ne 'linux') { throw 'Switch Docker Desktop to Linux containers, then rerun.' }
    if ($dockerStatus.memory_gib -lt 18) { throw 'Docker needs at least 18 GiB of memory for this benchmark.' }
    Write-Host ('Docker ready: {0}; RAM {1:N1} GiB' -f $dockerStatus.os, $dockerStatus.memory_gib)
}
$collectorPath = if ($Mode -eq 'LegacyPairs') { 'reference_v2\collection\run_once.py' } else { 'tb3_adapter\run_batch.py' }
$arguments = @('-u', (Join-Path $PSScriptRoot $collectorPath), '--study-dir', $StudyDir)
function Repair-BenchmarkCleanup {
    if ($Mode -ne 'LegacyPairs') { return [pscustomobject]@{ recovered = 0; pairs = @() } }
    $repairScript = Join-Path $PSScriptRoot 'recover_sample_cleanup.py'
    $repairText = (& $python $repairScript --study-dir $StudyDir) -join [Environment]::NewLine
    if ($LASTEXITCODE -ne 0) { throw "Docker cleanup is still unresolved. Existing results are preserved. $repairText" }
    $repair = $repairText | ConvertFrom-Json
    if ($repair.recovered -gt 0) {
        Write-Host ('Recovered cleanup for {0} sample(s); their outcomes are retained and will not be rerun.' -f $repair.recovered)
    }
    return $repair
}
function Invoke-BenchmarkCollector {
    param([scriptblock]$RunCollector, [scriptblock]$RepairCleanup, [string[]]$PreviouslyRecovered = @())
    $recoveredPairs = New-Object 'System.Collections.Generic.HashSet[string]'
    foreach ($pair in $PreviouslyRecovered) { $null = $recoveredPairs.Add($pair) }
    while ($true) {
        $collectorExit = & $RunCollector
        if ($collectorExit -eq 0) { return }
        $repair = & $RepairCleanup
        if ($repair.recovered -eq 0) { throw "Collection stopped with exit code $collectorExit. Inspect the last error; existing samples are preserved." }
        if ($repair.recovered -ne @($repair.pairs).Count) { throw 'Cleanup recovery returned inconsistent progress; stopping.' }
        foreach ($pair in $repair.pairs) {
            if (-not $recoveredPairs.Add([string]$pair)) {
                throw "The same sample requires cleanup recovery again ($pair); stopping without retrying it."
            }
        }
        # Each recovery is individually bounded by the Python helper. Distinct
        # handled samples are progress, not repeated failure of one recovery.
        Write-Host 'Docker cleanup verified. Resuming at the next unprocessed pair...'
    }
}
if ($Run -and -not $CheckOnly) {
    $arguments += '--run'
    $initialRepair = Repair-BenchmarkCleanup
    Invoke-BenchmarkCollector -PreviouslyRecovered @($initialRepair.pairs) -RunCollector {
        & $python @arguments | Out-Host
        return $LASTEXITCODE
    } -RepairCleanup { Repair-BenchmarkCleanup }
} else {
    & $python @arguments
    if ($LASTEXITCODE -ne 0) { throw "Collection stopped with exit code $LASTEXITCODE. Inspect the last error; existing samples are preserved." }
}
