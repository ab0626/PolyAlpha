# Harden the host power/network config for 24/7 PolyAlpha collection.
# Run from an ELEVATED PowerShell (right-click -> "Run as administrator"):
#   powershell -ExecutionPolicy Bypass -File scripts\harden_host_power.ps1
#
# Root causes this addresses (see AGENTS.md "Gotchas"):
#   * DC Wireless "Power Saving Mode = Medium" -> intermittent getaddrinfo/DNS
#     drops while on battery (the ~75 min collection outages).
#   * Critical-battery hard shutdown (winlogon reason 0x500ff) -> collectors
#     killed with exit code 0x40010004. No script can stop a drained battery;
#     keep the laptop on AC.
#   * Auto-logon is already enabled, so collectors restart after power-on.

$ErrorActionPreference = 'Stop'

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Error "This script must be run as Administrator."
    exit 1
}

$wireless = '19cbb8fa-5279-450e-9fac-8a3d5fedd0c1'   # Wireless Adapter Settings
$powsav   = '12bbebe6-58d6-4636-95bb-3217ef867c1a'   # Power Saving Mode
$sleep    = '238c9fa8-0aad-41ed-83f4-97be242c8f20'   # Sleep
$standby  = '29f6c1db-86da-48c5-9fdb-f2b67b1f44da'   # Sleep after

Write-Host "=== BEFORE ==="
powercfg /query SCHEME_CURRENT $wireless $powsav
powercfg /query SCHEME_CURRENT $sleep $standby

# Wi-Fi: Maximum Performance on BOTH AC and DC (index 0 = Maximum Performance).
powercfg /setacvalueindex SCHEME_CURRENT $wireless $powsav 0
powercfg /setdcvalueindex SCHEME_CURRENT $wireless $powsav 0

# Sleep: never on both AC and DC (0 seconds).
powercfg /setacvalueindex SCHEME_CURRENT $sleep $standby 0
powercfg /setdcvalueindex SCHEME_CURRENT $sleep $standby 0

powercfg /setactive SCHEME_CURRENT

Write-Host ""
Write-Host "=== AFTER ==="
powercfg /query SCHEME_CURRENT $wireless $powsav
powercfg /query SCHEME_CURRENT $sleep $standby

Write-Host ""
Write-Host "Done. Keep the laptop on AC power; a drained battery will still hard-shutdown."
Write-Host "Verify auto-logon is still enabled (should be):"
Write-Host '  (Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon").AutoAdminLogon'