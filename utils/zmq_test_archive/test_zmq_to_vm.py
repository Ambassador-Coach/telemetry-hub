#!/usr/bin/env python3
"""
ZMQ Test - Container to VM (Reverse Direction)
Tests sending messages from container to Windows VM.

Usage: python test_zmq_to_vm.py <vm_ip> [port]
  vm_ip: IP address of Windows VM (e.g., 192.168.142.133)
  port: Port to send to (default: 7777)
"""

import zmq
import json
import time
import sys
import socket as pysocket

def test_push_to_vm(vm_ip, port=7777):
    """Send test messages from container to VM"""
    print("=" * 60)
    print(f"ZMQ Container→VM Test (PUSH to {vm_ip}:{port})")
    print("=" * 60)
    
    # First, try to resolve/ping the VM
    print(f"\n1. Testing network connectivity to {vm_ip}...")
    try:
        # Try a simple socket connection test
        sock = pysocket.socket(pysocket.AF_INET, pysocket.SOCK_STREAM)
        sock.settimeout(2)
        # We don't expect this to connect (no service), just checking network
        result = sock.connect_ex((vm_ip, 445))  # Try SMB port as network test
        sock.close()
        
        if result == 0 or result == 111:  # 0=connected, 111=refused (but network works)
            print(f"   ✓ Network path to {vm_ip} exists")
        else:
            print(f"   ? Network test returned code {result}")
    except Exception as e:
        print(f"   ⚠ Network test failed: {e}")
    
    # Now test ZMQ
    context = zmq.Context()
    socket = context.socket(zmq.PUSH)
    
    # Configure socket to avoid blocking
    socket.setsockopt(zmq.SNDHWM, 1)  # Drop messages immediately if no receiver
    socket.setsockopt(zmq.LINGER, 0)   # Don't block on close
    socket.setsockopt(zmq.SNDTIMEO, 100)  # 100ms send timeout
    socket.setsockopt(zmq.IMMEDIATE, 1)  # Don't queue if not connected
    
    endpoint = f"tcp://{vm_ip}:{port}"
    
    try:
        print(f"\n2. Connecting ZMQ PUSH socket to {endpoint}...")
        socket.connect(endpoint)
        print("   ✓ Connected (or queued for connection)")
        
        print("\n3. Sending test messages...")
        print("   (Make sure VM is running listener on port", port, ")")
        
        for i in range(5):
            msg = {
                "test": "ping_from_container",
                "sequence": i,
                "timestamp": time.time(),
                "from": "telemetry-container",
                "to": vm_ip
            }
            
            # Try different message formats
            try:
                if i % 2 == 0:
                    # JSON message
                    socket.send_json(msg, flags=zmq.DONTWAIT)
                    print(f"   → Sent JSON message #{i}")
                else:
                    # Multipart message
                    parts = [
                        b"container-test",
                        json.dumps(msg).encode('utf-8')
                    ]
                    socket.send_multipart(parts, flags=zmq.DONTWAIT)
                    print(f"   → Sent multipart message #{i}")
            except zmq.Again:
                print(f"   ⚠ Message #{i} dropped (no receiver, HWM reached)")
                continue
            
            time.sleep(0.5)
        
        print("\n✓ Messages sent successfully!")
        print("\nNOTE: ZMQ PUSH sockets don't get confirmation.")
        print("Check the VM side listener to verify receipt.")
        
    except zmq.ZMQError as e:
        print(f"\n✗ ZMQ Error: {e}")
        return False
    except Exception as e:
        print(f"\n✗ Error: {e}")
        return False
    finally:
        socket.close()
        context.term()
    
    print("\n" + "=" * 60)
    print("Test complete. Check VM side for received messages.")
    print("=" * 60)
    return True

def test_req_to_vm(vm_ip, port=7778):
    """Test REQ/REP pattern to VM (bidirectional confirmation)"""
    print("=" * 60)
    print(f"ZMQ Container→VM REQ/REP Test ({vm_ip}:{port})")
    print("=" * 60)
    
    context = zmq.Context()
    socket = context.socket(zmq.REQ)
    socket.setsockopt(zmq.RCVTIMEO, 5000)  # 5 second timeout
    socket.setsockopt(zmq.LINGER, 0)
    
    endpoint = f"tcp://{vm_ip}:{port}"
    
    try:
        print(f"Connecting REQ socket to {endpoint}...")
        socket.connect(endpoint)
        print("✓ Connected")
        
        # Send request and wait for reply
        for i in range(3):
            msg = {"type": "ping", "seq": i, "from": "container"}
            print(f"\n→ Sending request #{i}: {msg}")
            socket.send_json(msg)
            
            try:
                reply = socket.recv_json()
                print(f"← Received reply: {reply}")
                
                if reply.get("type") == "pong":
                    print(f"  ✓ Request #{i} successful!")
                else:
                    print(f"  ? Unexpected reply")
                    
            except zmq.Again:
                print(f"  ✗ Timeout waiting for reply #{i}")
                print("     (Is VM listener running?)")
                return False
        
        print("\n✓ REQ/REP test successful - bidirectional communication works!")
        return True
        
    except Exception as e:
        print(f"✗ Error: {e}")
        return False
    finally:
        socket.close()
        context.term()

def main():
    if len(sys.argv) < 2:
        print("Usage: python test_zmq_to_vm.py <vm_ip> [port] [mode]")
        print("  vm_ip: IP address of Windows VM")
        print("  port: Port number (default: 7777)")
        print("  mode: 'push' (default) or 'req'")
        print("\nExample: python test_zmq_to_vm.py 192.168.142.133")
        sys.exit(1)
    
    vm_ip = sys.argv[1]
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 7777
    mode = sys.argv[3] if len(sys.argv) > 3 else "push"
    
    if mode == "push":
        success = test_push_to_vm(vm_ip, port)
    elif mode == "req":
        success = test_req_to_vm(vm_ip, port + 1)  # Use different port for REQ/REP
    else:
        print(f"Unknown mode: {mode}")
        sys.exit(1)
    
    sys.exit(0 if success else 1)

if __name__ == "__main__":
    main()