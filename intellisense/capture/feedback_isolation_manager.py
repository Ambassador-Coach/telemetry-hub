import logging
import copy # For deepcopying snapshot data to modify
from typing import Dict, Any, Optional, TYPE_CHECKING

if TYPE_CHECKING: # For type hinting IntelliSenseTestablePositionManager
    from intellisense.analysis.testable_components import IntelliSenseTestablePositionManager
    from intellisense.core.types import CleanedOCRSnapshotEventData # Actual type

from .interfaces import IFeedbackIsolationManager
from .feedback_isolation_types import PositionMockProfile, PositionState

logger = logging.getLogger(__name__)

class FeedbackIsolationManager(IFeedbackIsolationManager):
    """
    Concrete implementation of IFeedbackIsolationManager.
    Manages OCR output mocking and setting state on a testable PositionManager
    for "CONTROLLED_INJECTION" data capture mode.
    """
    def __init__(self, testable_position_manager: Optional['IntelliSenseTestablePositionManager']):
        self._testable_pm = testable_position_manager
        self._active_ocr_mocks: Dict[str, PositionMockProfile] = {} # symbol_upper -> PositionMockProfile

        if not self._testable_pm:
            logger.warning("FeedbackIsolationManager initialized without a TestablePositionManager. "
                           "PositionManager state control will be disabled.")
        elif not hasattr(self._testable_pm, 'force_set_internal_state') or \
             not hasattr(self._testable_pm, 'reset_forced_state') or \
             not hasattr(self._testable_pm, 'reset_all_forced_states'):
            logger.warning("Provided TestablePositionManager does not have expected methods "
                           "('force_set_internal_state', 'reset_forced_state', 'reset_all_forced_states'). "
                           "State control might be impaired.")

        logger.info("FeedbackIsolationManager initialized.")

    def activate_symbol_ocr_mock(self, symbol: str, mock_profile: PositionMockProfile) -> None:
        symbol_upper = symbol.upper()
        # Validate mock_profile structure (basic check)
        if not all(key in mock_profile for key in PositionMockProfile.__annotations__):
            logger.error(f"Cannot activate OCR mock for '{symbol_upper}': mock_profile is missing required keys. "
                         f"Expected: {list(PositionMockProfile.__annotations__.keys())}, Got: {list(mock_profile.keys())}")
            return
        self._active_ocr_mocks[symbol_upper] = mock_profile.copy() # Store a copy
        logger.info(f"FeedbackIsolationManager: Activated OCR mocking for symbol '{symbol_upper}'.")
        logger.debug(f"Mock profile for '{symbol_upper}': {mock_profile}")

    def deactivate_symbol_ocr_mock(self, symbol: str) -> None:
        symbol_upper = symbol.upper()
        if symbol_upper in self._active_ocr_mocks:
            del self._active_ocr_mocks[symbol_upper]
            logger.info(f"FeedbackIsolationManager: Deactivated OCR mocking for symbol '{symbol_upper}'.")

    def is_symbol_ocr_mocked(self, symbol: str) -> bool:
        return symbol.upper() in self._active_ocr_mocks

    def get_mocked_ocr_snapshot_data(
        self,
        symbol: str,
        actual_single_symbol_snapshot_data: Dict[str, Any] # This is one symbol's snapshot dict
    ) -> Dict[str, Any]:
        """
        Applies the mock profile to a single symbol's snapshot data if a mock is active.
        The returned dict should match the structure of CleanedOCRSnapshotEventData.snapshots[symbol].
        """
        symbol_upper = symbol.upper()
        mock_profile = self._active_ocr_mocks.get(symbol_upper)

        if not mock_profile:
            return actual_single_symbol_snapshot_data # No mock active, return original

        logger.debug(f"FeedbackIsolationManager: Applying OCR mock for symbol '{symbol_upper}'.")

        # Start with a copy of the actual data to preserve any fields not in the mock profile
        # but expected by downstream (e.g., ocr_confidence, specific text segments if any).
        mocked_symbol_snapshot = actual_single_symbol_snapshot_data.copy()

        # Override with fields from PositionMockProfile.
        # The keys in PositionMockProfile are:
        # strategy_hint, cost_basis, pnl_per_share, realized_pnl,
        # unrealized_pnl, total_shares, current_value.
        # These should align with keys expected in the snapshot dict.

        mocked_symbol_snapshot['strategy_hint'] = mock_profile['strategy_hint']
        mocked_symbol_snapshot['cost_basis'] = mock_profile['cost_basis']
        mocked_symbol_snapshot['pnl_per_share'] = mock_profile['pnl_per_share']
        mocked_symbol_snapshot['realized_pnl'] = mock_profile['realized_pnl']

        # These were in PositionMockProfile from SME spec, ensure they are handled if present
        if 'unrealized_pnl' in mock_profile: # Check if key exists in TypedDict
            mocked_symbol_snapshot['unrealized_pnl'] = mock_profile['unrealized_pnl']
        if 'total_shares' in mock_profile:
            mocked_symbol_snapshot['total_shares'] = mock_profile['total_shares']
        if 'current_value' in mock_profile:
            mocked_symbol_snapshot['current_value'] = mock_profile['current_value']

        # Ensure the symbol field in the snapshot itself is correct
        mocked_symbol_snapshot['symbol'] = symbol_upper

        logger.debug(f"FeedbackIsolationManager: Original snapshot for '{symbol_upper}': {actual_single_symbol_snapshot_data}")
        logger.debug(f"FeedbackIsolationManager: Mocked snapshot for '{symbol_upper}': {mocked_symbol_snapshot}")
        return mocked_symbol_snapshot

    def set_testable_position_manager_state(self, symbol: str, state: PositionState) -> bool:
        symbol_upper = symbol.upper()
        if not self._testable_pm:
            logger.warning(f"Cannot set PositionManager state for '{symbol_upper}': TestablePositionManager not provided.")
            return False

        # Validate state structure (basic check)
        if not all(key in state for key in PositionState.__annotations__):
            logger.error(f"Cannot set PM state for '{symbol_upper}': 'state' dict is missing required keys. "
                         f"Expected: {list(PositionState.__annotations__.keys())}, Got: {list(state.keys())}")
            return False

        if hasattr(self._testable_pm, 'force_set_internal_state'):
            try:
                logger.info(f"FeedbackIsolationManager: Setting TestablePositionManager state for '{symbol_upper}'.")
                logger.debug(f"State for '{symbol_upper}': {state}")
                return self._testable_pm.force_set_internal_state(symbol_upper, state)
            except Exception as e:
                logger.error(f"Error calling force_set_internal_state on TestablePositionManager for '{symbol_upper}': {e}", exc_info=True)
                return False
        else:
            logger.error("FeedbackIsolationManager: TestablePositionManager does not have 'force_set_internal_state' method.")
            return False

    def reset_all_mocking_and_states(self) -> None:
        self._active_ocr_mocks.clear()
        logger.info("FeedbackIsolationManager: All active OCR mocks cleared.")

        if self._testable_pm and hasattr(self._testable_pm, 'reset_all_forced_states'):
            try:
                self._testable_pm.reset_all_forced_states()
                logger.info("FeedbackIsolationManager: Requested TestablePositionManager to reset all forced states.")
            except Exception as e:
                logger.error(f"Error calling reset_all_forced_states on TestablePositionManager: {e}", exc_info=True)
        elif self._testable_pm:
            logger.warning("FeedbackIsolationManager: TestablePositionManager provided but lacks 'reset_all_forced_states' method.")
