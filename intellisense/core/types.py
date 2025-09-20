"""
Core Types for IntelliSense Module

Defines all dataclass types used throughout the IntelliSense system including
enhanced position structures, timeline events, and configuration objects.
"""

from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Any, Optional, List, Dict, Tuple, Union, TYPE_CHECKING, TypeVar, Type
import time
import logging
import dataclasses

# Import enums from local module
from .enums import (
    DataLoadStatus,
    SyncAccuracyStatus,
    TestStatus
)

# Define a generic type variable for the specific data part of TimelineEvent subclasses
TData = TypeVar('TData')


# --- Enhanced Position Structure (Refinement 2) ---
@dataclass
class Position:
    """Enhanced position representation for ground truth validation."""
    symbol: str
    shares: float
    avg_price: float
    timestamp: float
    total_cost_basis: float = field(init=False)
    realized_pnl_to_date: float = 0.0
    market_price: Optional[float] = None
    market_value: Optional[float] = None
    unrealized_pnl: Optional[float] = None

    def __post_init__(self):
        self.total_cost_basis = self.shares * self.avg_price

    def enrich_with_market_data(self, market_price: float):
        if self.shares == 0:
            self.market_price = market_price
            self.market_value = 0.0
            self.unrealized_pnl = 0.0
            return
        self.market_price = market_price
        self.market_value = self.shares * market_price
        if not hasattr(self, 'total_cost_basis') or self.total_cost_basis is None:
            self.total_cost_basis = self.shares * self.avg_price
        self.unrealized_pnl = self.market_value - self.total_cost_basis

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Position':
        """Create Position from dictionary with robust fallback defaults."""
        # Ensure all required fields for constructor have defaults if not in data
        # Constructor requires: symbol, shares, avg_price, timestamp
        return cls(
            symbol=data.get('symbol', 'UNKNOWN_SYM'),  # Provide default for required field
            shares=float(data.get('shares', 0.0)),
            avg_price=float(data.get('avg_price', 0.0)),
            timestamp=float(data.get('timestamp', time.time())),  # Default to current time if missing
            realized_pnl_to_date=float(data.get('realized_pnl_to_date', 0.0)),
            market_price=data.get('market_price'),
            market_value=data.get('market_value'),
            unrealized_pnl=data.get('unrealized_pnl')
        )


# --- Design Spec Section 5.2: Timeline Event Structures ---
@dataclass
class TimelineEvent:
    """Base timeline event with epoch timestamp correlation support."""
    global_sequence_id: int                       # Global monotonic ordering ID (required for Redis integration)
    epoch_timestamp_s: float                      # Was 'timestamp', renamed for clarity
    perf_counter_timestamp: int                   # TESTRADE's source perf_counter_ns from metadata.timestamp_ns
    source_sense: str                             # 'OCR', 'PRICE', 'BROKER', 'SYSTEM' - moved from derivative for universal access
    event_type: str                               # e.g., "OCR_FRAME_RAW", "PRICE_TICK", "BROKER_ACK" - moved from derivative
    correlation_id: Optional[str] = None          # Master correlation ID from TESTRADE
    source_info: Optional[Dict[str, Any]] = field(default_factory=dict)  # E.g., component name, file line
    processing_latency_ns: Optional[int] = None   # Latency within the source component if provided


@dataclass
class OCRTimelineEventData:
    """Data structure for OCRTimelineEvent payload."""
    frame_number: Optional[int] = None
    ocr_confidence: Optional[float] = None
    # For raw OCR, this might be the full OCRParsedData dict
    # For cleaned OCR, this might be the CleanedOCRSnapshotEventData.snapshots dict
    snapshots: Optional[Dict[str, Any]] = field(default_factory=dict)
    raw_text_preview: Optional[str] = None
    # Other fields based on whether it's raw or cleaned
    # Fields from OCRParsedData (for raw)
    event_id_native_ocr: Optional[str] = None # from OCRParsedData.event_id
    raw_ocr_processing_finish_timestamp: Optional[float] = None
    full_raw_text: Optional[str] = None
    # Fields from CleanedOCRSnapshotEventData (for cleaned)
    original_ocr_event_id_cleaned: Optional[str] = None # from CleanedOCRSnapshotEventData.original_ocr_event_id
    # NEW: Fields to hold propagated correlation_id/tracking_dict if logged this way
    correlation_id_propagated: Optional[str] = None  # If logged inside data, different from TimelineEvent.correlation_id
    tracking_dict_propagated: Optional[Dict[str, Any]] = None


@dataclass
class OCRTimelineEvent(TimelineEvent):
    """OCR-specific timeline event with refined structure."""
    # Specific fields for OCR events, derived from the payload published by TESTRADE
    # These are now largely illustrative as 'data' will hold the main payload dict
    # The properties below can be used for convenient access.
    data: OCRTimelineEventData = field(default_factory=OCRTimelineEventData)

    # Legacy fields for backward compatibility
    frame_number: int = field(default=0, init=False)  # Derived from data.frame_number
    validation_id: Optional[str] = field(default=None, init=False)

    def __post_init__(self):
        """Set legacy fields for backward compatibility."""
        if self.data and self.data.frame_number is not None:
            self.frame_number = self.data.frame_number

    @property
    def ocr_confidence(self) -> Optional[float]:
        """Get OCR confidence from the data payload."""
        return self.data.ocr_confidence if self.data else None

    @property
    def snapshots(self) -> Optional[Dict[str, Any]]:
        """Get snapshots from the data payload."""
        return self.data.snapshots if self.data else None

    @property
    def expected_position(self) -> Optional[Position]:
        """Get expected position from the stored private attribute."""
        return getattr(self, '_expected_position_obj', None)

    # --- NEW STATIC METHOD ---
    @staticmethod
    def from_redis_message(redis_msg_dict: Dict[str, Any], pds_gsi: int) -> Optional['OCRTimelineEvent']:
        """
        Creates an OCRTimelineEvent from a deserialized Redis message dictionary.
        redis_msg_dict is expected to have "metadata" and "payload" keys.
        """
        logger = logging.getLogger(__name__)
        try:
            metadata = redis_msg_dict.get("metadata", {})
            payload = redis_msg_dict.get("payload", {})

            if not metadata or not payload:
                logger.error(f"OCRTimelineEvent.from_redis_message: Missing metadata or payload. Keys: {list(redis_msg_dict.keys())}")
                return None

            testrade_event_type = metadata.get("eventType", "UNKNOWN_TESTRADE_OCR_EVENT")
            intellisense_event_type = "UNKNOWN_OCR" # Default

            # Map TESTRADE eventType to IntelliSense OCRTimelineEvent.event_type
            if testrade_event_type == "TESTRADE_RAW_OCR_DATA":
                intellisense_event_type = "OCR_FRAME_RAW"
            elif testrade_event_type == "TESTRADE_CLEANED_OCR_SNAPSHOT":
                intellisense_event_type = "OCR_SNAPSHOT_CLEANED"
            else:
                logger.debug(f"OCRTimelineEvent.from_redis_message: Skipping non-OCR eventType '{testrade_event_type}'.")
                return None  # Return None for non-OCR events

            # Timestamps
            # Primary epoch timestamp from payload (frame_timestamp)
            epoch_ts = payload.get("frame_timestamp")
            if epoch_ts is None: # Fallback if not in payload directly for some reason
                epoch_ts = metadata.get("timestamp_ns", time.time() * 1e9) / 1e9 # Use metadata publish time
                logger.debug(f"OCRTimelineEvent: Using metadata timestamp for epoch_ts for eventId {metadata.get('eventId')}")

            # Performance counter timestamp from TESTRADE metadata
            perf_ts_ns = metadata.get("timestamp_ns")
            if perf_ts_ns is None:
                logger.warning(f"OCRTimelineEvent: Missing 'timestamp_ns' in metadata for eventId {metadata.get('eventId')}. Using current perf_counter.")
                perf_ts_ns = time.perf_counter_ns()

            # Populate OCRTimelineEventData
            ocr_data = OCRTimelineEventData()
            if intellisense_event_type == "OCR_FRAME_RAW":
                # Payload is an OCRParsedData dict
                ocr_data.frame_number = payload.get("frame_number") # Primary frame number field
                ocr_data.ocr_confidence = payload.get("overall_confidence")
                ocr_data.snapshots = payload.get("snapshots") # This is Dict[str, OCRSnapshot_as_dict]
                ocr_data.raw_text_preview = payload.get("full_raw_text", "")[:150] # Truncate
                ocr_data.event_id_native_ocr = payload.get("event_id") # The original OCRParsedData.event_id
                ocr_data.raw_ocr_processing_finish_timestamp = payload.get("raw_ocr_processing_finish_timestamp")
                ocr_data.full_raw_text = payload.get("full_raw_text") # Store full if needed for deep replay
            elif intellisense_event_type == "OCR_SNAPSHOT_CLEANED":
                # Payload is a CleanedOCRSnapshotEventData dict
                ocr_data.frame_number = payload.get("frame_number") # Should be on CleanedOCRSnapshotEventData from frame_timestamp
                ocr_data.ocr_confidence = payload.get("raw_ocr_confidence")
                ocr_data.snapshots = payload.get("snapshots") # This is Dict[str, Dict[str, Any]] (5-token data)
                ocr_data.original_ocr_event_id_cleaned = payload.get("original_ocr_event_id")

            return OCRTimelineEvent(
                global_sequence_id=pds_gsi, # Provided by PDS after GSI reset
                epoch_timestamp_s=float(epoch_ts),
                perf_counter_timestamp=int(perf_ts_ns),
                source_sense="OCR",
                event_type=intellisense_event_type,
                correlation_id=metadata.get("correlationId"),
                # causation_id=metadata.get("causationId"), # Optional: Add to TimelineEvent if needed
                # event_id_native=metadata.get("eventId"), # Optional: Add to TimelineEvent if needed
                source_info={"component": metadata.get("sourceComponent"), "redis_stream_event_id": metadata.get("eventId")},
                processing_latency_ns=payload.get("component_processing_latency_ns"), # If TESTRADE component logged its own latency
                data=ocr_data
            )

        except Exception as e:
            logger.error(f"OCRTimelineEvent.from_redis_message: Failed to parse Redis message. Error: {e}. Message: {str(redis_msg_dict)[:500]}", exc_info=True)
            return None

    @staticmethod
    def from_correlation_log_entry(log_entry: Dict[str, Any]) -> Optional['OCRTimelineEvent']:
        """
        Create OCRTimelineEvent from correlation log entry.

        Expected log_entry structure from CorrelationLogger:
        {
            'global_sequence_id': int,
            'epoch_timestamp_s': float,
            'perf_timestamp_ns': int,
            'source_sense': str,
            'event_payload': dict,  # Contains the OCR data (asdict output of OCRTimelineEventData)
            'source_component_name': str (optional),
            'correlation_id': str (optional),
            'source_info': dict (optional),
            'component_processing_latency_ns': int (optional)
        }
        """
        logger = logging.getLogger(__name__)

        try:
            event_data_payload = log_entry.get('event_payload', {})

            # Correct approach: Use dictionary unpacking to robustly instantiate the dataclass
            # This automatically maps all keys in the dictionary to the dataclass fields
            if isinstance(event_data_payload, dict):
                try:
                    ocr_data = OCRTimelineEventData(**event_data_payload)
                except TypeError as te:
                    # Handle case where event_payload has extra keys not in OCRTimelineEventData
                    logger.warning(f"OCRTimelineEvent.from_correlation_log_entry: TypeError creating OCRTimelineEventData: {te}. Using filtered payload.")
                    # Filter to only known OCRTimelineEventData fields
                    import dataclasses
                    valid_fields = {f.name for f in dataclasses.fields(OCRTimelineEventData)}
                    filtered_payload = {k: v for k, v in event_data_payload.items() if k in valid_fields}
                    ocr_data = OCRTimelineEventData(**filtered_payload)
            else:
                ocr_data = OCRTimelineEventData()

            # Determine event_type - prioritize from log entry, fallback to payload or default
            event_type = log_entry.get('event_type')
            if not event_type:
                # Try to infer from payload or use default
                if 'cleaned' in str(event_data_payload.get('event_id_native_ocr', '')).lower():
                    event_type = 'OCR_SNAPSHOT_CLEANED'
                else:
                    event_type = 'OCR_FRAME_PROCESSED'

            return OCRTimelineEvent(
                global_sequence_id=log_entry['global_sequence_id'],
                epoch_timestamp_s=log_entry['epoch_timestamp_s'],
                perf_counter_timestamp=log_entry['perf_timestamp_ns'],
                source_sense=log_entry['source_sense'],
                event_type=event_type,
                correlation_id=log_entry.get('correlation_id'),
                source_info=log_entry.get('source_info', {}),
                processing_latency_ns=log_entry.get('component_processing_latency_ns'),
                data=ocr_data
            )

        except KeyError as ke:
            logger.error(f"OCRTimelineEvent.from_correlation_log_entry: Missing required key {ke} in log entry: {log_entry}", exc_info=True)
            return None
        except Exception as e:
            logger.error(f"OCRTimelineEvent.from_correlation_log_entry: Error parsing log entry: {e}. Entry: {str(log_entry)[:500]}", exc_info=True)
            return None


@dataclass
class VisualTimelineEventData:
    """Data structure for VisualTimelineEvent payload - the visual sense."""
    image_type: Optional[str] = None  # 'raw', 'processed', 'roi', etc.
    image_data_base64: Optional[str] = None  # Base64 encoded image data
    image_size_bytes: Optional[int] = None  # Size of image data in bytes
    roi_coordinates: Optional[List[int]] = None  # [x1, y1, x2, y2] ROI bounds
    capture_timestamp: Optional[float] = None  # When image was captured
    frame_number: Optional[int] = None  # Frame sequence number
    processing_info: Optional[Dict[str, Any]] = field(default_factory=dict)  # Processing metadata

    # Propagated correlation fields
    correlation_id_propagated: Optional[str] = None
    tracking_dict_propagated: Optional[Dict[str, Any]] = None


@dataclass
class ProcessedImageFrameData:
    """
    Data structure for processed ROI image frames, typically sourced from
    the 'testrade:processed-roi-image-frames' Redis stream.
    This is the 'data' attribute of ProcessedImageFrameTimelineEvent.
    """
    # --- Populated by from_redis_message, used by CorrelationLogger ---
    image_bytes_b64: Optional[str] = field(default=None, repr=False) # Base64 string from Redis, for PDS/Logger to decode & save
    image_format_from_payload: Optional[str] = None # e.g., "PNG", from Redis payload
    image_roi_coordinates_from_payload: Optional[List[int]] = field(default_factory=list)

    # --- Critical Timing & Correlation (extracted from Redis metadata & payload by from_redis_message) ---
    # These will be used to populate top-level TimelineEvent fields
    # but also stored here for direct access from the data object if needed.
    master_correlation_id: Optional[str] = None
    original_grab_perf_ns: Optional[int] = None # T0
    original_grab_epoch_s: Optional[float] = None # T0 epoch

    preprocessing_finish_perf_ns: Optional[int] = None # T1
    preprocessing_latency_ns: Optional[int] = None     # T1 - T0

    source_component_of_image: Optional[str] = None # From Redis metadata.sourceComponent

    # --- Populated by CorrelationLogger before writing to JSONL ---
    image_file_path_relative: Optional[str] = None # Relative path where image is saved

    # --- Raw payload for debugging ---
    raw_redis_payload_snapshot: Optional[Dict[str, Any]] = field(default_factory=dict, repr=False)

    def to_dict_for_log(self) -> Dict[str, Any]:
        """Prepares a dictionary for logging, excluding the raw image bytes."""
        return {
            "image_file_path_relative": self.image_file_path_relative,
            "image_format_from_payload": self.image_format_from_payload,
            "image_roi_coordinates_from_payload": self.image_roi_coordinates_from_payload,
            "master_correlation_id": self.master_correlation_id,
            "original_grab_perf_ns": self.original_grab_perf_ns,
            "original_grab_epoch_s": self.original_grab_epoch_s,
            "preprocessing_finish_perf_ns": self.preprocessing_finish_perf_ns,
            "preprocessing_latency_ns": self.preprocessing_latency_ns,
            "source_component_of_image": self.source_component_of_image
        }


@dataclass
class VisualTimelineEvent(TimelineEvent):
    """
    Visual timeline event - represents the "visual sense" in three senses replay.

    This captures what the trader actually SAW on screen during trading decisions.
    Essential for correlating visual cues with price movements and broker responses.
    """
    data: VisualTimelineEventData = field(default_factory=VisualTimelineEventData)

    def __post_init__(self):
        """Set source_sense to VISUAL for visual timeline events."""
        self.source_sense = "VISUAL"

    @property
    def image_type(self) -> Optional[str]:
        """Quick access to image type."""
        return self.data.image_type if isinstance(self.data, VisualTimelineEventData) else None

    @property
    def image_size_bytes(self) -> Optional[int]:
        """Quick access to image size."""
        return self.data.image_size_bytes if isinstance(self.data, VisualTimelineEventData) else None

    @property
    def roi_coordinates(self) -> Optional[List[int]]:
        """Quick access to ROI coordinates."""
        return self.data.roi_coordinates if isinstance(self.data, VisualTimelineEventData) else None


@dataclass
class ProcessedImageFrameTimelineEvent(TimelineEvent):
    data: ProcessedImageFrameData = field(default_factory=ProcessedImageFrameData)

    def __post_init__(self):
        # Source sense for this event, as it represents the visual input
        if not hasattr(self, 'source_sense') or not self.source_sense:
            self.source_sense = "VISUAL_INPUT_RAW"  # New sense reflecting it's the raw grab
        # Event type reflects what IntelliSense considers this event internally
        # Only set default if not already specified
        if not hasattr(self, 'event_type') or not self.event_type:
            self.event_type = "RAW_IMAGE_GRAB_CAPTURED"  # Internal IntelliSense event type
        # Populate top-level fields from data if available (for consistency)
        if self.data:
            if self.data.master_correlation_id and not self.correlation_id:
                self.correlation_id = self.data.master_correlation_id
            if self.data.original_grab_perf_ns and (self.perf_counter_timestamp is None or self.perf_counter_timestamp == 0):
                self.perf_counter_timestamp = self.data.original_grab_perf_ns
            if self.data.original_grab_epoch_s and (self.epoch_timestamp_s is None or self.epoch_timestamp_s == 0.0):
                self.epoch_timestamp_s = self.data.original_grab_epoch_s

    @staticmethod
    def from_redis_message(redis_msg_dict: Dict[str, Any], pds_gsi: int) -> Optional['ProcessedImageFrameTimelineEvent']:
        logger_types = logging.getLogger(f"{__name__}.types.ImageEventParser")
        try:
            metadata = redis_msg_dict.get("metadata", {})
            payload = redis_msg_dict.get("payload", {})

            if not metadata or not payload:
                logger_types.error(f"ImageEventParser: Missing metadata or payload. RedisMsgID: {redis_msg_dict.get('id', 'N/A')}")
                return None

            testrade_event_type = metadata.get("eventType")

            # --- MODIFICATION: Adapt to actual stream ---
            # We now expect "TESTRADE_RAW_IMAGE_GRAB" from "testrade:image-grabs"
            # The agent's report indicated "testrade:image-grabs" does not exist, but the smoke test found it.
            # This method will now explicitly handle "TESTRADE_RAW_IMAGE_GRAB".
            if testrade_event_type != "TESTRADE_RAW_IMAGE_GRAB":
                logger_types.debug(f"ImageEventParser: Skipping eventType '{testrade_event_type}'. Expected 'TESTRADE_RAW_IMAGE_GRAB'.")
                return None

            # Master Correlation ID from metadata (as per smoke test finding)
            master_corr_id = metadata.get("correlationId")
            if not master_corr_id:
                logger_types.error(f"ImageEventParser: Critical 'correlationId' missing in Redis metadata for {testrade_event_type} eventId {metadata.get('eventId')}.")
                return None

            # T0 Performance Counter Timestamp from PAYLOAD (as per smoke test finding for "TESTRADE_RAW_IMAGE_GRAB")
            original_grab_perf_ns_t0 = payload.get("grab_timestamp_ns") # payload.grab_timestamp_ns
            if original_grab_perf_ns_t0 is None:
                logger_types.error(f"ImageEventParser: Critical 'grab_timestamp_ns' (T0) missing in Redis PAYLOAD for {testrade_event_type} eventId {metadata.get('eventId')}, CorrID {master_corr_id}.")
                # Fallback to metadata.timestamp_ns if payload one is missing, but log error
                original_grab_perf_ns_t0 = metadata.get("timestamp_ns")
                if original_grab_perf_ns_t0 is None:
                     logger_types.error(f"ImageEventParser: CRITICAL FALLBACK FAILED - 'timestamp_ns' also missing in METADATA for {testrade_event_type} eventId {metadata.get('eventId')}, CorrID {master_corr_id}.")
                     return None # Cannot proceed without T0

            # T0 Epoch Timestamp - try payload first, then metadata as fallback
            original_grab_epoch_s_t0 = payload.get("frame_timestamp") # Common field name for epoch time
            if original_grab_epoch_s_t0 is None:
                # If metadata.timestamp_ns was used for perf_counter, use it for epoch too (converted)
                if metadata.get("timestamp_ns") == original_grab_perf_ns_t0 : # checks if it's the same source
                    original_grab_epoch_s_t0 = int(original_grab_perf_ns_t0) / 1e9
                else: # Last resort
                    original_grab_epoch_s_t0 = time.time()
                logger_types.debug(f"ImageEventParser: Using derived/fallback epoch_s for {master_corr_id}.")

            # Image data and ROI from PAYLOAD (as per smoke test finding)
            image_b64 = payload.get("frame_base64") # payload.frame_base64
            roi_coords = payload.get("roi_used")      # payload.roi_used

            # For "TESTRADE_RAW_IMAGE_GRAB", preprocessing fields are not applicable.
            data_obj = ProcessedImageFrameData(
                image_bytes_b64=image_b64,
                image_format_from_payload=payload.get("image_format", "PNG"), # Assume PNG if not specified
                image_roi_coordinates_from_payload=roi_coords,
                master_correlation_id=master_corr_id,
                original_grab_perf_ns=int(original_grab_perf_ns_t0),
                original_grab_epoch_s=float(original_grab_epoch_s_t0),
                preprocessing_finish_perf_ns=None, # Not applicable for raw grab
                preprocessing_latency_ns=None,     # Not applicable for raw grab
                source_component_of_image=metadata.get("sourceComponent"),
                raw_redis_payload_snapshot=payload.copy()
            )

            # For ProcessedImageFrameTimelineEvent:
            #   epoch_timestamp_s should be T0_epoch
            #   perf_counter_timestamp should be T0_perf_ns
            return ProcessedImageFrameTimelineEvent(
                global_sequence_id=pds_gsi,
                epoch_timestamp_s=data_obj.original_grab_epoch_s,
                perf_counter_timestamp=data_obj.original_grab_perf_ns,
                source_sense="VISUAL_INPUT_RAW", # Reflects it's the raw grab
                event_type="RAW_IMAGE_GRAB_CAPTURED",  # IntelliSense internal type
                correlation_id=master_corr_id,    # This is the key master ID
                source_info={
                    "component": metadata.get("sourceComponent"),
                    "redis_stream_event_id": metadata.get("eventId"),
                    "testrade_event_type": testrade_event_type, # Log actual TESTRADE type
                    "causation_id": metadata.get("causationId")
                },
                data=data_obj
            )

        except Exception as e:
            logger_types.error(f"ImageEventParser.from_redis_message error: {e}. Msg: {str(redis_msg_dict)[:500]}", exc_info=True)
            return None

    @staticmethod
    def from_correlation_log_entry(log_entry: Dict[str, Any]) -> Optional['ProcessedImageFrameTimelineEvent']:
        logger_types = logging.getLogger(f"{__name__}.types.ProcessedImageEvent")
        try:
            event_data_payload = log_entry.get('event_payload', {})
            # event_data_payload is the output of ProcessedImageFrameData.to_dict_for_log()
            data_obj = ProcessedImageFrameData(
                image_file_path_relative=event_data_payload.get("image_file_path_relative"),
                image_format_from_payload=event_data_payload.get("image_format_from_payload"),
                image_roi_coordinates_from_payload=event_data_payload.get("image_roi_coordinates_from_payload"),
                master_correlation_id=event_data_payload.get("master_correlation_id"),
                original_grab_perf_ns=event_data_payload.get("original_grab_perf_ns"),
                original_grab_epoch_s=event_data_payload.get("original_grab_epoch_s"),
                preprocessing_finish_perf_ns=event_data_payload.get("preprocessing_finish_perf_ns"),
                preprocessing_latency_ns=event_data_payload.get("preprocessing_latency_ns"),
                source_component_of_image=event_data_payload.get("source_component_of_image")
                # raw_redis_payload_snapshot is not logged to JSONL
            )
            return ProcessedImageFrameTimelineEvent(
                global_sequence_id=log_entry['global_sequence_id'],
                epoch_timestamp_s=log_entry['epoch_timestamp_s'],
                perf_counter_timestamp=log_entry['perf_timestamp_ns'],
                source_sense=log_entry.get('source_sense', "VISUAL_INPUT_RAW"),
                event_type=log_entry.get('event_type', "RAW_IMAGE_GRAB_CAPTURED"),
                correlation_id=log_entry.get('correlation_id'),
                source_info=log_entry.get('source_info', {}),
                processing_latency_ns=log_entry.get('component_processing_latency_ns'),
                data=data_obj
            )
        except Exception as e:
            logger_types.error(f"ProcessedImageFrameTimelineEvent.from_correlation_log_entry error: {e}. Entry: {str(log_entry)[:500]}", exc_info=True)
            return None


@dataclass
class PriceTimelineEventData:
    """Structure to hold the market data payload."""
    symbol: Optional[str] = None
    price: Optional[float] = None  # For trades
    volume: Optional[float] = None  # For trades
    bid_price: Optional[float] = None  # For quotes
    ask_price: Optional[float] = None  # For quotes
    bid_size: Optional[float] = None  # For quotes (float to accommodate various exchanges)
    ask_size: Optional[float] = None  # For quotes
    # This 'timestamp' is the original source timestamp from the payload
    original_event_timestamp: Optional[float] = None
    price_event_type_from_payload: Optional[str] = None  # e.g., "TRADE", "QUOTE", from payload.tick_type or inferred
    conditions: Optional[List[str]] = None
    # Store the raw payload for debugging or if not all fields are mapped directly
    raw_payload: Optional[Dict[str, Any]] = field(default_factory=dict)
    # NEW: Fields to hold propagated correlation_id/tracking_dict if logged this way
    correlation_id_propagated: Optional[str] = None
    tracking_dict_propagated: Optional[Dict[str, Any]] = None


@dataclass
class PriceTimelineEvent(TimelineEvent):
    """Price timeline event with comprehensive market data."""
    # Enhanced data structure for Redis integration
    data: Union[PriceTimelineEventData, Dict[str, Any]] = field(default_factory=PriceTimelineEventData)

    def __post_init__(self):
        """Set source_sense for price events."""
        super().__post_init__() if hasattr(super(), '__post_init__') else None
        if not hasattr(self, 'source_sense') or not self.source_sense:
            self.source_sense = "PRICE"

    # Specific fields are now expected to be in self.data (the payload dict) for flexibility
    # However, we can provide properties for convenient access to common fields.

    # Enhanced properties for convenient access to data fields (supports both dict and PriceTimelineEventData)
    @property
    def price_event_type(self) -> Optional[str]:
        """From BA12's schema for PriceTimelineEvent.event_type"""
        # This property should reflect the IntelliSense internal event type like "PRICE_TRADE" or "PRICE_QUOTE"
        # which is set in the base TimelineEvent.event_type field by from_redis_message.
        # The raw tick_type from payload is in self.data.price_event_type_from_payload
        return self.event_type

    @property
    def symbol(self) -> Optional[str]:
        if isinstance(self.data, PriceTimelineEventData):
            return self.data.symbol
        return self.data.get('symbol') if isinstance(self.data, dict) else None

    @property
    def price(self) -> Optional[float]:
        if isinstance(self.data, PriceTimelineEventData):
            return self.data.price
        val = self.data.get('price') if isinstance(self.data, dict) else None
        return float(val) if val is not None else None

    @property
    def volume(self) -> Optional[float]:  # Changed to float for consistency with PriceTimelineEventData
        if isinstance(self.data, PriceTimelineEventData):
            return self.data.volume
        val = self.data.get('volume') if isinstance(self.data, dict) else None
        return float(val) if val is not None else None

    @property
    def exchange(self) -> Optional[str]:
        # Legacy support - not in PriceTimelineEventData but may be in raw_payload
        if isinstance(self.data, PriceTimelineEventData):
            return self.data.raw_payload.get('exchange') if self.data.raw_payload else None
        return self.data.get('exchange') if isinstance(self.data, dict) else None

    @property
    def original_source_timestamp(self) -> Optional[float]:
        if isinstance(self.data, PriceTimelineEventData):
            return self.data.original_event_timestamp
        val = self.data.get('original_source_timestamp') if isinstance(self.data, dict) else None
        return float(val) if val is not None else None

    # Quote-specific properties
    @property
    def bid(self) -> Optional[float]:
        if isinstance(self.data, PriceTimelineEventData):
            return self.data.bid_price
        val = self.data.get('bid') if isinstance(self.data, dict) else None
        return float(val) if val is not None else None

    @property
    def ask(self) -> Optional[float]:
        if isinstance(self.data, PriceTimelineEventData):
            return self.data.ask_price
        val = self.data.get('ask') if isinstance(self.data, dict) else None
        return float(val) if val is not None else None

    @property
    def bid_size(self) -> Optional[float]:  # Changed to float for consistency with PriceTimelineEventData
        if isinstance(self.data, PriceTimelineEventData):
            return self.data.bid_size
        val = self.data.get('bid_size') if isinstance(self.data, dict) else None
        return float(val) if val is not None else None

    @property
    def ask_size(self) -> Optional[float]:  # Changed to float for consistency with PriceTimelineEventData
        if isinstance(self.data, PriceTimelineEventData):
            return self.data.ask_size
        val = self.data.get('ask_size') if isinstance(self.data, dict) else None
        return float(val) if val is not None else None

    @property
    def market_conditions(self) -> Optional[str]:
        # Legacy support - check raw_payload for conditions
        if isinstance(self.data, PriceTimelineEventData):
            conditions = self.data.conditions
            return ','.join(conditions) if conditions else 'REGULAR'
        return self.data.get('market_conditions', 'REGULAR') if isinstance(self.data, dict) else 'REGULAR'

    @staticmethod
    def from_correlation_log_entry(log_entry: Dict[str, Any]) -> Optional['PriceTimelineEvent']:
        """
        Create PriceTimelineEvent from correlation log entry.

        Expected log_entry structure from CorrelationLogger:
        {
            'global_sequence_id': int,
            'epoch_timestamp_s': float,
            'perf_timestamp_ns': int,
            'source_sense': str,
            'event_payload': dict,  # Contains the price data (asdict output of PriceTimelineEventData)
            'source_component_name': str (optional),
            'correlation_id': str (optional),
            'source_info': dict (optional),
            'component_processing_latency_ns': int (optional)
        }
        """
        logger = logging.getLogger(__name__)

        try:
            event_data_payload = log_entry.get('event_payload', {})

            # Correct approach: Use dictionary unpacking to robustly instantiate the dataclass
            # This automatically maps all keys in the dictionary to the dataclass fields
            if isinstance(event_data_payload, dict):
                try:
                    price_data = PriceTimelineEventData(**event_data_payload)
                except TypeError as te:
                    # Handle case where event_payload has extra keys not in PriceTimelineEventData
                    logger.warning(f"PriceTimelineEvent.from_correlation_log_entry: TypeError creating PriceTimelineEventData: {te}. Using filtered payload.")
                    # Filter to only known PriceTimelineEventData fields
                    import dataclasses
                    valid_fields = {f.name for f in dataclasses.fields(PriceTimelineEventData)}
                    filtered_payload = {k: v for k, v in event_data_payload.items() if k in valid_fields}
                    price_data = PriceTimelineEventData(**filtered_payload)
            else:
                price_data = PriceTimelineEventData()

            # Determine event_type - prioritize from log entry, fallback to payload or default
            event_type = log_entry.get('event_type')
            if not event_type:
                # Try to infer from payload or use default
                if price_data.price_event_type_from_payload:
                    if 'TRADE' in price_data.price_event_type_from_payload.upper():
                        event_type = 'PRICE_TRADE'
                    elif 'QUOTE' in price_data.price_event_type_from_payload.upper():
                        event_type = 'PRICE_QUOTE'
                    else:
                        event_type = 'PRICE_UNKNOWN'
                else:
                    event_type = 'PRICE_UNKNOWN'

            return PriceTimelineEvent(
                global_sequence_id=log_entry['global_sequence_id'],
                epoch_timestamp_s=log_entry['epoch_timestamp_s'],
                perf_counter_timestamp=log_entry['perf_timestamp_ns'],
                source_sense=log_entry['source_sense'],
                event_type=event_type,
                correlation_id=log_entry.get('correlation_id'),
                source_info=log_entry.get('source_info', {}),
                processing_latency_ns=log_entry.get('component_processing_latency_ns'),
                data=price_data
            )

        except KeyError as ke:
            logger.error(f"PriceTimelineEvent.from_correlation_log_entry: Missing required key {ke} in log entry: {log_entry}", exc_info=True)
            return None
        except Exception as e:
            logger.error(f"PriceTimelineEvent.from_correlation_log_entry: Error parsing log entry: {e}. Entry: {str(log_entry)[:500]}", exc_info=True)
            return None

    # --- NEW STATIC METHOD ---
    @staticmethod
    def from_redis_message(redis_msg_dict: Dict[str, Any], pds_gsi: int) -> Optional['PriceTimelineEvent']:
        """
        Creates a PriceTimelineEvent from a deserialized Redis message dictionary.
        redis_msg_dict is expected to have "metadata" and "payload" keys.
        Handles both TESTRADE_MARKET_TICK and TESTRADE_MARKET_QUOTE eventTypes.
        """
        logger = logging.getLogger(__name__)
        try:
            metadata = redis_msg_dict.get("metadata", {})
            payload = redis_msg_dict.get("payload", {})

            if not metadata or not payload:
                logger.error(f"PriceTimelineEvent.from_redis_message: Missing metadata or payload. Keys: {list(redis_msg_dict.keys())}")
                return None

            testrade_event_type = metadata.get("eventType", "UNKNOWN_TESTRADE_PRICE_EVENT")
            intellisense_price_event_type = "UNKNOWN_PRICE"  # Default for PriceTimelineEvent.event_type

            price_data = PriceTimelineEventData()
            price_data.symbol = payload.get("symbol")
            price_data.original_event_timestamp = payload.get("timestamp")  # This is key
            price_data.conditions = payload.get("conditions")
            price_data.raw_payload = payload.copy()  # Store the original payload

            if testrade_event_type == "TESTRADE_MARKET_TICK":
                intellisense_price_event_type = "PRICE_TRADE"  # Aligns with previous logic for file parsing
                price_data.price_event_type_from_payload = payload.get("tick_type", "TRADE").upper()  # Default to TRADE
                price_data.price = payload.get("price")
                price_data.volume = payload.get("volume")
            elif testrade_event_type == "TESTRADE_MARKET_QUOTE":
                intellisense_price_event_type = "PRICE_QUOTE"  # Aligns with previous logic
                price_data.price_event_type_from_payload = payload.get("tick_type", "QUOTE").upper()  # Default to QUOTE
                price_data.bid_price = payload.get("bid_price")
                price_data.ask_price = payload.get("ask_price")
                price_data.bid_size = payload.get("bid_size")
                price_data.ask_size = payload.get("ask_size")
            else:
                logger.debug(f"PriceTimelineEvent.from_redis_message: Skipping non-price eventType '{testrade_event_type}'.")
                return None  # Return None for non-price events

            # Timestamps for base TimelineEvent
            # Primary epoch timestamp from payload.timestamp (already in price_data.original_event_timestamp)
            epoch_ts = price_data.original_event_timestamp
            if epoch_ts is None:  # Fallback
                epoch_ts = metadata.get("timestamp_ns", time.time() * 1e9) / 1e9
                logger.debug(f"PriceTimelineEvent: Using metadata timestamp for epoch_ts for eventId {metadata.get('eventId')}")

            perf_ts_ns = metadata.get("timestamp_ns")
            if perf_ts_ns is None:
                logger.warning(f"PriceTimelineEvent: Missing 'timestamp_ns' in metadata for eventId {metadata.get('eventId')}. Using current perf_counter.")
                perf_ts_ns = time.perf_counter_ns()

            return PriceTimelineEvent(
                global_sequence_id=pds_gsi,
                epoch_timestamp_s=float(epoch_ts),
                perf_counter_timestamp=int(perf_ts_ns),
                source_sense="PRICE",  # Hardcoded for this type
                event_type=intellisense_price_event_type,  # "PRICE_TRADE" or "PRICE_QUOTE"
                correlation_id=metadata.get("correlationId"),  # Will be null or eventId for market data
                source_info={"component": metadata.get("sourceComponent"), "redis_stream_event_id": metadata.get("eventId")},
                processing_latency_ns=payload.get("component_processing_latency_ns"),  # If PriceRepository logged its own latency
                data=price_data
            )

        except Exception as e:
            logger.error(f"PriceTimelineEvent.from_redis_message: Failed to parse Redis message. Error: {e}. Message: {str(redis_msg_dict)[:500]}", exc_info=True)
            return None


@dataclass
class BrokerOrderRequestData:
    """Mirrors key fields from OrderRequestData for BrokerTimelineEvent.data"""
    request_id: Optional[str] = None
    symbol: Optional[str] = None
    side: Optional[str] = None
    quantity: Optional[float] = None
    order_type: Optional[str] = None
    limit_price: Optional[float] = None
    source_strategy: Optional[str] = None
    # Include the full original payload for completeness if needed
    original_payload: Optional[Dict[str, Any]] = field(default_factory=dict)
    # NEW: Fields to hold propagated correlation_id/tracking_dict if logged this way
    correlation_id_propagated: Optional[str] = None
    tracking_dict_propagated: Optional[Dict[str, Any]] = None


@dataclass
class BrokerAckData:
    """Based on OrderStatusUpdateData or specific ACK fields"""
    local_order_id: Optional[str] = None
    broker_order_id: Optional[str] = None
    symbol: Optional[str] = None
    status_from_broker: Optional[str] = None  # e.g., "WORKING", "PENDING_NEW"
    message: Optional[str] = None
    original_payload: Optional[Dict[str, Any]] = field(default_factory=dict)
    # NEW: Fields to hold propagated correlation_id/tracking_dict if logged this way
    correlation_id_propagated: Optional[str] = None
    tracking_dict_propagated: Optional[Dict[str, Any]] = None


@dataclass
class BrokerFillData:
    """Based on OrderFilledEventData"""
    local_order_id: Optional[str] = None
    broker_order_id: Optional[str] = None  # The main broker order ID
    symbol: Optional[str] = None
    side: Optional[str] = None
    filled_quantity: Optional[float] = None
    fill_price: Optional[float] = None
    fill_timestamp: Optional[float] = None  # Epoch
    execution_id: Optional[str] = None
    original_payload: Optional[Dict[str, Any]] = field(default_factory=dict)
    # NEW: Fields to hold propagated correlation_id/tracking_dict if logged this way
    # Note: BrokerTimelineEvent itself has correlation_id. These are if they are *also* in the data payload.
    correlation_id_propagated: Optional[str] = None
    tracking_dict_propagated: Optional[Dict[str, Any]] = None


@dataclass
class BrokerRejectionData:
    """Based on OrderRejectedData"""
    local_order_id: Optional[str] = None
    broker_order_id: Optional[str] = None
    symbol: Optional[str] = None
    rejection_reason: Optional[str] = None
    original_payload: Optional[Dict[str, Any]] = field(default_factory=dict)
    correlation_id_propagated: Optional[str] = None
    tracking_dict_propagated: Optional[Dict[str, Any]] = None


@dataclass
class BrokerCancelAckData:
    """Based on OrderStatusUpdateData for CANCELLED"""
    local_order_id: Optional[str] = None
    broker_order_id: Optional[str] = None
    symbol: Optional[str] = None
    original_payload: Optional[Dict[str, Any]] = field(default_factory=dict)
    correlation_id_propagated: Optional[str] = None
    tracking_dict_propagated: Optional[Dict[str, Any]] = None


@dataclass
class BrokerOtherData:
    """For generic status updates or unhandled types"""
    local_order_id: Optional[str] = None
    broker_order_id: Optional[str] = None
    symbol: Optional[str] = None
    status_from_broker: Optional[str] = None
    message: Optional[str] = None
    original_payload: Optional[Dict[str, Any]] = field(default_factory=dict)
    correlation_id_propagated: Optional[str] = None
    tracking_dict_propagated: Optional[Dict[str, Any]] = None


# Type alias for broker data types
BrokerDataType = Union[
    BrokerOrderRequestData, BrokerAckData, BrokerFillData,
    BrokerRejectionData, BrokerCancelAckData, BrokerOtherData, None
]


@dataclass
class BrokerTimelineEvent(TimelineEvent):
    """Enhanced broker timeline event with typed response data."""
    # These top-level fields are for quick access / filtering,
    # the full mapped data goes into self.data
    original_order_id: Optional[str] = None  # Typically TESTRADE's local_order_id or request_id
    broker_order_id_on_event: Optional[str] = None  # Broker's ID if present on this specific event

    data: BrokerDataType = None  # Holds specific typed data like BrokerFillData

    def __post_init__(self):
        """Set source_sense for broker events."""
        super().__post_init__() if hasattr(super(), '__post_init__') else None
        if not hasattr(self, 'source_sense') or not self.source_sense:
            self.source_sense = "BROKER"

    # --- NEW STATIC METHOD ---
    @staticmethod
    def from_redis_message(redis_msg_dict: Dict[str, Any], pds_gsi: int) -> Optional['BrokerTimelineEvent']:
        """
        Creates a BrokerTimelineEvent from a deserialized Redis message dictionary.
        redis_msg_dict is expected to have "metadata" and "payload" keys.
        Handles various TESTRADE order lifecycle eventTypes.
        """
        logger = logging.getLogger(__name__)
        try:
            metadata = redis_msg_dict.get("metadata", {})
            payload = redis_msg_dict.get("payload", {})  # This is the original TESTRADE event data dict

            if not metadata or not payload:
                logger.error(f"BrokerTimelineEvent.from_redis_message: Missing metadata or payload.")
                return None

            testrade_event_type_str = metadata.get("eventType", "UNKNOWN_TESTRADE_BROKER_EVENT")

            # IntelliSense specific event_type and typed_data for BrokerTimelineEvent
            intellisense_broker_event_type: str = "UNKNOWN"
            typed_broker_data: BrokerDataType = None

            # Common fields from payload
            symbol = payload.get("symbol")
            local_id = payload.get("local_order_id", payload.get("request_id"))  # OrderRequest uses request_id
            broker_id = payload.get("broker_order_id", payload.get("order_id"))  # Fill uses order_id for broker's ID

            # --- Mapping logic based on TESTRADE eventType from metadata ---
            if testrade_event_type_str == "TESTRADE_ORDER_REQUEST":
                intellisense_broker_event_type = "ORDER_REQUEST"  # For MOC pipeline to identify injection point
                typed_broker_data = BrokerOrderRequestData(
                    request_id=payload.get("request_id"), symbol=symbol,
                    side=str(payload.get("side")), quantity=payload.get("quantity"),
                    order_type=str(payload.get("order_type")), limit_price=payload.get("limit_price"),
                    source_strategy=payload.get("source_strategy"),
                    original_payload=payload
                )
            elif testrade_event_type_str == "TESTRADE_VALIDATED_ORDER_REQUEST":
                intellisense_broker_event_type = "VALIDATED_ORDER"  # For MOC pipeline
                # Payload is still OrderRequestData structure
                typed_broker_data = BrokerOrderRequestData(  # Re-use for simplicity or create BrokerValidatedOrderData
                    request_id=payload.get("request_id"), symbol=symbol,
                    side=str(payload.get("side")), quantity=payload.get("quantity"),
                    order_type=str(payload.get("order_type")), limit_price=payload.get("limit_price"),
                    source_strategy=payload.get("source_strategy"),
                    original_payload=payload
                )
            elif testrade_event_type_str == "TESTRADE_ORDER_FILL":
                intellisense_broker_event_type = "FILL" # Could also be "PARTIAL_FILL" if distinguishable
                typed_broker_data = BrokerFillData(
                    local_order_id=str(payload.get("local_order_id")) if payload.get("local_order_id") is not None else None,
                    broker_order_id=str(payload.get("order_id")) if payload.get("order_id") is not None else None, # broker's ID for this order
                    symbol=payload.get("symbol"),
                    side=str(payload.get("side")) if payload.get("side") is not None else None, # Should be enum value string e.g. "BUY"
                    filled_quantity=payload.get("fill_quantity"),
                    fill_price=payload.get("fill_price"),
                    fill_timestamp=payload.get("fill_timestamp"), # Epoch seconds from broker
                    execution_id=payload.get("execution_id"),
                    original_payload=payload
                )
            elif testrade_event_type_str == "TESTRADE_ORDER_STATUS_UPDATE":
                broker_status_from_payload = str(payload.get("status", "")).upper()
                if "ACK" in broker_status_from_payload or \
                   "WORKING" in broker_status_from_payload or \
                   "ACCEPTED" in broker_status_from_payload or \
                   "SUBMITTED" in broker_status_from_payload or \
                   "PENDING_NEW" in broker_status_from_payload: # Added PENDING_NEW
                    intellisense_broker_event_type = "ACK"
                    typed_broker_data = BrokerAckData(
                        local_order_id=local_id, broker_order_id=broker_id,
                        symbol=symbol, status_from_broker=broker_status_from_payload,
                        message=payload.get("message"), original_payload=payload)
                elif "CANCELLED" in broker_status_from_payload or "CANCEL_ACK" in broker_status_from_payload:
                    intellisense_broker_event_type = "CANCEL_ACK"
                    typed_broker_data = BrokerCancelAckData(
                        local_order_id=local_id, broker_order_id=broker_id,
                        symbol=symbol, original_payload=payload)
                else:
                    intellisense_broker_event_type = "STATUS_UPDATE" # Generic
                    typed_broker_data = BrokerOtherData(
                        local_order_id=local_id, broker_order_id=broker_id,
                        symbol=symbol, status_from_broker=broker_status_from_payload,
                        message=payload.get("message"), original_payload=payload)

            elif testrade_event_type_str == "TESTRADE_ORDER_REJECTION":
                intellisense_broker_event_type = "REJECT"
                typed_broker_data = BrokerRejectionData(
                    local_order_id=local_id, broker_order_id=broker_id, symbol=symbol,
                    rejection_reason=payload.get("reason"), original_payload=payload
                )
            else:
                logger.warning(f"BrokerTimelineEvent.from_redis_message: Unhandled TESTRADE eventType '{testrade_event_type_str}'.")
                intellisense_broker_event_type = testrade_event_type_str # Use as is
                typed_broker_data = BrokerOtherData(
                    local_order_id=local_id, broker_order_id=broker_id,
                    symbol=symbol, message=f"Unhandled TESTRADE event type: {testrade_event_type_str}",
                    original_payload=payload)

            # Determine epoch_timestamp_s for the TimelineEvent base
            # Prioritize specific timestamps from the payload if available and relevant
            epoch_ts = None
            if typed_broker_data and hasattr(typed_broker_data, 'fill_timestamp') and getattr(typed_broker_data, 'fill_timestamp') is not None:
                epoch_ts = getattr(typed_broker_data, 'fill_timestamp')
            elif 'timestamp' in payload: # Generic timestamp field in many payloads
                epoch_ts = payload['timestamp']
            elif 'ocr_frame_timestamp' in payload: # For order requests originating from OCR
                epoch_ts = payload['ocr_frame_timestamp']

            if epoch_ts is None: # Fallback to metadata publish time
                epoch_ts = metadata.get("timestamp_ns", time.time() * 1e9) / 1e9
                logger.debug(f"BrokerTimelineEvent: Using metadata timestamp for epoch_ts for eventId {metadata.get('eventId')}")

            perf_ts_ns = metadata.get("timestamp_ns")
            if perf_ts_ns is None:
                perf_ts_ns = time.perf_counter_ns()  # Fallback

            return BrokerTimelineEvent(
                global_sequence_id=pds_gsi,
                epoch_timestamp_s=float(epoch_ts),
                perf_counter_timestamp=int(perf_ts_ns),
                source_sense="BROKER",  # Hardcoded for this type
                event_type=intellisense_broker_event_type,
                correlation_id=metadata.get("correlationId"),
                source_info={"component": metadata.get("sourceComponent"),
                             "redis_stream_event_id": metadata.get("eventId"),
                             "testrade_event_type": testrade_event_type_str,
                             "causation_id": metadata.get("causationId")},
                processing_latency_ns=payload.get("component_processing_latency_ns"),  # If source component logged its own latency
                original_order_id=str(local_id) if local_id is not None else None,
                broker_order_id_on_event=str(broker_id) if broker_id is not None else None,
                data=typed_broker_data
            )

        except Exception as e:
            logger.error(f"BrokerTimelineEvent.from_redis_message: Failed to parse. Error: {e}. Message: {str(redis_msg_dict)[:500]}", exc_info=True)
            return None

    @staticmethod
    def from_correlation_log_entry(log_entry: Dict[str, Any]) -> Optional['BrokerTimelineEvent']:
        """
        Create BrokerTimelineEvent from correlation log entry.

        Expected log_entry structure from CorrelationLogger:
        {
            'global_sequence_id': int,
            'epoch_timestamp_s': float,
            'perf_timestamp_ns': int,
            'source_sense': str,
            'event_payload': dict,  # Contains the broker data (asdict output of Broker<Type>Data)
            'source_component_name': str (optional),
            'correlation_id': str (optional),
            'source_info': dict (optional),
            'component_processing_latency_ns': int (optional)
        }
        """
        logger = logging.getLogger(__name__)

        try:
            event_data_payload = log_entry.get('event_payload', {})
            intellisense_event_type = log_entry['event_type']

            typed_broker_data: BrokerDataType = None

            # Correct approach: Use the event_type to pick the class, then use ** to instantiate
            if isinstance(event_data_payload, dict):
                try:
                    if intellisense_event_type == "ORDER_REQUEST" or intellisense_event_type == "VALIDATED_ORDER":
                        typed_broker_data = BrokerOrderRequestData(**event_data_payload)  # Correct, robust method
                    elif intellisense_event_type == "FILL":
                        typed_broker_data = BrokerFillData(**event_data_payload)  # Correct, robust method
                    elif intellisense_event_type == "ACK":
                        typed_broker_data = BrokerAckData(**event_data_payload)  # Correct, robust method
                    elif intellisense_event_type == "REJECT":
                        typed_broker_data = BrokerRejectionData(**event_data_payload)  # Correct, robust method
                    elif intellisense_event_type == "CANCEL_ACK":
                        typed_broker_data = BrokerCancelAckData(**event_data_payload)  # Correct, robust method
                    else:  # STATUS_UPDATE or UNKNOWN
                        typed_broker_data = BrokerOtherData(**event_data_payload)
                except TypeError as te:
                    # Handle case where event_payload has extra keys not in the target dataclass
                    logger.warning(f"BrokerTimelineEvent.from_correlation_log_entry: TypeError creating {intellisense_event_type} data: {te}. Using filtered payload.")
                    # Filter to only known fields for the target dataclass
                    import dataclasses
                    if intellisense_event_type == "FILL":
                        valid_fields = {f.name for f in dataclasses.fields(BrokerFillData)}
                        filtered_payload = {k: v for k, v in event_data_payload.items() if k in valid_fields}
                        typed_broker_data = BrokerFillData(**filtered_payload)
                    elif intellisense_event_type == "ORDER_REQUEST" or intellisense_event_type == "VALIDATED_ORDER":
                        valid_fields = {f.name for f in dataclasses.fields(BrokerOrderRequestData)}
                        filtered_payload = {k: v for k, v in event_data_payload.items() if k in valid_fields}
                        typed_broker_data = BrokerOrderRequestData(**filtered_payload)
                    elif intellisense_event_type == "ACK":
                        valid_fields = {f.name for f in dataclasses.fields(BrokerAckData)}
                        filtered_payload = {k: v for k, v in event_data_payload.items() if k in valid_fields}
                        typed_broker_data = BrokerAckData(**filtered_payload)
                    elif intellisense_event_type == "REJECT":
                        valid_fields = {f.name for f in dataclasses.fields(BrokerRejectionData)}
                        filtered_payload = {k: v for k, v in event_data_payload.items() if k in valid_fields}
                        typed_broker_data = BrokerRejectionData(**filtered_payload)
                    elif intellisense_event_type == "CANCEL_ACK":
                        valid_fields = {f.name for f in dataclasses.fields(BrokerCancelAckData)}
                        filtered_payload = {k: v for k, v in event_data_payload.items() if k in valid_fields}
                        typed_broker_data = BrokerCancelAckData(**filtered_payload)
                    else:
                        valid_fields = {f.name for f in dataclasses.fields(BrokerOtherData)}
                        filtered_payload = {k: v for k, v in event_data_payload.items() if k in valid_fields}
                        typed_broker_data = BrokerOtherData(**filtered_payload)
            else:
                logger.warning(f"BrokerTimelineEvent.from_correlation_log_entry: event_payload is not a dict for event_type '{intellisense_event_type}'. Entry: {str(log_entry)[:500]}")
                typed_broker_data = BrokerOtherData(
                    message=f"Invalid payload for {intellisense_event_type}",
                    original_payload=event_data_payload if event_data_payload is not None else {}
                )

            return BrokerTimelineEvent(
                global_sequence_id=log_entry['global_sequence_id'],
                epoch_timestamp_s=log_entry['epoch_timestamp_s'],
                perf_counter_timestamp=log_entry['perf_timestamp_ns'],
                source_sense=log_entry['source_sense'],
                event_type=intellisense_event_type,
                correlation_id=log_entry.get('correlation_id'),
                source_info=log_entry.get('source_info', {}),
                processing_latency_ns=log_entry.get('component_processing_latency_ns'),
                # Get IDs from the top-level log entry (added by enhanced CorrelationLogger) or fallback to payload
                original_order_id=log_entry.get('original_order_id_top_level') or event_data_payload.get('local_order_id') or event_data_payload.get('request_id'),
                broker_order_id_on_event=log_entry.get('broker_order_id_on_event_top_level') or event_data_payload.get('broker_order_id') or event_data_payload.get('order_id'),
                data=typed_broker_data
            )

        except KeyError as ke:
            logger.error(f"BrokerTimelineEvent.from_correlation_log_entry: Missing required key {ke} in log entry: {log_entry}", exc_info=True)
            return None
        except Exception as e:
            logger.error(f"BrokerTimelineEvent.from_correlation_log_entry: Error parsing log entry: {e}. Entry: {str(log_entry)[:500]}", exc_info=True)
            return None

    # Existing properties can remain to access fields from self.data
    @property
    def symbol(self) -> Optional[str]:
        if hasattr(self.data, 'symbol'):
            return getattr(self.data, 'symbol')
        return None


# --- New TimelineEvent Types for System Events ---

@dataclass
class ConfigChangeData:
    """Data payload for configuration change events."""
    component_changed: Optional[str] = None       # e.g., "RiskManagementService", "OCRService.Preprocessing"
    parameter_changed: Optional[str] = None       # e.g., "max_position_size_aapl", "ocr_upscale_factor"
    old_value: Optional[Any] = None
    new_value: Optional[Any] = None
    change_source: Optional[str] = None           # e.g., "API_CALL", "GUI_UPDATE", "FILE_RELOAD"
    effective_timestamp_epoch_s: Optional[float] = None # When the change became effective in TESTRADE
    raw_redis_payload_snapshot: Optional[Dict[str, Any]] = field(default_factory=dict, repr=False)

    def to_dict_for_log(self) -> Dict[str, Any]:
        return asdict(self) # Can be customized if needed


@dataclass
class ConfigChangeTimelineEvent(TimelineEvent):
    data: ConfigChangeData = field(default_factory=ConfigChangeData)

    def __post_init__(self):
        self.source_sense = "SYSTEM_CONFIG"
        if not self.event_type:
            self.event_type = "CONFIG_CHANGE_DETECTED"

    @staticmethod
    def from_redis_message(redis_msg_dict: Dict[str, Any], pds_gsi: int) -> Optional['ConfigChangeTimelineEvent']:
        logger_types = logging.getLogger(f"{__name__}.types.ConfigChangeEvent")
        try:
            metadata = redis_msg_dict.get("metadata", {})
            payload = redis_msg_dict.get("payload", {})
            if not metadata or not payload or metadata.get("eventType") != "TESTRADE_CONFIG_CHANGE_APPLIED": # Expected
                return None

            data_obj = ConfigChangeData(
                component_changed=payload.get("component"),
                parameter_changed=payload.get("parameter"),
                old_value=payload.get("old_value"),
                new_value=payload.get("new_value"),
                change_source=payload.get("source"),
                effective_timestamp_epoch_s=payload.get("effective_timestamp"),
                raw_redis_payload_snapshot=payload.copy()
            )
            return ConfigChangeTimelineEvent(
                global_sequence_id=pds_gsi,
                epoch_timestamp_s=float(payload.get("effective_timestamp", metadata.get("timestamp_ns", time.time() * 1e9) / 1e9)),
                perf_counter_timestamp=int(metadata.get("timestamp_ns", time.perf_counter_ns())),
                source_sense="SYSTEM_CONFIG",
                event_type="CONFIG_CHANGE_DETECTED", # IntelliSense internal type
                correlation_id=metadata.get("correlationId"), # May or may not be present/relevant
                source_info={"component": metadata.get("sourceComponent"), "redis_stream_event_id": metadata.get("eventId")},
                data=data_obj
            )
        except Exception as e:
            logger_types.error(f"ConfigChangeTimelineEvent.from_redis_message error: {e}", exc_info=True)
            return None

    @staticmethod
    def from_correlation_log_entry(log_entry: Dict[str, Any]) -> Optional['ConfigChangeTimelineEvent']:
        logger_types = logging.getLogger(f"{__name__}.types.ConfigChangeEvent")
        try:
            data_obj = ConfigChangeData(**log_entry.get('event_payload', {}))
            return ConfigChangeTimelineEvent(
                global_sequence_id=log_entry['global_sequence_id'],
                epoch_timestamp_s=log_entry['epoch_timestamp_s'],
                perf_counter_timestamp=log_entry['perf_timestamp_ns'],
                source_sense=log_entry.get('source_sense', "SYSTEM_CONFIG"),
                event_type=log_entry.get('event_type', "CONFIG_CHANGE_DETECTED"),
                correlation_id=log_entry.get('correlation_id'),
                source_info=log_entry.get('source_info', {}),
                processing_latency_ns=log_entry.get('component_processing_latency_ns'),
                data=data_obj
            )
        except Exception as e:
            logger_types.error(f"ConfigChangeTimelineEvent.from_correlation_log_entry error: {e}", exc_info=True)
            return None


@dataclass
class TradingStateChangeData:
    """Data payload for trading state change events."""
    state_type: Optional[str] = None               # "TRADING_ENABLED", "OCR_ACTIVE", etc.
    enabled: Optional[bool] = None                 # True if enabled, False if disabled
    timestamp: Optional[float] = None              # Timestamp from payload
    raw_redis_payload_snapshot: Optional[Dict[str, Any]] = field(default_factory=dict, repr=False)

    def to_dict_for_log(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class TradingStateChangeTimelineEvent(TimelineEvent):
    data: TradingStateChangeData = field(default_factory=TradingStateChangeData)

    def __post_init__(self):
        self.source_sense = "SYSTEM_STATE"
        if not self.event_type:
            self.event_type = "TRADING_STATE_CHANGED"

    @staticmethod
    def from_redis_message(redis_msg_dict: Dict[str, Any], pds_gsi: int) -> Optional['TradingStateChangeTimelineEvent']:
        logger_types = logging.getLogger(f"{__name__}.types.TradingStateEvent")
        try:
            metadata = redis_msg_dict.get("metadata", {})
            payload = redis_msg_dict.get("payload", {})
            testrade_event_type = metadata.get("eventType")
            if testrade_event_type != "TESTRADE_TRADING_STATE_CHANGE":
                return None

            data_obj = TradingStateChangeData(
                state_type=payload.get("state_type"),
                enabled=payload.get("enabled"),
                timestamp=payload.get("timestamp"),
                raw_redis_payload_snapshot=payload.copy()
            )

            # Map to IntelliSense event type based on state_type and enabled
            intellisense_event_type = "TRADING_STATE_CHANGED"
            if data_obj.state_type == "TRADING_ENABLED":
                intellisense_event_type = "TRADING_STATE_ENABLED" if data_obj.enabled else "TRADING_STATE_DISABLED"

            return TradingStateChangeTimelineEvent(
                global_sequence_id=pds_gsi,
                epoch_timestamp_s=float(payload.get("timestamp", metadata.get("timestamp_ns", time.time() * 1e9) / 1e9)),
                perf_counter_timestamp=int(metadata.get("timestamp_ns", time.perf_counter_ns())),
                source_sense="SYSTEM_STATE",
                event_type=intellisense_event_type,
                correlation_id=metadata.get("correlationId"),
                source_info={"component": metadata.get("sourceComponent"), "redis_stream_event_id": metadata.get("eventId")},
                data=data_obj
            )
        except Exception as e:
            logger_types.error(f"TradingStateChangeTimelineEvent.from_redis_message error: {e}", exc_info=True)
            return None

    @staticmethod
    def from_correlation_log_entry(log_entry: Dict[str, Any]) -> Optional['TradingStateChangeTimelineEvent']:
        logger_types = logging.getLogger(f"{__name__}.types.TradingStateEvent")
        try:
            data_obj = TradingStateChangeData(**log_entry.get('event_payload', {}))
            return TradingStateChangeTimelineEvent(
                global_sequence_id=log_entry['global_sequence_id'],
                epoch_timestamp_s=log_entry['epoch_timestamp_s'],
                perf_counter_timestamp=log_entry['perf_timestamp_ns'],
                source_sense=log_entry.get('source_sense', "SYSTEM_STATE"),
                event_type=log_entry.get('event_type', "TRADING_STATE_CHANGED"), # Actual logged type
                correlation_id=log_entry.get('correlation_id'),
                source_info=log_entry.get('source_info', {}),
                processing_latency_ns=log_entry.get('component_processing_latency_ns'),
                data=data_obj
            )
        except Exception as e:
            logger_types.error(f"TradingStateChangeTimelineEvent.from_correlation_log_entry error: {e}", exc_info=True)
            return None


@dataclass
class TradeLifecycleData:
    """Data payload for high-level trade lifecycle events."""
    event_type: Optional[str] = None               # "TRADE_CREATED", "TRADE_UPDATED", etc.
    trade_id: Optional[str] = None                 # Unique ID for the entire trade lifecycle
    symbol: Optional[str] = None
    lifecycle_state: Optional[str] = None          # "OPEN_PENDING", "CLOSED", etc.
    correlation_id: Optional[str] = None
    origin: Optional[str] = None                   # "OCR_SCALPING_OPEN_LONG", etc.
    comment: Optional[str] = None
    shares: Optional[float] = None
    avg_price: Optional[float] = None
    local_orders: Optional[List[Any]] = field(default_factory=list)
    timestamp: Optional[float] = None
    raw_redis_payload_snapshot: Optional[Dict[str, Any]] = field(default_factory=dict, repr=False)

    def to_dict_for_log(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class TradeLifecycleTimelineEvent(TimelineEvent):
    data: TradeLifecycleData = field(default_factory=TradeLifecycleData)

    def __post_init__(self):
        self.source_sense = "TRADE_LIFECYCLE"
        # event_type will be set by from_redis_message based on TESTRADE_TRADE_OPENED/MODIFIED/CLOSED

    @staticmethod
    def from_redis_message(redis_msg_dict: Dict[str, Any], pds_gsi: int) -> Optional['TradeLifecycleTimelineEvent']:
        logger_types = logging.getLogger(f"{__name__}.types.TradeLifecycleEvent")
        try:
            metadata = redis_msg_dict.get("metadata", {})
            payload = redis_msg_dict.get("payload", {})
            testrade_event_type = metadata.get("eventType")

            if testrade_event_type != "TESTRADE_TRADE_LIFECYCLE_EVENT":
                return None

            # Map the payload event_type to IntelliSense event type
            payload_event_type = payload.get("event_type", "UNKNOWN")
            intellisense_event_type = f"TRADE_LIFECYCLE_{payload_event_type}"

            data_obj = TradeLifecycleData(
                event_type=payload.get("event_type"),
                trade_id=str(payload.get("trade_id")) if payload.get("trade_id") is not None else None,
                symbol=payload.get("symbol"),
                lifecycle_state=payload.get("lifecycle_state"),
                correlation_id=payload.get("correlation_id"),
                origin=payload.get("origin"),
                comment=payload.get("comment"),
                shares=payload.get("shares"),
                avg_price=payload.get("avg_price"),
                local_orders=payload.get("local_orders", []),
                timestamp=payload.get("timestamp"),
                raw_redis_payload_snapshot=payload.copy()
            )

            return TradeLifecycleTimelineEvent(
                global_sequence_id=pds_gsi,
                epoch_timestamp_s=float(payload.get("timestamp", metadata.get("timestamp_ns", time.time() * 1e9) / 1e9)),
                perf_counter_timestamp=int(metadata.get("timestamp_ns", time.perf_counter_ns())),
                source_sense="TRADE_LIFECYCLE",
                event_type=intellisense_event_type,
                correlation_id=metadata.get("correlationId", payload.get("correlation_id")),
                source_info={"component": metadata.get("sourceComponent"), "redis_stream_event_id": metadata.get("eventId")},
                data=data_obj
            )
        except Exception as e:
            logger_types.error(f"TradeLifecycleTimelineEvent.from_redis_message error: {e}", exc_info=True)
            return None

    @staticmethod
    def from_correlation_log_entry(log_entry: Dict[str, Any]) -> Optional['TradeLifecycleTimelineEvent']:
        logger_types = logging.getLogger(f"{__name__}.types.TradeLifecycleEvent")
        try:
            data_obj = TradeLifecycleData(**log_entry.get('event_payload', {}))
            return TradeLifecycleTimelineEvent(
                global_sequence_id=log_entry['global_sequence_id'],
                epoch_timestamp_s=log_entry['epoch_timestamp_s'],
                perf_counter_timestamp=log_entry['perf_timestamp_ns'],
                source_sense=log_entry.get('source_sense', "TRADE_LIFECYCLE"),
                event_type=log_entry.get('event_type', "UNKNOWN_TRADE_LIFECYCLE_EVENT"),
                correlation_id=log_entry.get('correlation_id'),
                source_info=log_entry.get('source_info', {}),
                processing_latency_ns=log_entry.get('component_processing_latency_ns'),
                data=data_obj
            )
        except Exception as e:
            logger_types.error(f"TradeLifecycleTimelineEvent.from_correlation_log_entry error: {e}", exc_info=True)
            return None


@dataclass
class SystemAlertData:
    """Data payload for system alert events."""
    alert_id: Optional[str] = None
    severity: Optional[str] = None # e.g., "CRITICAL", "WARNING", "INFO"
    alert_message: Optional[str] = None
    affected_component: Optional[str] = None
    details: Optional[Dict[str, Any]] = field(default_factory=dict)
    raw_redis_payload_snapshot: Optional[Dict[str, Any]] = field(default_factory=dict, repr=False)

    def to_dict_for_log(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class SystemAlertTimelineEvent(TimelineEvent):
    data: SystemAlertData = field(default_factory=SystemAlertData)

    def __post_init__(self):
        self.source_sense = "SYSTEM_ALERT"
        if not self.event_type:
            self.event_type = "SYSTEM_ALERT_RAISED"

    @staticmethod
    def from_redis_message(redis_msg_dict: Dict[str, Any], pds_gsi: int) -> Optional['SystemAlertTimelineEvent']:
        logger_types = logging.getLogger(f"{__name__}.types.SystemAlertEvent")
        try:
            metadata = redis_msg_dict.get("metadata", {})
            payload = redis_msg_dict.get("payload", {})
            if not metadata or not payload or metadata.get("eventType") != "TESTRADE_SYSTEM_ALERT":
                return None

            data_obj = SystemAlertData(
                alert_id=payload.get("alert_id"),
                severity=payload.get("severity"),
                alert_message=payload.get("message"),
                affected_component=payload.get("component"),
                details=payload.get("details", {}),
                raw_redis_payload_snapshot=payload.copy()
            )
            return SystemAlertTimelineEvent(
                global_sequence_id=pds_gsi,
                epoch_timestamp_s=float(payload.get("timestamp", metadata.get("timestamp_ns", time.time() * 1e9) / 1e9)),
                perf_counter_timestamp=int(metadata.get("timestamp_ns", time.perf_counter_ns())),
                source_sense="SYSTEM_ALERT",
                event_type="SYSTEM_ALERT_RAISED",
                correlation_id=metadata.get("correlationId"),
                source_info={"component": metadata.get("sourceComponent"), "redis_stream_event_id": metadata.get("eventId")},
                data=data_obj
            )
        except Exception as e:
            logger_types.error(f"SystemAlertTimelineEvent.from_redis_message error: {e}", exc_info=True)
            return None

    @staticmethod
    def from_correlation_log_entry(log_entry: Dict[str, Any]) -> Optional['SystemAlertTimelineEvent']:
        logger_types = logging.getLogger(f"{__name__}.types.SystemAlertEvent")
        try:
            data_obj = SystemAlertData(**log_entry.get('event_payload', {}))
            return SystemAlertTimelineEvent(
                global_sequence_id=log_entry['global_sequence_id'],
                epoch_timestamp_s=log_entry['epoch_timestamp_s'],
                perf_counter_timestamp=log_entry['perf_timestamp_ns'],
                source_sense=log_entry.get('source_sense', "SYSTEM_ALERT"),
                event_type=log_entry.get('event_type', "SYSTEM_ALERT_RAISED"),
                correlation_id=log_entry.get('correlation_id'),
                source_info=log_entry.get('source_info', {}),
                processing_latency_ns=log_entry.get('component_processing_latency_ns'),
                data=data_obj
            )
        except Exception as e:
            logger_types.error(f"SystemAlertTimelineEvent.from_correlation_log_entry error: {e}", exc_info=True)
            return None


@dataclass
class AccountSummaryData:
    """Data payload for account summary events."""
    account_value: Optional[float] = None
    buying_power: Optional[float] = None
    buying_power_used: Optional[float] = None
    day_pnl: Optional[float] = None
    cash_balance: Optional[float] = None
    timestamp: Optional[float] = None
    is_bootstrap: Optional[bool] = None
    data_source: Optional[str] = None
    raw_redis_payload_snapshot: Optional[Dict[str, Any]] = field(default_factory=dict, repr=False)

    def to_dict_for_log(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class AccountSummaryTimelineEvent(TimelineEvent):
    data: AccountSummaryData = field(default_factory=AccountSummaryData)

    def __post_init__(self):
        self.source_sense = "ACCOUNT"
        if not self.event_type:
            self.event_type = "ACCOUNT_SUMMARY_UPDATED"

    @staticmethod
    def from_redis_message(redis_msg_dict: Dict[str, Any], pds_gsi: int) -> Optional['AccountSummaryTimelineEvent']:
        logger_types = logging.getLogger(f"{__name__}.types.AccountSummaryEvent")
        try:
            metadata = redis_msg_dict.get("metadata", {})
            payload = redis_msg_dict.get("payload", {})
            if not metadata or not payload or metadata.get("eventType") != "TESTRADE_ACCOUNT_SUMMARY_UPDATE":
                return None

            data_obj = AccountSummaryData(
                account_value=payload.get("account_value"),
                buying_power=payload.get("buying_power"),
                buying_power_used=payload.get("buying_power_used"),
                day_pnl=payload.get("day_pnl"),
                cash_balance=payload.get("cash_balance"),
                timestamp=payload.get("timestamp"),
                is_bootstrap=payload.get("is_bootstrap"),
                data_source=payload.get("data_source"),
                raw_redis_payload_snapshot=payload.copy()
            )
            return AccountSummaryTimelineEvent(
                global_sequence_id=pds_gsi,
                epoch_timestamp_s=float(payload.get("timestamp", metadata.get("timestamp_ns", time.time() * 1e9) / 1e9)),
                perf_counter_timestamp=int(metadata.get("timestamp_ns", time.perf_counter_ns())),
                source_sense="ACCOUNT",
                event_type="ACCOUNT_SUMMARY_UPDATED",
                correlation_id=metadata.get("correlationId"),
                source_info={"component": metadata.get("sourceComponent"), "redis_stream_event_id": metadata.get("eventId")},
                data=data_obj
            )
        except Exception as e:
            logger_types.error(f"AccountSummaryTimelineEvent.from_redis_message error: {e}", exc_info=True)
            return None

    @staticmethod
    def from_correlation_log_entry(log_entry: Dict[str, Any]) -> Optional['AccountSummaryTimelineEvent']:
        logger_types = logging.getLogger(f"{__name__}.types.AccountSummaryEvent")
        try:
            data_obj = AccountSummaryData(**log_entry.get('event_payload', {}))
            return AccountSummaryTimelineEvent(
                global_sequence_id=log_entry['global_sequence_id'],
                epoch_timestamp_s=log_entry['epoch_timestamp_s'],
                perf_counter_timestamp=log_entry['perf_timestamp_ns'],
                source_sense=log_entry.get('source_sense', "ACCOUNT"),
                event_type=log_entry.get('event_type', "ACCOUNT_SUMMARY_UPDATED"),
                correlation_id=log_entry.get('correlation_id'),
                source_info=log_entry.get('source_info', {}),
                processing_latency_ns=log_entry.get('component_processing_latency_ns'),
                data=data_obj
            )
        except Exception as e:
            logger_types.error(f"AccountSummaryTimelineEvent.from_correlation_log_entry error: {e}", exc_info=True)
            return None


# --- Design Spec Section 9.1: Test Session Configuration ---
@dataclass
class OCRTestConfig:
    """Configuration for OCR testing parameters."""
    frame_processing_fps: int = 30
    accuracy_validation_enabled: bool = True
    ground_truth_comparison_enabled: bool = True
    confidence_threshold: float = 0.80

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'OCRTestConfig':
        """Create OCRTestConfig from dictionary."""
        return cls(
            frame_processing_fps=int(data.get('frame_processing_fps', 30)),
            accuracy_validation_enabled=bool(data.get('accuracy_validation_enabled', True)),
            ground_truth_comparison_enabled=bool(data.get('ground_truth_comparison_enabled', True)),
            confidence_threshold=float(data.get('confidence_threshold', 0.80))
        )


@dataclass
class PriceTestConfig:
    """Configuration for price data testing parameters."""
    aapl_sync_enabled: bool = True
    price_correlation_validation: bool = True
    stress_testing_enabled: bool = True
    stress_symbols_count: int = 50
    target_stress_tps: int = 1000

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'PriceTestConfig':
        """Create PriceTestConfig from dictionary."""
        return cls(
            aapl_sync_enabled=bool(data.get('aapl_sync_enabled', True)),
            price_correlation_validation=bool(data.get('price_correlation_validation', True)),
            stress_testing_enabled=bool(data.get('stress_testing_enabled', True)),
            stress_symbols_count=int(data.get('stress_symbols_count', 50)),
            target_stress_tps=int(data.get('target_stress_tps', 1000))
        )


@dataclass
class DelayScenario:
    """Configuration for broker delay simulation scenarios."""
    event_match_pattern: str
    fixed_delay_ms: Optional[float] = None
    delay_range_ms: Optional[Tuple[float, float]] = None
    failure_probability: Optional[float] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'DelayScenario':
        """Create DelayScenario from dictionary."""
        delay_range = data.get('delay_range_ms')
        if delay_range and isinstance(delay_range, (list, tuple)) and len(delay_range) == 2:
            delay_range = (float(delay_range[0]), float(delay_range[1]))
        else:
            delay_range = None

        return cls(
            event_match_pattern=str(data.get('event_match_pattern', '')),
            fixed_delay_ms=float(data['fixed_delay_ms']) if data.get('fixed_delay_ms') is not None else None,
            delay_range_ms=delay_range,
            failure_probability=float(data['failure_probability']) if data.get('failure_probability') is not None else None
        )


@dataclass
class BrokerTestConfig:
    """Configuration for broker testing and simulation parameters."""
    delay_profile: str = 'normal'
    response_validation_enabled: bool = True
    custom_delay_scenarios: Dict[str, DelayScenario] = field(default_factory=dict)
    failure_simulation_enabled: bool = True

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'BrokerTestConfig':
        """Create BrokerTestConfig from dictionary."""
        # Handle custom_delay_scenarios dict
        scenarios_data = data.get('custom_delay_scenarios', {})
        scenarios = {}
        if isinstance(scenarios_data, dict):
            for key, scenario_data in scenarios_data.items():
                if isinstance(scenario_data, dict):
                    scenarios[key] = DelayScenario.from_dict(scenario_data)
                else:
                    # If it's already a DelayScenario object, keep it
                    scenarios[key] = scenario_data

        return cls(
            delay_profile=str(data.get('delay_profile', 'normal')),
            response_validation_enabled=bool(data.get('response_validation_enabled', True)),
            custom_delay_scenarios=scenarios,
            failure_simulation_enabled=bool(data.get('failure_simulation_enabled', True))
        )


@dataclass
class TestSessionConfig:
    """Comprehensive test session configuration."""
    session_name: str
    description: str
    test_date: str
    controlled_trade_log_path: str
    historical_price_data_path_sync: str
    broker_response_log_path: str

    historical_price_data_path_stress: Optional[str] = None
    duration_minutes: int = 60
    speed_factor: float = 1.0

    ocr_config: OCRTestConfig = field(default_factory=OCRTestConfig)
    price_config: PriceTestConfig = field(default_factory=PriceTestConfig)
    broker_config: BrokerTestConfig = field(default_factory=BrokerTestConfig)

    monitoring_enabled: bool = True
    dashboard_update_interval_sec: float = 5.0
    metrics_collection_interval_sec: float = 1.0

    validation_enabled: bool = True
    correlation_tolerance_ms: float = 10.0
    accuracy_threshold_percent: float = 95.0
    output_directory: Optional[str] = None

    # Redis stream configuration for processed ROI images
    redis_stream_processed_roi_images: Optional[str] = "testrade:processed-roi-image-frames"

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'TestSessionConfig':
        """Create TestSessionConfig from dictionary with nested dataclass handling."""
        # Handle nested configs
        ocr_config_data = data.get('ocr_config', {})
        if isinstance(ocr_config_data, dict):
            ocr_config = OCRTestConfig.from_dict(ocr_config_data)
        else:
            ocr_config = ocr_config_data if isinstance(ocr_config_data, OCRTestConfig) else OCRTestConfig()

        price_config_data = data.get('price_config', {})
        if isinstance(price_config_data, dict):
            price_config = PriceTestConfig.from_dict(price_config_data)
        else:
            price_config = price_config_data if isinstance(price_config_data, PriceTestConfig) else PriceTestConfig()

        broker_config_data = data.get('broker_config', {})
        if isinstance(broker_config_data, dict):
            broker_config = BrokerTestConfig.from_dict(broker_config_data)
        else:
            broker_config = broker_config_data if isinstance(broker_config_data, BrokerTestConfig) else BrokerTestConfig()

        return cls(
            session_name=str(data.get('session_name', 'unknown_session')),
            description=str(data.get('description', '')),
            test_date=str(data.get('test_date', '1970-01-01')),
            controlled_trade_log_path=str(data.get('controlled_trade_log_path', '')),
            historical_price_data_path_sync=str(data.get('historical_price_data_path_sync', '')),
            broker_response_log_path=str(data.get('broker_response_log_path', '')),
            historical_price_data_path_stress=data.get('historical_price_data_path_stress'),
            duration_minutes=int(data.get('duration_minutes', 60)),
            speed_factor=float(data.get('speed_factor', 1.0)),
            ocr_config=ocr_config,
            price_config=price_config,
            broker_config=broker_config,
            monitoring_enabled=bool(data.get('monitoring_enabled', True)),
            dashboard_update_interval_sec=float(data.get('dashboard_update_interval_sec', 5.0)),
            metrics_collection_interval_sec=float(data.get('metrics_collection_interval_sec', 1.0)),
            validation_enabled=bool(data.get('validation_enabled', True)),
            correlation_tolerance_ms=float(data.get('correlation_tolerance_ms', 10.0)),
            accuracy_threshold_percent=float(data.get('accuracy_threshold_percent', 95.0)),
            output_directory=data.get('output_directory'),
            redis_stream_processed_roi_images=data.get('redis_stream_processed_roi_images', 'testrade:processed-roi-image-frames')
        )


# --- IntelliSense Configuration (Refinement 1) ---
@dataclass
class IntelliSenseConfig:
    """Configuration for IntelliSense mode operation."""
    test_session_config: TestSessionConfig
    # Example: {"ocr_data_source": "replay_file_X", "price_data_source": "live_alpaca_subset"}
    data_source_overrides: Dict[str, Any] = field(default_factory=dict)
    performance_monitoring_active: bool = True  # To enable/disable benchmarker during IntelliSense
    real_time_dashboard_enabled: bool = True  # If IntelliSense has its own dashboard


# --- Helper Dataclasses for Controller Return Types ---
@dataclass
class DataLoadResult:
    """Result of data loading operations."""
    status: DataLoadStatus
    message: str
    ocr_stats: Optional[Dict[str, Any]] = None
    price_stats: Optional[Dict[str, Any]] = None
    broker_stats: Optional[Dict[str, Any]] = None


@dataclass
class SynchronizationResult:
    """Result of timeline synchronization operations."""
    status: SyncAccuracyStatus
    message: str
    overall_correlation_score: Optional[float] = None


@dataclass
class TestExecutionResult:
    """Result of test execution operations."""
    status: TestStatus
    message: str
    session_id: str
    execution_time_seconds: float
    metrics_summary: Optional[Dict[str, Any]] = None


@dataclass
class ComprehensiveReport:
    """Comprehensive test session report."""
    session_id: str
    content: str  # Placeholder for report content


@dataclass
class CreateSessionResponse:
    """Response from session creation operations."""
    session_id: str
    status: str  # e.g., "created", "failed"
    message: str


@dataclass
class ExecutionResponse:
    """Response from test execution operations."""
    session_id: str
    status: str  # e.g., "running", "completed", "failed"
    execution_time_seconds: Optional[float] = None  # If completed
    metrics_summary: Optional[Dict[str, Any]] = None


# --- Session Management Types (Chunk 2) ---
@dataclass
class TestSession:
    """Represents an active or completed IntelliSense test session."""
    session_id: str
    config: TestSessionConfig  # The configuration used for this session
    status: TestStatus = TestStatus.PENDING  # Current status of the session

    # Enhanced timing fields for Redis compatibility
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None

    # Legacy timing fields (for backward compatibility)
    start_time: Optional[float] = None
    end_time: Optional[float] = None

    # Progress and phase tracking
    progress_percent: float = 0.0
    current_phase: Optional[str] = None

    # Store results of MillisecondOptimizationCapture.execute_plan()
    plan_execution_results: Optional[List[Dict[str, Any]]] = field(default_factory=list)

    # Data paths might be resolved from config and stored here for convenience
    resolved_ocr_data_path: Optional[str] = None
    resolved_price_sync_data_path: Optional[str] = None
    resolved_price_stress_data_path: Optional[str] = None
    resolved_broker_log_path: Optional[str] = None

    # Results and reporting
    results_summary: Optional[Dict[str, Any]] = None
    detailed_report_path: Optional[str] = None

    # Enhanced error and data tracking
    error_messages: List[str] = field(default_factory=list)
    data_files_loaded: List[str] = field(default_factory=list)
    memory_usage_mb: Optional[float] = None

    # Legacy error field (for backward compatibility)
    errors: List[str] = field(default_factory=list)

    def get_duration_seconds(self) -> Optional[float]:
        """Calculate session duration in seconds."""
        if self.started_at and self.completed_at:
            return self.completed_at - self.started_at
        elif self.started_at:
            return time.time() - self.started_at
        return None

    def add_error(self, error_message: str):
        """Add an error message to the session's error list."""
        if error_message and error_message not in self.error_messages:
            self.error_messages.append(error_message)
        # Also add to legacy errors field for backward compatibility
        if error_message and error_message not in self.errors:
            self.errors.append(error_message)

    def update_status(self, new_status: TestStatus, message: Optional[str] = None):
        """Update session status with optional message."""
        self.status = new_status
        # Log status change (implementation detail)
        print(f"Session {self.session_id} status changed to {new_status.name}" + (f": {message}" if message else ""))

    def to_dict(self) -> Dict[str, Any]:
        """Convert session to dictionary for JSON serialization (Redis compatible)."""
        data = {}
        for f in dataclasses.fields(self):
            value = getattr(self, f.name)
            if isinstance(value, TestStatus):  # Handle Enums
                data[f.name] = value.value
            elif isinstance(value, TestSessionConfig):  # Handle nested dataclass
                data[f.name] = value.to_dict() if hasattr(value, 'to_dict') else asdict(value)
            elif isinstance(value, list) and value and hasattr(value[0], '__dict__'):
                # Handle list of dataclasses (like plan_execution_results)
                data[f.name] = [asdict(item) if dataclasses.is_dataclass(item) else item for item in value]
            elif dataclasses.is_dataclass(value) and not isinstance(value, type):  # Other dataclasses
                data[f.name] = asdict(value)
            else:
                data[f.name] = value
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'TestSession':
        """Create TestSession from dictionary (e.g., loaded from Redis JSON)."""
        logger = logging.getLogger(__name__)

        # Convert config dict back to TestSessionConfig object
        config_dict = data.get('config', {})
        try:
            # Use the enhanced from_dict method with nested dataclass handling
            session_cfg_obj = TestSessionConfig.from_dict(config_dict) if config_dict else TestSessionConfig(
                session_name="unknown_from_redis",
                description="",
                test_date="1970-01-01",
                controlled_trade_log_path="",
                historical_price_data_path_sync="",
                broker_response_log_path=""
            )
        except Exception as e_cfg:
            logger.error(f"Error deserializing TestSessionConfig from Redis data: {e_cfg}. Data: {config_dict}", exc_info=True)
            # Fallback to a default config
            session_cfg_obj = TestSessionConfig(
                session_name="error_deserializing_config",
                description="",
                test_date="1970-01-01",
                controlled_trade_log_path="",
                historical_price_data_path_sync="",
                broker_response_log_path=""
            )

        # Deserialize plan_execution_results
        plan_results_data = data.get('plan_execution_results', [])
        plan_execution_results_objs = []
        if plan_results_data:
            try:
                # For now, store as raw dicts if deserialization is complex
                logger.debug(f"Deserializing plan_execution_results (count: {len(plan_results_data)})")
                plan_execution_results_objs = plan_results_data
            except Exception as e_plan:
                logger.error(f"Error deserializing plan_execution_results: {e_plan}", exc_info=True)
                plan_execution_results_objs = []

        return cls(
            session_id=data['session_id'],
            config=session_cfg_obj,
            status=TestStatus(data.get('status', TestStatus.ERROR.value)),
            created_at=float(data.get('created_at', time.time())),
            started_at=float(data['started_at']) if data.get('started_at') is not None else None,
            completed_at=float(data['completed_at']) if data.get('completed_at') is not None else None,
            start_time=float(data['start_time']) if data.get('start_time') is not None else None,
            end_time=float(data['end_time']) if data.get('end_time') is not None else None,
            progress_percent=float(data.get('progress_percent', 0.0)),
            current_phase=data.get('current_phase'),
            plan_execution_results=plan_execution_results_objs,
            resolved_ocr_data_path=data.get('resolved_ocr_data_path'),
            resolved_price_sync_data_path=data.get('resolved_price_sync_data_path'),
            resolved_price_stress_data_path=data.get('resolved_price_stress_data_path'),
            resolved_broker_log_path=data.get('resolved_broker_log_path'),
            results_summary=data.get('results_summary'),
            detailed_report_path=data.get('detailed_report_path'),
            error_messages=data.get('error_messages', []),
            data_files_loaded=data.get('data_files_loaded', []),
            memory_usage_mb=float(data['memory_usage_mb']) if data.get('memory_usage_mb') is not None else None,
            errors=data.get('errors', [])  # Legacy field
        )


@dataclass
class SessionCreationResult:
    """Result of session creation operations."""
    success: bool
    message: str
    session_id: Optional[str] = None
    validation_errors: Optional[List[str]] = None
