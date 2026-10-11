$ErrorActionPreference = 'Stop'
$parseTokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile((Join-Path $PSScriptRoot 'run-roleplay-fullset.ps1'), [ref]$parseTokens, [ref]$parseErrors)
if ($parseErrors.Count -gt 0) { throw ($parseErrors | Out-String) }
$definition = $ast.Find({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Invoke-BenchmarkCollector' }, $true)
# Load only the exact production recovery function, without running the launcher.
. ([scriptblock]::Create($definition.Extent.Text))

$state = @{ calls = 0; repairs = 0 }
Invoke-BenchmarkCollector -RunCollector {
    $state.calls++
    if ($state.calls -le 5) { return 1 }
    return 0
} -RepairCleanup {
    $state.repairs++
    return [PSCustomObject]@{ recovered = 1; pairs = @('sample-' + $state.repairs) }
}
if ($state.calls -ne 6 -or $state.repairs -ne 5) { throw 'Distinct recovered samples stopped at a lifetime limit' }

$state = @{ calls = 0; repairs = 0 }
$stopped = $false
try {
    Invoke-BenchmarkCollector -RunCollector { $state.calls++; return 1 } -RepairCleanup {
        $state.repairs++; return [PSCustomObject]@{ recovered = 1; pairs = @('same-sample') }
    }
} catch {
    if ($_.Exception.Message -notmatch 'same sample') { throw }
    $stopped = $true
}
if (-not $stopped -or $state.calls -ne 2) { throw 'No-progress recovery must stop' }

$state = @{ calls = 0 }
$stopped = $false
try {
    Invoke-BenchmarkCollector -RunCollector { $state.calls++; return 7 } -RepairCleanup {
        return [PSCustomObject]@{ recovered = 0; pairs = @() }
    }
} catch {
    if ($_.Exception.Message -notmatch 'exit code 7') { throw }
    $stopped = $true
}
if (-not $stopped -or $state.calls -ne 1) { throw 'Non-cleanup failures must not restart' }

$state = @{ calls = 0 }
$stopped = $false
try {
    Invoke-BenchmarkCollector -RunCollector { $state.calls++; return 1 } -RepairCleanup { throw 'containers remain' }
} catch {
    if ($_.Exception.Message -notmatch 'containers remain') { throw }
    $stopped = $true
}
if (-not $stopped -or $state.calls -ne 1) { throw 'Failed cleanup must not restart' }
Write-Output 'PASS: five separate recoveries continue; repeated sample, unrelated failure and unresolved cleanup stop.'
