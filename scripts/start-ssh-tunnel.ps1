param(
    [string]$IdentityFile = (Join-Path $HOME ".ssh\coderus_tunnel_ed25519"),
    [string]$SshUser = "coderus",
    [string]$SshHost = "127.0.0.1",
    [int]$SshPort = 2222,
    [int]$LocalPort = 18082,
    [int]$RemotePort = 18082
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $IdentityFile)) {
    throw "SSH identity file not found: $IdentityFile"
}

while ($true) {
    & ssh.exe -N -T `
        -L "127.0.0.1:${LocalPort}:127.0.0.1:${RemotePort}" `
        -i $IdentityFile `
        -p $SshPort `
        -o BatchMode=yes `
        -o ExitOnForwardFailure=yes `
        -o ServerAliveInterval=30 `
        -o ServerAliveCountMax=3 `
        -o StrictHostKeyChecking=accept-new `
        "${SshUser}@${SshHost}"
    Start-Sleep -Seconds 5
}
