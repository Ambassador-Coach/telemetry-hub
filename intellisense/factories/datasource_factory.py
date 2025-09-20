"""
Concrete Data Source Factory Implementation

Factory for creating data source implementations based on operation mode and configuration.
Handles both live and IntelliSense replay data sources.
"""

import logging
from typing import Optional, Any, TYPE_CHECKING # Added TYPE_CHECKING

# Core IntelliSense Types & Enums
from intellisense.core.enums import OperationMode
from intellisense.core.types import IntelliSenseConfig, TestSessionConfig
from intellisense.core.interfaces import IOCRDataSource, IPriceDataSource, IBrokerDataSource
from intellisense.core.exceptions import IntelliSenseInitializationError

# Interface for the factory
from .interfaces import IDataSourceFactory # Assuming this is intellisense.factories.interfaces

if TYPE_CHECKING:
    from intellisense.core.application_core_intellisense import IntelliSenseApplicationCore as ApplicationCoreType
    # Or if it can take the base ApplicationCore:
    # from main_application.application_core import ApplicationCore as ApplicationCoreType

    # Concrete Data Source Implementations for type hinting factory's knowledge,
    # though the factory methods return interfaces.
    from intellisense.engines.datasources import (
        LiveOCRDataSource, IntelliSenseOCRReplaySource,
        LivePriceDataSource, IntelliSensePriceReplaySource,
        LiveBrokerDataSource, IntelliSenseBrokerReplaySource
    )
else:
    ApplicationCoreType = Any # Runtime fallback for ApplicationCore type hint

# Runtime imports of concrete data source implementations needed for instantiation
from intellisense.engines.datasources import (
    LiveOCRDataSource, IntelliSenseOCRReplaySource,
    LivePriceDataSource, IntelliSensePriceReplaySource,
    LiveBrokerDataSource, IntelliSenseBrokerReplaySource
)

logger = logging.getLogger(__name__)


class DataSourceFactory(IDataSourceFactory):
    """
    Factory for creating data source implementations based on operation mode and configuration.
    """
    
    def __init__(self,
                 app_core: 'ApplicationCoreType',  # Use string hint or alias from TYPE_CHECKING
                 intellisense_config: Optional[IntelliSenseConfig] = None):  # IntelliSenseConfig for replay sources
        """
        Initialize the data source factory.
        
        Args:
            app_core: Application core instance for accessing live sources
            intellisense_config: IntelliSense configuration containing data source overrides
        """
        self.app_core = app_core
        self.intellisense_config = intellisense_config
        
        # Determine operation mode from config
        if intellisense_config is not None:
            self.mode = OperationMode.INTELLISENSE
        else:
            self.mode = OperationMode.LIVE
        
        if self.mode == OperationMode.INTELLISENSE and self.intellisense_config is None:
            raise IntelliSenseInitializationError("IntelliSenseConfig is required for INTELLISENSE mode in DataSourceFactory.")
        
        logger.info(f"DataSourceFactory initialized. Mode: {self.mode.name}")

    def create_ocr_data_source(self, session_config: TestSessionConfig) -> Optional[IOCRDataSource]:
        """
        Create OCR data source based on factory mode and session configuration.

        Args:
            session_config: Test session configuration containing data paths and settings

        Returns:
            OCR data source instance or None if creation fails
        """
        try:
            if self.mode == OperationMode.LIVE:
                logger.info("Creating LiveOCRDataSource.")
                return LiveOCRDataSource(self.app_core)

            elif self.mode == OperationMode.INTELLISENSE:
                logger.info(f"Creating IntelliSenseOCRReplaySource from: {session_config.controlled_trade_log_path}")

                if session_config.controlled_trade_log_path:
                    return IntelliSenseOCRReplaySource(
                        replay_file_path=session_config.controlled_trade_log_path,
                        ocr_config=session_config.ocr_config,
                        session_config=session_config
                    )
                else:
                    logger.error("OCR replay path not found in session config.")
                    return None

        except Exception as e:
            logger.error(f"Failed to create OCR data source: {e}")
            return None

        logger.error(f"Could not create OCR data source for mode {self.mode.name}.")
        return None

    def create_price_data_source(self, session_config: TestSessionConfig) -> Optional[IPriceDataSource]:
        """
        Create price data source. For IntelliSense, this usually means a replay source.

        Args:
            session_config: Test session configuration containing data paths and settings

        Returns:
            Price data source instance or None if creation fails
        """
        try:
            if self.mode == OperationMode.LIVE:
                logger.info("Creating LivePriceDataSource.")
                return LivePriceDataSource(self.app_core)

            elif self.mode == OperationMode.INTELLISENSE:
                logger.info(f"Creating IntelliSensePriceReplaySource (sync feed) from: {session_config.historical_price_data_path_sync}")

                if session_config.historical_price_data_path_sync:
                    return IntelliSensePriceReplaySource(session_config, feed_type="sync")
                else:
                    logger.error("Sync price replay path not found in session config.")
                    return None

        except Exception as e:
            logger.error(f"Failed to create Price data source: {e}")
            return None

        logger.error(f"Could not create Price data source for mode {self.mode.name}.")
        return None

    def create_broker_data_source(self, session_config: TestSessionConfig) -> Optional[IBrokerDataSource]:
        """
        Create broker data source.

        Args:
            session_config: Test session configuration containing data paths and settings

        Returns:
            Broker data source instance or None if creation fails
        """
        try:
            if self.mode == OperationMode.LIVE:
                logger.info("Creating LiveBrokerDataSource.")
                return LiveBrokerDataSource(self.app_core)

            elif self.mode == OperationMode.INTELLISENSE:
                logger.info(f"Creating IntelliSenseBrokerReplaySource from: {session_config.broker_response_log_path}")

                if session_config.broker_response_log_path:
                    return IntelliSenseBrokerReplaySource(
                        response_log_path=session_config.broker_response_log_path,
                        broker_config=session_config.broker_config
                    )
                else:
                    logger.error("Broker response log path not found in session config.")
                    return None

        except Exception as e:
            logger.error(f"Failed to create Broker data source: {e}")
            return None

        logger.error(f"Could not create Broker data source for mode {self.mode.name}.")
        return None
