#!/usr/bin/env pwsh
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$tokens = $null
$errors = $null
$tree = [System.Management.Automation.Language.Parser]::ParseFile(
    (Join-Path $repo 'src/startup.ps1'), [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw 'Startup has PowerShell parse errors.' }
foreach ($name in 'ConvertTo-NativeArgumentString', 'Start-Service-Bg', 'Install-WindowsDependencies') {
    $definition = $tree.Find({ param($node)
        $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name
    }, $false)
    if (-not $definition) { throw "Missing startup helper: $name" }
    . ([scriptblock]::Create($definition.Extent.Text))
}

$temp = Join-Path ([IO.Path]::GetTempPath()) ('sutando startup 空间 ' + [guid]::NewGuid())
New-Item -ItemType Directory -Path $temp | Out-Null
try {
    $LOGS_DIR = $temp
    $node = Join-Path $temp 'node fixture.exe'
    Copy-Item (Get-Command node).Source $node
    $capture = Join-Path $temp 'capture arguments.mjs'
    Set-Content $capture 'process.stdout.write(JSON.stringify(process.argv.slice(2)))' -Encoding utf8
    $pwsh = (Get-Process -Id $PID).Path
    function Test-Port { return $false }
    function Start-Process {
        param($FilePath, $ArgumentList, $WindowStyle, $RedirectStandardOutput, $RedirectStandardError)
        if ($FilePath -eq 'pwsh.exe') { $FilePath = $pwsh }
        $process = Microsoft.PowerShell.Management\Start-Process -FilePath $FilePath -ArgumentList $ArgumentList `
            -RedirectStandardOutput $RedirectStandardOutput -RedirectStandardError $RedirectStandardError -Wait -PassThru
        if ($process.ExitCode -ne 0) { throw "Child failed: $(Get-Content $RedirectStandardError -Raw)" }
    }
    function Assert-Arguments($log, $expected) {
        $actual = @(Get-Content (Join-Path $temp $log) -Raw | ConvertFrom-Json)
        if (($actual | ConvertTo-Json -Compress) -cne ($expected | ConvertTo-Json -Compress)) {
            throw "Arguments changed: actual=$($actual | ConvertTo-Json -Compress) expected=$($expected | ConvertTo-Json -Compress)"
        }
    }
    $expected = @('path with spaces', '空间', '', 'trailing slash\', 'embedded"quote')
    Start-Service-Bg 'exe fixture' 0 $node (@($capture) + $expected) 'exe.log'
    Assert-Arguments 'exe.log' $expected

    $shim = Join-Path $temp 'fixture shim.ps1'
    Set-Content $shim ("& '" + $node.Replace("'", "''") + "' '" + $capture.Replace("'", "''") + "' @args") -Encoding utf8
    $expected = @('path with spaces', '空间', 'trailing slash\')
    Start-Service-Bg 'ps1 fixture' 0 $shim $expected 'ps1.log'
    Assert-Arguments 'ps1.log' $expected
    if ($IsWindows) {
        $cmdShim = Join-Path $temp 'fixture shim.cmd'
        $previousNode = $env:SUTANDO_TEST_NODE
        $previousCapture = $env:SUTANDO_TEST_CAPTURE
        try {
            $env:SUTANDO_TEST_NODE = (Get-Command node).Source
            $env:SUTANDO_TEST_CAPTURE = $capture
            Set-Content $cmdShim '@"%SUTANDO_TEST_NODE%" "%SUTANDO_TEST_CAPTURE%" %*' -Encoding ascii
            Start-Service-Bg 'cmd fixture' 0 $cmdShim $expected 'cmd.log'
            Assert-Arguments 'cmd.log' $expected
        } finally {
            $env:SUTANDO_TEST_NODE = $previousNode
            $env:SUTANDO_TEST_CAPTURE = $previousCapture
        }
    }

    & {
        function Test-Path {
            param($Path)
            if ([IO.Path]::GetFileName($Path) -eq 'node_modules') { return $case.installed }
            return $case.prebuilt
        }
        function npm {
            $calls.Add($args -join ' ')
            $global:LASTEXITCODE = $case.exitCode
        }
        $cases = @(
            @{ installed = $false; prebuilt = $true; exitCode = 0; fails = $false },
            @{ installed = $true; prebuilt = $true; exitCode = 0; fails = $false },
            @{ installed = $false; prebuilt = $true; exitCode = 1; fails = $true },
            @{ installed = $false; prebuilt = $false; exitCode = 0; fails = $true },
            @{ installed = $true; prebuilt = $false; exitCode = 0; fails = $true }
        )
        foreach ($case in $cases) {
            $calls = [Collections.Generic.List[string]]::new()
            $failed = $false
            try { Install-WindowsDependencies $temp } catch { $failed = $true }
            $wantedCalls = if ($case.installed) { '' } else { 'ci --ignore-scripts' }
            if ($failed -ne $case.fails -or ($calls -join ',') -ne $wantedCalls) {
                throw "Dependency installation policy failed: $($case | ConvertTo-Json -Compress)"
            }
        }
    }
    Write-Output 'Windows startup: native argv round trips and 5 installation cases passed.'
} finally {
    Remove-Item -Recurse -Force $temp
}
