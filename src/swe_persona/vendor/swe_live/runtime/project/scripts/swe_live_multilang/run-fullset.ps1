param(
    [switch]$Run,
    [ValidateSet('all', 'calibrate', 'final', 'report')][string]$Stage = 'all',
    [string]$StudyDir = '',
    [string]$QualificationDir = '',
    [string]$AdmissionDir = '',
    [ValidateSet('online', 'reference')][string]$TimingMode = 'online',
    [double]$InitialThresholdSeconds = 900,
    [double]$OnlineMaxSeconds = 3600,
    [int]$UpdateEvery = 20,
    [double]$ReferenceSafetySeconds = 600,
    [double]$ReferenceMaxSeconds = 7200,
    [switch]$NoTimePrefilter,
    [switch]$KeepDownloadedImages
)
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$python = Join-Path $repo '.local/venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Project virtual environment is missing. See README.md.' }
$arguments = @('-u', (Join-Path $PSScriptRoot 'reference_v2/run_fullset.py'),
    '--stage', $Stage, '--reference-safety-seconds', $ReferenceSafetySeconds.ToString([Globalization.CultureInfo]::InvariantCulture),
    '--reference-max-seconds', $ReferenceMaxSeconds.ToString([Globalization.CultureInfo]::InvariantCulture),
    '--timing-mode', $TimingMode, '--initial-threshold-seconds', $InitialThresholdSeconds.ToString([Globalization.CultureInfo]::InvariantCulture),
    '--online-max-seconds', $OnlineMaxSeconds.ToString([Globalization.CultureInfo]::InvariantCulture), '--update-every', $UpdateEvery)
if ($StudyDir) { $arguments += @('--study-dir', $StudyDir) }
if ($QualificationDir) { $arguments += @('--qualification-dir', $QualificationDir) }
if ($AdmissionDir) { $arguments += @('--admission-dir', $AdmissionDir) }
if ($Run) { $arguments += '--run' }
if ($NoTimePrefilter) { $arguments += '--no-time-prefilter' }
if ($KeepDownloadedImages) { $arguments += '--keep-downloaded-images' }
& $python @arguments
exit $LASTEXITCODE
