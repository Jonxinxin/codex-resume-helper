param(
    [ValidatePattern('^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$')]
    [string]$Version = '1.0.2',
    [string]$Python = 'python'
)

$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    & $Python -c "import sys, struct; assert sys.platform == 'win32' and struct.calcsize('P') == 8, 'Build requires Windows x64 Python'"
    if ($LASTEXITCODE -ne 0) { throw 'Build requires Windows x64 Python.' }

    & $Python -m unittest -v test_helper test_ui test_connection
    if ($LASTEXITCODE -ne 0) { throw 'Tests failed; package was not built.' }

    & $Python -m PyInstaller --noconfirm --clean --distpath dist --workpath build 'Codex续接助手.spec'
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed.' }

    $releaseDirectory = Join-Path $PSScriptRoot 'release'
    [IO.Directory]::CreateDirectory($releaseDirectory) | Out-Null
    $packageName = "CodexResumeHelper-v$Version-windows-x64"
    $stagingDirectory = Join-Path $releaseDirectory ($packageName + '-stage-' + [Guid]::NewGuid().ToString('N'))
    [IO.Directory]::CreateDirectory($stagingDirectory) | Out-Null
    foreach ($file in @('dist\Codex续接助手.exe', 'install-connection.ps1', 'register-runtime.ps1',
                         '使用说明.md', 'README.md', 'CHANGELOG.md', '界面预览.png', '限流提示预览.png')) {
        Copy-Item -LiteralPath (Join-Path $PSScriptRoot $file) -Destination $stagingDirectory
    }
    $archive = Join-Path $releaseDirectory ($packageName + '.zip')
    Compress-Archive -Path (Join-Path $stagingDirectory '*') -DestinationPath $archive -Force
    $hash = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
    $checksum = Join-Path $releaseDirectory 'SHA256SUMS.txt'
    [IO.File]::WriteAllText($checksum, "$hash  $packageName.zip`n", [Text.UTF8Encoding]::new($false))
    Write-Output "Release archive: $archive"
    Write-Output "SHA-256: $hash"
} finally {
    Pop-Location
}
