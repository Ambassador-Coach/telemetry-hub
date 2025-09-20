#!/usr/bin/env python3
"""
Monitor telemetry flow from TANK to container.
Watches both ZMQ ports and Redis streams.
"""

import zmq
import redis
import threading
import time
import signal
import sys
from datetime import datetime

# Global stop flag
stop_event = threading.Event()

def signal_handler(sig, frame):
    print("\n[SHUTDOWN] Stopping monitors...")
    stop_event.set()
    sys.exit(0)

def monitor_zmq_port(port, channel_name):
    """Monitor incoming ZMQ messages on a specific port"""
    context = zmq.Context()
    socket = context.socket(zmq.PULL)
    socket.setsockopt(zmq.RCVTIMEO, 1000)  # 1 second timeout
    
    try:
        socket.connect(f"tcp://localhost:{port}")
        print(f"[ZMQ-{channel_name}] Monitoring port {port}")
        
        msg_count = 0
        last_msg_time = None
        
        while not stop_event.is_set():
            try:
                parts = socket.recv_multipart()
                msg_count += 1
                last_msg_time = datetime.now()
                
                # Parse message structure
                if len(parts) >= 3:
                    stream_name = parts[0].decode('utf-8', errors='ignore')
                    timestamp = parts[1].decode('utf-8', errors='ignore')
                    data_preview = parts[2][:100] if len(parts[2]) > 100 else parts[2]
                    
                    print(f"[ZMQ-{channel_name}] MSG #{msg_count} @ {last_msg_time.strftime('%H:%M:%S.%f')[:-3]}")
                    print(f"  Stream: {stream_name}")
                    print(f"  Timestamp: {timestamp}")
                    print(f"  Data preview: {data_preview[:50]}...")
                else:
                    print(f"[ZMQ-{channel_name}] MSG #{msg_count} with {len(parts)} parts @ {last_msg_time.strftime('%H:%M:%S.%f')[:-3]}")
                    
            except zmq.Again:
                continue  # Timeout, no message
            except Exception as e:
                if "Interrupted system call" not in str(e):
                    print(f"[ZMQ-{channel_name}] Error: {e}")
                    
    except Exception as e:
        print(f"[ZMQ-{channel_name}] Failed to connect: {e}")
    finally:
        socket.close()
        context.term()

def monitor_redis_streams():
    """Monitor Redis streams for telemetry data"""
    try:
        r = redis.Redis(host='localhost', port=6379, decode_responses=True)
        print("[REDIS] Connected to Redis, monitoring streams...")
        
        # Streams to monitor
        streams_to_watch = [
            "testrade:raw-ocr-events",
            "telemetry:bulk",
            "telemetry:trading", 
            "telemetry:system",
            "ocr:cold-path",
            "ocr:events"
        ]
        
        # Get initial stream info
        for stream in streams_to_watch:
            try:
                info = r.xinfo_stream(stream)
                print(f"[REDIS] Stream '{stream}' exists with {info['length']} entries")
            except:
                pass  # Stream doesn't exist yet
        
        last_counts = {}
        while not stop_event.is_set():
            time.sleep(2)  # Check every 2 seconds
            
            for stream in streams_to_watch:
                try:
                    length = r.xlen(stream)
                    if length > 0:
                        if stream not in last_counts:
                            print(f"[REDIS] NEW Stream '{stream}' created with {length} entries")
                            last_counts[stream] = length
                        elif length > last_counts[stream]:
                            new_msgs = length - last_counts[stream]
                            print(f"[REDIS] Stream '{stream}': +{new_msgs} new entries (total: {length})")
                            
                            # Show last entry
                            last_entry = r.xrevrange(stream, count=1)
                            if last_entry:
                                entry_id, data = last_entry[0]
                                print(f"  Last entry ID: {entry_id}")
                                if 'data' in data:
                                    print(f"  Data preview: {str(data['data'])[:100]}...")
                                    
                            last_counts[stream] = length
                except:
                    pass  # Stream doesn't exist
                    
    except Exception as e:
        print(f"[REDIS] Error connecting: {e}")

def main():
    signal.signal(signal.SIGINT, signal_handler)
    
    print("=" * 70)
    print("TELEMETRY FLOW MONITOR")
    print("=" * 70)
    print("Monitoring:")
    print("  - ZMQ Bulk Port: 5555")
    print("  - ZMQ Trading Port: 5556")
    print("  - ZMQ System Port: 5557")
    print("  - Redis Streams")
    print("=" * 70)
    print("Press Ctrl+C to stop\n")
    
    # Start monitoring threads
    threads = []
    
    # Monitor ZMQ ports
    for port, name in [(5555, "BULK"), (5556, "TRADING"), (5557, "SYSTEM")]:
        t = threading.Thread(target=monitor_zmq_port, args=(port, name))
        t.daemon = True
        t.start()
        threads.append(t)
    
    # Monitor Redis
    t = threading.Thread(target=monitor_redis_streams)
    t.daemon = True
    t.start()
    threads.append(t)
    
    # Keep main thread alive
    try:
        while not stop_event.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    
    print("\n[SHUTDOWN] Monitor stopped")

if __name__ == "__main__":
    main()