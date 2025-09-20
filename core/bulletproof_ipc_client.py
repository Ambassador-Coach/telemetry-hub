# core/bulletproof_ipc_client.py

import logging
import threading
import time
import os
import mmap
import json
import struct
from pathlib import Path
from queue import Queue, Full, Empty
from typing import Optional, Dict, Any, TYPE_CHECKING

from abc import ABC, abstractmethod
from interfaces.core.services import IBulletproofBabysitterIPCClient

# Lazy ZMQ import to prevent startup failures
if TYPE_CHECKING:
    import zmq
else:
    zmq = None
    
def _get_zmq():
    """Lazy import of ZMQ with proper error handling"""
    global zmq
    if zmq is None:
        try:
            import zmq as _zmq
            zmq = _zmq
        except ImportError as e:
            raise IPCInitializationError(f"ZMQ not available: {e}")
    return zmq

class IPCInitializationError(Exception): pass

class IMissionControlNotifier(ABC):
    @abstractmethod
    def notify_event(self, event_type: str, severity: str, message: str, details: Optional[Dict] = None) -> None: pass

class PlaceholderMissionControlNotifier(IMissionControlNotifier):
    def __init__(self, logger_instance): 
        self.logger = logger_instance
    
    def notify_event(self, event_type: str, severity: str, message: str, details: Optional[Dict] = None): 
        pass

# --- Mmap Header Constants (Re-introduced) ---
MMAP_HEADER_MAGIC_NUMBER = 0x746E6B42 # "tnkB"
MMAP_HEADER_VERSION = 1
MMAP_HEADER_LAYOUT = [
    ('magic', 'I'), ('version', 'B'), ('total_data_size', 'Q'),
    ('write_ptr', 'Q'), ('read_ptr', 'Q'), ('message_count', 'Q'),
]
MMAP_HEADER_FORMAT_STRING = '<' + ''.join([fmt for _, fmt in MMAP_HEADER_LAYOUT])
MMAP_HEADER_SIZE_BYTES = struct.calcsize(MMAP_HEADER_FORMAT_STRING)
MMAP_PAGE_SIZE = mmap.PAGESIZE
MMAP_EFFECTIVE_HEADER_SIZE = ((MMAP_HEADER_SIZE_BYTES + MMAP_PAGE_SIZE - 1) // MMAP_PAGE_SIZE) * MMAP_PAGE_SIZE
MSG_FRAME_TOTAL_LEN_BYTES = 4
MSG_FRAME_STREAM_LEN_BYTES = 2
MSG_FRAME_DATA_LEN_BYTES = 4

class BulletproofBabysitterIPCClient(IBulletproofBabysitterIPCClient):
    """
    High-performance, multi-lane IPC client with mmap-backed disk buffering for resilience.
    """
    def __init__(self, zmq_context: Optional[Any], ipc_config: Any, logger_instance: Optional[logging.Logger] = None, **kwargs):
        self.logger = logger_instance or logging.getLogger(__name__)
        self.ipc_config = ipc_config
        
        # --- THE GUARD CLAUSE ---
        # Step 1: Determine the mode immediately using the centralized mode detection
        from utils.testrade_modes import get_current_mode, TestradeMode, requires_ipc_services
        
        current_mode = get_current_mode()
        
        # IPC is disabled in TANK_SEALED and TANK_BUFFERED modes
        self._offline_mode = not requires_ipc_services()
        
        # Also check legacy config flags for backward compatibility
        if not self._offline_mode:
            self._offline_mode = (
                bool(getattr(ipc_config, 'BABYSITTER_IPC_OFFLINE_MODE', False)) or
                not bool(getattr(ipc_config, 'ENABLE_IPC_DATA_DUMP', True))
            )
        
        # Step 2: If in offline mode, set everything to a safe, inert state and RETURN.
        if self._offline_mode:
            self.logger.critical("!!!!!!!!!! TANK MODE ACTIVATED !!!!!!!!!!")
            self.logger.critical("BulletproofIPCClient is in OFFLINE (TANK) mode. All IPC is disabled.")
            
            # Set all critical components to None or empty.
            self.zmq_context = None
            self.sockets = {}
            self.mmap_files = {}
            self.mmap_locks = {}
            self.worker_threads = {}
            self.data_queues = {}
            self.conditions = {}
            self._shutdown_event = threading.Event()
            self._shutdown_event.set()  # Ensure any potential loops would exit
            return  # <-- EXIT THE CONSTRUCTOR EARLY

        # --- ONLINE MODE LOGIC ---
        # If we reach here, we are guaranteed to be in ONLINE mode.
        # All the original initialization logic now lives inside this block.
        
        self.logger.info("BulletproofIPCClient is in ONLINE mode. Initializing ZMQ, mmap, and workers.")
        
        self.zmq_context = zmq_context
        self.base_address = getattr(ipc_config, 'babysitter_base_ipc_address', "tcp://127.0.0.1")
        
        self.sockets: Dict[str, Any] = {}
        self._socket_configs = {
            'trading': {'port': 5556, 'hwm': 5000, 'mmap_gb': 2.0},
            'system':  {'port': 5557, 'hwm': 10000, 'mmap_gb': 1.0},
            'bulk':    {'port': 5555, 'hwm': 50000, 'mmap_gb': 4.0}
        }
        
        try:
            self._connect_all_sockets()

            # --- Multi-Lane Resources ---
            queue_size = int(getattr(ipc_config, 'IPC_CLIENT_PREBUFFER_SIZE', 10000))
            self.data_queues = {name: Queue(maxsize=queue_size) for name in self._socket_configs}
            self.conditions = {name: threading.Condition() for name in self._socket_configs}
            self.worker_threads: Dict[str, threading.Thread] = {}
            
            # --- Mmap Resources (per-lane) ---
            self.mmap_files: Dict[str, Optional[mmap.mmap]] = {}
            self.mmap_locks: Dict[str, threading.Lock] = {name: threading.Lock() for name in self._socket_configs}
            self.mmap_data_area_size: Dict[str, int] = {}
            project_root = Path(__file__).parent.parent.resolve()
            self.mmap_base_path = project_root / getattr(ipc_config, 'IPC_MMAP_BUFFER_BASE_PATH', "data/ipc_mmap_buffers")
            self._initialize_all_mmap_buffers()

            self._shutdown_event = threading.Event()
            
            # Start worker threads only in ONLINE mode
            for name in self._socket_configs:
                thread = threading.Thread(target=self._worker_loop, args=(name,), name=f"IPC-Worker-{name}", daemon=True)
                self.worker_threads[name] = thread
                thread.start()
            
            self.logger.info("BulletproofIPCClient (Multi-Lane with Mmap) initialized successfully for ONLINE mode.")
            
        except Exception as e:
            self.logger.critical(f"FATAL: Failed to initialize ONLINE mode IPC client: {e}", exc_info=True)
            # As a final safety net, revert to offline mode if online init fails.
            self._offline_mode = True
            self.sockets = {}
            self.worker_threads = {}
            self.data_queues = {}
            self.conditions = {}
            self.mmap_files = {}
            self.mmap_locks = {}

    @property
    def offline_mode(self) -> bool:
        return self._offline_mode

    def is_healthy(self) -> bool:
        if self._offline_mode: return True
        return all(t.is_alive() for t in self.worker_threads.values()) and any(not s.closed for s in self.sockets.values())

    def _connect_all_sockets(self):
        for name, config in self._socket_configs.items():
            try:
                # Use PUSH socket for fire-and-forget data path (hot/cold path)
                _zmq = _get_zmq()
                socket = self.zmq_context.socket(_zmq.PUSH)
                # Timeouts and HWM: fail fast to buffer to mmap
                socket.setsockopt(_zmq.SNDTIMEO, 250)  # 250ms send timeout
                try:
                    socket.setsockopt(_zmq.SNDHWM, config['hwm'])
                except Exception:
                    pass
                socket.setsockopt(_zmq.LINGER, 0)
                socket.connect(f"{self.base_address}:{config['port']}")
                self.sockets[name] = socket
                self.logger.info(f"ZMQ PUSH socket '{name}' connected to {self.base_address}:{config['port']}")
            except Exception as e:
                self.logger.critical(f"Failed to connect ZMQ socket '{name}': {e}", exc_info=True)

    # --- Mmap Helper Methods (Re-introduced) ---
    def _initialize_all_mmap_buffers(self):
        self.mmap_base_path.mkdir(parents=True, exist_ok=True)
        for stype, config in self._socket_configs.items():
            mmap_file_path = self.mmap_base_path / f"buffer_{stype}.mmap"
            data_size_bytes = int(config['mmap_gb'] * 1024**3)
            total_file_size = MMAP_EFFECTIVE_HEADER_SIZE + data_size_bytes
            
            # Create/resize file if needed
            if not mmap_file_path.exists() or mmap_file_path.stat().st_size != total_file_size:
                with open(mmap_file_path, "wb") as f: 
                    f.truncate(total_file_size)
            
            # Open and map the file
            fd = os.open(mmap_file_path, os.O_RDWR)
            mmap_obj = mmap.mmap(fd, total_file_size, access=mmap.ACCESS_WRITE)
            os.close(fd) # We can close the descriptor after mapping
            self.mmap_files[stype] = mmap_obj
            
            # Initialize or validate header
            self.mmap_data_area_size[stype] = data_size_bytes
            header = self._read_mmap_header(stype)
            if not header or header.get('magic') != MMAP_HEADER_MAGIC_NUMBER or header.get('total_data_size') != data_size_bytes:
                new_header = {
                    'magic': MMAP_HEADER_MAGIC_NUMBER, 
                    'version': 1, 
                    'total_data_size': data_size_bytes, 
                    'write_ptr': 0, 
                    'read_ptr': 0, 
                    'message_count': 0
                }
                self._write_mmap_header(stype, new_header)
                self.logger.info(f"Initialized new mmap header for lane '{stype}'.")
            else:
                self.logger.info(f"Valid mmap header found for lane '{stype}'.")
    
    def _read_mmap_header(self, socket_type: str) -> Optional[Dict[str, Any]]:
        mmap_obj = self.mmap_files.get(socket_type)
        if not mmap_obj: return None
        try:
            header_bytes = mmap_obj[:MMAP_HEADER_SIZE_BYTES]
            unpacked = struct.unpack(MMAP_HEADER_FORMAT_STRING, header_bytes)
            return {desc[0]: val for desc, val in zip(MMAP_HEADER_LAYOUT, unpacked)}
        except Exception as e:
            self.logger.error(f"Error reading mmap header for '{socket_type}': {e}")
            return None

    def _write_mmap_header(self, socket_type: str, header_data: Dict[str, Any]):
        mmap_obj = self.mmap_files.get(socket_type)
        if not mmap_obj: return
        try:
            values = [header_data[desc[0]] for desc in MMAP_HEADER_LAYOUT]
            header_bytes = struct.pack(MMAP_HEADER_FORMAT_STRING, *values)
            mmap_obj[:MMAP_HEADER_SIZE_BYTES] = header_bytes
        except Exception as e:
            self.logger.error(f"Error writing mmap header for '{socket_type}': {e}")

    def send_data(self, target_redis_stream: str, data_json_string: str, explicit_socket_type: Optional[str] = None) -> bool:
        if self._offline_mode or self._shutdown_event.is_set():
            if self._offline_mode:
                self.logger.debug(f"TANK_MODE: IPC message for stream '{target_redis_stream}' discarded (offline mode).")
            return False

        socket_type = explicit_socket_type or self._determine_socket_type(target_redis_stream)
        
        if socket_type not in self.sockets:
            self.logger.error(f"Invalid socket type '{socket_type}'. Dropping message for stream '{target_redis_stream}'.")
            return False

        # Get the dedicated queue and condition for this lane
        lane_queue = self.data_queues[socket_type]
        lane_condition = self.conditions[socket_type]

        try:
            # Place message on the correct lane's queue
            lane_queue.put_nowait((target_redis_stream, data_json_string))
            
            # Notify the specific worker for this lane
            with lane_condition:
                lane_condition.notify()
            return True
        except Full:
            self.logger.error(f"IPC client's in-memory queue for lane '{socket_type}' is full. Message dropped.")
            return False

    # --- Main Worker and Buffer Logic ---

    def _worker_loop(self, socket_type: str):
        """A dedicated, event-driven worker for a single lane."""
        self.logger.info(f"IPC Worker for lane '{socket_type}' starting.")
        
        # Note: In TANK mode, worker threads are never started, so this code only runs in ONLINE mode
        lane_queue = self.data_queues[socket_type]
        lane_condition = self.conditions[socket_type]
        socket = self.sockets.get(socket_type)

        if not socket:
            self.logger.error(f"No socket for lane '{socket_type}'. Worker thread exiting.")
            return

        while not self._shutdown_event.is_set():
            messages_to_process = []
            try:
                # Wait for a notification that there's work to do
                with lane_condition:
                    if lane_queue.empty():
                        # Wait efficiently, also checking for disk replay periodically
                        lane_condition.wait(timeout=5.0)
                    
                    # Drain the entire queue to process as a batch
                    while not lane_queue.empty():
                        messages_to_process.append(lane_queue.get_nowait())

                # --- FAST PATH: Try to send all messages from the batch ---
                if messages_to_process:
                    print(f"!!!!!!!!!! IPC WORKER ('{socket_type}'): Woke up, processing batch of {len(messages_to_process)}.")

                    for stream, data in messages_to_process:
                        try:
                            # PUSH pattern: fire-and-forget; rely on SNDTIMEO to fail fast and buffer
                            socket.send_multipart([stream.encode(), data.encode()])
                        except Exception as e:
                            # Any send failure (e.g., peer down or HWM) → buffer to disk
                            self.logger.warning(f"ZMQ PUSH send failed on lane '{socket_type}' (Error: {e}). Buffering to disk.")
                            print(f"!!!!!!!!!! IPC WORKER ('{socket_type}'): ZMQ PUSH FAILED (Error: {e}). Buffering to disk. !!!!!!!!!!")
                            self._add_to_mmap_buffer(socket_type, stream, data)
                
                # --- RECOVERY PATH: Always check for disk replay ---
                # Do this outside the main batch processing, especially when the queue was empty
                self._replay_from_mmap(socket_type)

            except Empty:
                # This can happen if the queue is emptied by another part of the code, which is fine.
                # Also check for disk replay here.
                self._replay_from_mmap(socket_type)
                continue
            except Exception as e:
                self.logger.critical(f"FATAL ERROR in IPC Worker thread for lane '{socket_type}': {e}", exc_info=True)
                time.sleep(5) # Prevent a tight crash loop

        self.logger.info(f"IPC Worker for lane '{socket_type}' stopped.")

    def _add_to_mmap_buffer(self, socket_type: str, stream: str, data: str):
        """Writes a message to the correct mmap circular buffer."""
        # *** THE FINAL DIAGNOSTIC ***
        print(f"!!!!!!!!!! _add_to_mmap_buffer CALLED for lane '{socket_type}'. Path: {self.mmap_base_path} !!!!!!!!!!")
        
        try:
            with self.mmap_locks[socket_type]:
                header = self._read_mmap_header(socket_type)
                if not header:
                    print(f"!!!!!!!!!! _add_to_mmap_buffer FAILED: No valid header for lane '{socket_type}' !!!!!!!!!!")
                    return
                
                # Serialize the message
                stream_bytes = stream.encode('utf-8')
                data_bytes = data.encode('utf-8')
                
                # Calculate message frame size
                total_len = MSG_FRAME_TOTAL_LEN_BYTES + MSG_FRAME_STREAM_LEN_BYTES + len(stream_bytes) + MSG_FRAME_DATA_LEN_BYTES + len(data_bytes)
                
                # Check if we have space (simple check, more complex logic would handle wrapping)
                data_area_size = self.mmap_data_area_size[socket_type]
                if total_len > data_area_size:
                    print(f"!!!!!!!!!! _add_to_mmap_buffer FAILED: Message too large for lane '{socket_type}' !!!!!!!!!!")
                    self.logger.error(f"Message too large for mmap buffer in lane '{socket_type}'")
                    return
                
                # Write the message to the mmap buffer
                mmap_obj = self.mmap_files[socket_type]
                write_offset = MMAP_EFFECTIVE_HEADER_SIZE + header['write_ptr']
                
                # Write message frame
                mmap_obj[write_offset:write_offset + MSG_FRAME_TOTAL_LEN_BYTES] = struct.pack('<I', total_len)
                write_offset += MSG_FRAME_TOTAL_LEN_BYTES
                
                mmap_obj[write_offset:write_offset + MSG_FRAME_STREAM_LEN_BYTES] = struct.pack('<H', len(stream_bytes))
                write_offset += MSG_FRAME_STREAM_LEN_BYTES
                
                mmap_obj[write_offset:write_offset + len(stream_bytes)] = stream_bytes
                write_offset += len(stream_bytes)
                
                mmap_obj[write_offset:write_offset + MSG_FRAME_DATA_LEN_BYTES] = struct.pack('<I', len(data_bytes))
                write_offset += MSG_FRAME_DATA_LEN_BYTES
                
                mmap_obj[write_offset:write_offset + len(data_bytes)] = data_bytes
                
                # Update header
                header['write_ptr'] = (header['write_ptr'] + total_len) % data_area_size
                header['message_count'] += 1
                self._write_mmap_header(socket_type, header)
                
                print(f"!!!!!!!!!! _add_to_mmap_buffer SUCCEEDED for lane '{socket_type}'. Message buffered to mmap. !!!!!!!!!!")
                
        except Exception as e:
            print(f"!!!!!!!!!! _add_to_mmap_buffer FAILED WITH EXCEPTION: {e} !!!!!!!!!!")
            self.logger.critical(f"CRITICAL: Failed to buffer message to mmap for lane '{socket_type}': {e}", exc_info=True)

    def _replay_from_mmap(self, socket_type: str):
        """Reads one message from the mmap buffer and tries to send it with proper REQ/REP handling."""
        with self.mmap_locks[socket_type]:
            header = self._read_mmap_header(socket_type)
            if not header or header['message_count'] == 0:
                return # Nothing to replay

            # Read the message from read_ptr
            message_tuple = self._read_one_message_from_mmap(socket_type, header)
            if not message_tuple:
                return

            stream, data = message_tuple
            socket = self.sockets.get(socket_type)
            try:
                # Send the replayed message
                socket.send_multipart([stream.encode(), data.encode()])
                
                # *** THE CRITICAL FIX IS HERE ***
                # We MUST wait for the reply to reset the socket's state.
                reply = socket.recv()
                
                # Success! Update header to reflect the sent message
                message_len = self._get_message_len(stream, data)
                header['read_ptr'] = (header['read_ptr'] + message_len) % self.mmap_data_area_size[socket_type]
                header['message_count'] -= 1
                self._write_mmap_header(socket_type, header)
                print(f"!!!!!!!!!! IPC REPLAY ('{socket_type}'): Replayed one message from mmap. !!!!!!!!!!")
            except Exception as e:
                # THE FIX: If any error occurs, assume the socket is stale.
                # Force a disconnect and reconnect to clear any bad state.
                self.logger.warning(f"Replay for lane '{socket_type}' failed: {e}. Forcing socket reconnection.")
                print(f"!!!!!!!!!! IPC REPLAY ('{socket_type}'): FAILED. Forcing socket reconnection. !!!!!!!!!!")
                
                # Close the old socket
                if socket:
                    socket.close(linger=0)
                
                # Create a brand new socket and connect
                config = self._socket_configs[socket_type]
                _zmq = _get_zmq()
                new_socket = self.zmq_context.socket(_zmq.REQ)
                new_socket.setsockopt(_zmq.SNDTIMEO, 250)
                new_socket.setsockopt(_zmq.RCVTIMEO, 250)
                new_socket.setsockopt(_zmq.LINGER, 0)
                new_socket.connect(f"{self.base_address}:{config['port']}")
                self.sockets[socket_type] = new_socket
                
                # The message was not sent, so it remains in the mmap buffer
                # to be re-attempted on the next cycle with the new socket.
                return

    def _read_one_message_from_mmap(self, socket_type: str, header: Dict[str, Any]) -> Optional[tuple]:
        """Reads one message from the mmap buffer at the read pointer."""
        try:
            mmap_obj = self.mmap_files[socket_type]
            read_offset = MMAP_EFFECTIVE_HEADER_SIZE + header['read_ptr']
            
            # Read total length
            total_len_bytes = mmap_obj[read_offset:read_offset + MSG_FRAME_TOTAL_LEN_BYTES]
            total_len = struct.unpack('<I', total_len_bytes)[0]
            read_offset += MSG_FRAME_TOTAL_LEN_BYTES
            
            # Read stream length
            stream_len_bytes = mmap_obj[read_offset:read_offset + MSG_FRAME_STREAM_LEN_BYTES]
            stream_len = struct.unpack('<H', stream_len_bytes)[0]
            read_offset += MSG_FRAME_STREAM_LEN_BYTES
            
            # Read stream
            stream_bytes = mmap_obj[read_offset:read_offset + stream_len]
            stream = stream_bytes.decode('utf-8')
            read_offset += stream_len
            
            # Read data length
            data_len_bytes = mmap_obj[read_offset:read_offset + MSG_FRAME_DATA_LEN_BYTES]
            data_len = struct.unpack('<I', data_len_bytes)[0]
            read_offset += MSG_FRAME_DATA_LEN_BYTES
            
            # Read data
            data_bytes = mmap_obj[read_offset:read_offset + data_len]
            data = data_bytes.decode('utf-8')
            
            return (stream, data)
            
        except Exception as e:
            self.logger.error(f"Error reading message from mmap buffer for lane '{socket_type}': {e}")
            return None

    def _get_message_len(self, stream: str, data: str) -> int:
        """Calculate the total length of a message frame."""
        stream_bytes = stream.encode('utf-8')
        data_bytes = data.encode('utf-8')
        return MSG_FRAME_TOTAL_LEN_BYTES + MSG_FRAME_STREAM_LEN_BYTES + len(stream_bytes) + MSG_FRAME_DATA_LEN_BYTES + len(data_bytes)

    def _determine_socket_type(self, target_redis_stream: str) -> str:
        stream_lower = target_redis_stream.lower()
        if any(keyword in stream_lower for keyword in ['ocr', 'image', 'grab', 'frame', 'price', 'market', 'quote', 'trade', 'raw-ocr']):
            return 'bulk'
        elif any(keyword in stream_lower for keyword in ['position', 'order', 'fill', 'pnl', 'validated-order', 'risk-actions']):
            return 'trading'
        return 'system'
        
    def close(self):
        self.logger.info("Closing Multi-Lane IPC Client with Mmap...")
        
        if self._offline_mode:
            self.logger.info("IPC Client was in TANK mode - no cleanup needed.")
            return
        
        self._shutdown_event.set()
        # Wake up all threads
        for cond in self.conditions.values():
            with cond:
                cond.notify_all()
        # Join all threads
        for thread in self.worker_threads.values():
            thread.join(timeout=2.0)
        # Close sockets
        for socket in self.sockets.values():
            socket.close(linger=0)
        # Close mmap files
        for mmap_obj in self.mmap_files.values():
            if mmap_obj:
                mmap_obj.close()
        self.logger.info("Multi-Lane IPC Client with Mmap closed.")

    # Interface compliance methods
    def get_ipc_buffer_stats(self): 
        if self._offline_mode:
            return {"mode": "offline", "stats": "disabled"}
        
        stats = {name: q.qsize() for name, q in self.data_queues.items()}
        # Add mmap stats
        for name in self._socket_configs:
            header = self._read_mmap_header(name)
            if header:
                stats[f"{name}_mmap_count"] = header['message_count']
        return stats
    
    def get_total_emergency_buffer_size(self): 
        if self._offline_mode:
            return 0
        
        total = 0
        for name in self._socket_configs:
            header = self._read_mmap_header(name)
            if header:
                total += header['message_count']
        return total
    
    def send_trading_data(self, stream, data, **kwargs): 
        if self._offline_mode:
            self.logger.debug(f"TANK_MODE: Trading data for stream '{stream}' discarded (offline mode).")
            return False
        return self.send_data(stream, data, explicit_socket_type='trading')
        
    def publish_to_babysitter(self, target_redis_stream: str, data_dict: Dict[str, Any], 
                             explicit_socket_type: Optional[str] = None) -> bool:
        """Compatibility method that converts dict to JSON string."""
        if self._offline_mode:
            self.logger.debug(f"TANK_MODE: Babysitter publish for stream '{target_redis_stream}' discarded (offline mode).")
            return False
        
        try:
            data_json_string = json.dumps(data_dict)
            return self.send_data(target_redis_stream, data_json_string, explicit_socket_type)
        except Exception as e:
            self.logger.error(f"Failed to serialize data for stream '{target_redis_stream}': {e}")
            return False
