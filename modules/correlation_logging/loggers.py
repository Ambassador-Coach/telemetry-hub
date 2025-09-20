# modules/correlation_logging/loggers.py
import logging
from typing import Dict, Any, Optional

from interfaces.logging.services import ICorrelationLogger
from interfaces.core.telemetry_interfaces import ITelemetryService

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Implementation 1: The Null Logger (for TANK MODE)
# ---------------------------------------------------------------------------
class NullCorrelationLogger(ICorrelationLogger):
    """
    A 'no-op' implementation of the ICorrelationLogger.

    This implementation does absolutely nothing. It is injected into core
    TESTRADE services when running in a headless 'TANK' mode to ensure
    that logging calls have zero performance impact.
    """
    def log_event(self, *args, **kwargs) -> None:
        """This method is intentionally empty and accepts any parameters."""
        pass

# ---------------------------------------------------------------------------
# Implementation 2: The Telemetry Logger (for INTELLISENSE MODE)
# ---------------------------------------------------------------------------
class TelemetryCorrelationLogger(ICorrelationLogger):
    """
    An implementation of ICorrelationLogger that forwards logs to the
    central TelemetryStreamingService.

    This acts as a lightweight, fire-and-forget adapter that bridges the
    TESTRADE core to the telemetry "Airlock" without creating a direct
    dependency in the core services themselves.
    """
    def __init__(self, telemetry_service: ITelemetryService):
        self._telemetry_service = telemetry_service
        # Use a generic stream name for all correlation events
        self._target_stream = "testrade:telemetry:correlation-events"

    def log_event(
        self,
        source_component: str,
        event_name: str,
        payload: Dict[str, Any],
        stream_override: Optional[str] = None
    ) -> None:
        """
        Formats the log into a telemetry payload and enqueues it.
        This call is non-blocking and thread-safe, relying on the
        underlying TelemetryStreamingService's queue.
        """
        # The Telemetry service's enqueue method is already designed to be
        # a fire-and-forget, non-blocking call.
        try:
            # Extract special fields from payload if present
            origin_timestamp_s = payload.get('origin_timestamp_s')
            # Use the provided stream_override parameter, or fall back to default
            target_stream = stream_override if stream_override is not None else self._target_stream
            
            # Map event names to original event types
            event_type_mapping = {
                "OcrCaptureStarted": "TESTRADE_OCR_CAPTURE_STARTED",
                "OcrCaptureFailure": "TESTRADE_OCR_CAPTURE_FAILURE",
                "OcrCaptureMilestones": "OCR_CAPTURE_MILESTONES",
                "ServiceHealthStatus": "TESTRADE_SERVICE_HEALTH_STATUS",
                "OcrConditioningLatency": "OCR_CONDITIONING_LATENCY",
                "CleanedOcrSnapshot": "TESTRADE_CLEANED_OCR_SNAPSHOT",
                "NoTradeData": "TESTRADE_NO_TRADE_DATA",
                "OcrConditioningError": "TESTRADE_OCR_CONDITIONING_ERROR",
                "SignalDecision": "TESTRADE_SIGNAL_DECISION",
                "SignalSuppression": "TESTRADE_SIGNAL_SUPPRESSION",
                "SignalValidationFailure": "TESTRADE_SIGNAL_VALIDATION_FAILURE",
                "SignalGenerationFailure": "TESTRADE_SIGNAL_GENERATION_FAILURE",
                "OrchestrationMilestones": "ORCHESTRATION_MILESTONES",
                # Order Management event mappings
                "OrderStatusUpdate": "ORDER_STATUS_UPDATE",
                "OrderFill": "ORDER_FILL",
                "OrderRejection": "ORDER_REJECTION",
                "OrderValidationFailure": "ORDER_VALIDATION_FAILURE",
                "OrphanedFill": "ORPHANED_FILL",
                "FillValidationFailure": "FILL_VALIDATION_FAILURE",
                "StaleOrderCancel": "STALE_ORDER_CANCEL",
                # Snapshot Interpreter mapping
                "SignalInterpretation": "TESTRADE_SIGNAL_INTERPRETATION",
                # Risk Management event mappings
                "OrderValidated": "VALIDATED_ORDER_REQUEST",
                "RiskRejection": "RISK_REJECTION",
                "AccountUpdate": "ACCOUNT_UPDATE",
                "RiskTradingStopped": "RISK_TRADING_STOPPED",
                "RiskEscalation": "RISK_ESCALATION",
                "MarketHaltRiskEvent": "MARKET_HALT_RISK_EVENT",
                "RiskAssessmentCompleted": "RISK_ASSESSMENT_COMPLETED",
                "RiskAssessmentMilestones": "RISK_ASSESSMENT_MILESTONES",
                "RiskRejectionMilestones": "RISK_REJECTION_MILESTONES",
                "ServiceHealthStatus": "SERVICE_HEALTH_STATUS",
                "PeriodicHealthStatus": "PERIODIC_HEALTH_STATUS",
                "RiskSystemError": "RISK_SYSTEM_ERROR",
                "PriceProviderFailure": "PRICE_PROVIDER_FAILURE",
                "AccountLimitViolation": "ACCOUNT_LIMIT_VIOLATION",
                "MeltdownFactorAnalysis": "MELTDOWN_FACTOR_ANALYSIS"
            }
            
            # Use the mapped event type or fall back to CORRELATION_LOG
            event_type = event_type_mapping.get(event_name, "CORRELATION_LOG")
            
            # Handle correlation ID - check for __correlation_id key
            if "__correlation_id" in payload:
                # This follows the pattern used by OCRDataConditioningService
                telemetry_payload = payload.copy()
            else:
                # For other services, wrap the payload
                telemetry_payload = {
                    "correlation_event_name": event_name,
                    **payload
                }

            self._telemetry_service.enqueue(
                source_component=source_component,
                event_type=event_type,
                payload=telemetry_payload,
                stream_override=target_stream,
                origin_timestamp_s=origin_timestamp_s
            )
        except Exception as e:
            # This should rarely happen as enqueue handles its own errors,
            # but we log it just in case.
            logger.error(
                f"TelemetryCorrelationLogger failed to enqueue event "
                f"'{event_name}' from '{source_component}': {e}"
            )