import logging
import time
from typing import List, Dict, Any, Optional, TYPE_CHECKING # Added TYPE_CHECKING
from dataclasses import asdict

from intellisense.core.interfaces import IBrokerIntelligenceEngine, IBrokerDataSource, IEventBus
from intellisense.core.types import BrokerTimelineEvent, BrokerFillData, BrokerAckData, BrokerRejectionData, BrokerCancelAckData, BrokerOtherData
from intellisense.config.session_config import BrokerTestConfig, TestSessionConfig
from intellisense.factories.interfaces import IDataSourceFactory
from intellisense.analysis.broker_results import BrokerAnalysisResult, BrokerValidationResult, OrderRepositoryValidation, PositionManagerValidation

if TYPE_CHECKING:
    from intellisense.analysis.testable_components import IntelliSenseTestableOrderRepository, IntelliSenseTestablePositionManager
    # When actual domain events/enums are used from TESTRADE core:
    # from core.events import OrderFilledEvent, OrderFilledEventData, OrderStatusUpdateEvent, OrderStatusUpdateData, OrderRejectedEvent, OrderRejectedData
    # from modules.order_management.order_data_types import OrderSide, OrderLSAgentStatus
else:
    # Runtime placeholders for Testable components
    class IntelliSenseTestableOrderRepository:
        __version__ = "unknown_runtime_placeholder_itor"
        def on_test_broker_event(self, event_type: str, payload: Dict[str, Any]): pass
        def get_order_by_local_id(self, order_id: int) -> Optional[Any]: return None

    class IntelliSenseTestablePositionManager:
        __version__ = "unknown_runtime_placeholder_itpm"
        def on_test_broker_fill(self, payload: Dict[str, Any]): pass

    # Runtime placeholders for Domain Events/Enums (if not importing actuals)
    class OrderFilledEvent:
        def __init__(self, data): self.data = data
    class OrderFilledEventData:
        def __init__(self, order_id, local_order_id, symbol, side, fill_quantity, fill_price, fill_timestamp):
            self.order_id = order_id; self.local_order_id = local_order_id; self.symbol = symbol
            self.side = side; self.fill_quantity = fill_quantity; self.fill_price = fill_price; self.fill_timestamp = fill_timestamp
    class OrderStatusUpdateEvent:
        def __init__(self, data): self.data = data
    class OrderStatusUpdateData:
        def __init__(self, local_order_id, broker_order_id, symbol, status, message=None):
            self.local_order_id = local_order_id; self.broker_order_id = broker_order_id
            self.symbol = symbol; self.status = status; self.message = message
    class OrderRejectedEvent:
        def __init__(self, data): self.data = data
    class OrderRejectedData:
        def __init__(self, local_order_id, broker_order_id, symbol, reason, timestamp):
            self.local_order_id = local_order_id; self.broker_order_id = broker_order_id
            self.symbol = symbol; self.reason = reason; self.timestamp = timestamp
    class OrderSide:
        BUY = "BUY"; SELL = "SELL"; UNKNOWN = "UNKNOWN"
    class OrderLSAgentStatus:
        UNKNOWN = "UNKNOWN"; FILLED = "FILLED"; REJECTED = "REJECTED"; PARTIALLY_FILLED = "PARTIALLY_FILLED"; CANCELLED = "CANCELLED"; WORKING="WORKING" # Added common ones
        __members__ = {"UNKNOWN": UNKNOWN, "FILLED": FILLED, "REJECTED": REJECTED,
                       "PARTIALLY_FILLED": PARTIALLY_FILLED, "CANCELLED": CANCELLED, "WORKING":WORKING}


# Runtime imports for concrete implementations needed for instantiation/isinstance
from intellisense.analysis.testable_components import IntelliSenseTestableOrderRepository as OrderRepository
from intellisense.analysis.testable_components import IntelliSenseTestablePositionManager as PositionManager
# If actual domain events are used, they'd be imported here for runtime too, not just in TYPE_CHECKING.
# For now, placeholders are defined in the `else` block.

logger = logging.getLogger(__name__)

class BrokerIntelligenceEngine(IBrokerIntelligenceEngine):
    """
    Intelligence engine for Broker event analysis, using hybrid approach.
    """
    def __init__(self):
        self.test_config: Optional[BrokerTestConfig] = None
        self.data_source_factory: Optional[IDataSourceFactory] = None
        self.data_source: Optional[IBrokerDataSource] = None
        
        self._event_bus: Optional[IEventBus] = None # Interface type
        self._order_repository: Optional['OrderRepository'] = None # Uses alias, string hint
        self._position_manager: Optional['PositionManager'] = None # Uses alias, string hint
        
        self.analysis_results: List[BrokerAnalysisResult] = []
        self._engine_active = False
        self._current_session_id: Optional[str] = None
        logger.info("BrokerIntelligenceEngine instance created.")

    def initialize(self,
                   config: BrokerTestConfig,
                   data_source_factory: IDataSourceFactory,
                   real_services: Dict[str, Any]
                  ) -> bool:
        self.test_config = config
        self.data_source_factory = data_source_factory
        
        self._event_bus = real_services.get('event_bus')
        self._order_repository = real_services.get('order_repository')
        self._position_manager = real_services.get('position_manager')

        if not all([self._event_bus, self._order_repository, self._position_manager]):
            logger.error("BrokerIntelligenceEngine: Missing critical real service instances (EventBus, OrderRepository, or PositionManager).")
            return False
        
        logger.info("Broker Intelligence Engine initialized with real services.")
        return True

    def setup_for_session(self, session_config: TestSessionConfig) -> bool:
        if not self.data_source_factory or not self.test_config: return False
        try:
            self.data_source = self.data_source_factory.create_broker_data_source(session_config)
            if self.data_source:
                if self.data_source.load_timeline_data():
                    if hasattr(self.data_source, 'set_delay_profile'):
                        self.data_source.set_delay_profile(self.test_config.delay_profile)
                    logger.info(f"Broker Engine setup data source for session: {session_config.session_name}")
                    self._current_session_id = session_config.session_name
                    self.analysis_results = []
                    return True
                else: logger.error(f"Failed to load Broker timeline for {session_config.session_name}"); return False
            else: logger.error("Failed to create Broker data source."); return False
        except Exception as e: logger.error(f"Failed setup Broker Engine for {session_config.session_name}: {e}", exc_info=True); return False
            
    def start(self) -> None:
        if not self.data_source or not self._event_bus or not self._order_repository or not self._position_manager:
            logger.error("Broker Engine cannot start: not properly initialized or session not set up."); self._engine_active = False; return
        if self.data_source: self.data_source.start()
        self._engine_active = True
        logger.info(f"Broker Intelligence Engine started for session {self._current_session_id}.")

    def stop(self) -> None:
        self._engine_active = False
        if self.data_source: self.data_source.stop()
        logger.info(f"Broker Intelligence Engine stopped for session {self._current_session_id}.")
        self._current_session_id = None

    def get_status(self) -> Dict[str, Any]:
        # ... (similar to OCR/Price engines)
        progress_info = self.data_source.get_replay_progress() if self.data_source and self.data_source.is_active() else {}
        return {"active": self._engine_active, "session_id": self._current_session_id,
                "replay_progress_percent": progress_info.get('progress_percentage_loaded',0.0),
                "analysis_results_count": len(self.analysis_results)}

    def _create_domain_event_from_broker_timeline(self, event: BrokerTimelineEvent) -> Optional[Any]:
        """Transforms a replayed BrokerTimelineEvent into a live system domain event."""
        # event.data is BrokerFillData, BrokerAckData, etc.
        # event.event_type is "FILL", "ACK", "REJECT", etc.

        payload = event.data
        domain_event = None

        if isinstance(payload, BrokerFillData):
            # Need to map BrokerFillData fields to OrderFilledEventData fields
            # Assuming local_order_id is on the BrokerTimelineEvent.original_order_id
            # And broker_order_id is on BrokerTimelineEvent.broker_order_id_on_event
            # Side needs to be determined or passed if not in BrokerFillData directly
            # For now, assume payload contains enough info or we fetch from order_repo
            order_info = self._order_repository.get_order_by_local_id(int(event.original_order_id)) if self._order_repository and event.original_order_id else None
            side = order_info.side if order_info else OrderSide.UNKNOWN # Get side from existing order

            fill_event_data = OrderFilledEventData(
                order_id=event.broker_order_id_on_event or "", # Broker's ID for the order
                local_order_id=event.original_order_id, # Our local ID
                symbol=payload.get('symbol', order_info.symbol if order_info else "UNK"), # Symbol from fill or order
                side=side, # This needs careful mapping
                fill_quantity=payload.filled_quantity,
                fill_price=payload.fill_price,
                fill_timestamp=payload.fill_timestamp or event.epoch_timestamp_s
            )
            domain_event = OrderFilledEvent(data=fill_event_data)
        elif isinstance(payload, (BrokerAckData, BrokerRejectionData, BrokerCancelAckData, BrokerOtherData)):
            # Map to OrderStatusUpdateEvent or OrderRejectedEvent
            # This requires mapping payload fields to OrderStatusUpdateData
            # For simplicity, a generic mapping. This needs to be robust.
            order_info = self._order_repository.get_order_by_local_id(int(event.original_order_id)) if self._order_repository and event.original_order_id else None
            symbol = payload.get('symbol', order_info.symbol if order_info else "UNK")

            status_data = OrderStatusUpdateData(
                local_order_id=event.original_order_id,
                broker_order_id=event.broker_order_id_on_event,
                symbol=symbol,
                status=OrderLSAgentStatus(event.event_type) if event.event_type in OrderLSAgentStatus.__members__ else OrderLSAgentStatus.UNKNOWN, # Map string event_type
                message=getattr(payload, 'rejection_reason', getattr(payload,'message', None)) # Example
                # filled_quantity, avg_fill_price, remaining_quantity may need to be derived or from payload
            )
            if event.event_type == "REJECT" and isinstance(payload, BrokerRejectionData):
                 domain_event = OrderRejectedEvent(data=OrderRejectedData(local_order_id=event.original_order_id, broker_order_id=event.broker_order_id_on_event, symbol=symbol, reason=payload.rejection_reason, timestamp=event.epoch_timestamp_s))
            else:
                 domain_event = OrderStatusUpdateEvent(data=status_data)

        if domain_event: logger.debug(f"Created domain event: {type(domain_event).__name__} from BrokerTimelineEvent {event.global_sequence_id}")
        else: logger.warning(f"Could not create domain event for BrokerTimelineEvent type {event.event_type}, data: {event.data}")
        return domain_event

    def _perform_validation(self, event: BrokerTimelineEvent, domain_event_published: bool) -> BrokerValidationResult:
        """Performs validation after event processing."""
        # Placeholder: Query OrderRepository and PositionManager for state related to event.original_order_id
        # Compare against expected state changes based on event.data
        # Example: if event.data is BrokerFillData, check if OR order is partially/fully filled
        # and if PM position reflects the fill.
        logger.debug(f"Performing validation for broker event GSI {event.global_sequence_id} (placeholder).")
        # This requires access to the state of the *real* OR and PM after the event bus + direct calls.
        # This would be complex to implement fully here, needs careful thought on what to assert.
        return BrokerValidationResult(is_overall_valid=True, discrepancies=["Validation logic placeholder."])

    def process_event(self, event: BrokerTimelineEvent) -> BrokerAnalysisResult:
        if not self._engine_active or not all([self._event_bus, self._order_repository, self._position_manager]):
            return BrokerAnalysisResult(broker_event_type=event.event_type, original_order_id=event.original_order_id,
                                        epoch_timestamp_s=event.epoch_timestamp_s, perf_counter_timestamp=event.perf_counter_timestamp,
                                        analysis_error="Broker Engine not active or services missing.")

        analysis_start_ns = time.perf_counter_ns()
        domain_event_latency_ns: Optional[int] = None
        or_direct_lat_ns: Optional[int] = None
        pm_direct_lat_ns: Optional[int] = None
        analysis_err_str: Optional[str] = None
        validation_res: Optional[BrokerValidationResult] = None
        derived_domain_event_type_str: Optional[str] = None

        # The payload for direct injection is event.data (BrokerFillData, etc.)
        # The payload for domain event needs to be constructed based on it.
        broker_event_payload_for_direct_call = event.data

        try:
            # 1. Transform to domain event
            domain_event_start_ns = time.perf_counter_ns()
            domain_event_to_publish = self._create_domain_event_from_broker_timeline(event)
            domain_event_latency_ns = time.perf_counter_ns() - domain_event_start_ns
            if domain_event_to_publish:
                derived_domain_event_type_str = type(domain_event_to_publish).__name__

            # 2. Publish to event bus (core pathway)
            if domain_event_to_publish:
                self._event_bus.publish(domain_event_to_publish)
                logger.debug(f"Published {derived_domain_event_type_str} for GSI {event.global_sequence_id} to event bus.")

            # 3. Direct call to testable components (if available)
            # The payload for these direct calls is event.data (e.g. BrokerFillData)
            # The on_test_... methods in testable components need to be able to handle this.
            # For direct calls, we pass a dict version of the specific broker data type for simplicity.
            direct_call_payload_dict = asdict(broker_event_payload_for_direct_call) if broker_event_payload_for_direct_call and not isinstance(broker_event_payload_for_direct_call, dict) else \
                                       broker_event_payload_for_direct_call if isinstance(broker_event_payload_for_direct_call, dict) else {}


            if hasattr(self._order_repository, 'on_test_broker_event'):
                or_call_start_ns = time.perf_counter_ns()
                # Pass event.event_type (e.g. "FILL") and the dict payload
                self._order_repository.on_test_broker_event(event.event_type, direct_call_payload_dict)
                or_direct_lat_ns = time.perf_counter_ns() - or_call_start_ns

            # Only call PositionManager for fills
            if isinstance(broker_event_payload_for_direct_call, BrokerFillData):
                if hasattr(self._position_manager, 'on_test_broker_fill'):
                    pm_call_start_ns = time.perf_counter_ns()
                    self._position_manager.on_test_broker_fill(direct_call_payload_dict) # Pass dict form of BrokerFillData
                    pm_direct_lat_ns = time.perf_counter_ns() - pm_call_start_ns

            # 4. Perform Validation (after event bus and direct calls have had time to process)
            # This might need a small delay or a way to wait for event bus propagation if checks are immediate.
            # For now, assume immediate check is okay.
            validation_res = self._perform_validation(event, domain_event_published=(domain_event_to_publish is not None))

        except Exception as e:
            analysis_err_str = str(e)
            logger.error(f"Error processing BrokerTimelineEvent GSI {event.global_sequence_id}: {e}", exc_info=True)

        analysis_result = BrokerAnalysisResult(
            broker_event_type=event.event_type, original_order_id=event.original_order_id,
            broker_order_id_on_event=event.broker_order_id_on_event,
            epoch_timestamp_s=event.epoch_timestamp_s, perf_counter_timestamp=event.perf_counter_timestamp,
            original_broker_event_global_sequence_id=event.global_sequence_id,
            captured_processing_latency_ns=event.processing_latency_ns,
            event_bus_publish_latency_ns=domain_event_latency_ns,
            order_repo_direct_call_latency_ns=or_direct_lat_ns,
            position_manager_direct_call_latency_ns=pm_direct_lat_ns,
            replayed_event_data=broker_event_payload_for_direct_call, # Store the specific typed data
            derived_domain_event_type=derived_domain_event_type_str,
            validation_result=validation_res,
            service_versions=self._get_service_versions(), # Implement this helper
            analysis_error=analysis_err_str
        )
        self.analysis_results.append(analysis_result)
        return analysis_result

    def _get_service_versions(self) -> Dict[str, str]: # Placeholder
        return {"broker_engine_version": "1.0",
                "order_repo_version": getattr(self._order_repository, '__version__', 'unknown'),
                "position_mgr_version": getattr(self._position_manager, '__version__', 'unknown')}

    def get_analysis_results(self) -> List[BrokerAnalysisResult]:
        return list(self.analysis_results)
