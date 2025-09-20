#!/bin/bash
# TESTRADE TELEMETRY Machine Firewall Configuration
# Surgical approach: Maximum internal speed, fortress to outside

set -e

echo "========================================================"
echo "🔒 TESTRADE TELEMETRY Firewall Configuration"
echo "========================================================"
echo ""
echo "This script configures UFW (Ubuntu Firewall) for:"
echo "  ✅ UNRESTRICTED traffic from BEAST (192.168.50.100)"
echo "  ✅ SSH access for management"
echo "  ❌ DENY all other incoming traffic"
echo ""
echo "Press Ctrl+C to cancel, or Enter to continue..."
read

# Reset UFW to clean state
echo "🔧 Resetting UFW to default state..."
sudo ufw --force disable
sudo ufw --force reset

# Default policies
echo "🔧 Setting default policies..."
sudo ufw default deny incoming
sudo ufw default allow outgoing

# Allow SSH for management (from anywhere - be careful!)
echo "🔧 Allowing SSH (port 22)..."
sudo ufw allow ssh comment 'SSH for management'

# TESTRADE specific rules - ONLY from BEAST machine
BEAST_IP="192.168.50.100"

echo "🔧 Adding TESTRADE service rules (from BEAST: $BEAST_IP)..."

# Redis
sudo ufw allow from $BEAST_IP to any port 6379 comment 'Redis from BEAST'

# ZMQ Telemetry Lanes
sudo ufw allow from $BEAST_IP to any port 7777 comment 'ZMQ Bulk Lane from BEAST'
sudo ufw allow from $BEAST_IP to any port 7778 comment 'ZMQ Trading Lane from BEAST'
sudo ufw allow from $BEAST_IP to any port 7779 comment 'ZMQ System Lane from BEAST'

# Babysitter IPC
sudo ufw allow from $BEAST_IP to any port 5555 comment 'Babysitter IPC from BEAST'

# GUI Backend (if accessed from BEAST)
sudo ufw allow from $BEAST_IP to any port 8001 comment 'GUI Backend from BEAST'

# Optional: Allow GUI from local network (uncomment if needed)
# echo "🔧 Allowing GUI access from local network..."
# sudo ufw allow from 192.168.50.0/24 to any port 8001 comment 'GUI from LAN'

# Enable UFW
echo "🔧 Enabling UFW..."
sudo ufw --force enable

# Show status
echo ""
echo "========================================================"
echo "✅ Firewall Configuration Complete!"
echo "========================================================"
echo ""
sudo ufw status verbose

echo ""
echo "📝 Notes:"
echo "  - BEAST (192.168.50.100) has unrestricted access to TESTRADE ports"
echo "  - SSH is allowed from anywhere (consider restricting if needed)"
echo "  - All other incoming traffic is BLOCKED"
echo "  - No encryption/authentication on internal TESTRADE traffic"
echo ""
echo "For Windows BEAST firewall setup, see: network.json"