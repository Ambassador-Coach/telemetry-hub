import logging
from collections import defaultdict
from typing import Callable, Type, List, Any, Dict
import weakref # To prevent memory leaks from subscribers

logger = logging.getLogger(__name__)

class InternalIntelliSenseEventBus:
    def __init__(self):
        self._subscribers: Dict[Type[Any], weakref.WeakSet[Callable[[Any], None]]] = defaultdict(weakref.WeakSet)

    def subscribe(self, event_type: Type[Any], handler: Callable[[Any], None]):
        self._subscribers[event_type].add(handler)
        logger.debug(f"Handler {getattr(handler,'__name__','<unknown>')} subscribed to internal event {event_type.__name__}")

    def unsubscribe(self, event_type: Type[Any], handler: Callable[[Any], None]):
        self._subscribers[event_type].discard(handler)
        logger.debug(f"Handler {getattr(handler,'__name__','<unknown>')} unsubscribed from internal event {event_type.__name__}")

    def publish(self, event: Any):
        event_type = type(event)
        # Create a concrete list of handlers to call to avoid issues if a handler unsubscribes itself during iteration
        handlers_to_call = list(self._subscribers.get(event_type, weakref.WeakSet()))
        
        if not handlers_to_call:
            # logger.debug(f"No internal subscribers for event type {event_type.__name__}") # Can be noisy
            return

        # logger.debug(f"Publishing internal event {event_type.__name__} to {len(handlers_to_call)} subscribers.")
        for handler in handlers_to_call:
            try:
                handler(event)
            except Exception as e:
                logger.error(f"Error in internal event handler {getattr(handler,'__name__','<unknown>')} for {event_type.__name__}: {e}", exc_info=True)
