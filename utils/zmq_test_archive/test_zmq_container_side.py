#!/usr/bin/env python3
"""
ZMQ Connectivity Test - Container Side (Server/PULL)
Listens for connections from Windows VM.
Mimics the exact setup used by babysitter service.

Usage: python test_zmq_container_side.py [mode]
  mode: 'pull' (default) or 'rep' for REQ/REP pattern
"""

import zmq
import time
import sys
import json
import threading

def handle_pull_socket(port, channel_name, stop_event):
    """Handler for a single PULL socket"""
    context = zmq.Context()
    socket = context.socket(zmq.PULL)
    socket.setsockopt(zmq.RCVTIMEO, 1000)  # 1 second timeout for checking stop
    msg_count = 0
    
    try:
        bind_addr = f"tcp://*:{port}"
        socket.bind(bind_addr)
        print(f"  [{channel_name:8s}] Listening on {bind_addr}")
        while not stop_event.is_set():
            try:
                if socket.poll(1000):  # 1 second timeout
                    # Try multipart first
                    try:
                        parts = socket.recv_multipart(flags=zmq.DONTWAIT)
                        if len(parts) >= 2:
                            stream = parts[0].decode('utf-8')
                            payload = json.loads(parts[1].decode('utf-8'))
                            msg_count += 1
                            print(f"  [{channel_name:8s}] ← Multipart #{msg_count}: stream={stream}, payload={payload}")
                        else:
                            # Single part message
                            data = json.loads(parts[0].decode('utf-8'))
                            msg_count += 1
                            print(f"  [{channel_name:8s}] ← JSON #{msg_count}: {data}")
                    except:
                        # Try as simple JSON
                        data = socket.recv_json(flags=zmq.DONTWAIT)
                        msg_count += 1
                        print(f"  [{channel_name:8s}] ← JSON #{msg_count}: {data}")
                        
            except zmq.Again:
                continue  # Timeout, check stop_event
            except Exception as e:
                print(f"  [{channel_name:8s}] Error: {e}")
                
    except Exception as e:
        print(f"  [{channel_name:8s}] Failed to bind: {e}")
    finally:
        socket.close()
        context.term()
        print(f"  [{channel_name:8s}] Stopped. Received {msg_count} messages.")

def test_pull_push():
    """Test PULL/PUSH pattern (what babysitter uses)"""
    print("=" * 60)
    print("ZMQ PULL/PUSH Connectivity Test - Container Side")
    print("=" * 60)
    print("\nStarting listeners (press Ctrl+C to stop)...")
    
    # Configuration matching babysitter
    channels = [
        (5555, "bulk"),
        (5556, "trading"),
        (5557, "system"),
    ]
    
    stop_event = threading.Event()
    threads = []
    
    # Start a thread for each channel
    for port, name in channels:
        thread = threading.Thread(
            target=handle_pull_socket,
            args=(port, name, stop_event),
            daemon=True
        )
        thread.start()
        threads.append(thread)
    
    print("\n✓ All listeners started. Waiting for messages from VM...\n")
    print("Run test_zmq_vm_side.py on the Windows VM to send test messages.")
    print("Press Ctrl+C to stop.\n")
    
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n\nShutting down...")
        stop_event.set()
        
        for thread in threads:
            thread.join(timeout=2)
        
        print("✓ All listeners stopped.")

def test_rep_req():
    """Test REP/REQ pattern for bidirectional verification"""
    print("=" * 60)
    print("ZMQ REP/REQ Connectivity Test - Container Side")
    print("=" * 60)
    
    context = zmq.Context()
    socket = context.socket(zmq.REP)
    
    try:
        bind_addr = "tcp://*:5558"
        socket.bind(bind_addr)
        print(f"Listening on {bind_addr} for REQ/REP test...")
        print("Run test_zmq_vm_side.py req on the Windows VM to test.")
        print("Press Ctrl+C to stop.\n")
        
        msg_count = 0
        while True:
            try:
                # Wait for request
                request = socket.recv_json()
                msg_count += 1
                print(f"← Received request #{msg_count}: {request}")
                
                # Send reply
                if request.get("type") == "ping":
                    reply = {
                        "type": "pong",
                        "seq": request.get("seq", 0),
                        "from": "container",
                        "echo": request
                    }
                    socket.send_json(reply)
                    print(f"→ Sent reply: {reply}")
                else:
                    reply = {"error": "Unknown request type"}
                    socket.send_json(reply)
                    print(f"→ Sent error: {reply}")
                    
            except KeyboardInterrupt:
                break
            except Exception as e:
                print(f"Error: {e}")
                
    except Exception as e:
        print(f"Failed to bind: {e}")
    finally:
        socket.close()
        context.term()
        print(f"\n✓ Stopped. Handled {msg_count} requests.")

def check_ports():
    """Check if ports are already in use"""
    import socket as pysocket
    
    ports = [5555, 5556, 5557, 5558]
    blocked = []
    
    for port in ports:
        sock = pysocket.socket(pysocket.AF_INET, pysocket.SOCK_STREAM)
        result = sock.connect_ex(('127.0.0.1', port))
        sock.close()
        
        if result == 0:
            blocked.append(port)
    
    if blocked:
        print(f"⚠️  WARNING: Ports already in use: {blocked}")
        print("   Another process might be using these ports.")
        print("   Check with: sudo lsof -i :5555")
        return False
    
    return True

def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "pull"
    
    print("Checking if ports are available...")
    if not check_ports():
        print("\nContinuing anyway... (existing process might be the real babysitter)\n")
    
    if mode == "pull":
        test_pull_push()
    elif mode == "rep":
        test_rep_req()
    else:
        print(f"Unknown mode: {mode}")
        print("Usage: python test_zmq_container_side.py [pull|rep]")
        sys.exit(1)

if __name__ == "__main__":
    main()