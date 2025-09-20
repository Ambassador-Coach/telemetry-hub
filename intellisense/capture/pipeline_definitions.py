# --- START OF REGENERATED intellisense/capture/pipeline_definitions.py (Chunk 34 - Rev 3) ---
import logging
from typing import Dict, List, Any, Callable, Tuple, Optional

# Type alias for the checker function
# Takes: log_payload (Dict[str, Any]), stimulus_data (Dict[str, Any])
# Returns: bool
EventConditionChecker = Callable[[Dict[str, Any], Dict[str, Any]], bool]

# PIPELINE_STAGE_CRITERIA Tuple structure:
# (source_sense: str, event_type_description: str, checker_function: EventConditionChecker, stage_name_key: str)
PIPELINE_STAGE_CRITERIA = Tuple[str, str, EventConditionChecker, str]

logger = logging.getLogger(__name__)

# --- Standardized EventConditionChecker Functions ---

def check_always_true(log_payload: Dict[str, Any], stimulus_data: Dict[str, Any]) -> bool:
    """A generic checker that always returns True, useful for stages where only source_sense might matter or for simpler checks."""
    return True

# --- Refined EventConditionChecker Functions ---

def check_trade_command_injection_logged(log_payload: Dict[str, Any], stimulus_data: Dict[str, Any]) -> bool:
    """
    Checks if the log entry is for the INJECTION_CONTROL logging of the specific trade command stimulus.
    It uses the 'correlation_id' and 'derived_order_request_id' from stimulus_data.
    (This checker is for when the *initial stimulus* is a trade command).
    """
    if log_payload.get('injection_type') != "trade_command": return False
    # 'correlation_id' in stimulus_data is the step_corr_id for this specific injection log
    if log_payload.get('correlation_id') != stimulus_data.get('step_correlation_id'): # MOC adds step_correlation_id to stimulus_data
        # logger.debug(f"InjectLogChk: step_corr_id mismatch. Log: {log_payload.get('correlation_id')}, Stimulus: {stimulus_data.get('step_correlation_id')}")
        return False
    if log_payload.get('derived_order_request_id') != stimulus_data.get('derived_order_request_id'):
        # logger.debug(f"InjectLogChk: derived_order_request_id mismatch. Log: {log_payload.get('derived_order_request_id')}, Stimulus: {stimulus_data.get('derived_order_request_id')}")
        return False
    return True

def check_ocr_stimulus_injection_logged(log_payload: Dict[str, Any], stimulus_data: Dict[str, Any]) -> bool:
    """Checks if the log entry is for INJECTION_CONTROL logging of an OCR stimulus."""
    if log_payload.get('injection_type') != "ocr_event": return False
    # 'correlation_id' in stimulus_data is the step_corr_id for this specific OCR injection log
    if log_payload.get('correlation_id') != stimulus_data.get('step_correlation_id'):
        return False
    if 'injected_frame_number' in stimulus_data and \
       log_payload.get('injected_frame_number') != stimulus_data.get('injected_frame_number'):
        return False
    return True

def check_risk_management_service_output_logged(
    log_payload: Dict[str, Any], stimulus_data: Dict[str, Any], expected_rms_status: Optional[str]
) -> bool:
    """Checks log from RiskManagementService, using propagated correlation_id and specific request_id."""
    if log_payload.get('event_name') != "OrderRequestEvent": return False
    # stimulus_data.correlation_id is the master/chain correlation_id (e.g., from OCR stimulus)
    if log_payload.get('correlation_id') != stimulus_data.get('step_correlation_id'): # Check against the propagated ID
        return False
    # For orchestrated trades, use trade_derived_order_request_id; for direct trades, use derived_order_request_id
    expected_request_id = stimulus_data.get('trade_derived_order_request_id') or stimulus_data.get('derived_order_request_id')
    if log_payload.get('request_id') != expected_request_id:
        return False
    if expected_rms_status and log_payload.get('risk_assessment_status') != expected_rms_status:
        return False
    return True

def check_trade_manager_service_output_logged(
    log_payload: Dict[str, Any], stimulus_data: Dict[str, Any], expected_tms_status: Optional[str]
) -> bool:
    """Checks log from TradeManagerService, using propagated correlation_id and specific request_id."""
    if log_payload.get('event_name_in_payload') != "ValidatedOrderProcessedByExecutor": return False
    if log_payload.get('correlation_id') != stimulus_data.get('step_correlation_id'):
        return False
    # For orchestrated trades, use trade_derived_order_request_id; for direct trades, use derived_order_request_id
    expected_request_id = stimulus_data.get('trade_derived_order_request_id') or stimulus_data.get('derived_order_request_id')
    if log_payload.get('request_id') != expected_request_id:
        return False
    if expected_tms_status and log_payload.get('status') != expected_tms_status:
        return False
    return True

def check_broker_response_logged(
    log_payload: Dict[str, Any], stimulus_data: Dict[str, Any], expected_broker_response_type: str
) -> bool:
    """Checks log from EnhancedBrokerInterface, using propagated correlation_id and specific original_request_id."""
    if log_payload.get('broker_response_type') != expected_broker_response_type: return False
    if log_payload.get('correlation_id') != stimulus_data.get('step_correlation_id'):
        return False
    # For orchestrated trades, use trade_derived_order_request_id; for direct trades, use derived_order_request_id
    expected_request_id = stimulus_data.get('trade_derived_order_request_id') or stimulus_data.get('derived_order_request_id')
    if log_payload.get('original_request_id') != expected_request_id:
        return False
    return True

def check_enhanced_ocr_output_logged(log_payload: Dict[str, Any], stimulus_data: Dict[str, Any]) -> bool:
    """
    Checks log entry from EnhancedOCRDataConditioningService.
    Matches on 'correlation_id' (which should be the original OCR stimulus's step_corr_id).
    """
    # Check for the actual field structure from enhanced_ocr_service.py
    if log_payload.get('event_type') != "ocr_data_processed": return False
    # stimulus_data.correlation_id here is the original OCR stimulus's step_correlation_id
    if log_payload.get('correlation_id') != stimulus_data.get('step_correlation_id'):
        return False
    # Check frame number if provided
    if 'injected_frame_number' in stimulus_data and \
       log_payload.get('frame_number') != stimulus_data.get('injected_frame_number'):
        return False
    return True

# --- Pre-defined Pipeline Signatures ---
PIPELINE_SIGNATURES: Dict[str, List[PIPELINE_STAGE_CRITERIA]] = {
    "TRADE_CMD_TO_BROKER_ACK": [
        # stimulus_data for these checkers will contain step_correlation_id and derived_order_request_id from the MOC trade injection.
        ("INJECTION_CONTROL", "Trade Command Injected", check_trade_command_injection_logged, "T0_CmdInjectLog"),
        ("CAPTURE", "RMS: OrderRequestEvent (VALIDATED)", lambda lp, sd: check_risk_management_service_output_logged(lp, sd, expected_rms_status="VALIDATED"), "T1_RMSValidatedLog"),
        ("CAPTURE", "TMS: ValidatedOrderProcessed (FORWARDED)", lambda lp, sd: check_trade_manager_service_output_logged(lp, sd, expected_tms_status="FORWARDED_TO_EXECUTOR"), "T2_TMSForwardedLog"),
        ("CAPTURE", "Broker: ORDER_ACCEPTED", lambda lp, sd: check_broker_response_logged(lp, sd, "ORDER_ACCEPTED"), "T3_BrokerAckLog")
    ],
    "TRADE_CMD_TO_BROKER_FILL": [
        ("INJECTION_CONTROL", "Trade Command Injected", check_trade_command_injection_logged, "T0_CmdInjectLog"),
        ("CAPTURE", "RMS: OrderRequestEvent (VALIDATED)", lambda lp, sd: check_risk_management_service_output_logged(lp, sd, "VALIDATED"), "T1_RMSValidatedLog"),
        ("CAPTURE", "TMS: ValidatedOrderProcessed (FORWARDED)", lambda lp, sd: check_trade_manager_service_output_logged(lp, sd, "FORWARDED_TO_EXECUTOR"), "T2_TMSForwardedLog"),
        ("CAPTURE", "Broker: ORDER_ACCEPTED", lambda lp, sd: check_broker_response_logged(lp, sd, "ORDER_ACCEPTED"), "T3_BrokerAckLog"),
        ("CAPTURE", "Broker: FILL", lambda lp, sd: check_broker_response_logged(lp, sd, "FILL"), "T4_BrokerFillLog")
    ],
    "TRADE_CMD_REJECTED_BY_RISK": [
        ("INJECTION_CONTROL", "Trade Command Injected by MOC", check_trade_command_injection_logged, "T0_CmdInjectLog"),
        ("CAPTURE", "RMS Output: OrderRequestEvent (REJECTED)", lambda lp, sd: check_risk_management_service_output_logged(lp, sd, expected_rms_status="REJECTED"), "T1_RMSRejectedLog")
    ],
    "TRADE_CMD_REJECTED_BY_BROKER": [ # New signature based on Plan 7
        ("INJECTION_CONTROL", "Trade Command Injected by MOC", check_trade_command_injection_logged, "T0_CmdInjectLog"),
        ("CAPTURE", "RMS Output: OrderRequestEvent (VALIDATED)", lambda lp, sd: check_risk_management_service_output_logged(lp, sd, expected_rms_status="VALIDATED"), "T1_RMSValidatedLog"),
        ("CAPTURE", "TMS Output: ValidatedOrderProcessed (FORWARDED)", lambda lp, sd: check_trade_manager_service_output_logged(lp, sd, expected_tms_status="FORWARDED_TO_EXECUTOR"), "T2_TMSForwardedLog"),
        # Expect an ACK first, even if the order is quickly rejected by downstream broker systems after acceptance by front-end
        ("CAPTURE", "Broker Log: ORDER_ACCEPTED (Potentially)", lambda lp, sd: check_broker_response_logged(lp, sd, expected_broker_response_type="ORDER_ACCEPTED"), "T3_BrokerAckLog_ForRejectPath"),
        ("CAPTURE", "Broker Log: ORDER_REJECTED", lambda lp, sd: check_broker_response_logged(lp, sd, expected_broker_response_type="ORDER_REJECTED"), "T4_BrokerRejectLog") # Assuming ORDER_REJECTED is a broker_response_type
    ],
    "OCR_STIMULUS_TO_ENHANCED_OCR_LOG": [
        ("INJECTION_CONTROL", "OCR Stimulus Injected by MOC", check_ocr_stimulus_injection_logged, "T0_OCRStimulusInjectLog"),
        ("CAPTURE", "EnhancedOCRService Output Logged", check_enhanced_ocr_output_logged, "T1_EnhancedOCROutputLog")
    ],
    # NEW/REFINED Comprehensive Pipeline for OCR Mock -> Trade -> Fill
    "OCR_STIMULUS_TO_TRADE_FILL": [
        # For this pipeline, stimulus_data initially contains the OCR step_correlation_id.
        # The mock orchestrator reuses this step_correlation_id as the correlation_id for the trade it generates.
        # It also generates a derived_order_request_id for that trade.
        # MOC needs to ensure stimulus_data for the monitor has both.
        ("INJECTION_CONTROL", "OCR Stimulus Injected", check_ocr_stimulus_injection_logged, "S0_OCRStimulusInjectLog"),
        ("CAPTURE", "EnhancedOCR Log (Mocked)", check_enhanced_ocr_output_logged, "S1_EnhancedOCROutputLog"),
        # The following stage is the CAPTURE log for the trade *generated by the mock orchestrator*.
        # Based on debug output, this event has source_sense="CAPTURE" and source_component="MockS2_OrchestratedTradeInjectLog"
        # This trade's log should have the *same correlation_id* as the OCR stimulus (reused by mock orchestrator).
        # It will have its *own derived_order_request_id*.
        ("CAPTURE", "Trade Command Injected (by Mock Orchestrator)",
         lambda lp, sd: (lp.get('injection_type') == "trade_command" and \
                         lp.get('correlation_id') == sd.get('step_correlation_id') and \
                         lp.get('stimulus_details',{}).get('symbol') == sd.get('symbol')), # Match symbol from original OCR stimulus
         "S2_OrchestratedTradeInjectLog"),
        ("CAPTURE", "RMS Output for Orchestrated Trade (VALIDATED)",
         lambda lp, sd: check_risk_management_service_output_logged(lp, sd, "VALIDATED"), # Uses step_correlation_id & trade_derived_order_request_id
         "S3_RMSTradeValidatedLog"),
        ("CAPTURE", "TMS Output for Orchestrated Trade (FORWARDED)",
         lambda lp, sd: check_trade_manager_service_output_logged(lp, sd, "FORWARDED_TO_EXECUTOR"),
         "S4_TMSTradeForwardedLog"),
        ("CAPTURE", "Broker ACK for Orchestrated Trade",
         lambda lp, sd: check_broker_response_logged(lp, sd, "ORDER_ACCEPTED"),
         "S5_BrokerAckOrchestratedTradeLog"),
        ("CAPTURE", "Broker FILL for Orchestrated Trade",
         lambda lp, sd: check_broker_response_logged(lp, sd, "FILL"),
         "S6_BrokerFillOrchestratedTradeLog")
    ],

    # === NEW PIPELINE SIGNATURES FOR FINALIZED INJECTION PLANS ===

    # For IP_V1_OCR_Accuracy_Latency.json
    "OCR_STIMULUS_TO_ORDER_REQUEST": [
        ("INJECTION_CONTROL", "OCR Stimulus Injected", check_ocr_stimulus_injection_logged, "S1_RawOcrLog"),
        ("CAPTURE", "EnhancedOCR Cleaned Snapshot", check_enhanced_ocr_output_logged, "S3_CleanedOcrSnapshotLog"),
        ("CAPTURE", "Order Request Generated",
         lambda lp, sd: (lp.get('event_name') == "OrderRequestEvent" and
                        lp.get('correlation_id') == sd.get('step_correlation_id')),
         "S4_OrderRequestLog")
    ],

    # For IP_V1_Order_Execution_Full_Path.json
    "TRADE_CMD_TO_POSITION_UPDATE": [
        ("INJECTION_CONTROL", "Trade Command Injected", check_trade_command_injection_logged, "S1_RMSValidatedLog"),
        ("CAPTURE", "RMS Validation", lambda lp, sd: check_risk_management_service_output_logged(lp, sd, "VALIDATED"), "S1_RMSValidatedLog"),
        ("CAPTURE", "Broker Acknowledgment", lambda lp, sd: check_broker_response_logged(lp, sd, "ORDER_ACCEPTED"), "S3_BrokerAckLog"),
        ("CAPTURE", "Broker Fill", lambda lp, sd: check_broker_response_logged(lp, sd, "FILL"), "S4_BrokerFillLog"),
        ("CAPTURE", "Position Update",
         lambda lp, sd: (lp.get('event_type') == "position_updated" and
                        lp.get('correlation_id') == sd.get('step_correlation_id')),
         "S5_PositionUpdateLog")
    ],

    # For IP_V1_Contextual_Behavior.json
    "TRADE_CMD_WHEN_TRADING_DISABLED": [
        ("INJECTION_CONTROL", "Trade Command Injected", check_trade_command_injection_logged, "S1_TradeCommandLog"),
        ("CAPTURE", "Trading State Check",
         lambda lp, sd: (lp.get('event_type') == "trading_state_check" and
                        lp.get('trading_enabled') == False),
         "S2_TradingStateLog"),
        ("CAPTURE", "RMS Rejection Due to Trading Disabled",
         lambda lp, sd: check_risk_management_service_output_logged(lp, sd, "REJECTED"),
         "S3_RMSRejectionLog")
    ],

    # More signatures can be added:
    # - TRADE_CMD_TO_PARTIAL_FILL_THEN_FULL_FILL
    # - TRADE_CMD_TO_CANCEL_ACK
}

def get_expected_pipeline_stages(
    stimulus_type_from_plan: Optional[str],
    stimulus_data: Dict[str, Any] # stimulus_data now contains step_correlation_id and potentially trade_derived_order_request_id
) -> Optional[List[PIPELINE_STAGE_CRITERIA]]:
    # ... (Logic as updated in previous response, using stimulus_data.get("target_pipeline_key"))
    actual_pipeline_key_to_use = stimulus_data.get("target_pipeline_key")
    if not stimulus_type_from_plan and not actual_pipeline_key_to_use: # If both are None
        logger.warning("get_expected_pipeline_stages called with no stimulus_type_from_plan and no target_pipeline_key in stimulus_data.")
        return None
    if not actual_pipeline_key_to_use: # Fallback if target_pipeline_key is missing
        logger.warning(f"No 'target_pipeline_key' in stimulus_data for stimulus type '{stimulus_type_from_plan}'. Inferring default (not recommended).")
        if stimulus_type_from_plan and stimulus_type_from_plan.upper() == "TRADE_COMMAND": actual_pipeline_key_to_use = "TRADE_CMD_TO_BROKER_ACK"
        elif stimulus_type_from_plan and stimulus_type_from_plan.upper() == "OCR_EVENT": actual_pipeline_key_to_use = "OCR_STIMULUS_TO_ENHANCED_OCR_LOG"
        else: logger.error(f"Cannot infer pipeline key for stimulus type '{stimulus_type_from_plan}'."); return None

    key_upper = actual_pipeline_key_to_use.upper()
    stages = PIPELINE_SIGNATURES.get(key_upper)
    if stages is None: logger.warning(f"No pipeline signature defined for key: '{key_upper}'")
    else: logger.info(f"Selected pipeline signature '{key_upper}' with {len(stages)} stages.")
    return stages

def get_available_pipeline_types() -> List[str]:
    return list(PIPELINE_SIGNATURES.keys())

def describe_pipeline_signature(pipeline_type_key: str) -> str:
    stages = PIPELINE_SIGNATURES.get(pipeline_type_key.upper())
    if not stages:
        return f"No pipeline signature found for '{pipeline_type_key}'."
    description_lines = [f"Pipeline Signature for '{pipeline_type_key}':"]
    for i, (sense, desc, _, key) in enumerate(stages):
        description_lines.append(f"  Stage {i} ({key}): Sense='{sense}', Expected='{desc}'")
    return "\n".join(description_lines)

# --- END OF REGENERATED intellisense/capture/pipeline_definitions.py ---