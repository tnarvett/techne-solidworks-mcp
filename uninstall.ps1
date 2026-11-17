# Techne uninstaller: unregisters the add-in and puts Claude Desktop back on the server it had
# before (or removes the entry when there was none).
#   powershell -ExecutionPolicy Bypass -File "<this folder>\uninstall.ps1"

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) {
    Start-Process powershell -Verb RunAs -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"")
    exit
}
$dll = Join-Path $Root "addin\bin\Techne.dll"
$regasm = Join-Path $env:WINDIR "Microsoft.NET\Framework64\v4.0.30319\RegAsm.exe"
if (Test-Path $dll) {
    & $regasm /unregister "$dll"
    Write-Host "Techne add-in unregistered." -ForegroundColor Green
}
$guid = "{6E3C2A51-9B7D-4F3E-A8C1-5D2B9E7F4A10}"
Remove-Item "HKLM:\SOFTWARE\SolidWorks\Addins\$guid" -ErrorAction SilentlyContinue
Remove-Item "HKCU:\Software\SolidWorks\AddInsStartup\$guid" -ErrorAction SilentlyContinue

$uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $uv) { $uv = Join-Path $env:USERPROFILE ".local\bin\uv.exe" }
if (Test-Path $uv) {
    & $uv run --no-project python (Join-Path $Root "claude_config.py") uninstall
    Write-Host "Claude Desktop config: the SolidWorks server is back to what it was before Techne." -ForegroundColor Green
} else {
    Write-Host "uv not found: remove the solidworks entry from Claude Desktop (Settings > Developer > Edit Config)." -ForegroundColor Yellow
}
Write-Host "Restart SolidWorks and Claude Desktop."
Read-Host "Press Enter to close" | Out-Null
