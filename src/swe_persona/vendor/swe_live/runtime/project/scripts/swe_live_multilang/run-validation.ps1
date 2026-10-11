param([switch]$Run)
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$python = Join-Path $repo '.local/venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Project virtual environment is missing. See README.md.' }
$arguments = @('-u', (Join-Path $PSScriptRoot 'reference_v2/run_study.py'),
    '--study-dir', (Join-Path $repo '.local/multilang-study/validation-5-v5'),
    '--from-study', (Join-Path $repo '.local/multilang-study/study-20-v2'),
    '--task-keys', '03-c', '04-cpp', '12-js', '15-java', '20-cs',
    '--interaction-seconds', '1800')
if ($Run) { $arguments += '--run' }
& $python @arguments
exit $LASTEXITCODE
