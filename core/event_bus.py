import threading
import logging
import time
import weakref
from collections import deque, defaultdict
from typing import Callable, Type, Dict, Optional, Any

from interfaces.core.services import IEventBus
from core.events import BaseEvent

logger = logging.getLogger(__name__)

class EventBus(IEventBus):
    def __init__(self, max_queue_size: int = 10000, benchmarker: Optional[Any] = None):
        self._subscribers: Dict[Type[BaseEvent], weakref.WeakSet[Callable[[BaseEvent], None]]] = defaultdict(weakref.WeakSet)
        self._queue: deque[BaseEvent] = deque(maxlen=max_queue_size)
        self._lock = threading.RLock()  # Used to protect _queue and _subscribers - RLock for re-entrant safety
        self._condition = threading.Condition(self._lock) # Used to signal new events or shutdown
        self._processing_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._benchmarker = benchmarker

    def subscribe(self, event_type: Type[BaseEvent], handler: Callable[[BaseEvent], None]) -> None:
        with self._lock:
            # WeakSet automatically handles duplicates, so we can just add
            initial_size = len(self._subscribers[event_type])
            self._subscribers[event_type].add(handler)

            # Enhanced ID tracking for handler matching verification
            handler_name = getattr(handler, '__name__', str(handler))
            handler_id = id(handler)

            if len(self._subscribers[event_type]) > initial_size:
                logger.debug(f"Handler {handler_name} subscribed to {event_type.__name__}")
                logger.critical(f"HANDLER_ID_TRACKING_SUBSCRIBE: Event={event_type.__name__}, Handler={handler_name}, ID={handler_id}, Action=ADDED")

                # Special logging for ValidatedOrderRequestEvent
                if event_type.__name__ == 'ValidatedOrderRequestEvent':
                    logger.critical(f"EVENTBUS_SUBSCRIBE_DEBUG: ValidatedOrderRequestEvent handler added: {handler_name}")
                    logger.critical(f"EVENTBUS_SUBSCRIBE_DEBUG: Total ValidatedOrderRequestEvent handlers: {len(self._subscribers[event_type])}")
            else:
                logger.debug(f"Handler {handler_name} already subscribed to {event_type.__name__}")
                logger.critical(f"HANDLER_ID_TRACKING_SUBSCRIBE: Event={event_type.__name__}, Handler={handler_name}, ID={handler_id}, Action=ALREADY_EXISTS")

    def unsubscribe(self, event_type: Type[BaseEvent], handler: Callable[[BaseEvent], None]) -> None:
        with self._lock:
            try:
                # Enhanced ID tracking for handler matching verification
                handler_name = getattr(handler, '__name__', str(handler))
                handler_id = id(handler)

                # Check if handler exists before removal
                handler_exists = handler in self._subscribers[event_type]

                # WeakSet.discard() is safe if handler not present
                self._subscribers[event_type].discard(handler)

                if handler_exists:
                    logger.debug(f"Successfully unsubscribed handler {handler_name} from {event_type.__name__}")
                    logger.critical(f"HANDLER_ID_TRACKING_UNSUBSCRIBE: Event={event_type.__name__}, Handler={handler_name}, ID={handler_id}, Action=REMOVED")
                else:
                    logger.debug(f"Handler {handler_name} was not subscribed to {event_type.__name__}")
                    logger.critical(f"HANDLER_ID_TRACKING_UNSUBSCRIBE: Event={event_type.__name__}, Handler={handler_name}, ID={handler_id}, Action=NOT_FOUND")

            except KeyError:  # Should not happen with defaultdict, but good for safety
                logger.debug(f"Event type {event_type.__name__} not found in subscribers during discard.")
                logger.critical(f"HANDLER_ID_TRACKING_UNSUBSCRIBE: Event={event_type.__name__}, Handler=UNKNOWN, ID=UNKNOWN, Action=EVENT_TYPE_NOT_FOUND")


    def publish(self, event: BaseEvent) -> None:
        # Start timing for performance metrics
        publish_start_time = time.time()

        if self._stop_event.is_set():
            logger.warning(f"EventBus is stopping/stopped. Ignoring publish of event: {type(event).__name__}")
            return

        # --- AGENT_DEBUG: START ---
        event_type_name_publish = type(event).__name__
        event_id_publish = getattr(event, 'event_id', 'N/A')
        event_symbol_publish = getattr(event.data, 'symbol', 'NO_SYMBOL_ATTR') if hasattr(event, 'data') else 'NO_DATA_ATTR'
        logger.critical(f"AGENT_DEBUG_EB_PUBLISH_ENTRY: Attempting to publish Event Type: {event_type_name_publish}, Event ID: {event_id_publish}, Symbol (if any): {event_symbol_publish}, Event Type Object ID: {id(type(event))}")
        # --- AGENT_DEBUG: END ---

        with self._lock: # Acquire lock to modify the queue and notify
            if self._queue.maxlen is not None and len(self._queue) == self._queue.maxlen:
                # Deque is full AND has a maxlen. The append operation below will automatically
                # discard the leftmost (oldest) element. We log this fact.
                # We get the details of the event *about to be discarded* before it's gone.
                event_to_be_discarded_id = self._queue[0].event_id
                event_to_be_discarded_type = type(self._queue[0]).__name__
                logger.error(
                    f"EVENT DISCARDED (deque full): Oldest event {event_to_be_discarded_type} (ID: {event_to_be_discarded_id}) "
                    f"will be implicitly discarded by deque to make space for new event {type(event).__name__} (ID: {event.event_id}). "
                    f"Consider increasing max_queue_size if this happens frequently."
                )

            self._queue.append(event) # If maxlen is reached, append discards the item from the left end.

            # --- AGENT_DEBUG: START ---
            logger.critical(f"AGENT_DEBUG_EB_PUBLISH_APPENDED: Successfully appended Event Type: {event_type_name_publish}, Event ID: {event_id_publish}, Symbol: {event_symbol_publish} to queue. Queue size now: {len(self._queue)}")
            # --- AGENT_DEBUG: END ---

            # Enhanced logging for order-related events
            event_type = type(event).__name__
            if event_type in ['OrderRequestEvent', 'ValidatedOrderRequestEvent']:
                logger.debug(f"ORDER_PATH_DEBUG: Publishing {event_type} (ID: {event.event_id})")
                logger.debug(f"ORDER_PATH_DEBUG: Event data content: {event.data}")
            elif 'broker' in event_type.lower() or 'order' in event_type.lower():
                logger.debug(f"BROKER_EVENT_DEBUG: Publishing {event_type} (ID: {event.event_id})")
                logger.debug(f"BROKER_EVENT_DEBUG: Event data content: {event.data}")
            else:
                # Standard logging for other events
                try:
                    # Attempt to get a string representation. Dataclasses usually provide a good one.
                    payload_str = str(event)
                except Exception:
                    # Fallback if str(event) fails for some reason
                    payload_str = f"Instance of {event_type}"

                if len(payload_str) > 150:
                    payload_str = payload_str[:150] + "..."

                logger.debug(f"Event {event_type} (ID: {event.event_id}) published. Content: {payload_str}")
            self._condition.notify() # Signal the processing thread that a new event is available

        # Capture EventBus performance metrics
        if self._benchmarker:
            publish_duration_ms = (time.time() - publish_start_time) * 1000
            self._benchmarker.capture_metric(
                "event_bus.publish_latency_ms",
                publish_duration_ms,
                context={'event_type': type(event).__name__, 'event_id': getattr(event, 'event_id', 'N/A')}
            )
            self._benchmarker.capture_metric(
                "event_bus.events_published_count",
                1.0,
                context={'event_type': type(event).__name__}
            )

    def _process_events(self) -> None:
        logger.info("EventBus processing thread started.")
        # --- AGENT_DEBUG: START - TODO: REMOVE WHEN DEBUGGING COMPLETE ---
        thread_name = threading.current_thread().name
        thread_id = threading.current_thread().ident
        logger.critical(f"[EB_LIFECYCLE] EventBus _process_events LOOP ENTERED by thread ({thread_name}, ID: {thread_id}).")
        # --- AGENT_DEBUG: END - TODO: REMOVE WHEN DEBUGGING COMPLETE ---
        STALE_EVENT_THRESHOLD_SECONDS = 1.0 # 1000ms stale threshold - make configurable if needed

        while not self._stop_event.is_set():
            # Periodic logging for EventBus monitoring (roughly every 5 seconds)
            current_time = time.time()
            if not hasattr(self, '_last_log_time_eb') or (current_time - self._last_log_time_eb > 5.0):
                q_size = self.get_queue_size()
                logger.info(f"EventBus Status: Current queue size = {q_size}")

                # Capture EventBus queue size metric
                if self._benchmarker:
                    self._benchmarker.capture_metric(
                        "event_bus.queue_size_current",
                        float(q_size),
                        context={'max_queue_size': self._queue.maxlen}
                    )

                self._last_log_time_eb = current_time

            batch = []
            with self._lock: # Acquire lock to check/wait for events and get them
                # Wait until there's something in the queue or stop_event is set
                while not self._queue and not self._stop_event.is_set():
                    self._condition.wait(timeout=0.1) # Wait with a timeout to re-check stop_event

                if self._stop_event.is_set(): # Check again after waking up
                    break

                # Debug logging for queue processing
                if self._queue:
                    logger.info(f" EVENT_BUS_PROCESS: Queue size before pop: {len(self._queue)}")

                # Pop all current events from the queue into a batch
                while self._queue:
                    event = self._queue.popleft()
                    batch.append(event)
                    # Special logging for PositionUpdateEvent
                    if type(event).__name__ == 'PositionUpdateEvent':
                        logger.info(f" EVENT_BUS_PROCESS: Found PositionUpdateEvent in queue (ID: {event.event_id})")

            if not batch and self._stop_event.is_set(): # If woken by stop and batch is empty
                 break

            if batch:
                logger.info(f" EVENT_BUS_PROCESS: Processing batch of {len(batch)} events.")

                # --- AGENT_DEBUG: START ---
                logger.critical(f"AGENT_DEBUG_EB_BATCH_CONTENT: Batch (size {len(batch)}) contains event types: {[type(e).__name__ for e in batch]}, Event IDs: {[getattr(e, 'event_id', 'N/A') for e in batch]}")
                for idx, evt_in_batch in enumerate(batch):
                    evt_type_debug_batch = type(evt_in_batch).__name__
                    evt_id_debug_batch = getattr(evt_in_batch, 'event_id', 'N/A')
                    evt_symbol_debug_batch = getattr(evt_in_batch.data, 'symbol', 'NO_SYM') if hasattr(evt_in_batch, 'data') else 'NO_DATA'
                    logger.critical(f"AGENT_DEBUG_EB_BATCH_ITEM_{idx}: Type={evt_type_debug_batch}, ID={evt_id_debug_batch}, Symbol={evt_symbol_debug_batch}")
                # --- AGENT_DEBUG: END ---

                # Special logging for PositionUpdateEvent in batch
                for event in batch:
                    if type(event).__name__ == 'PositionUpdateEvent':
                        logger.info(f" EVENT_BUS_PROCESS: About to dispatch PositionUpdateEvent (ID: {event.event_id})")
                current_time = time.time() # Get current time once for the batch

                for event in batch:
                    # --- AGENT_DEBUG: START ---
                    event_type_name_process = type(event).__name__
                    event_id_process = getattr(event, 'event_id', 'N/A')
                    event_symbol_process = "NO_SYMBOL"
                    if hasattr(event, 'data') and event.data is not None and hasattr(event.data, 'symbol') and event.data.symbol is not None:
                        event_symbol_process = event.data.symbol
                    elif hasattr(event, 'symbol') and event.symbol is not None: # Some events might have symbol directly
                        event_symbol_process = event.symbol

                    # Extract correlation ID if available
                    correlation_id = None
                    if hasattr(event, 'data') and event.data:
                        if hasattr(event.data, 'correlation_id'):
                            correlation_id = event.data.correlation_id
                        elif hasattr(event.data, 'origin_correlation_id'):
                            correlation_id = event.data.origin_correlation_id
                        elif hasattr(event.data, 'master_correlation_id'):
                            correlation_id = event.data.master_correlation_id
                    
                    logger.critical(
                        f"AGENT_DEBUG_EB_PROCESS_EVENT: Processing Event Type: {event_type_name_process}, "
                        f"Symbol: {event_symbol_process}, Event ID: {event_id_process}, "
                        f"EventTypeID: {id(type(event))}, Timestamp: {event.timestamp}, "
                        f"CorrID: {correlation_id}"
                    )
                    # --- AGENT_DEBUG: END ---

                    # --- BEGIN STALE EVENT FILTERING ---
                    if (current_time - event.timestamp) > STALE_EVENT_THRESHOLD_SECONDS:
                        logger.warning(
                            f"STALE EVENT DISCARDED: Event {type(event).__name__} (ID: {event.event_id}) "
                            f"is {(current_time - event.timestamp)*1000:.2f}ms old (timestamp: {event.timestamp}, current: {current_time}), "
                            f"exceeding threshold of {STALE_EVENT_THRESHOLD_SECONDS*1000:.0f}ms. Discarding."
                        )
                        continue # Skip processing this stale event
                    # --- END STALE EVENT FILTERING ---

                    # Performance benchmarking: Start dispatch timing
                    dispatch_start_time = time.time()

                    # Debug logging disabled for market data events to reduce console spam
                    # Only log non-market-data events for debugging
                    event_type_name = type(event).__name__
                    if event_type_name not in ['MarketDataTickEvent', 'MarketDataQuoteEvent']:
                        logger.debug(f"DEBUG: Processing event {event_type_name} (ID: {event.event_id})")

                    # Get handlers for the specific event type
                    # No lock needed here for _subscribers read if we assume it's mostly read-heavy
                    # or changes are infrequent enough not to warrant locking during dispatch.
                    # However, for strict safety if handlers can un/subscribe during dispatch, locking _subscribers read is safer.
                    # Let's keep it simple for now: read outside lock, assuming handlers don't modify _subscribers.
                    # If frequent modifications or re-entrancy becomes an issue, this can be revisited.

                    # Create a temporary list of handlers to call, to avoid issues if a handler unsubscribes itself.
                    current_handlers = []
                    with self._lock: # Brief lock to copy handlers from WeakSet
                        specific_event_handlers = self._subscribers.get(type(event))
                        if specific_event_handlers:  # specific_event_handlers is a WeakSet
                            current_handlers.extend(list(specific_event_handlers))  # Convert to list for safe iteration

                        # Add BaseEvent handlers if any, ensuring not to add them if the event is BaseEvent itself
                        if type(event) is not BaseEvent:
                            base_event_handlers = self._subscribers.get(BaseEvent)
                            if base_event_handlers:  # base_event_handlers is a WeakSet
                                current_handlers.extend(list(base_event_handlers))  # Convert to list

                    # --- AGENT_DEBUG: START ---
                    # Re-fetch event_type_name_process and event_id_process if they went out of scope or for clarity
                    event_type_name_found = type(event).__name__
                    event_id_found = getattr(event, 'event_id', 'N/A')
                    event_symbol_found = "NO_SYMBOL"
                    if hasattr(event, 'data') and event.data is not None and hasattr(event.data, 'symbol') and event.data.symbol is not None:
                        event_symbol_found = event.data.symbol
                    elif hasattr(event, 'symbol') and event.symbol is not None:
                        event_symbol_found = event.symbol

                    handler_names_debug_found = [getattr(h, '__name__', str(h)) for h in current_handlers]
                    logger.critical(
                        f"AGENT_DEBUG_EB_FOUND_HANDLERS: For Event Type: {event_type_name_found}, "
                        f"Symbol: {event_symbol_found}, Event ID: {event_id_found}, "
                        f"Found {len(current_handlers)} handlers: {handler_names_debug_found}"
                    )
                    # --- AGENT_DEBUG: END ---

                    # Special logging for ValidatedOrderRequestEvent
                    if type(event).__name__ == 'ValidatedOrderRequestEvent':
                        logger.critical(f"EVENTBUS_DISPATCH_DEBUG: ValidatedOrderRequestEvent (ID: {event.event_id}) - Found {len(current_handlers)} handlers")
                        for i, handler in enumerate(current_handlers):
                            handler_name = getattr(handler, '__name__', str(handler))
                            logger.critical(f"EVENTBUS_DISPATCH_DEBUG: Handler {i+1}: {handler_name}")

                    if not current_handlers:
                        logger.debug(f"No handlers for event type {type(event).__name__} (ID: {event.event_id}) or BaseEvent.")
                        # Special warning for ValidatedOrderRequestEvent
                        if type(event).__name__ == 'ValidatedOrderRequestEvent':
                            logger.critical(f"EVENTBUS_DISPATCH_DEBUG: NO HANDLERS FOUND for ValidatedOrderRequestEvent (ID: {event.event_id})!")

                    # Enhanced logging for order-related event dispatching
                    event_type_name = type(event).__name__
                    if event_type_name in ['OrderRequestEvent', 'ValidatedOrderRequestEvent'] or 'order' in event_type_name.lower():
                        logger.debug(f"ORDER_PATH_DEBUG: Dispatching {event_type_name} (ID: {event.event_id}) to {len(current_handlers)} handler(s)")
                        for i, handler in enumerate(current_handlers):
                            handler_name = getattr(handler, '__name__', str(handler))
                            logger.debug(f"ORDER_PATH_DEBUG: Calling handler {i+1}/{len(current_handlers)}: {handler_name}")

                    for handler_idx, handler in enumerate(current_handlers): # Added enumerate for indexing
                        handler_name_call = getattr(handler, '__name__', str(handler))
                        handler_id_call = id(handler)
                        # Re-fetch event_type_name_process and event_id_process if needed
                        event_type_name_call = type(event).__name__
                        event_id_call = getattr(event, 'event_id', 'N/A')

                        # --- AGENT_DEBUG: START ---
                        logger.critical(
                            f"AGENT_DEBUG_EB_CALLING_HANDLER: For Event Type: {event_type_name_call}, "
                            f"Event ID: {event_id_call}, Calling handler {handler_idx + 1}/{len(current_handlers)}: {handler_name_call}"
                        )
                        # Enhanced handler ID tracking during dispatch
                        logger.critical(f"HANDLER_ID_TRACKING_DISPATCH: Event={event_type_name_call}, Handler={handler_name_call}, ID={handler_id_call}, Action=CALLING")
                        # --- AGENT_DEBUG: END ---

                        # --- AGENT_DEBUG & PROFILING: START ---
                        profiling_handler_start_time = time.perf_counter() # Use perf_counter for more precision
                        logger.critical(
                            f"[EB_HANDLER_PROFILE] START Calling handler: {handler_name_call} "
                            f"for Event Type: {event_type_name_call}, Event ID: {event_id_call}. "
                            f"Timestamp: {profiling_handler_start_time:.6f}"
                        )
                        # --- AGENT_DEBUG & PROFILING: END ---

                        # Original metric_name_handler_time and handler_start_time for benchmarker can remain
                        event_type_name = type(event).__name__
                        metric_name_handler_time = f"eventbus.handler_time.{event_type_name}.{handler_name_call}"
                        handler_start_time = time.time() # Keep separate if benchmarker uses time.time()

                        try:
                            handler(event)
                        except Exception as e:
                            logger.error(f"Event handler {getattr(handler, '__name__', str(handler))} failed for {type(event).__name__} (ID: {event.event_id}): {e}", exc_info=True)
                        finally:
                            # --- AGENT_DEBUG & PROFILING: START ---
                            profiling_handler_end_time = time.perf_counter()
                            profiling_duration_ms = (profiling_handler_end_time - profiling_handler_start_time) * 1000
                            logger.critical(
                                f"[EB_HANDLER_PROFILE] END Calling handler: {handler_name_call} "
                                f"for Event Type: {event_type_name_call}, Event ID: {event_id_call}. "
                                f"Duration: {profiling_duration_ms:.3f} ms. "
                                f"End Timestamp: {profiling_handler_end_time:.6f}"
                            )
                            # --- AGENT_DEBUG & PROFILING: END ---

                            # Original benchmarking code
                            handler_duration_ms = (time.time() - handler_start_time) * 1000
                            if self._benchmarker:
                                # Debug logging disabled for performance metrics to reduce console spam
                                self._benchmarker.capture_metric(metric_name_handler_time, handler_duration_ms, context={'event_id': event.event_id})

                    # Performance benchmarking: Capture dispatch metrics
                    dispatch_duration_ms = (time.time() - dispatch_start_time) * 1000  # Time to dispatch to ALL handlers for this event
                    if self._benchmarker:
                        self._benchmarker.capture_metric(f"eventbus.dispatch_time.{type(event).__name__}", dispatch_duration_ms, context={'event_id': event.event_id})
                        # Time event spent in queue before dispatch
                        queue_time_ms = (dispatch_start_time - event.timestamp) * 1000
                        if queue_time_ms >= 0:  # event.timestamp should be less than dispatch_start_time
                            self._benchmarker.capture_metric(f"eventbus.queue_time.{type(event).__name__}", queue_time_ms, context={'event_id': event.event_id})
            # No task_done needed for deque

        # --- AGENT_DEBUG: START - TODO: REMOVE WHEN DEBUGGING COMPLETE ---
        # This log will appear when the loop exits, either due to _stop_event or an unhandled break/exception within the loop.
        thread_name = threading.current_thread().name
        thread_id = threading.current_thread().ident
        logger.critical(f"[EB_LIFECYCLE] EventBus _process_events LOOP EXITED by thread ({thread_name}, ID: {thread_id}). Stop event set: {self._stop_event.is_set()}")
        # --- AGENT_DEBUG: END - TODO: REMOVE WHEN DEBUGGING COMPLETE ---
        logger.info("EventBus processing thread finished.")

    def start(self) -> None:
        if self._processing_thread and self._processing_thread.is_alive():
            logger.warning("EventBus processing thread already running.")
            return

        self._stop_event.clear()
        self._processing_thread = threading.Thread(target=self._process_events, name="EventBusThread", daemon=True)
        self._processing_thread.start()
        logger.info("EventBus processing thread initiated.")
        # --- AGENT_DEBUG: START - TODO: REMOVE WHEN DEBUGGING COMPLETE ---
        logger.critical(f"[EB_LIFECYCLE] EventBus processing thread ({self._processing_thread.name}, ID: {self._processing_thread.ident}) STARTED.")
        # --- AGENT_DEBUG: END - TODO: REMOVE WHEN DEBUGGING COMPLETE ---

    def stop(self, timeout: float = 2.0) -> None:
        logger.info("EventBus stop requested...")
        if self._stop_event.is_set():
            logger.info("EventBus already stopping or stopped.")
            return

        self._stop_event.set()
        with self._lock: # Acquire lock to notify condition for shutdown
            self._condition.notify_all() # Wake up the processing thread if it's waiting

        if self._processing_thread and self._processing_thread.is_alive():
            self._processing_thread.join(timeout=timeout)
            if self._processing_thread.is_alive():
                logger.warning("EventBus processing thread did not stop gracefully within timeout.")
            else:
                logger.info("EventBus processing thread joined successfully.")

        self._processing_thread = None
        # Clear the deque on stop to release any unprocessed events
        with self._lock:
            self._queue.clear()
        logger.info("EventBus stopped and queue cleared.")
        # --- AGENT_DEBUG: START - TODO: REMOVE WHEN DEBUGGING COMPLETE ---
        logger.critical(f"[EB_LIFECYCLE] EventBus stop() method COMPLETED.")
        # --- AGENT_DEBUG: END - TODO: REMOVE WHEN DEBUGGING COMPLETE ---

    def get_queue_size(self) -> int:
        """Returns the current number of events in the bus's main queue."""
        with self._lock:
            return len(self._queue)