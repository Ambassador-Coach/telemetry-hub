#!/bin/bash
# Check firewall rules for ZMQ ports used by telemetry

echo "========================================="
echo "Firewall Check for Telemetry ZMQ Ports"
echo "========================================="

PORTS=(5555 5556 5557 5558)
TELEMETRY_IP=$(hostname -I | awk '{print $1}')

echo ""
echo "Container IP: $TELEMETRY_IP"
echo ""

# Check if firewall is active
echo "1. Checking firewall status..."
if command -v ufw &> /dev/null; then
    sudo ufw status | head -5
    echo ""
    
    echo "2. Checking UFW rules for our ports..."
    for port in "${PORTS[@]}"; do
        echo -n "   Port $port: "
        if sudo ufw status | grep -q "$port"; then
            echo "✓ Rule exists"
        else
            echo "✗ No rule found"
        fi
    done
else
    echo "   UFW not installed"
fi

if command -v iptables &> /dev/null; then
    echo ""
    echo "3. Checking iptables rules..."
    for port in "${PORTS[@]}"; do
        echo -n "   Port $port: "
        if sudo iptables -L INPUT -n | grep -q "dpt:$port"; then
            echo "✓ Rule exists"
        else
            echo "✗ No rule found"
        fi
    done
fi

echo ""
echo "4. Checking listening ports..."
for port in "${PORTS[@]}"; do
    echo -n "   Port $port: "
    if netstat -tuln 2>/dev/null | grep -q ":$port "; then
        echo "✓ Something listening"
        netstat -tuln 2>/dev/null | grep ":$port " | head -1
    else
        echo "✗ Nothing listening"
    fi
done

echo ""
echo "5. Testing port connectivity locally..."
for port in "${PORTS[@]}"; do
    echo -n "   Port $port: "
    timeout 1 bash -c "echo >/dev/tcp/localhost/$port" 2>/dev/null && echo "✓ Open" || echo "✗ Closed/Filtered"
done

echo ""
echo "========================================="
echo "To open ports in UFW (if needed):"
echo ""
for port in "${PORTS[@]}"; do
    echo "  sudo ufw allow $port/tcp"
done

echo ""
echo "To test from Windows VM:"
echo "  1. Add to C:\\Windows\\System32\\drivers\\etc\\hosts:"
echo "     $TELEMETRY_IP  telemetry-hub"
echo ""
echo "  2. Test connectivity:"
echo "     Test-NetConnection -ComputerName telemetry-hub -Port 5555"
echo ""
echo "  3. Or use Python test script:"
echo "     python C:\\TANK\\utils\\test_zmq_vm_side.py"
echo "========================================="