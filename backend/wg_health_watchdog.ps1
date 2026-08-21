# Restarts the WireGuard tunnel service if its handshake with the VPS
# peer has gone stale (NAT rebinding on this LAN can silently break the
# UDP mapping even with PersistentKeepalive configured - the fix is just
# forcing a fresh handshake).
$wg = "C:\Program Files\WireGuard\wg.exe"
$maxStaleSeconds = 150

$output = & $wg show gpuhost latest-handshakes 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Output "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') wg show failed: $output"
    exit 1
}

$parts = $output -split "\s+"
$lastHandshake = [int]$parts[1]
$now = [int][double]::Parse((Get-Date -UFormat %s))
$age = $now - $lastHandshake

if ($lastHandshake -eq 0 -or $age -gt $maxStaleSeconds) {
    Write-Output "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') handshake stale (${age}s) - restarting tunnel service"
    Restart-Service -Name 'WireGuardTunnel$gpuhost' -Force
} else {
    Write-Output "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') handshake fresh (${age}s ago), ok"
}
