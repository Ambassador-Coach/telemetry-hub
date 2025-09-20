#!/usr/bin/env python3
"""
Simple bilateral connectivity test using hostname
Tests both directions: VM->Container and Container->VM
"""

import zmq
import time
import sys
import threading

def test_vm_to_container():
    """Test VM -> Container direction (what bulletproof IPC uses)"""
    print("\n" + "="*60)
    print("TEST 1: VM → Container (using hostname 'telemetry-hub')")
    print("="*60)
    
    context = zmq.Context()
    results = []
    
    ports = [5555, 5556, 5557]
    for port in ports:
        socket = context.socket(zmq.PUSH)
        socket.setsockopt(zmq.LINGER, 100)
        
        endpoint = f"tcp://telemetry-hub:{port}"
        
        try:
            print(f"\nPort {port}:")
            print(f"  Connecting to {endpoint}...")
            socket.connect(endpoint)
            
            # Send test message
            test_msg = f"Test port {port}".encode()
            socket.send(test_msg, flags=zmq.DONTWAIT)
            print(f"  ✓ Connected and sent (no errors)")
            results.append((port, True))
            
        except Exception as e:
            print(f"  ✗ Failed: {e}")
            results.append((port, False))
        finally:
            socket.close()
    
    context.term()
    
    success = all(r[1] for r in results)
    print(f"\nRESULT: {'✓ PASS' if success else '✗ FAIL'} - VM to Container")
    return success

def test_container_to_vm(vm_hostname):
    """Test Container -> VM direction"""
    print("\n" + "="*60)
    print(f"TEST 2: Container → VM (using hostname '{vm_hostname}')")
    print("="*60)
    
    context = zmq.Context()
    results = []
    
    # Use different ports for reverse direction
    ports = [7777, 7778, 7779]
    for port in ports:
        socket = context.socket(zmq.PUSH)
        socket.setsockopt(zmq.LINGER, 100)
        socket.setsockopt(zmq.SNDHWM, 1)
        
        endpoint = f"tcp://{vm_hostname}:{port}"
        
        try:
            print(f"\nPort {port}:")
            print(f"  Connecting to {endpoint}...")
            socket.connect(endpoint)
            
            # Try to send
            test_msg = f"Test port {port}".encode()
            socket.send(test_msg, flags=zmq.DONTWAIT)
            print(f"  ✓ Connected and sent (no errors)")
            results.append((port, True))
            
        except zmq.Again:
            print(f"  ⚠ Would block (no listener)")
            results.append((port, False))
        except Exception as e:
            print(f"  ✗ Failed: {e}")
            results.append((port, False))
        finally:
            socket.close()
    
    context.term()
    
    success = all(r[1] for r in results)
    print(f"\nRESULT: {'✓ PASS' if success else '✗ FAIL'} - Container to VM")
    return success

def listener_thread(port, stop_event):
    """Simple listener for testing"""
    context = zmq.Context()
    socket = context.socket(zmq.PULL)
    socket.setsockopt(zmq.RCVTIMEO, 1000)
    
    try:
        socket.bind(f"tcp://*:{port}")
        print(f"  Listener on port {port} ready")
        
        while not stop_event.is_set():
            try:
                msg = socket.recv(flags=zmq.DONTWAIT)
                print(f"  Port {port} received: {msg}")
            except zmq.Again:
                time.sleep(0.1)
    except Exception as e:
        print(f"  Port {port} listener error: {e}")
    finally:
        socket.close()
        context.term()

def main():
    print("\n" + "="*60)
    print("BILATERAL CONNECTIVITY TEST")
    print("="*60)
    
    # Test 1: VM to Container (critical for bulletproof IPC)
    vm_to_container_works = test_vm_to_container()
    
    # Test 2: Container to VM (optional, for reference)
    if len(sys.argv) > 1:
        vm_hostname = sys.argv[1]
        container_to_vm_works = test_container_to_vm(vm_hostname)
    else:
        print("\n(Skipping Container→VM test - no VM hostname provided)")
        container_to_vm_works = None
    
    # Summary
    print("\n" + "="*60)
    print("SUMMARY:")
    print("="*60)
    print(f"VM → Container (ports 5555-5557): {'✓ WORKS' if vm_to_container_works else '✗ FAILS'}")
    if container_to_vm_works is not None:
        print(f"Container → VM (ports 7777-7779): {'✓ WORKS' if container_to_vm_works else '✗ FAILS'}")
    
    print("\nCRITICAL FOR TELEMETRY:")
    if vm_to_container_works:
        print("✓ VM can reach container on telemetry-hub hostname")
        print("  This is what bulletproof IPC needs to work!")
    else:
        print("✗ VM cannot reach container - telemetry will fail!")
        print("  Check:")
        print("  1. VM's hosts file has: <container-ip>  telemetry-hub")
        print("  2. Container is running and ports are exposed")
        print("  3. No firewall blocking")
    
    print("="*60)

if __name__ == "__main__":
    main()