# --- START OF REGENERATED intellisense/capture/bootstrap.py (Chunk C.1 - Part 2) ---
import logging
import time
import threading
from typing import Optional, List, TYPE_CHECKING, Dict, Any

if TYPE_CHECKING:
    from intellisense.core.application_core_intellisense import IntelliSenseApplicationCore
    from .interfaces import IFeedbackIsolationManager # Using interface
    from .session import ProductionDataCaptureSession # For capture_session_ref type hint

logger = logging.getLogger(__name__)

class PrecisionBootstrap:
    """
    Manages the bootstrapping process for "CONTROLLED_INJECTION" mode.
    Ensures price feeds are active and optionally verifies OCR detection of a
    minimal trade before the main injection plan starts.
    """
    DEFAULT_PRICE_FEED_TIMEOUT_S = 30.0
    DEFAULT_PRICE_FEED_POLL_INTERVAL_S = 1.0
    DEFAULT_BOOTSTRAP_TRADE_OCR_TIMEOUT_S = 15.0
    DEFAULT_BOOTSTRAP_ORDER_QTY = 1.0 # Small quantity for bootstrap trade

    def __init__(self,
                 app_core: 'IntelliSenseApplicationCore',
                 feedback_isolator: 'IFeedbackIsolationManager', # Now using interface
                 capture_session_ref: 'ProductionDataCaptureSession'):
        self.app_core = app_core
        self.feedback_isolator = feedback_isolator # Used to mock around bootstrap symbol if needed
        self.capture_session = capture_session_ref # Reference to call register/unregister OCR monitor

        self._is_bootstrapped = False
        self._bootstrap_active = False
        self._bootstrap_lock = threading.Lock()

        # Get bootstrap account ID from config (via app_core or passed directly)
        # Assuming app_core.config has these attributes or a dedicated bootstrap config section
        self.bootstrap_account_id = getattr(self.app_core.config, 'intellisense_bootstrap_account_id', "BOOTSTRAP_ACCT")

        # Configurable parameters for bootstrap process
        cfg = getattr(self.app_core, 'config', None) # General config access
        self.price_feed_timeout_s = float(getattr(cfg, 'bootstrap_price_feed_timeout_s', self.DEFAULT_PRICE_FEED_TIMEOUT_S))
        self.price_feed_poll_interval_s = float(getattr(cfg, 'bootstrap_price_feed_poll_interval_s', self.DEFAULT_PRICE_FEED_POLL_INTERVAL_S))
        self.bootstrap_trade_ocr_timeout_s = float(getattr(cfg, 'bootstrap_trade_ocr_timeout_s', self.DEFAULT_BOOTSTRAP_TRADE_OCR_TIMEOUT_S))
        self.bootstrap_order_qty = float(getattr(cfg, 'bootstrap_order_qty', self.DEFAULT_BOOTSTRAP_ORDER_QTY))
        self.perform_ocr_verification_trade = bool(getattr(cfg, 'bootstrap_perform_ocr_verification_trade', True))


        logger.info(f"PrecisionBootstrap initialized. OCR Verification Trade: {self.perform_ocr_verification_trade}, "
                    f"Bootstrap Account: {self.bootstrap_account_id}, Order Qty: {self.bootstrap_order_qty}")
        logger.debug(f"Timeouts: PriceFeed={self.price_feed_timeout_s}s, OCRTrade={self.bootstrap_trade_ocr_timeout_s}s")


    def is_bootstrapped(self) -> bool:
        return self._is_bootstrapped

    def start_bootstrap_mode(self) -> None:
        """Activates bootstrap mode, preparing for bootstrapping actions."""
        with self._bootstrap_lock:
            self._bootstrap_active = True
            self._is_bootstrapped = False # Reset on start
            logger.info("PrecisionBootstrap mode STARTED.")
            # Potentially, set FIM to mock all *other* symbols if bootstrap trade needs clean OCR on its symbol
            # This is complex and depends on strategy; for now, FIM is managed by MillisecondOptimizationCapture plan.

    def stop_bootstrap_mode(self) -> None:
        """Deactivates bootstrap mode."""
        with self._bootstrap_lock:
            self._bootstrap_active = False
            logger.info("PrecisionBootstrap mode STOPPED.")
            # Cleanup any FIM mocks specifically set by bootstrap if any were.

    def bootstrap_system_sense(self, symbol_to_bootstrap: str) -> bool:
        """
        Ensures a specific symbol's price feed is active and, if configured,
        executes a minimal trade and verifies its OCR detection.
        This is a blocking call.
        """
        if not self._bootstrap_active:
            logger.warning(f"[{symbol_to_bootstrap}] Cannot bootstrap sense: Bootstrap mode not active.")
            return False

        with self._bootstrap_lock: # Ensure only one bootstrap operation at a time
            logger.info(f"[{symbol_to_bootstrap}] Starting system sense bootstrap process...")

            if not self._ensure_price_feed_active(symbol_to_bootstrap):
                logger.error(f"[{symbol_to_bootstrap}] Bootstrap FAILED: Price feed did not become active.")
                self._is_bootstrapped = False # Mark overall bootstrap as failed if critical part fails
                return False

            logger.info(f"[{symbol_to_bootstrap}] Price feed confirmed active.")

            if self.perform_ocr_verification_trade:
                logger.info(f"[{symbol_to_bootstrap}] Proceeding with OCR verification trade.")
                broker_order_id = self._execute_minimal_bootstrap_trade(symbol_to_bootstrap)
                if not broker_order_id:
                    logger.error(f"[{symbol_to_bootstrap}] Bootstrap FAILED: Could not execute minimal bootstrap trade for OCR verification.")
                    self._is_bootstrapped = False
                    return False

                logger.info(f"[{symbol_to_bootstrap}] Minimal bootstrap trade placed (Broker Order ID: {broker_order_id}). Waiting for OCR detection...")

                if not self._wait_for_ocr_detection(symbol_to_bootstrap):
                    logger.error(f"[{symbol_to_bootstrap}] Bootstrap FAILED: OCR did not detect effect of bootstrap trade within timeout.")
                    # Consider attempting to cancel the bootstrap order here if it's still potentially active
                    # self.app_core.broker_interface.cancel_order(broker_order_id) # Requires broker_order_id to be string/int
                    self._is_bootstrapped = False
                    return False
                logger.info(f"[{symbol_to_bootstrap}] OCR detection of bootstrap trade successful.")
            else:
                logger.info(f"[{symbol_to_bootstrap}] Skipping OCR verification trade as per configuration.")

            self._is_bootstrapped = True
            logger.info(f"[{symbol_to_bootstrap}] System sense bootstrap process COMPLETED successfully.")
            return True

    def bootstrap_symbol_feeds(self, symbols: List[str]) -> bool:
        """Ensures price feeds are active for a list of symbols."""
        if not self._bootstrap_active:
            logger.warning("Cannot bootstrap symbol feeds: Bootstrap mode not active.")
            return False

        all_successful = True
        logger.info(f"Bootstrapping price feeds for symbols: {symbols}")
        for symbol in symbols:
            if not self._ensure_price_feed_active(symbol):
                logger.error(f"Failed to activate price feed for {symbol} during multi-symbol bootstrap.")
                all_successful = False
                # Decide on behavior: continue with others or stop? For now, continue.

        if all_successful:
            logger.info("All specified symbol price feeds successfully bootstrapped.")
        else:
            logger.warning("One or more symbol price feeds failed to bootstrap.")
        return all_successful

    def _ensure_price_feed_active(self, symbol: str) -> bool:
        """Polls the PriceRepository until the feed for the symbol is active or timeout."""
        if not self.app_core.price_repository or not hasattr(self.app_core.price_repository, 'is_price_feed_active'):
            logger.error(f"[{symbol}] PriceRepository or is_price_feed_active method not available. Cannot ensure feed active.")
            return False

        start_time = time.time()
        logger.info(f"[{symbol}] Waiting for price feed to become active (timeout: {self.price_feed_timeout_s}s)...")
        while time.time() - start_time < self.price_feed_timeout_s:
            try:
                if self.app_core.price_repository.is_price_feed_active(symbol): # Uses EnhancedPriceRepository method
                    logger.info(f"[{symbol}] Price feed is now ACTIVE.")
                    return True
            except Exception as e:
                logger.error(f"[{symbol}] Error checking price feed status: {e}", exc_info=True)
                # If there's an error, likely best to consider it not active and let timeout handle it.

            logger.debug(f"[{symbol}] Price feed not yet active, polling again in {self.price_feed_poll_interval_s}s...")
            time.sleep(self.price_feed_poll_interval_s)

        logger.error(f"[{symbol}] TIMEOUT waiting for price feed to become active.")
        return False

    def _execute_minimal_bootstrap_trade(self, symbol: str) -> Optional[str]:
        """Executes a small trade on the bootstrap account to generate screen activity."""
        if not self.app_core.broker_interface or not hasattr(self.app_core.broker_interface, 'execute_bootstrap_trade'):
            logger.error(f"[{symbol}] BrokerInterface or execute_bootstrap_trade method not available.")
            return None
        try:
            # TODO: Determine side and order type more intelligently if needed, or make configurable.
            # For now, a simple MKT BUY.
            logger.info(f"[{symbol}] Executing minimal bootstrap trade: BUY {self.bootstrap_order_qty} MKT on account {self.bootstrap_account_id}")
            broker_order_id = self.app_core.broker_interface.execute_bootstrap_trade(
                symbol=symbol,
                side="BUY",
                quantity=int(self.bootstrap_order_qty), # Ensure integer quantity
                order_type="MKT",
                account=self.bootstrap_account_id
            )
            if broker_order_id:
                logger.info(f"[{symbol}] Bootstrap trade submitted. Broker Order ID: {broker_order_id}")
            else:
                logger.warning(f"[{symbol}] Bootstrap trade submitted, but no immediate broker order ID returned (may be async).")
            return broker_order_id # Can be None if not returned immediately
        except Exception as e:
            logger.error(f"[{symbol}] Failed to execute minimal bootstrap trade: {e}", exc_info=True)
            return None

    def _wait_for_ocr_detection(self, symbol: str) -> bool:
        """
        Registers an OCR monitor with ProductionDataCaptureSession and waits for
        the bootstrap trade's effect to be detected in the OCR stream.
        """
        if not hasattr(self.capture_session, 'register_bootstrap_ocr_monitor') or \
           not hasattr(self.capture_session, 'unregister_bootstrap_ocr_monitor'):
            logger.error(f"[{symbol}] ProductionDataCaptureSession missing OCR monitor registration methods.")
            return False

        detection_event = threading.Event()
        # Expected shares for OCR detection should match self.bootstrap_order_qty
        # The ProductionDataCaptureSession.on_ocr_frame_processed will look for this quantity.
        expected_shares_on_screen = self.bootstrap_order_qty

        try:
            self.capture_session.register_bootstrap_ocr_monitor(symbol, expected_shares_on_screen, detection_event)
            logger.info(f"[{symbol}] OCR monitor registered. Waiting for detection of ~{expected_shares_on_screen} shares (timeout: {self.bootstrap_trade_ocr_timeout_s}s)...")

            detected = detection_event.wait(timeout=self.bootstrap_trade_ocr_timeout_s)

            if detected:
                logger.info(f"[{symbol}] OCR detection successful: Bootstrap trade effect observed on screen.")
                return True
            else:
                logger.error(f"[{symbol}] TIMEOUT waiting for OCR detection of bootstrap trade effect.")
                return False
        except Exception as e:
            logger.error(f"[{symbol}] Error during OCR detection wait: {e}", exc_info=True)
            return False
        finally:
            self.capture_session.unregister_bootstrap_ocr_monitor(symbol)
            logger.debug(f"[{symbol}] OCR monitor unregistered.")

    def cleanup(self) -> None:
        """Cleanup resources or state if PrecisionBootstrap becomes more stateful."""
        logger.info("PrecisionBootstrap cleanup called.")
        self.stop_bootstrap_mode() # Ensure mode is stopped

# --- END OF REGENERATED intellisense/capture/bootstrap.py ---
