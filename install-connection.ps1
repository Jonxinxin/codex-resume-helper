$ErrorActionPreference = 'Stop'
$resumeDirectory = Join-Path $env:LOCALAPPDATA 'CodexResumeHelper'
[IO.Directory]::CreateDirectory($resumeDirectory) | Out-Null
$resumeHook = Join-Path $resumeDirectory 'register-runtime.ps1'
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'register-runtime.ps1') -Destination $resumeHook -Force
$resumeDocuments = [Environment]::GetFolderPath('MyDocuments')
$resumeProfiles = @(
    (Join-Path $resumeDocuments 'PowerShell\profile.ps1'),
    (Join-Path $resumeDocuments 'WindowsPowerShell\profile.ps1')
)
$resumeBlock = @'
# BEGIN CodexResumeHelper connection registration
if ($env:TERM_PROGRAM -eq 'pebrel' -and $env:PEBREL_RUNTIME_ENDPOINT) {
    $resumeRegistration = Join-Path $env:LOCALAPPDATA 'CodexResumeHelper\register-runtime.ps1'
    if (Test-Path -LiteralPath $resumeRegistration) { & $resumeRegistration }
    Remove-Variable resumeRegistration -ErrorAction SilentlyContinue
}
# END CodexResumeHelper connection registration
'@
foreach ($resumeProfile in $resumeProfiles) {
    [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($resumeProfile)) | Out-Null
    $resumeContent = if (Test-Path -LiteralPath $resumeProfile) { [IO.File]::ReadAllText($resumeProfile) } else { '' }
    if ($resumeContent.Contains('# BEGIN CodexResumeHelper connection registration')) { continue }
    if (Test-Path -LiteralPath $resumeProfile) {
        $resumeBackup = $resumeProfile + '.before-codex-resume-' + [DateTime]::Now.ToString('yyyyMMdd-HHmmss') + '.bak'
        Copy-Item -LiteralPath $resumeProfile -Destination $resumeBackup
    }
    # A BOM keeps an existing Chinese profile readable in Windows PowerShell 5.1.
    [IO.File]::WriteAllText($resumeProfile, $resumeContent + [Environment]::NewLine + $resumeBlock + [Environment]::NewLine,
        [Text.UTF8Encoding]::new($true))
    Write-Output "Installed Pebrel connection registration: $resumeProfile"
}
& $resumeHook
Write-Output 'Connection registration installed. No execution-policy or Codex configuration changes.'
