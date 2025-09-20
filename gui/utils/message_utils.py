# gui/utils/message_utils.py - GUI-specific message utilities (INDEPENDENT)
import json
import time
import uuid
from typing import Dict, Any

def create_redis_message_json(
    payload: Dict[str, Any],
    event_type_str: str,
    correlation_id_val: str,
    source_component_name: str
) -> str:
    """
    Create Redis message JSON - GUI backend version
    Self-contained, no dependencies on TESTRADE core
    """
    message_wrapper = {
        "metadata": {
            "eventId": str(uuid.uuid4()),
            "eventType": event_type_str,
            "correlationId": correlation_id_val,
            "causationId": correlation_id_val,
            "sourceComponent": source_component_name,
            "timestamp": time.time(),
            "timestamp_ns": time.time_ns(),
            "version": "1.0"
        },
        "payload": payload
    }
    
    return json.dumps(message_wrapper)

def parse_redis_message(raw_redis_message: Dict[str, Any]) -> Dict[str, Any]:
    """Parse Redis message - GUI backend version"""
    json_payload_str = raw_redis_message.get("json_payload")
    if not json_payload_str:
        return None
    
    try:
        wrapper_dict = json.loads(json_payload_str)
        payload = wrapper_dict.get("payload", {})
        metadata = wrapper_dict.get("metadata", {})
        
        # Preserve correlation ID from metadata
        if "correlationId" in metadata and "bootstrap_correlation_id" not in payload:
            payload["bootstrap_correlation_id"] = metadata["correlationId"]
        
        return {"metadata": metadata, "payload": payload}
    except (json.JSONDecodeError, TypeError) as e:
        return None