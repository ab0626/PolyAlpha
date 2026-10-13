# Enable Windows auto-logon so the PolyAlpha collectors restart after a reboot
# without a manual login. This is the single biggest lever for closing the
# forward-evidence coverage gap (see AGENTS.md "Gotchas").
#
# Usage: run from an ELEVATED PowerShell (right-click -> "Run as administrator"):
#   powershell -ExecutionPolicy Bypass -File scripts\enable_autologon.ps1
#
# SECURITY: this stores the account password in clear text in the registry
# (HKLM\...\Winlogon\DefaultPassword). Anyone with local admin can read it.
# For a personal research laptop this is the standard trade-off; if you want it
# encrypted instead, use Sysinternals Autologon (stores an LSA secret).

$ErrorActionPreference = 'Stop'

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Error "This script must be run as Administrator."
    exit 1
}

$user = $env:USERNAME
if (-not $user) { $user = "adith" }

$secure = Read-Host "Enter the Windows login password for '$user'" -AsSecureString
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
$password = [Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
[Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)

$key = "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon"
Set-ItemProperty -Path $key -Name AutoAdminLogon   -Value "1"     -Type String
Set-ItemProperty -Path $key -Name DefaultUserName  -Value $user   -Type String
Set-ItemProperty -Path $key -Name DefaultPassword  -Value $password -Type String

Write-Host ""
Write-Host "Auto-logon enabled for user '$user'."
Write-Host "To disable it later: set AutoAdminLogon to '0' under $key"