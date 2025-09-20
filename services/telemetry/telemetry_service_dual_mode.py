# core/services/telemetry_service_dual_mode.py

"""
Enhanced TelemetryService with dual TCP/IPC support.

This service listens on both TCP (for network/WSL clients) and IPC (for local Windows processes).
Provides 10x latency improvement for local communication while maintaining compatibility.
"""

import os
import time
import signal
import threading
import zmq
import psutil
import platform
import logging
import json
import sys
import queue
from typing import Dict, List, Optional, Tuple

# --- PATH SETUP FOR STANDALONE EXECUTION ---
# Get the directory of the current script (e.g., C:\TESTRADE\core\services)
script_dir = os.path.dirname(os.path.abspath(__file__))
# Get the parent directory twice to reach project root
# From C:\TESTRADE\core\services -> C:\TESTRADE\core -> C:\TESTRADE
project_root = os.path.dirname(os.path.dirname(script_dir))
# Add the project root to the Python path
if project_root not in sys.path:
    sys.path.insert(0, project_root)
# --- END OF PATH SETUP ---

from core.bulletproof_ipc_client import BulletproofBabysitterIPCClient
from utils.global_config import load_global_config
from utils.ipc_endpoints import IPC_ENDPOINTS, cleanup_ipc_files, get_endpoint_info

# Thread monitoring removed for production stability

logger = logging.getLogger("TelemetryService")


def telemetry_receiver_loop(
    stop_event: threading.Event, 
    zmq_context: zmq.Context, 
    endpoint: str, 
    processing_queue: queue.Queue,
    lane_id: str
):
    """
    A dedicated thread to receive data for one telemetry lane.
    Enhanced with transport type awareness.
    """
    logger = logging.getLogger(f"TelemetryReceiver-{lane_id}")
    
    # Determine transport type from endpoint
    transport_type = "IPC" if endpoint.startswith("ipc://") else "TCP"
    
    receiver_socket = zmq_context.socket(zmq.PULL)
    
    # Configure socket based on transport
    if transport_type == "IPC":
        # IPC optimizations
        receiver_socket.setsockopt(zmq.RCVHWM, 10000)  # Higher HWM for IPC
        receiver_socket.setsockopt(zmq.IMMEDIATE, 1)
    else:
        # TCP settings
        receiver_socket.setsockopt(zmq.RCVHWM, 5000)
    
    try:
        receiver_socket.bind(endpoint)
        logger.info(f"Receiver for '{lane_id}' bound to {endpoint} ({transport_type})")
    except zmq.ZMQError as e:
        if transport_type == "IPC" and "Address already in use" in str(e):
            # Try to clean up stale socket file
            logger.warning(f"IPC endpoint in use, attempting cleanup: {endpoint}")
            cleanup_ipc_files()
            time.sleep(0.1)
            try:
                receiver_socket.bind(endpoint)
                logger.info(f"Successfully bound after cleanup: {endpoint}")
            except:
                logger.error(f"Failed to bind {transport_type} endpoint: {endpoint}")
                return
        else:
            logger.error(f"Failed to bind {transport_type} endpoint: {endpoint}: {e}")
            return

    message_count = 0
    last_log_time = time.time()
    
    while not stop_event.is_set():
        try:
            if receiver_socket.poll(timeout=1000):
                # Receive multipart message
                message_parts = receiver_socket.recv_multipart()
                
                # Tag message with lane info for debugging
                processing_queue.put_nowait((lane_id, message_parts))
                message_count += 1
                
                # Periodic throughput logging
                current_time = time.time()
                if current_time - last_log_time >= 60:  # Log every minute
                    rate = message_count / (current_time - last_log_time)
                    logger.info(f"{lane_id}: {message_count} messages, {rate:.1f} msg/sec")
                    message_count = 0
                    last_log_time = current_time
                    
        except zmq.Again:
            continue
        except queue.Full:
            logger.error(f"'{lane_id}' lane is back-pressured! Internal queue is full.")
        except Exception as e:
            logger.error(f"Error in '{lane_id}' receiver loop: {e}")
    
    receiver_socket.close()
    logger.info(f"Receiver for '{lane_id}' stopped.")


def run_telemetry_service_dual_mode(config_path: str, *args):
    """
    Enhanced TelemetryService with dual TCP/IPC support for optimal performance.
    """
    try:
        config = load_global_config(config_path)
        logging.basicConfig(
            level=logging.INFO, 
            format='%(asctime)s - %(name)s - %(process)d - %(levelname)s - %(message)s'
        )
        
        logger.info(f"Telemetry Service (Dual-Mode) starting, PID: {os.getpid()}")
        logger.info(f"Platform info: {get_endpoint_info()}")

        # Thread monitoring removed for production stability

        # ZMQ context
        zmq_context = zmq.Context()

        # Set low OS priority
        p = psutil.Process(os.getpid())
        if platform.system() == "Windows":
            p.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
            logger.info("Windows process priority set to BELOW_NORMAL.")
        else:
            p.nice(15)
            logger.info("Unix process niceness set to 15.")

        # Internal processing queue
        processing_queue = queue.Queue(maxsize=50000)

        # Initialize BulletproofIPCClient
        ipc_client_to_babysitter = BulletproofBabysitterIPCClient(
            zmq_context=zmq_context,
            ipc_config=config,
            logger_instance=logging.getLogger("TelemetryService.IPC_Client")
        )
        
        # Define lanes with dual endpoints
        lanes_config = []
        
        # DISTRIBUTED MODE: Read network topology from local config
        # No business logic here - just plumbing!
        import json
        from pathlib import Path
        
        network_config_path = Path(__file__).parent.parent.parent / "network_config.json"
        if network_config_path.exists():
            with open(network_config_path) as f:
                net_config = json.load(f)
                bind_addrs = net_config["telemetry_service"]["bind_addresses"]
                tcp_lanes = {
                    "bulk-tcp": bind_addrs["bulk"],
                    "trading-tcp": bind_addrs["trading"],
                    "system-tcp": bind_addrs["system"]
                }
                logger.info(f"Loaded network config from {network_config_path}")
        else:
            # Fallback for backward compatibility
            tcp_lanes = {
                "bulk-tcp": "tcp://0.0.0.0:7777",
                "trading-tcp": "tcp://0.0.0.0:7778",
                "system-tcp": "tcp://0.0.0.0:7779",
            }
            logger.warning("No network_config.json found, using defaults")
        
        for lane_id, endpoint in tcp_lanes.items():
            lanes_config.append((lane_id, endpoint))
        
        # Add IPC endpoints on Windows (for local processes)
        if platform.system() == "Windows" and not getattr(config, 'DISABLE_IPC_MODE', False):
            ipc_lanes = {
                "bulk-ipc": IPC_ENDPOINTS["TELEMETRY_BULK_ENDPOINT_IPC"],
                "trading-ipc": IPC_ENDPOINTS["TELEMETRY_TRADING_ENDPOINT_IPC"],
                "system-ipc": IPC_ENDPOINTS["TELEMETRY_SYSTEM_ENDPOINT_IPC"],
            }
            
            for lane_id, endpoint in ipc_lanes.items():
                lanes_config.append((lane_id, endpoint))
            
            logger.info("Dual-mode enabled: Listening on both TCP and IPC endpoints")
        else:
            logger.info("TCP-only mode: IPC disabled or not on Windows")

        stop_event = threading.Event()
        
        def shutdown_handler(signum, frame):
            logger.warning(f"Shutdown signal {signum} received.")
            stop_event.set()
        
        signal.signal(signal.SIGTERM, shutdown_handler)
        signal.signal(signal.SIGINT, shutdown_handler)

        # Start receiver threads
        receiver_threads = []
        for lane_id, endpoint in lanes_config:
            thread = threading.Thread(
                target=telemetry_receiver_loop,
                args=(stop_event, zmq_context, endpoint, processing_queue, lane_id),
                name=f"Receiver-{lane_id}"
            )
            thread.daemon = True
            thread.start()
            receiver_threads.append(thread)

        logger.info(f"Started {len(receiver_threads)} receiver threads")

        # Main processing loop
        logger.info("Telemetry Service ready. Processing messages...")
        
        stats = {
            "tcp_messages": 0,
            "ipc_messages": 0,
            "binary_messages": 0,
            "json_messages": 0,
            "start_time": time.time()
        }
        
        while not stop_event.is_set():
            try:
                # Get message from queue
                lane_id, message_parts = processing_queue.get(timeout=1.0)
                
                # Track transport stats
                if "ipc" in lane_id:
                    stats["ipc_messages"] += 1
                else:
                    stats["tcp_messages"] += 1
                
                # Process message (existing polymorphic logic)
                if len(message_parts) > 2:
                    # Binary multipart message
                    stats["binary_messages"] += 1
                    stream = message_parts[0].decode('utf-8')
                    metadata_json = message_parts[1].decode('utf-8')
                    binary_parts = message_parts[2:]
                    
                    ipc_client_to_babysitter.send_multipart_data(stream, metadata_json, binary_parts)
                    
                elif len(message_parts) == 2:
                    # JSON-only message
                    stats["json_messages"] += 1
                    stream = message_parts[0].decode('utf-8')
                    payload_json = message_parts[1].decode('utf-8')
                    
                    ipc_client_to_babysitter.send_data(stream, payload_json)
                    
                elif len(message_parts) == 1:
                    # Legacy single-part
                    stats["json_messages"] += 1
                    payload_json = message_parts[0].decode('utf-8')
                    try:
                        data = json.loads(payload_json)
                        stream = data.get('stream', 'testrade:default')
                        ipc_client_to_babysitter.send_data(stream, payload_json)
                    except:
                        logger.error("Failed to parse single-part message")
                
                processing_queue.task_done()
                
                # Periodic stats logging
                if (stats["tcp_messages"] + stats["ipc_messages"]) % 10000 == 0:
                    elapsed = time.time() - stats["start_time"]
                    total = stats["tcp_messages"] + stats["ipc_messages"]
                    ipc_pct = (stats["ipc_messages"] / total * 100) if total > 0 else 0
                    
                    logger.info(f"Stats: Total={total}, IPC={ipc_pct:.1f}%, "
                               f"Binary={stats['binary_messages']}, "
                               f"Rate={total/elapsed:.1f} msg/sec")
                    
                    # Estimate latency savings
                    ipc_savings_ms = (stats["ipc_messages"] * 45) / 1000
                    logger.info(f"Estimated latency savings from IPC: {ipc_savings_ms:.1f}ms")
                
            except queue.Empty:
                continue
            except Exception as e:
                logger.error(f"Error in processing loop: {e}", exc_info=True)
                time.sleep(0.1)
                
    except Exception as e:
        logger.critical(f"Telemetry Service failed: {e}", exc_info=True)
    finally:
        logger.info("Telemetry Service shutting down...")

        # Thread monitoring removed

        # Stop all threads
        if 'stop_event' in locals():
            stop_event.set()
        
        # Wait for threads
        if 'receiver_threads' in locals():
            for thread in receiver_threads:
                thread.join(timeout=2.0)
        
        # Clean up
        if 'ipc_client_to_babysitter' in locals():
            ipc_client_to_babysitter.close()
            
        # Clean up IPC files on Unix
        if platform.system() != "Windows":
            cleanup_ipc_files("telemetry")
            
        if 'zmq_context' in locals():
            zmq_context.term()
            
        logger.info("Telemetry Service stopped.")


if __name__ == "__main__":
    # Setup signal handlers for graceful shutdown
    def signal_handler(signum, frame):
        logger.info(f"Telemetry Service received signal {signum} - shutting down gracefully")
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    if hasattr(signal, 'SIGBREAK'):
        signal.signal(signal.SIGBREAK, signal_handler)

    try:
        # Allow running enhanced version directly
        if len(sys.argv) > 1:
            run_telemetry_service_dual_mode(sys.argv[1])
        else:
            default_config = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                'utils', 'control.json'
            )
            print(f"Usage: python telemetry_service_dual_mode.py [config_path]")
            print(f"Running with default: {default_config}")
            run_telemetry_service_dual_mode(default_config)
    except KeyboardInterrupt:
        logger.info("Telemetry Service interrupted - exiting gracefully")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Telemetry Service fatal error: {e}")
        sys.exit(1)