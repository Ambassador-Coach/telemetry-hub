# gui/services/stream_service.py

import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)

class StreamService:
    """
    Manages Redis stream consumption using the efficient XREAD command.
    Its sole responsibility is to fetch messages and delegate them to the
    appropriate handler in the HandlerRegistry.
    """
    
    def __init__(self, app_state, redis_service, handler_registry):
        self.app_state = app_state
        self.redis_service = redis_service
        self.handler_registry = handler_registry
        self.xread_task: Optional[asyncio.Task] = None
        self._running = False
        
    async def start_consumers(self):
        """Starts the main XREAD consumer loop."""
        if self.xread_task and not self.xread_task.done():
            logger.warning("XREAD consumer is already running.")
            return
            
        logger.info("Starting XREAD consumer task...")
        self._running = True
        self.xread_task = asyncio.create_task(self._xread_consumer_loop())
        logger.info("✅ XREAD consumer task started.")
    
    async def _xread_consumer_loop(self):
        """The main loop that continuously reads from multiple Redis streams."""
        # Get all stream names that the HandlerRegistry knows how to process.
        streams_to_watch = {
            stream_name: '$' for stream_name in self.handler_registry.get_all_stream_names()
        }
        
        if not streams_to_watch:
            logger.error("No streams are registered in HandlerRegistry. Consumer will not run.")
            return
            
        logger.info(f"XREAD consumer is now watching streams: {list(streams_to_watch.keys())}")

        while self._running:
            try:
                # Use the high-level xread method from our RedisService
                messages = await self.redis_service.xread(
                    streams=streams_to_watch,
                    count=100,
                    block=1000  # Block for up to 1 second
                )
                
                if messages:
                    
                    logger.debug(f"Received {len(messages)} messages from Redis streams.")
                    # Process all messages received in this batch
                    for stream_name, stream_messages in messages:
                        for message_id, data in stream_messages:
                            # Update the last ID for this stream so we don't process it again
                            streams_to_watch[stream_name] = message_id
                            # Delegate the message to the handler registry
                            await self.delegate_message(stream_name, message_id, data)

            except asyncio.CancelledError:
                logger.info("XREAD consumer loop cancelled.")
                break
            except Exception as e:
                logger.error(f"Error in XREAD consumer loop: {e}", exc_info=True)
                await asyncio.sleep(5)
                
        logger.info("XREAD consumer loop has stopped.")

    async def delegate_message(self, stream_name: str, message_id: str, data: dict):
        """Finds the correct handler and executes it, ensuring no crashes."""
        try:
            handler_func = self.handler_registry.get_handler(stream_name)
            if handler_func:
                # CORRECTED: handler_func is already a method of the handler_registry instance,
                # so 'self' is passed automatically. We only need to pass the other two arguments.
                await handler_func(message_id, data)
            else:
                logger.warning(f"No handler found for stream '{stream_name}'.")
        except Exception as e:
            logger.error(f"Error executing handler for stream '{stream_name}': {e}", exc_info=True)

    async def stop_all_consumers(self):
        """Stops the XREAD consumer loop gracefully."""
        self._running = False
        if self.xread_task and not self.xread_task.done():
            self.xread_task.cancel()
            try:
                await self.xread_task
            except asyncio.CancelledError:
                pass
        logger.info("✅ All stream consumers stopped.")