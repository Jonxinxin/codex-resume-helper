# Called only by a Pebrel PowerShell session. The endpoint includes a local
# credential, so store it with Windows DPAPI instead of writing it as plaintext.
if (-not $env:PEBREL_RUNTIME_ENDPOINT -or $env:TERM_PROGRAM -ne 'pebrel') { return }
$resumeBytes = $null
$resumeTemp = $null
$resumeBackup = $null
try {
    $resumeCli = if ($env:PEBREL_CLI) { $env:PEBREL_CLI } else { $env:NEBULA_CLI }
    if (-not $resumeCli) { return }
    Add-Type -AssemblyName System.Security -ErrorAction Stop
    $resumeBytes = [Text.Encoding]::UTF8.GetBytes($env:PEBREL_RUNTIME_ENDPOINT)
    $resumeCipher = [Security.Cryptography.ProtectedData]::Protect(
        $resumeBytes, $null, [Security.Cryptography.DataProtectionScope]::CurrentUser)
    $resumeDirectory = Join-Path $env:LOCALAPPDATA 'CodexResumeHelper'
    [IO.Directory]::CreateDirectory($resumeDirectory) | Out-Null
    $resumeRecord = @{
        version = 1
        cli = $resumeCli
        protected_endpoint = [Convert]::ToBase64String($resumeCipher)
        registered_at = [DateTime]::UtcNow.ToString('o')
    } | ConvertTo-Json -Compress
    $resumeTarget = Join-Path $resumeDirectory 'connection.json'
    $resumeTemp = Join-Path $resumeDirectory ('connection-' + [Guid]::NewGuid().ToString('N') + '.tmp')
    [IO.File]::WriteAllText($resumeTemp, $resumeRecord, [Text.UTF8Encoding]::new($false))
    if ([IO.File]::Exists($resumeTarget)) {
        # PowerShell coerces $null to an empty path for this overload.
        $resumeBackup = $resumeTemp + '.bak'
        [IO.File]::Replace($resumeTemp, $resumeTarget, $resumeBackup)
    } else {
        [IO.File]::Move($resumeTemp, $resumeTarget)
    }
} catch {
    # Never disrupt the user's shell or print connection credentials.
} finally {
    if ($resumeBytes) { [Array]::Clear($resumeBytes, 0, $resumeBytes.Length) }
    foreach ($resumeCleanup in @($resumeTemp, $resumeBackup)) {
        if ($resumeCleanup -and [IO.File]::Exists($resumeCleanup)) { [IO.File]::Delete($resumeCleanup) }
    }
    Remove-Variable resumeCli, resumeBytes, resumeCipher, resumeDirectory, resumeRecord,
        resumeTarget, resumeTemp, resumeBackup, resumeCleanup -ErrorAction SilentlyContinue
}
