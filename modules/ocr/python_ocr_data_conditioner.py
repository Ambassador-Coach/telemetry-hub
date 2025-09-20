# modules/ocr_processing/python_ocr_data_conditioner.py
import logging
import re
import difflib
import math
import time  # For flicker discard timestamps, candidate logic timestamps
from dataclasses import dataclass, field
from typing import Dict, Any, Optional, List

# Core/Module Imports
from interfaces.ocr.services import IOCRDataConditioner
from interfaces.data.services import IPriceProvider
from modules.ocr.data_types import OCRParsedData, CleanedOcrResult
from utils.global_config import GlobalConfig
from utils.flicker_filter_config import FlickerFilterParams
from interfaces.logging.services import ICorrelationLogger
from utils.flicker_filter import flicker_check_new_cost_basis
from utils.thread_safe_uuid import get_thread_safe_uuid
# Legacy symbol loader imports removed - now using ActiveSymbolsService for JIT validation
# Fallback functions for any remaining legacy code paths
def get_tradeable_symbols():
    return set()
def get_non_tradeable_symbols():
    return set()

# Import APIError for REST client error handling
try:
    from alpaca_trade_api.rest import APIError
except ImportError:
    # Fallback if alpaca_trade_api is not available
    class APIError(Exception):
        pass

# Import price data exceptions for enhanced price fetching
try:
    from data_models.pricing import StalePriceError, PriceUnavailableError
except ImportError:
    # Fallback if data models are not available
    class StalePriceError(Exception):
        pass
    class PriceUnavailableError(Exception):
        pass

# Module-level helper functions (exact replicas from legacy)
def clean_ocr_line(line: str) -> str:
    """Removes most non-alphanumeric chars except essential punctuation for numbers/symbols."""
    if not isinstance(line, str):
        return ""
    # Allow letters, numbers, whitespace, plus, minus, dot, comma, hyphen
    cleaned_line = re.sub(r"[^A-Za-z0-9\s+,.\\-]", "", line)
    return cleaned_line.strip()

def remove_non_ascii(s: str) -> str:
    """Strips out any characters outside printable ASCII range 32..126."""
    if not isinstance(s, str):
        return ""
    return re.sub(r"[^\x20-\x7E]+", "", s)

def fuzzy_number_clean(num_str: str) -> str:
    """Cleans OCR'd numbers, handling commas, multiple dots, non-numeric chars."""
    if not isinstance(num_str, str):
        num_str = str(num_str)  # Ensure string
    cleaned = num_str.replace(",", "")
    if cleaned.count('.') > 1:
        first_dot = cleaned.find('.')
        cleaned = cleaned[:first_dot+1] + cleaned[first_dot+1:].replace('.', '')
    # Allow leading +/- and dots, remove others except digits
    cleaned = re.sub(r"[^0-9.+\-]", "", cleaned)
    # Handle cases like empty string, only sign, or only dot after cleaning
    if cleaned in ("", "+", "-", "."):
        cleaned = "0"
    # Handle case like "+." or "-."
    if cleaned in ("+.", "-."):
        cleaned = "0"
    # Ensure it's a valid float representation before returning
    try:
        float(cleaned)
        return cleaned
    except ValueError:
        return "0"  # Return "0" if cleaning resulted in invalid float string

def is_token_numeric(token: str) -> bool:
    """True if token looks like a valid number (int/float, optional sign/commas/dots)."""
    if not isinstance(token, str):
        return False
    # Allow optional sign, digits, commas, and AT MOST one decimal point
    match = re.match(r'^[+\-]?\d{1,3}(?:,\d{3})*(?:\.\d*)?$|^[+\-]?\d*\.\d+$|^[+\-]?\d+$', token)
    return bool(match)

def strip_trailing_letters_if_numeric(token: str) -> str:
    """If 'token' looks like "123.45B", remove trailing letters => "123.45"."""
    if not isinstance(token, str):
        return ""
    # Match numeric part followed by letters at the end
    match = re.match(r'^([+\-]?\d[\d,.]*)([A-Za-z]+)$', token)
    if match:
        return match.group(1)  # Return only the numeric part
    return token

def fix_leading_4_if_no_sign(token: str) -> str:
    """If token starts with '4' and no +/-, interpret as misread '+'. E.g., "4123.45" -> "+123.45"."""
    if not isinstance(token, str):
        return ""
    t = token.strip()
    # Check if it starts with '4' and has digits/commas/dots after, but no explicit sign
    if t.startswith('4') and not t.startswith(('+4', '-4')):
         # Ensure the rest looks like part of a number
         if re.match(r'^4[\d,.]+$', t):
              return '+' + t[1:]  # Replace '4' with '+'
    return token

def fuzzy_match_word(word: str, candidates: List[str], threshold: float = 0.7) -> tuple:
    """Finds best fuzzy match for word in candidates list using difflib.SequenceMatcher."""
    if not isinstance(word, str) or not candidates:
        return None, 0.0
    best_match = None
    best_ratio = 0.0
    word_lower = word.lower()
    for candidate in candidates:
        if not isinstance(candidate, str):
            continue  # Skip non-strings
        ratio = difflib.SequenceMatcher(None, word_lower, candidate.lower()).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_match = candidate
    if best_match and best_ratio >= threshold:
        return best_match, best_ratio
    return None, 0.0

logger = logging.getLogger(__name__)

class PythonOCRDataConditioner(IOCRDataConditioner):
    def __init__(self,
                 price_provider: IPriceProvider,
                 active_symbols_service: 'ActiveSymbolsService',
                 config_service: GlobalConfig,
                 benchmarker: Optional[Any] = None,
                 ipc_client: Optional[Any] = None,
                 correlation_logger: Optional[ICorrelationLogger] = None):
        self.logger = logger
        self.price_provider = price_provider
        self.active_symbols_service = active_symbols_service
        self.config_service = config_service
        self._benchmarker = benchmarker
        self._ipc_client = ipc_client
        # App core dependency removed - using correlation logger directly
        self._correlation_logger = correlation_logger

        # Initialize state variables
        self._last_stable_cost_map: Dict[str, float] = {}
        self._last_known_strategy_map: Dict[str, str] = {}  # Stores lowercase strategy
        # self._candidate_stable_cost_map: Dict[str, float] = {}  # REMOVED - no longer needed with SME logic
        # self._candidate_stable_cost_count: Dict[str, int] = {}  # REMOVED - no longer needed with SME logic
        self._cost_basis_flicker_tracker: Dict[str, tuple[float, int]] = {}  # NEW: stores (last_rejected_value, count)
        self._discarded_flickers: List[Dict] = []  # Optional for metrics/debug

        # Load flicker parameters
        if hasattr(self.config_service, 'flicker_filter') and \
           isinstance(self.config_service.flicker_filter, FlickerFilterParams):
            self._flicker_params: FlickerFilterParams = self.config_service.flicker_filter
        else:  # Fallback logic for flicker_params
            flicker_config_dict = getattr(self.config_service, 'flicker_params', {}) 
            if not flicker_config_dict and isinstance(self.config_service, dict):
                flicker_config_dict = self.config_service.get('flicker_filter', {})
            self.logger.info(f"PythonOCRDataConditioner: Attempting to load flicker_params from: {flicker_config_dict}")
            self._flicker_params: FlickerFilterParams = FlickerFilterParams(**flicker_config_dict)

        # Load other configurable parameters for parsing
        self._STABLE_COST_CONFIRMATION_FRAMES = getattr(self.config_service, 'stable_cost_confirmation_frames', 3)
        self._debug_mode = getattr(self.config_service, 'debug_ocr_handler', False) or \
                           getattr(self.config_service, 'DEBUG_OCR_HANDLER', False)
        self._include_all_symbols = getattr(self.config_service, 'include_all_symbols', False)
        self._fuzzy_match_threshold = getattr(self.config_service, 'ocr_fuzzy_match_threshold', 0.7)
        self._max_symbol_length = getattr(self.config_service, 'ocr_max_symbol_length', 6)
        self._min_symbol_length = getattr(self.config_service, 'ocr_min_symbol_length', 1)

        self._decimal_fix_min_ratio = getattr(self._flicker_params, 'decimal_fix_min_ratio',
                                            getattr(self.config_service, 'ocr_decimal_fix_min_ratio', 0.05))
        self._decimal_fix_max_ratio = getattr(self._flicker_params, 'decimal_fix_max_ratio',
                                            getattr(self.config_service, 'ocr_decimal_fix_max_ratio', 20.0))
        self._decimal_fix_max_exponent = getattr(self.config_service, 'ocr_decimal_fix_max_exponent', 6)
        
        # Metrics tracking
        self._flickers_rejected_count = 0
        self._decimal_corrections_count = 0
        self._lines_processed_count = 0
        
        # Validation failure tracking
        self._strategy_validation_failures = 0
        self._symbol_validation_failures = 0
        self._numeric_token_failures = 0
        self._non_tradeable_rejections = 0
        self._unknown_symbol_rejections = 0
        
        # Flicker decision tracking
        self._flicker_confirmations = 0
        self._flicker_rejections = 0
        self._non_flicker_accepts = 0
        self._filter_disabled_accepts = 0
        self._initial_value_accepts = 0
        
        # Decimal correction decision tracking
        self._decimal_corrections_applied = 0
        self._decimal_corrections_skipped = 0
        self._decimal_no_reference_price = 0
        
        self.logger.info(f"PythonOCRDataConditioner initialized. Flicker: {self._flicker_params.enable_flicker_filter}, "
                         f"Confirm Frames: {self._STABLE_COST_CONFIRMATION_FRAMES}, Debug: {self._debug_mode}")

        # Add debug log statement at the VERY END of __init__ method
        self.logger.info(f"[{self.__class__.__name__}] FINAL INITIALIZED self._flicker_params: {self._flicker_params}")
        self.logger.info(f"[{self.__class__.__name__}] FINAL INITIALIZED self._STABLE_COST_CONFIRMATION_FRAMES: {self._STABLE_COST_CONFIRMATION_FRAMES}")

    def _debug_print(self, msg: str):
        if self._debug_mode:
            self.logger.debug(f"[OCR_CONDITIONER_DEBUG] {msg}")

    def _log_pipeline_event(self, event_type: str, data: Any):
        self.logger.debug(f"[CONDITIONER_PIPELINE_EVENT:{event_type}] {data}")

    def _publish_ocr_validation_decision(self, decision_type: str, decision_reason: str, 
                                        context: Dict[str, Any], correlation_id: str = None):
        """Publish OCR validation decisions to dedicated Redis stream."""
        if not self._correlation_logger:
            self.logger.warning(f"Correlation logger not available to publish OCR validation decision: {decision_type}")
            return
            
        try:
            # TANK Mode: Check if IPC data dumping is disabled
            if (hasattr(self.config_service, 'ENABLE_IPC_DATA_DUMP') and 
                not self.config_service.ENABLE_IPC_DATA_DUMP):
                self.logger.debug(f"TANK_MODE: Skipping IPC send for OCR validation decision (Type: {decision_type})")
                return

            import time
            correlation_id = correlation_id or f"ocr_validation_{int(time.time_ns())}"
            
            validation_decision_payload = {
                "decision_type": decision_type,
                "decision_reason": decision_reason,
                "context": context,
                "timestamp": time.time(),
                "component": "PythonOCRDataConditioner"
            }

            if self._correlation_logger:
                # Add correlation_id to payload
                validation_decision_payload["correlation_id"] = correlation_id
                
                self._correlation_logger.log_event(
                    source_component="PythonOCRDataConditioner",
                    event_name="OCRValidationDecision",
                    payload=validation_decision_payload
                )
                self.logger.debug(f"Published OCR validation decision: {decision_type}")
            else:
                self.logger.warning(f"Correlation logger not available to publish OCR validation decision: {decision_type}")
                    
        except Exception as e:
            self.logger.error(f"Error publishing OCR validation decision: {e}", exc_info=True)

    def _publish_ocr_flicker_decision(self, decision_type: str, decision_reason: str, 
                                     context: Dict[str, Any], correlation_id: str = None):
        """Publish OCR flicker filtering decisions to dedicated Redis stream."""
        if not self._ipc_client or not self._correlation_logger:
            return
            
        try:
            # TANK Mode: Check if IPC data dumping is disabled
            if (hasattr(self.config_service, 'ENABLE_IPC_DATA_DUMP') and 
                not self.config_service.ENABLE_IPC_DATA_DUMP):
                self.logger.debug(f"TANK_MODE: Skipping IPC send for OCR flicker decision (Type: {decision_type})")
                return

            import time
            correlation_id = correlation_id or f"ocr_flicker_{int(time.time_ns())}"
            
            flicker_decision_payload = {
                "decision_type": decision_type,
                "decision_reason": decision_reason,
                "context": context,
                "timestamp": time.time(),
                "component": "PythonOCRDataConditioner"
            }

            # Use telemetry service for flicker decision publishing
            target_stream = getattr(self.config_service, 'redis_stream_ocr_cleaned', 'testrade:cleaned-ocr-snapshots')
            
            # Use correlation logger if available
            if self._correlation_logger:
                # Prepare data for correlation logger
                telemetry_data = {
                    "payload": flicker_decision_payload,
                    "correlation_id": correlation_id,
                    "causation_id": None
                }
                
                self._correlation_logger.log_event(
                    source_component="PythonOCRDataConditioner",
                    event_name="OCRFlickerDecision",
                    payload=telemetry_data
                )
                self.logger.debug(f"Published OCR flicker decision: {decision_type}")
            else:
                self.logger.warning(f"Correlation logger not available to publish OCR flicker decision: {decision_type}")
                    
        except Exception as e:
            self.logger.error(f"Error publishing OCR flicker decision: {e}", exc_info=True)

    def _publish_ocr_decimal_correction_decision(self, decision_type: str, decision_reason: str, 
                                               context: Dict[str, Any], correlation_id: str = None):
        """Publish OCR decimal correction decisions to dedicated Redis stream."""
        if not self._ipc_client or not self._correlation_logger:
            return
            
        try:
            # TANK Mode: Check if IPC data dumping is disabled
            if (hasattr(self.config_service, 'ENABLE_IPC_DATA_DUMP') and 
                not self.config_service.ENABLE_IPC_DATA_DUMP):
                self.logger.debug(f"TANK_MODE: Skipping IPC send for OCR decimal correction decision (Type: {decision_type})")
                return

            import time
            correlation_id = correlation_id or f"ocr_decimal_{int(time.time_ns())}"
            
            decimal_decision_payload = {
                "decision_type": decision_type,
                "decision_reason": decision_reason,
                "context": context,
                "timestamp": time.time(),
                "component": "PythonOCRDataConditioner"
            }

            if self._correlation_logger:
                # Prepare data for correlation logger
                telemetry_data = {
                    "payload": decimal_decision_payload,
                    "correlation_id": correlation_id,
                    "causation_id": None
                }
                
                self._correlation_logger.log_event(
                    source_component="PythonOCRDataConditioner",
                    event_name="OCRDecimalCorrectionDecision",
                    payload=telemetry_data
                )
                self.logger.debug(f"Published OCR decimal correction decision: {decision_type}")
            else:
                self.logger.warning(f"Correlation logger not available to publish OCR decimal correction decision: {decision_type}")
                    
        except Exception as e:
            self.logger.error(f"Error publishing OCR decimal correction decision: {e}", exc_info=True)

    async def _get_robust_price_for_filter(self, symbol: str) -> float:
        """
        Gets the best available price by delegating entirely to the
        central PriceRepository. It will use the cache if available, or
        trigger a blocking, instrumented network call if not. This approach
        solves the "Thundering Herd" and "Cache Incoherence" problems.
        """
        correlation_id = get_thread_safe_uuid()
        self.logger.info(f"Conditioner requesting price for {symbol} via IPriceProvider. CorrID: {correlation_id}")
        try:
            # This single call leverages all of PriceRepository's caching,
            # in-flight request locking, and network fallback logic.
            # The get_price_blocking method is safe to call from here because
            # this worker is already in a separate thread from the main event loop.
            price_data = self.price_provider.get_price_blocking(
                symbol,
                timeout_sec=2.0,  # A reasonable timeout for a new symbol
                correlation_id=correlation_id
            )
            price = price_data.price
            self.logger.info(f"Conditioner received price for {symbol}: ${price:.4f} from source '{price_data.source}'. CorrID: {correlation_id}")
            return price

        except (StalePriceError, PriceUnavailableError) as e:
            # This is an expected failure case (e.g., market closed, invalid symbol).
            self.logger.warning(
                f"Price fetch failed for {symbol} via PriceRepository: {e}. "
                f"Returning 0.0 for conditioning. CorrID: {correlation_id}"
            )
            return 0.0
        except Exception as e_main:
            # This is an unexpected failure.
            self.logger.error(
                f"Critical error in _get_robust_price_for_filter for {symbol} via PriceRepository: {e_main}. CorrID: {correlation_id}",
                exc_info=True
            )
            return 0.0

    def _correct_decimal_ocr(self, ocr_val: float, ref_price: float) -> float:
        """Internal decimal correction logic with robustness improvements and legacy clamping."""
        # Handle invalid inputs and near-zero cases (SME suggestion + original check)
        if not isinstance(ocr_val, (int, float)) or not isinstance(ref_price, (int, float)):
            self.logger.debug(f"Decimal correction: Invalid input types. ocr_val: {ocr_val}, ref_price: {ref_price}")
            return ocr_val
        if abs(ref_price) < 1e-9: # If ref_price is essentially zero, no basis for correction
            self._debug_print(f"Decimal correction: ref_price is near zero ({ref_price}). Original ocr_val ({ocr_val}) kept.")
            return ocr_val
        if abs(ocr_val) < 1e-9: # If ocr_val is essentially zero, it's likely correct or too small to meaningfully correct
            self._debug_print(f"Decimal correction: ocr_val is near zero ({ocr_val}). Original ocr_val kept.")
            return ocr_val

        # Use class members for configuration
        min_ratio_cfg = self._decimal_fix_min_ratio
        max_ratio_cfg = self._decimal_fix_max_ratio
        max_exponent_cfg = self._decimal_fix_max_exponent

        original_ocr_val_for_log = ocr_val # Preserve for logging
        ocr_val_sign = -1 if ocr_val < 0 else 1 # Preserve original sign (SME suggestion)

        # Work with absolute values for ratio and log calculation
        abs_ocr_val = abs(ocr_val)
        abs_ref_price = abs(ref_price) # ref_price sign doesn't affect magnitude of correction

        ratio = abs_ocr_val / abs_ref_price # Ratio of magnitudes

        # If already within acceptable band, no correction needed (Legacy behavior)
        if min_ratio_cfg <= ratio <= max_ratio_cfg:
            self._debug_print(f"Decimal correction: Ratio {ratio:.4f} for ocr_val {original_ocr_val_for_log} (ref_price {ref_price}) is within [{min_ratio_cfg}, {max_ratio_cfg}]. Original value kept.")
            
            # Publish decimal correction skipped decision
            skip_context = {
                "original_value": original_ocr_val_for_log,
                "reference_price": ref_price,
                "ratio": ratio,
                "min_ratio_threshold": min_ratio_cfg,
                "max_ratio_threshold": max_ratio_cfg,
                "skip_reason": "RATIO_WITHIN_ACCEPTABLE_RANGE",
                "decision_stage": "DECIMAL_CORRECTION_EVALUATION"
            }
            self._publish_ocr_decimal_correction_decision(
                decision_type="DECIMAL_CORRECTION_SKIPPED",
                decision_reason=f"Value {original_ocr_val_for_log} within acceptable ratio range {ratio:.4f} [{min_ratio_cfg}, {max_ratio_cfg}]",
                context=skip_context
            )
            self._decimal_corrections_skipped += 1
            
            return original_ocr_val_for_log

        try:
            exponent = round(math.log10(ratio))

            if abs(exponent) > max_exponent_cfg:
                self._debug_print(f"Decimal correction: Exponent {exponent} for ocr_val {original_ocr_val_for_log} (ref_price {ref_price}) exceeds max_exponent {max_exponent_cfg}. Original value kept.")
                
                # Publish decimal correction skipped decision
                skip_context = {
                    "original_value": original_ocr_val_for_log,
                    "reference_price": ref_price,
                    "ratio": ratio,
                    "calculated_exponent": exponent,
                    "max_exponent_threshold": max_exponent_cfg,
                    "skip_reason": "EXPONENT_EXCEEDS_MAXIMUM",
                    "decision_stage": "DECIMAL_CORRECTION_EVALUATION"
                }
                self._publish_ocr_decimal_correction_decision(
                    decision_type="DECIMAL_CORRECTION_SKIPPED",
                    decision_reason=f"Value {original_ocr_val_for_log} exponent {exponent} exceeds max {max_exponent_cfg}",
                    context=skip_context
                )
                self._decimal_corrections_skipped += 1
                
                return original_ocr_val_for_log

            correction_factor = 10 ** (-exponent)
            # Apply correction to the absolute value, then reapply original sign
            shifted_abs_val = abs_ocr_val * correction_factor
            shifted_val_signed = shifted_abs_val * ocr_val_sign

        except (ValueError, OverflowError) as e:
            self.logger.warning(f"Decimal correction math error for ocr_val {original_ocr_val_for_log}, ref_price {ref_price}: {e}. Original value kept.")
            return original_ocr_val_for_log

        # CRUCIAL: Legacy post-shift clamping logic
        # Re-calculate ratio based on the *shifted value's magnitude* and *reference price's magnitude*
        # The clamp limits are based on the reference price.
        shifted_ratio_final_check = shifted_abs_val / abs_ref_price if abs_ref_price > 1e-9 else float('inf')

        final_corrected_val = shifted_val_signed # Start with the sign-corrected shifted value

        # Min and Max clamp limits based on abs_ref_price and config ratios
        # These limits should also consider the original sign of ocr_val if clamping is to preserve directionality.
        # However, typically clamping is done on magnitude against ref_price.
        # Let's keep it simple: clamp the magnitude, then re-apply sign.

        # Clamp limits for the magnitude
        abs_min_clamp_val = abs_ref_price * min_ratio_cfg
        abs_max_clamp_val = abs_ref_price * max_ratio_cfg

        clamped_abs_val = shifted_abs_val

        if shifted_abs_val < abs_min_clamp_val:
            clamped_abs_val = abs_min_clamp_val
            self._debug_print(f"Decimal correction: Clamped low. Original: {original_ocr_val_for_log}, Shifted_abs: {shifted_abs_val:.4f}, Clamped_abs: {clamped_abs_val:.4f} (abs_ref_price={abs_ref_price:.4f}, min_ratio={min_ratio_cfg})")
        elif shifted_abs_val > abs_max_clamp_val:
            clamped_abs_val = abs_max_clamp_val
            self._debug_print(f"Decimal correction: Clamped high. Original: {original_ocr_val_for_log}, Shifted_abs: {shifted_abs_val:.4f}, Clamped_abs: {clamped_abs_val:.4f} (abs_ref_price={abs_ref_price:.4f}, max_ratio={max_ratio_cfg})")

        final_corrected_val = clamped_abs_val * ocr_val_sign # Re-apply original sign to the (potentially) clamped absolute value

        # Log meaningful correction details (SME suggestion for precision check)
        if abs(final_corrected_val - original_ocr_val_for_log) > 1e-5: # Check if a significant correction was made
            self.logger.debug(
                f"Decimal corrected: {original_ocr_val_for_log} -> {final_corrected_val:.4f} "
                f"(initial_ratio: {ratio:.4f}, exponent: {exponent}, final_shifted_ratio: {shifted_ratio_final_check:.4f})"
            )
            
            # Publish decimal correction applied decision
            correction_context = {
                "original_value": original_ocr_val_for_log,
                "corrected_value": final_corrected_val,
                "reference_price": ref_price,
                "initial_ratio": ratio,
                "correction_exponent": exponent,
                "final_ratio": shifted_ratio_final_check,
                "min_ratio_threshold": min_ratio_cfg,
                "max_ratio_threshold": max_ratio_cfg,
                "max_exponent_threshold": max_exponent_cfg,
                "was_clamped": abs(clamped_abs_val - shifted_abs_val) > 1e-9,
                "decision_stage": "DECIMAL_CORRECTION_APPLIED"
            }
            self._publish_ocr_decimal_correction_decision(
                decision_type="DECIMAL_CORRECTION_APPLIED",
                decision_reason=f"Value {original_ocr_val_for_log} corrected to {final_corrected_val:.4f} using reference price {ref_price}",
                context=correction_context
            )
            
            # Assuming you have a metrics lock and counter as suggested by SME, or adapt to your existing _decimal_corrections_count
            # For now, using the existing counter:
            self._decimal_corrections_count += 1
            self._decimal_corrections_applied += 1

        return final_corrected_val

    async def _fix_price_with_live(self, ocr_val: float, symbol: str, field_name: str) -> float:
        """
        Apply decimal correction using a live price reference, and proactively
        trigger a subscription if the symbol is new.
        """
        # Skip for non-cost fields
        if field_name.lower() not in ("cost", "cost_basis"):
            self._debug_print(f"Skipping decimal fix for {symbol} field '{field_name}' as it's not cost/cost_basis. Value: {ocr_val}")
            
            # Publish decimal correction skipped decision
            field_skip_context = {
                "symbol": symbol,
                "field_name": field_name,
                "original_value": ocr_val,
                "skip_reason": "FIELD_NOT_COST_BASIS",
                "allowed_fields": ["cost", "cost_basis"],
                "decision_stage": "FIELD_TYPE_CHECK"
            }
            self._publish_ocr_decimal_correction_decision(
                decision_type="DECIMAL_CORRECTION_SKIPPED",
                decision_reason=f"Field {field_name} is not cost/cost_basis, skipping decimal correction",
                context=field_skip_context
            )
            self._decimal_corrections_skipped += 1
            
            return ocr_val

        correlation_id = get_thread_safe_uuid()
        price_data = None
        try:
            # Step 1: Get the price using the central provider.
            price_data = self.price_provider.get_price_blocking(symbol, timeout_sec=2.0, correlation_id=correlation_id)

            # Step 2: Check the "tell" and proactively subscribe if it was a network fetch.
            if price_data and 'rest' in price_data.source:
                self.logger.info(f"New symbol '{symbol}' detected by conditioner (source: {price_data.source}). Activating subscription.")
                # This call is fire-and-forget from the conditioner's perspective.
                self.active_symbols_service.activate_symbol(symbol)

            # Step 3: Perform the decimal correction using the fetched price.
            ref_price = price_data.price if price_data else 0.0
            if ref_price <= 1e-9:
                self.logger.warning(f"No valid reference price for {symbol}. Skipping decimal correction.")
                
                # Publish decimal correction skipped decision
                no_ref_context = {
                    "symbol": symbol,
                    "field_name": field_name,
                    "original_value": ocr_val,
                    "reference_price": ref_price,
                    "skip_reason": "NO_VALID_REFERENCE_PRICE",
                    "decision_stage": "REFERENCE_PRICE_CHECK"
                }
                self._publish_ocr_decimal_correction_decision(
                    decision_type="DECIMAL_CORRECTION_SKIPPED",
                    decision_reason=f"No valid reference price for {symbol} (ref_price={ref_price}), skipping decimal correction",
                    context=no_ref_context
                )
                self._decimal_no_reference_price += 1
                
                return ocr_val

            # Apply decimal correction with valid reference price
            corrected_val = self._correct_decimal_ocr(ocr_val, ref_price)

            if abs(corrected_val - ocr_val) > 1e-5:  # Significant correction made
                self._decimal_corrections_count += 1
                self._debug_print(f"Price correction applied to {symbol} {field_name}: {ocr_val} -> {corrected_val} "
                                f"(ref_price: {ref_price})")
            else:
                self._debug_print(f"No correction needed for {symbol} {field_name}: {ocr_val} "
                                f"(ref_price: {ref_price})")

            return corrected_val

        except (StalePriceError, PriceUnavailableError) as e:
            self.logger.warning(f"Price fetch failed for {symbol} via PriceRepository: {e}. Returning original OCR value.")
            return ocr_val
        except Exception as e:
            self.logger.error(f"Critical error in _fix_price_with_live for {symbol}: {e}", exc_info=True)
            return ocr_val

    async def _parse_cost_basis_token(self, cost_basis_token_str: str, symbol: str) -> float:
        """Parse and condition cost basis token with flicker filtering."""
        try:
            raw_cost_basis = float(fuzzy_number_clean(cost_basis_token_str))
            corrected_cost_basis = await self._fix_price_with_live(raw_cost_basis, symbol, "cost_basis")  # This is new_cost_basis for the filter

            final_cost_basis = corrected_cost_basis  # Default if filter is off or no old_cb

            if self._flicker_params.enable_flicker_filter:
                old_stable_cost = self._last_stable_cost_map.get(symbol)
                # _apply_flicker_filter_to_cost_basis decides if corrected_cost_basis is kept or old_stable_cost
                evaluated_cost_basis = self._apply_flicker_filter_to_cost_basis(corrected_cost_basis, symbol)

                if old_stable_cost is None or abs(evaluated_cost_basis - old_stable_cost) > 1e-5:  # If it's a new stable value
                    if abs(evaluated_cost_basis - corrected_cost_basis) < 1e-5:  # And it's the current OCR value (not reverted)
                        self._last_stable_cost_map[symbol] = evaluated_cost_basis
                        self._debug_print(f"[{symbol}] _parse_cost_basis_token: Updated _last_stable_cost_map to {evaluated_cost_basis}")
                    # If evaluated_cost_basis is old_stable_cost, _last_stable_cost_map is already correct.

                final_cost_basis = evaluated_cost_basis
            else:  # Flicker filter disabled
                self._last_stable_cost_map[symbol] = corrected_cost_basis  # Directly update stable cost
                self._debug_print(f"[{symbol}] _parse_cost_basis_token: Flicker disabled. Updated _last_stable_cost_map to {corrected_cost_basis}")

            return final_cost_basis

        except ValueError:
            self.logger.warning(f"Could not parse cost basis token '{cost_basis_token_str}' for {symbol}")
            return self._last_stable_cost_map.get(symbol, 0.0)  # Fallback to last known stable or 0

    def _apply_flicker_filter_to_cost_basis(self, new_cost_basis: float, symbol: str) -> float:
        """Apply flicker filtering to cost basis using SME's simplified logic."""
        import math
        import time

        self.logger.info(f"[{symbol}] ENTERING _apply_flicker_filter (SME Logic) with new_cost_basis: {new_cost_basis}")

        old_cb = self._last_stable_cost_map.get(symbol)

        if old_cb is None:  # No prior stable cost
            self._debug_print(f"[{symbol}] Flicker: No old_cb. Initial value scenario.")
            
            # Publish initial value decision
            initial_context = {
                "symbol": symbol,
                "accepted_value": new_cost_basis,
                "previous_stable_value": None,
                "decision_reason": "INITIAL_VALUE_NO_REFERENCE",
                "flicker_enabled": self._flicker_params.enable_flicker_filter,
                "decision_stage": "INITIAL_VALUE_CHECK"
            }
            self._publish_ocr_flicker_decision(
                decision_type="INITIAL_VALUE_ACCEPTED",
                decision_reason=f"Cost basis {new_cost_basis} accepted as initial value for {symbol} (no prior reference)",
                context=initial_context
            )
            self._initial_value_accepts += 1
            
            # For the very first value, or after a reset, it doesn't make sense to call it a "flicker" yet.
            # It should become the first candidate for stability.
            # This new SME logic focuses on what to do IF something IS a flicker.
            # If there's no old_cb, the flicker_check_new_cost_basis would likely pass it or have no reference.
            # Let's assume if old_cb is None, the value is accepted and becomes the new stable.
            # The _parse_cost_basis_token should handle setting _last_stable_cost_map.
            # This function's job is to decide if new_cost_basis is a flicker against old_cb.
            # If old_cb is None, it's not a flicker in this context.
            if symbol in self._cost_basis_flicker_tracker:  # Reset tracker if symbol had prior flicker state
                del self._cost_basis_flicker_tracker[symbol]
            return new_cost_basis  # Accept and let _parse_cost_basis_token make it stable

        if not self._flicker_params.enable_flicker_filter:
            self._debug_print(f"[{symbol}] Flicker: Filter disabled. Accepting {new_cost_basis}")
            
            # Publish flicker filter disabled decision
            disabled_context = {
                "symbol": symbol,
                "accepted_value": new_cost_basis,
                "previous_stable_value": old_cb,
                "decision_reason": "FLICKER_FILTER_DISABLED",
                "flicker_enabled": self._flicker_params.enable_flicker_filter,
                "decision_stage": "FLICKER_FILTER_CHECK"
            }
            self._publish_ocr_flicker_decision(
                decision_type="FILTER_DISABLED_ACCEPTED",
                decision_reason=f"Cost basis {new_cost_basis} accepted for {symbol} (flicker filter disabled)",
                context=disabled_context
            )
            self._filter_disabled_accepts += 1
            
            if symbol in self._cost_basis_flicker_tracker:
                del self._cost_basis_flicker_tracker[symbol]
            return new_cost_basis

        # Use existing flicker_check_new_cost_basis as the "is_flicker" function
        is_value_a_flicker = not flicker_check_new_cost_basis(  # Note the "not"
            price_provider=self.price_provider,
            S_old=getattr(self._flicker_params, 'known_add_shares', 10000),  # Or your default
            C_old=old_cb,
            C_new_ocr=new_cost_basis,
            flicker_params=self._flicker_params,
            symbol=symbol
        )
        self.logger.info(f"[{symbol}] Flicker: flicker_check_new_cost_basis determined is_value_a_flicker: {is_value_a_flicker} (True means REJECT by check)")

        if is_value_a_flicker:
            self._debug_print(f"[{symbol}] Flicker: DETECTED. new_cost_basis={new_cost_basis}, old_cb={old_cb}")
            # FLICKER DETECTED - Apply stateful flicker filtering logic

            last_rejected_value_for_symbol = None
            current_flicker_count = 0

            if symbol in self._cost_basis_flicker_tracker:
                tracked_value, count = self._cost_basis_flicker_tracker[symbol]
                if math.isclose(tracked_value, new_cost_basis, abs_tol=1e-5):  # Is it the same flickering value?
                    current_flicker_count = count + 1
                    last_rejected_value_for_symbol = new_cost_basis
                    self._debug_print(f"[{symbol}] Flicker: Same flickering value {new_cost_basis} seen. Count incremented to {current_flicker_count}.")
                else:  # New flickering value
                    current_flicker_count = 1  # SME said 0, but to meet N on Nth occurrence, start at 1 for first sighting OF THIS FLICKER
                    last_rejected_value_for_symbol = new_cost_basis
                    self._debug_print(f"[{symbol}] Flicker: New flickering value {new_cost_basis}. Count reset to {current_flicker_count}.")
            else:  # First time this symbol is triggering a flicker
                current_flicker_count = 1  # SME said 0, but let's align: first sighting of a flicker = count 1
                last_rejected_value_for_symbol = new_cost_basis
                self._debug_print(f"[{symbol}] Flicker: First flicker detected for symbol: {new_cost_basis}. Count initialized to {current_flicker_count}.")

            self._cost_basis_flicker_tracker[symbol] = (last_rejected_value_for_symbol, current_flicker_count)

            # --- MODIFIED ACCEPTANCE LOGIC ---
            accept_flicker_by_confirmation = False
            if self._STABLE_COST_CONFIRMATION_FRAMES == 1:
                # For CONF_FRAMES = 1, we need to see the flicker *more than once* to accept it.
                # So, if count is 1 (first sighting of this flicker), reject. If count > 1, accept.
                if current_flicker_count > 1:  # Effectively means it's the 2nd consecutive sighting
                    accept_flicker_by_confirmation = True
            else:  # For CONF_FRAMES > 1
                if current_flicker_count >= self._STABLE_COST_CONFIRMATION_FRAMES:
                    accept_flicker_by_confirmation = True
            # --- END OF MODIFIED ACCEPTANCE LOGIC ---

            if accept_flicker_by_confirmation:
                self._debug_print(f"[{symbol}] Flicker: Value {new_cost_basis} CONFIRMED after {current_flicker_count} occurrences (threshold logic met for CONF_FRAMES={self._STABLE_COST_CONFIRMATION_FRAMES}). ACCEPTING.")
                
                # Publish flicker confirmation decision
                confirmation_context = {
                    "symbol": symbol,
                    "accepted_value": new_cost_basis,
                    "previous_stable_value": old_cb,
                    "confirmation_count": current_flicker_count,
                    "required_confirmations": self._STABLE_COST_CONFIRMATION_FRAMES,
                    "decision_reason": "FLICKER_CONFIRMED_BY_REPETITION",
                    "flicker_enabled": self._flicker_params.enable_flicker_filter,
                    "decision_stage": "FLICKER_CONFIRMATION"
                }
                self._publish_ocr_flicker_decision(
                    decision_type="FLICKER_CONFIRMED",
                    decision_reason=f"Cost basis {new_cost_basis} confirmed as valid for {symbol} after {current_flicker_count} occurrences",
                    context=confirmation_context
                )
                self._flicker_confirmations += 1
                
                if symbol in self._cost_basis_flicker_tracker:
                    del self._cost_basis_flicker_tracker[symbol]
                self._flickers_rejected_count += 1  # Log as it was initially a flicker
                self._discarded_flickers.append({
                    'symbol': symbol, 'rejected_value': new_cost_basis, 'kept_value': old_cb,
                    'timestamp': time.time(), 'reason': 'flicker_confirmed_override'
                })
                return new_cost_basis
            else:
                # Reject the flicker
                self._debug_print(f"[{symbol}] Flicker: Value {new_cost_basis} REJECTED. Count {current_flicker_count} not sufficient for CONF_FRAMES={self._STABLE_COST_CONFIRMATION_FRAMES}. Using old_cb: {old_cb}")
                
                # Publish flicker rejection validation failure
                flicker_context = {
                    "symbol": symbol,
                    "rejected_value": new_cost_basis,
                    "stable_value": old_cb,
                    "flicker_count": current_flicker_count,
                    "required_confirmations": self._STABLE_COST_CONFIRMATION_FRAMES,
                    "rejection_reason": "FLICKER_FILTERING",
                    "flicker_enabled": self._flicker_params.enable_flicker_filter,
                    "validation_stage": "FLICKER_FILTER"
                }
                self._publish_ocr_flicker_decision(
                    decision_type="FLICKER_REJECTED",
                    decision_reason=f"Cost basis {new_cost_basis} rejected as flicker for {symbol} (count {current_flicker_count}/{self._STABLE_COST_CONFIRMATION_FRAMES})",
                    context=flicker_context
                )
                self._flicker_rejections += 1
                
                # Note: _flickers_rejected_count & _discarded_flickers for this path
                # should only be incremented if it's a *new* flicker rejection instance,
                # or handled carefully to avoid double counting if already done by primary check.
                # For simplicity, let's assume the earlier log was sufficient, or add a distinct reason.
                # The previous code already incremented based on `is_value_a_flicker`.
                return old_cb
        else:
            # NOT A FLICKER: accept the new cost basis and reset the flicker tracker for this symbol
            self._debug_print(f"[{symbol}] Flicker: NOT a flicker. Accepting {new_cost_basis}. Resetting tracker.")
            
            # Publish non-flicker decision
            non_flicker_context = {
                "symbol": symbol,
                "accepted_value": new_cost_basis,
                "previous_stable_value": old_cb,
                "decision_reason": "VALUE_NOT_A_FLICKER",
                "flicker_enabled": self._flicker_params.enable_flicker_filter,
                "decision_stage": "FLICKER_EVALUATION",
                "flicker_check_passed": True
            }
            self._publish_ocr_flicker_decision(
                decision_type="NON_FLICKER_ACCEPTED",
                decision_reason=f"Cost basis {new_cost_basis} accepted as non-flicker for {symbol}",
                context=non_flicker_context
            )
            self._non_flicker_accepts += 1
            
            if symbol in self._cost_basis_flicker_tracker:
                del self._cost_basis_flicker_tracker[symbol]
            return new_cost_basis

    async def _parse_pnl_per_share_token(self, token_str: str, symbol: str, cost_basis: Optional[float]) -> float:
        self._debug_print(f"Parsing PnL/Share for {symbol}: '{token_str}'")
        # Apply common cleaning/fixing steps from standalone helpers (already available in your class)
        cleaned_token = strip_trailing_letters_if_numeric(token_str)
        cleaned_token = fix_leading_4_if_no_sign(cleaned_token)
        cleaned_token = fuzzy_number_clean(cleaned_token) # Use main cleaner

        # Skip single-digit values like "5", "+3", "-1" as likely noise
        if re.match(r'^[+\-]?\d$', cleaned_token):
            self._debug_print(f"PnL/Share skipping single-digit token: '{token_str}' -> '{cleaned_token}'")
            return 0.0

        # Attempt sign inference if no sign present and we have price/cost data
        if symbol and cost_basis is not None and cost_basis > 1e-9 and \
           cleaned_token and not cleaned_token.startswith(('+', '-')):
            try:
                val = float(cleaned_token) # Ensure this can be float before proceeding
                if abs(val) > 1e-9: # Only infer if value is non-zero
                    live_price = await self._get_robust_price_for_filter(symbol)
                    if live_price > 1e-9:
                        if live_price > cost_basis:
                            cleaned_token = '+' + cleaned_token
                        elif live_price < cost_basis:
                            cleaned_token = '-' + cleaned_token
                        self._debug_print(f"PnL/Share sign inferred: {cleaned_token} (Live:{live_price:.2f} vs Cost:{cost_basis:.2f})")
                    else:
                        # No valid live price - skip sign inference (normal during market closure)
                        self._debug_print(f"PnL/Share sign inference skipped for {symbol}: no valid live price "
                                        f"(live_price={live_price}). This is normal during market closure. "
                                        f"Token remains: {cleaned_token}")
            except ValueError:
                pass # Ignore if token wasn't float after all
            except Exception as e:
                self._debug_print(f"PnL/Share sign inference failed for {symbol}: {e}. Token remains: {cleaned_token}")

        # Insert decimal if missing (assume last two digits are cents)
        # Ensure it's done *after* sign inference/cleaning
        if '.' not in cleaned_token:
            match = re.match(r'^([+\-]?)(\d+)$', cleaned_token)
            if match:
                sign, digits = match.groups()
                if len(digits) <= 2: cleaned_token = f"{sign}0.{digits.zfill(2)}" # Pad with leading zero if needed
                else: cleaned_token = f"{sign}{digits[:-2]}.{digits[-2:]}"
                self._debug_print(f"PnL/Share decimal inserted: {cleaned_token}")

        try: final_val = float(cleaned_token); return final_val
        except ValueError: self._debug_print(f"PnL/Share parse error: '{token_str}' -> '{cleaned_token}'. Fallback 0.0"); return 0.0

    def _parse_realized_pnl_token(self, token_str: str, symbol: str) -> float:
        self._debug_print(f"Parsing Realized PnL: '{token_str}' for symbol {symbol} (symbol unused in this legacy logic)")
        # Apply common cleaning/fixing steps from standalone helpers (already available in your class)
        cleaned_token = strip_trailing_letters_if_numeric(token_str)
        cleaned_token = fix_leading_4_if_no_sign(cleaned_token)
        # Realized PnL often lacks explicit '+', remove if present for cleaner parsing
        if cleaned_token.startswith('+'): cleaned_token = cleaned_token[1:]
        cleaned_token = fuzzy_number_clean(cleaned_token) # Use main cleaner

        # Skip single-digit values like "5", "+3", "-1" as likely noise
        if re.match(r'^[+\-]?\d$', cleaned_token):
            self._debug_print(f"Realized PnL skipping single-digit token: '{token_str}' -> '{cleaned_token}'")
            return 0.0

        # Insert decimal if missing (assume last two digits are cents)
        if '.' not in cleaned_token:
             match = re.match(r'^([+\-]?)(\d+)$', cleaned_token)
             if match:
                 sign, digits = match.groups()
                 if len(digits) <= 2: cleaned_token = f"{sign}0.{digits.zfill(2)}"
                 else: cleaned_token = f"{sign}{digits[:-2]}.{digits[-2:]}"
                 self._debug_print(f"Realized PnL decimal inserted: {cleaned_token}")

        try: final_val = float(cleaned_token); return final_val
        except ValueError: self._debug_print(f"Realized PnL parse error: '{token_str}' -> '{cleaned_token}'. Fallback 0.0"); return 0.0

    async def _process_single_line(self, line: str, perf_timestamps: Optional[Dict] = None) -> Optional[Dict[str, Any]]:
        """
        Processes a single line of OCR text into structured 5-token data.
        Enhanced version with legacy ProcessedOCRHandler logic.
        """
        self._debug_print(f"Processing line: '{line}'")
        self._lines_processed_count += 1

        if not line:
            return None

        try:
            cleaned_line = remove_non_ascii(line)
            cleaned_line = clean_ocr_line(cleaned_line)
            if not cleaned_line:
                return None

            tokens = cleaned_line.split()
            if not tokens:
                return None

            # 1. Detect Strategy Hint
            trade_keywords = ["Long", "Closed", "Short"]
            ocr_display_strategy, _ = fuzzy_match_word(tokens[0], trade_keywords, threshold=self._fuzzy_match_threshold)

            if not ocr_display_strategy:
                 if tokens[0].lower() == "trade" and len(tokens) > 1:  # Handle "Trade Long", "Trade Closed"
                      ocr_display_strategy, _ = fuzzy_match_word(tokens[1], trade_keywords, threshold=self._fuzzy_match_threshold)
                      if ocr_display_strategy:
                          tokens.pop(0)  # Consume "Trade"
                 if not ocr_display_strategy:
                      self.logger.debug(f"No valid strategy keyword in line: '{line}'")
                      self._strategy_validation_failures += 1
                      
                      # Publish strategy validation failure
                      strategy_context = {
                          "raw_line": line,
                          "tokens": tokens,
                          "first_token": tokens[0] if tokens else "",
                          "expected_keywords": trade_keywords,
                          "fuzzy_match_threshold": self._fuzzy_match_threshold,
                          "validation_stage": "STRATEGY_DETECTION"
                      }
                      self._publish_ocr_validation_decision(
                          decision_type="STRATEGY_VALIDATION_FAILURE",
                          decision_reason=f"No valid strategy keyword found in line, first token: '{tokens[0] if tokens else ''}'",
                          context=strategy_context
                      )
                      
                      return None

            current_ocr_internal_strategy = ocr_display_strategy.lower()

            # 2. Detect Symbol with robust validation
            symbol = None
            symbol_token_index = -1
            if len(tokens) > 1:
                potential_symbol_token = tokens[1] if tokens[0].lower() != "trade" else tokens[2] if len(tokens) > 2 else ""
                cleaned_symbol_token = re.sub(r"^[^A-Za-z0-9]+|[^A-Za-z0-9]+$", "", potential_symbol_token)
                if (cleaned_symbol_token and not is_token_numeric(cleaned_symbol_token) and
                    self._min_symbol_length <= len(cleaned_symbol_token) <= self._max_symbol_length):
                     symbol = cleaned_symbol_token.upper()
                     symbol_token_index = tokens.index(potential_symbol_token)

            if not symbol:
                self.logger.debug(f"No valid symbol found in line: '{line}'")
                self._symbol_validation_failures += 1
                
                # Publish symbol validation failure
                symbol_context = {
                    "raw_line": line,
                    "tokens": tokens,
                    "potential_symbol_token": tokens[1] if len(tokens) > 1 else "",
                    "strategy": current_ocr_internal_strategy,
                    "min_symbol_length": self._min_symbol_length,
                    "max_symbol_length": self._max_symbol_length,
                    "validation_stage": "SYMBOL_DETECTION"
                }
                self._publish_ocr_validation_decision(
                    decision_type="SYMBOL_VALIDATION_FAILURE",
                    decision_reason="No valid symbol found in line",
                    context=symbol_context
                )
                
                return None

            ### --- NEW JIT VALIDATION BLOCK START --- ###
            
            # 3. Validate Symbol on-the-fly if it's not already known
            if not self.active_symbols_service.is_relevant_for_publishing(symbol):
                self.logger.info(f"Conditioner: New symbol '{symbol}' detected. Attempting JIT validation via price fetch...")
                try:
                    # This is the blocking API call that acts as our validator.
                    price_data = self.price_provider.get_price_blocking(symbol, timeout_sec=2.0)
                    
                    # If we get here without an error, the symbol is real.
                    if price_data and price_data.price > 0:
                        self.logger.critical(f"Conditioner: JIT VALIDATION SUCCESS for '{symbol}'. Activating and subscribing.")
                        
                        # Command ActiveSymbolsService to make this symbol "hot".
                        self.active_symbols_service.activate_symbol(symbol)
                        
                        # The symbol is now considered valid for processing.
                    else:
                        # API returned a valid response but with no price. Treat as invalid for trading.
                        self.logger.warning(f"Conditioner: JIT VALIDATION FAILED for '{symbol}'. Symbol may exist but has no price data. Discarding line.")
                        self._unknown_symbol_rejections += 1
                        
                        # Publish validation failure decision
                        validation_context = {
                            "raw_line": line,
                            "symbol": symbol,
                            "strategy": current_ocr_internal_strategy,
                            "rejection_reason": "SYMBOL_NO_PRICE_DATA",
                            "validation_stage": "JIT_PRICE_VALIDATION"
                        }
                        self._publish_ocr_validation_decision(
                            decision_type="JIT_VALIDATION_FAILURE",
                            decision_reason=f"Symbol {symbol} exists but has no price data",
                            context=validation_context
                        )
                        return None

                except (PriceUnavailableError, StalePriceError) as e:
                    # This is the primary failure path for an invalid symbol (e.g., "FSLLLY").
                    # The PriceRepository correctly told us the symbol is not available.
                    self.logger.warning(f"Conditioner: JIT VALIDATION FAILED for '{symbol}'. It is likely an invalid symbol. Discarding line. Reason: {e}")
                    self._unknown_symbol_rejections += 1
                    
                    # Publish validation failure decision
                    validation_context = {
                        "raw_line": line,
                        "symbol": symbol,
                        "strategy": current_ocr_internal_strategy,
                        "rejection_reason": "SYMBOL_INVALID_OR_UNAVAILABLE",
                        "validation_stage": "JIT_PRICE_VALIDATION",
                        "error_details": str(e)
                    }
                    self._publish_ocr_validation_decision(
                        decision_type="JIT_VALIDATION_FAILURE",
                        decision_reason=f"Symbol {symbol} validation failed: {e}",
                        context=validation_context
                    )
                    return None # Stop processing this line.
            
            ### --- NEW JIT VALIDATION BLOCK END --- ###
            
            # If we've reached this point, the symbol is guaranteed to be valid and active (or will be shortly).

            # --- Strategy-Based State Management (Invalidate Stable Cost) ---
            previous_internal_strategy = self._last_known_strategy_map.get(symbol)
            invalidate_stable_cost = False
            if current_ocr_internal_strategy in ["long", "short"]:
                if not previous_internal_strategy or previous_internal_strategy == "closed":
                    invalidate_stable_cost = True
            elif current_ocr_internal_strategy == "closed":
                if previous_internal_strategy and previous_internal_strategy not in ["closed", None]:
                    invalidate_stable_cost = True

            if invalidate_stable_cost and symbol in self._last_stable_cost_map:
                self.logger.info(f"Strategy change for {symbol} ('{previous_internal_strategy}' -> '{current_ocr_internal_strategy}'). Invalidating stable cost.")
                del self._last_stable_cost_map[symbol]
                # Clear flicker tracker for this symbol as well
                if symbol in self._cost_basis_flicker_tracker:
                    del self._cost_basis_flicker_tracker[symbol]

            self._last_known_strategy_map[symbol] = current_ocr_internal_strategy

            # 3. Extract Numeric Tokens (Cost Basis, PnL/Share, Realized PnL)
            start_index_numerics = (symbol_token_index + 1) if symbol_token_index != -1 else (2 if tokens[0].lower() == "trade" else 1)

            numeric_tokens_raw = [t for t in tokens[start_index_numerics:]]

            # Clean them up first
            numeric_tokens_cleaned = []
            for token_raw in numeric_tokens_raw:
                cleaned_num_token = strip_trailing_letters_if_numeric(token_raw.replace(",", ""))
                if is_token_numeric(fuzzy_number_clean(cleaned_num_token)):
                    numeric_tokens_cleaned.append(cleaned_num_token)

            self._debug_print(f"For {symbol}, extracted numeric tokens: {numeric_tokens_cleaned} from raw: {numeric_tokens_raw}")

            if len(numeric_tokens_cleaned) < 3:
                self.logger.debug(f"Insufficient numeric tokens for {symbol} in line: '{line}'. Found: {numeric_tokens_cleaned}")
                self._numeric_token_failures += 1
                
                # Publish numeric token validation failure
                numeric_context = {
                    "raw_line": line,
                    "symbol": symbol,
                    "strategy": current_ocr_internal_strategy,
                    "numeric_tokens_raw": numeric_tokens_raw,
                    "numeric_tokens_cleaned": numeric_tokens_cleaned,
                    "required_tokens": 3,
                    "found_tokens": len(numeric_tokens_cleaned),
                    "validation_stage": "NUMERIC_TOKENS_EXTRACTION"
                }
                self._publish_ocr_validation_decision(
                    decision_type="INSUFFICIENT_NUMERIC_TOKENS",
                    decision_reason=f"Insufficient numeric tokens for {symbol}: found {len(numeric_tokens_cleaned)}, required 3",
                    context=numeric_context
                )
                
                return None

            cost_basis_token_str = numeric_tokens_cleaned[0]
            pnl_token_str = numeric_tokens_cleaned[1]
            realized_pnl_token_str = numeric_tokens_cleaned[2]

            # --- Parse each of the 5 tokens ---
            # Cost Basis: If strategy is active ("long", "short"), use flicker-filtered parsing.
            cost_basis_for_snapshot = 0.0
            if current_ocr_internal_strategy in ["long", "short"]:
                cost_basis_for_snapshot = await self._parse_cost_basis_token(cost_basis_token_str, symbol)
            elif current_ocr_internal_strategy == "closed":
                try:
                    raw_cb_closed = float(fuzzy_number_clean(cost_basis_token_str))
                    cost_basis_for_snapshot = await self._fix_price_with_live(raw_cb_closed, symbol, "cost_basis")
                except ValueError:
                    cost_basis_for_snapshot = 0.0
            else:  # Unknown strategy, parse directly
                try:
                    cost_basis_for_snapshot = float(fuzzy_number_clean(cost_basis_token_str))
                except ValueError:
                    cost_basis_for_snapshot = 0.0

            # PnL/Share:
            pnl_per_share_for_snapshot = await self._parse_pnl_per_share_token(pnl_token_str, symbol, cost_basis_for_snapshot)

            # Realized PnL:
            realized_pnl_for_snapshot = self._parse_realized_pnl_token(realized_pnl_token_str, symbol)

            parsed_line_data = {
                'strategy_hint': ocr_display_strategy,  # Use original case for display
                'symbol': symbol,
                'cost_basis': cost_basis_for_snapshot,
                'pnl_per_share': pnl_per_share_for_snapshot,
                'realized_pnl': realized_pnl_for_snapshot,
                'raw_line_for_debug': line  # Optional: for easier debugging
            }
            self._debug_print(f"Successfully parsed line for {symbol}: {parsed_line_data}")
            return parsed_line_data

        except Exception as e:
            self.logger.error(f"PythonOCRDataConditioner: Error processing line '{line}': {e}", exc_info=True)
            return None

    # --- IOCRDataConditioner Interface Methods ---
    async def condition_ocr_data(self, raw_ocr_event_data: OCRParsedData) -> CleanedOcrResult:
        """
        Takes raw OCRParsedData, performs all parsing, cleaning, validation,
        decimal correction, and flicker filtering to produce clean, stable 5-token data.
        
        Returns CleanedOcrResult with world-class metadata/payload separation.
        """
        # T4: Conditioning pipeline start (nanosecond precision)
        t4_start_ns = time.perf_counter_ns()
        
        # Extract metadata from parent OCR event
        parent_metadata = getattr(raw_ocr_event_data, 'metadata', {})
        parent_event_id = parent_metadata.get('eventId') or getattr(raw_ocr_event_data, 'event_id', None)
        parent_correlation_id = parent_metadata.get('correlationId') or getattr(raw_ocr_event_data, 'master_correlation_id', None) or parent_event_id or "NO_CORR_ID"
        
        # Create child event metadata with proper causation chain
        child_event_id = get_thread_safe_uuid()
        child_metadata = {
            "eventId": child_event_id,
            "correlationId": parent_correlation_id,  # Maintain correlation chain
            "causationId": parent_event_id,          # Link to immediate parent
            "timestamp_ns": t4_start_ns,
            "epoch_timestamp_s": time.time(),
            "eventType": "TESTRADE_OCR_CLEANED_RESULT",
            "sourceComponent": "PythonOCRDataConditioner",
            "pipeline_stage": "T4_T5_T6_CONDITIONING",
            "parent_event_metadata": {
                "parent_event_id": parent_event_id,
                "parent_event_type": parent_metadata.get('eventType', 'UNKNOWN'),
                "parent_source_component": parent_metadata.get('sourceComponent', 'UNKNOWN')
            }
        }
        
        # Extract T0 timestamp from parent for end-to-end timing analysis
        t0_timestamp_ns = parent_metadata.get('T0_ImageIngress_ns') or getattr(raw_ocr_event_data, 'T0_ImageIngress_ns', None)
        if t0_timestamp_ns:
            child_metadata['pipeline_timing'] = {
                'T0_to_T4_latency_ms': (t4_start_ns - t0_timestamp_ns) / 1_000_000
            }
        
        self.logger.info(f"[OCR_CONDITIONER] T4 Start - Processing OCR data with CorrID: {parent_correlation_id}, ChildEventID: {child_event_id}")
        
        # Initialize result with metadata
        result = CleanedOcrResult(
            metadata=child_metadata,
            T4_ConditioningStart_ns=t4_start_ns,
            parent_ocr_event_id=parent_event_id,
            parent_correlation_id=parent_correlation_id
        )
        
        # --- PROPAGATE C++ TIMESTAMPS ---
        # Copy the C++ timestamps from the input object to the output object.
        if hasattr(raw_ocr_event_data, 'cxx_milestones_ns') and raw_ocr_event_data.cxx_milestones_ns:
            result.metadata['cxx_milestones_ns'] = raw_ocr_event_data.cxx_milestones_ns
            self.logger.debug(f"[OCR_CONDITIONER] Propagated C++ timestamps for CorrID: {parent_correlation_id}")
        # --- END OF C++ TIMESTAMP PROPAGATION ---
        
        try:
            # Validate input data
            if not raw_ocr_event_data or not raw_ocr_event_data.full_raw_text:
                # T6: Early completion for empty data
                result.T6_ConditioningComplete_ns = time.perf_counter_ns()
                self.logger.warning(f"[OCR_CONDITIONER] T6 Early - Empty OCR data for CorrID: {parent_correlation_id}")
                return result

            self._debug_print(f"Conditioning OCR data. Frame TS: {raw_ocr_event_data.frame_timestamp}, Text: '{raw_ocr_event_data.full_raw_text[:50] if raw_ocr_event_data.full_raw_text else ''}...'")

            # T5: Validation and line processing phase
            t5_validation_start_ns = time.perf_counter_ns()
            
            conditioned_snapshots: Dict[str, Dict[str, Any]] = {}
            lines = raw_ocr_event_data.full_raw_text.split('\n')
            
            for line_content in lines:
                result.lines_processed += 1
                line_data = await self._process_single_line(line_content, None)

                if line_data and line_data.get('symbol'):
                    symbol = line_data['symbol']
                    # Construct the snapshot with the 5 tokens + symbol
                    # Ensure all numeric values are floats. Strategy hint is string.
                    conditioned_snapshots[symbol] = {
                        'symbol': symbol,
                        'strategy_hint': str(line_data.get('strategy_hint', 'Unknown')),
                        'cost_basis': float(line_data.get('cost_basis', 0.0)),
                        'pnl_per_share': float(line_data.get('pnl_per_share', 0.0)),
                        'realized_pnl': float(line_data.get('realized_pnl', 0.0))
                    }
                    result.symbols_processed += 1

            # T5: Validation complete
            result.T5_ValidationComplete_ns = time.perf_counter_ns()
            result.conditioned_snapshots = conditioned_snapshots
            
            # Capture conditioning metrics
            result.flickers_rejected = self._flickers_rejected_count
            result.decimal_corrections_applied = self._decimal_corrections_applied
            result.validation_failures = (self._strategy_validation_failures + 
                                        self._symbol_validation_failures + 
                                        self._numeric_token_failures)

            self._debug_print(f"Conditioned snapshots: {conditioned_snapshots}")
            
            # T6: Conditioning complete
            result.T6_ConditioningComplete_ns = time.perf_counter_ns()
            
            # Calculate precise timing metrics for metadata
            t4_to_t5_latency_ms = (result.T5_ValidationComplete_ns - t4_start_ns) / 1_000_000
            t5_to_t6_latency_ms = (result.T6_ConditioningComplete_ns - result.T5_ValidationComplete_ns) / 1_000_000
            total_conditioning_ms = (result.T6_ConditioningComplete_ns - t4_start_ns) / 1_000_000
            
            # Update metadata with timing analysis
            result.metadata['conditioning_timing'] = {
                'T4_to_T5_validation_ms': t4_to_t5_latency_ms,
                'T5_to_T6_finalization_ms': t5_to_t6_latency_ms,
                'total_T4_to_T6_ms': total_conditioning_ms
            }
            
            # Add end-to-end pipeline timing if T0 is available
            if t0_timestamp_ns:
                t0_to_t6_total_ms = (result.T6_ConditioningComplete_ns - t0_timestamp_ns) / 1_000_000
                result.metadata['pipeline_timing']['T0_to_T6_total_ms'] = t0_to_t6_total_ms
            
            self.logger.info(f"[OCR_CONDITIONER] T6 Complete - CorrID: {parent_correlation_id}, "
                           f"Symbols: {result.symbols_processed}, Lines: {result.lines_processed}, "
                           f"T4->T6: {total_conditioning_ms:.2f}ms")
            
            # WORLD-CLASS INSTRUMENTATION: Fire-and-forget T4-T6 milestone telemetry
            if self._correlation_logger:
                try:
                    telemetry_payload = {
                        # Milestone timestamps (nanoseconds)
                        "T4_ConditioningStart_ns": result.T4_ConditioningStart_ns,
                        "T5_ValidationComplete_ns": result.T5_ValidationComplete_ns,
                        "T6_ConditioningComplete_ns": result.T6_ConditioningComplete_ns,
                        
                        # Calculated latencies
                        "T4_to_T5_validation_ms": t4_to_t5_latency_ms,
                        "T5_to_T6_finalization_ms": t5_to_t6_latency_ms,
                        "total_T4_to_T6_ms": total_conditioning_ms,
                        
                        # Processing context
                        "symbols_processed": result.symbols_processed,
                        "lines_processed": result.lines_processed,
                        "flickers_rejected": result.flickers_rejected,
                        "decimal_corrections_applied": result.decimal_corrections_applied,
                        "validation_failures": result.validation_failures,
                        
                        # Causation chain
                        "parent_event_id": parent_event_id,
                        "correlation_id": parent_correlation_id,
                        "child_event_id": result.metadata.get('eventId'),
                        
                        # Pipeline timing if T0 available
                        "T0_to_T4_latency_ms": result.metadata.get('pipeline_timing', {}).get('T0_to_T4_latency_ms'),
                        "T0_to_T6_total_ms": t0_to_t6_total_ms if t0_timestamp_ns else None,
                        
                        # Add origin timestamp for correlation logger
                        "origin_timestamp_s": result.T4_ConditioningStart_ns / 1_000_000_000
                    }
                    
                    # Fire and forget - no blocking!
                    self._correlation_logger.log_event(
                        source_component="PythonOCRDataConditioner",
                        event_name="OCRConditioningMilestones",
                        payload=telemetry_payload
                    )
                except Exception as telemetry_error:
                    # Never let telemetry errors affect trading
                    self.logger.debug(f"Telemetry log error (non-critical): {telemetry_error}")
            
            return result

        except Exception as e:
            # T6: Error completion
            result.T6_ConditioningComplete_ns = time.perf_counter_ns()
            result.metadata['error_details'] = {
                'error_message': str(e),
                'error_type': type(e).__name__,
                'error_stage': 'conditioning_pipeline'
            }
            
            self.logger.error(f"[OCR_CONDITIONER] T6 Error - CorrID: {parent_correlation_id}, Error: {e}", exc_info=True)
            
            # WORLD-CLASS INSTRUMENTATION: Fire-and-forget T4-T6 error telemetry
            if self._correlation_logger:
                try:
                    error_telemetry_payload = {
                        # Milestone timestamps (what we have)
                        "T4_ConditioningStart_ns": result.T4_ConditioningStart_ns,
                        "T5_ValidationComplete_ns": result.T5_ValidationComplete_ns,  # May be None
                        "T6_ConditioningComplete_ns": result.T6_ConditioningComplete_ns,
                        
                        # Error context
                        "error_type": type(e).__name__,
                        "error_message": str(e),
                        "error_stage": "conditioning_pipeline",
                        
                        # Processing metrics (what we completed)
                        "symbols_processed": result.symbols_processed,
                        "lines_processed": result.lines_processed,
                        
                        # Causation chain
                        "parent_event_id": parent_event_id,
                        "correlation_id": parent_correlation_id,
                        "child_event_id": result.metadata.get('eventId'),
                        
                        # Timing (what we can calculate)
                        "total_duration_ms": (result.T6_ConditioningComplete_ns - t4_start_ns) / 1_000_000,
                        
                        # Add origin timestamp for correlation logger
                        "origin_timestamp_s": result.T4_ConditioningStart_ns / 1_000_000_000
                    }
                    
                    # Fire and forget - no blocking!
                    self._correlation_logger.log_event(
                        source_component="PythonOCRDataConditioner",
                        event_name="OCRConditioningError",
                        payload=error_telemetry_payload
                    )
                except Exception as telemetry_error:
                    # Never let telemetry errors affect trading
                    self.logger.debug(f"Telemetry log error (non-critical): {telemetry_error}")
            
            # Capture benchmarking metrics even on error if benchmarker is available
            if self._benchmarker:
                error_duration_ms = (result.T6_ConditioningComplete_ns - t4_start_ns) / 1_000_000
                error_context = {
                    'correlation_id': parent_correlation_id,
                    'error_type': type(e).__name__,
                    'conditioning_stage': 'error'
                }
                self._benchmarker.capture_metric("ocr.conditioning_error_time_ms", error_duration_ms, context=error_context)
            
            return result
        
        finally:
            # Capture benchmarking metrics if benchmarker is available
            if self._benchmarker and result.T6_ConditioningComplete_ns:
                total_duration_ms = (result.T6_ConditioningComplete_ns - t4_start_ns) / 1_000_000
                
                benchmarker_context = {
                    'correlation_id': parent_correlation_id,
                    'symbols_processed': result.symbols_processed,
                    'lines_processed': result.lines_processed,
                    'flickers_rejected': result.flickers_rejected,
                    'decimal_corrections': result.decimal_corrections_applied,
                    'validation_failures': result.validation_failures
                }
                
                # Capture T4->T6 conditioning metrics
                self._benchmarker.capture_metric("ocr.conditioning_time_ms", total_duration_ms, context=benchmarker_context)
                
                # Capture T0->T6 pipeline metric if T0 is available
                if t0_timestamp_ns:
                    t0_to_t6_pipeline_ms = (result.T6_ConditioningComplete_ns - t0_timestamp_ns) / 1_000_000
                    pipeline_context = benchmarker_context.copy()
                    pipeline_context['metric_type'] = 'T0_to_T6_pipeline'
                    self._benchmarker.capture_metric("ocr_full_pipeline_processing_time_ms", t0_to_t6_pipeline_ms, context=pipeline_context)

    def get_conditioner_metrics(self) -> Dict[str, float]:
        """
        Returns performance and operational metrics for the conditioner.
        """
        return {
            'flickers_rejected': float(self._flickers_rejected_count),
            'decimal_corrections': float(self._decimal_corrections_count),
            'symbols_processed': float(len(self._last_known_strategy_map)),
            'parsing_errors': 0.0,  # Could be tracked if needed
            'avg_processing_time_ms': 0.0,  # Could be tracked if needed
            'lines_processed': float(self._lines_processed_count),
            # Validation failure metrics
            'strategy_validation_failures': float(self._strategy_validation_failures),
            'symbol_validation_failures': float(self._symbol_validation_failures),
            'numeric_token_failures': float(self._numeric_token_failures),
            'non_tradeable_rejections': float(self._non_tradeable_rejections),
            'unknown_symbol_rejections': float(self._unknown_symbol_rejections),
            'total_validation_failures': float(
                self._strategy_validation_failures + 
                self._symbol_validation_failures + 
                self._numeric_token_failures + 
                self._non_tradeable_rejections + 
                self._unknown_symbol_rejections
            ),
            # Flicker decision metrics
            'flicker_confirmations': float(self._flicker_confirmations),
            'flicker_rejections': float(self._flicker_rejections),
            'non_flicker_accepts': float(self._non_flicker_accepts),
            'filter_disabled_accepts': float(self._filter_disabled_accepts),
            'initial_value_accepts': float(self._initial_value_accepts),
            'total_flicker_decisions': float(
                self._flicker_confirmations + 
                self._flicker_rejections +
                self._non_flicker_accepts + 
                self._filter_disabled_accepts + 
                self._initial_value_accepts
            ),
            # Decimal correction decision metrics
            'decimal_corrections_applied': float(self._decimal_corrections_applied),
            'decimal_corrections_skipped': float(self._decimal_corrections_skipped),
            'decimal_no_reference_price': float(self._decimal_no_reference_price),
            'total_decimal_decisions': float(
                self._decimal_corrections_applied + 
                self._decimal_corrections_skipped + 
                self._decimal_no_reference_price
            )
        }
