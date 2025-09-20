#!/usr/bin/env python3
"""
Simple runner for TCS service
"""
import asyncio
import logging
import redis.asyncio as redis
from utils.global_config import GlobalConfig
from services.telemetry_correlation_service import TelemetryCorrelationService

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def main():
    logger.info("Initializing TCS runner...")
    
    # Create config and Redis client
    config = GlobalConfig()
    redis_client = await redis.from_url(
        f"redis://{config.REDIS_HOST}:{config.REDIS_PORT}/{config.REDIS_DB}"
    )
    
    # Create and start TCS
    tcs = TelemetryCorrelationService(config, redis_client)
    
    try:
        logger.info("Starting TCS service...")
        await tcs.start()
    except KeyboardInterrupt:
        logger.info("Shutting down TCS...")
        await tcs.stop()
    finally:
        await redis_client.close()

if __name__ == "__main__":
    asyncio.run(main())
