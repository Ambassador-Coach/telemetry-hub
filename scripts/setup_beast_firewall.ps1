# TESTRADE BEAST Machine Windows Firewall Configuration
# Run as Administrator in PowerShell
# Surgical approach: Allow specific TESTRADE traffic only

Write-Host "========================================================" -ForegroundColor Cyan
Write-Host "🔒 TESTRADE BEAST Windows Firewall Configuration" -ForegroundColor Cyan
Write-Host "========================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "This script configures Windows Firewall for:" -ForegroundColor Yellow
Write-Host "  ✅ Outbound TESTRADE traffic to TELEMETRY (192.168.50.101)" -ForegroundColor Green
Write-Host "  ✅ SSH inbound from TELEMETRY for management" -ForegroundColor Green
Write-Host "  ❌ Block all other unnecessary traffic" -ForegroundColor Red
Write-Host ""
Write-Host "Press Ctrl+C to cancel, or Enter to continue..." -ForegroundColor Yellow
Read-Host

$TELEMETRY_IP = "192.168.50.101"
$TESTRADE_PATH = "C:\TESTRADE"

# Remove existing TESTRADE rules
Write-Host "🔧 Removing existing TESTRADE firewall rules..." -ForegroundColor Yellow
Get-NetFirewallRule -DisplayName "TESTRADE-*" -ErrorAction SilentlyContinue | Remove-NetFirewallRule

# Inbound Rules
Write-Host "🔧 Creating inbound rules..." -ForegroundColor Yellow

# Allow SSH from TELEMETRY (for management)
New-NetFirewallRule -DisplayName "TESTRADE-SSH-Inbound" `
    -Direction Inbound `
    -Protocol TCP `
    -LocalPort 22 `
    -RemoteAddress $TELEMETRY_IP `
    -Action Allow `
    -Description "Allow SSH from TELEMETRY machine for management"

# Outbound Rules for TESTRADE services
Write-Host "🔧 Creating outbound rules for TESTRADE services..." -ForegroundColor Yellow

# Redis connection
New-NetFirewallRule -DisplayName "TESTRADE-Redis-Outbound" `
    -Direction Outbound `
    -Protocol TCP `
    -RemotePort 6379 `
    -RemoteAddress $TELEMETRY_IP `
    -Action Allow `
    -Program "$TESTRADE_PATH\python.exe" `
    -Description "Allow Redis connection to TELEMETRY"

# ZMQ Telemetry Lanes (Bulk, Trading, System)
New-NetFirewallRule -DisplayName "TESTRADE-ZMQ-Telemetry-Outbound" `
    -Direction Outbound `
    -Protocol TCP `
    -RemotePort 7777,7778,7779 `
    -RemoteAddress $TELEMETRY_IP `
    -Action Allow `
    -Program "$TESTRADE_PATH\python.exe" `
    -Description "Allow ZMQ telemetry lanes to TELEMETRY"

# Babysitter IPC
New-NetFirewallRule -DisplayName "TESTRADE-Babysitter-Outbound" `
    -Direction Outbound `
    -Protocol TCP `
    -RemotePort 5555 `
    -RemoteAddress $TELEMETRY_IP `
    -Action Allow `
    -Program "$TESTRADE_PATH\python.exe" `
    -Description "Allow Babysitter IPC to TELEMETRY"

# GUI Backend access
New-NetFirewallRule -DisplayName "TESTRADE-GUI-Outbound" `
    -Direction Outbound `
    -Protocol TCP `
    -RemotePort 8001 `
    -RemoteAddress $TELEMETRY_IP `
    -Action Allow `
    -Description "Allow GUI Backend access on TELEMETRY"

# Allow Python.exe if in venv
if (Test-Path "$TESTRADE_PATH\.venv\Scripts\python.exe") {
    New-NetFirewallRule -DisplayName "TESTRADE-Python-Venv-Outbound" `
        -Direction Outbound `
        -Protocol TCP `
        -RemotePort 6379,7777,7778,7779,5555 `
        -RemoteAddress $TELEMETRY_IP `
        -Action Allow `
        -Program "$TESTRADE_PATH\.venv\Scripts\python.exe" `
        -Description "Allow TESTRADE venv Python to TELEMETRY"
}

# Performance optimization - Disable Windows Defender for TESTRADE folder (optional)
Write-Host ""
Write-Host "⚡ Optional: Exclude TESTRADE from Windows Defender scanning?" -ForegroundColor Yellow
Write-Host "This improves performance but reduces security. (Y/N)" -ForegroundColor Yellow
$response = Read-Host
if ($response -eq 'Y' -or $response -eq 'y') {
    Add-MpPreference -ExclusionPath $TESTRADE_PATH
    Add-MpPreference -ExclusionProcess "$TESTRADE_PATH\python.exe"
    Add-MpPreference -ExclusionProcess "$TESTRADE_PATH\.venv\Scripts\python.exe"
    Write-Host "✅ TESTRADE excluded from Windows Defender" -ForegroundColor Green
}

# Show all TESTRADE rules
Write-Host ""
Write-Host "========================================================" -ForegroundColor Cyan
Write-Host "✅ Firewall Configuration Complete!" -ForegroundColor Green
Write-Host "========================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Current TESTRADE firewall rules:" -ForegroundColor Yellow
Get-NetFirewallRule -DisplayName "TESTRADE-*" | Format-Table DisplayName, Direction, Action, Enabled

Write-Host ""
Write-Host "📝 Notes:" -ForegroundColor Cyan
Write-Host "  - TESTRADE can connect to TELEMETRY (192.168.50.101) on required ports"
Write-Host "  - SSH inbound allowed from TELEMETRY only"
Write-Host "  - No encryption/authentication on internal traffic for maximum speed"
Write-Host "  - All other traffic follows default Windows Firewall rules"