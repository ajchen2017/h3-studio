# Restarts the WireGuard tunnel service if its handshake with the VPS
# peer has gone stale (NAT rebinding on this LAN can silently break the
# UDP mapping even with PersistentKeepalive configured - the fix is just
# forcing a fresh handshake).
$wg = "C:\Program Files\WireGuard\wg.exe"
$wireguardExe = "C:\Program Files\WireGuard\wireguard.exe"
$permanentConf = "C:\Users\user\.wireguard\gpuhost.conf"
$maxStaleSeconds = 150

# The service stores the path of the .conf it was installed from; if that
# file disappears (2026-09 outage: it had been installed from a temp dir
# that got cleaned up) every start fails with "path not found", so
# Start-Service below can never recover it. Reinstall from the permanent copy.
$imagePath = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Services\WireGuardTunnel$gpuhost' -ErrorAction SilentlyContinue).ImagePath
if ($imagePath -match '/tunnelservice\s+"?([^"]+?)"?\s*$') {
    $currentConf = $Matches[1]
    if (-not (Test-Path $currentConf) -and (Test-Path $permanentConf)) {
        Write-Output "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') tunnel config $currentConf missing - reinstalling from $permanentConf"
        & $wireguardExe /uninstalltunnelservice gpuhost
        Start-Sleep -Seconds 3
        & $wireguardExe /installtunnelservice $permanentConf
        exit 0
    }
}

# A fully stopped service has no interface, so `wg show` below would just
# fail every run and nothing would ever bring the tunnel back.
$svc = Get-Service -Name 'WireGuardTunnel$gpuhost'
if ($svc.Status -ne 'Running') {
    Write-Output "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') tunnel service $($svc.Status) - starting"
    Start-Service -Name 'WireGuardTunnel$gpuhost'
    exit 0
}

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
