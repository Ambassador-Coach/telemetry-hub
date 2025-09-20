#!/usr/bin/env python3
"""
OCR Control Publisher (ZMQ PUSH) + control.json updater

Purpose:
- Push control messages to the OCR ZMQ listener (PULL at tcp://127.0.0.1:5559 by default)
- Persist changes into utils/control.json so GUI and services stay in sync

Commands:
- set-roi x1 y1 x2 y2
- set-telemetry on|off
- set-opencv --threads N [--optimized {0,1}] [--log-level silent|fatal|error|warning|info|debug]
- set-params key=value [key=value ...]   # generic params (e.g., ocr_upscale_factor=3.0)

Examples:
- python utils/ocr_control_publisher.py set-roi 64 159 681 296
- python utils/ocr_control_publisher.py set-telemetry on
- python utils/ocr_control_publisher.py set-opencv --threads 1 --optimized 1 --log-level warning
- python utils/ocr_control_publisher.py set-params ocr_apply_text_mask_cleaning=1 ocr_enhance_small_symbols=1

Notes:
- Message format matches OCRZMQCommandListener: multipart [command_type, json]
- Inner JSON: {"command_id": uuid4, "parameters": {...}}
"""

import argparse
import json
import os
import sys
import uuid
from typing import Dict, Any


def _send_push(command_type: str, parameters: Dict[str, Any], address: str) -> None:
    import zmq
    ctx = zmq.Context.instance()
    sock = ctx.socket(zmq.PUSH)
    sock.connect(address)
    payload = {"command_id": str(uuid.uuid4()), "parameters": parameters}
    sock.send_multipart([command_type.encode("utf-8"), json.dumps(payload).encode("utf-8")])


def _update_control_json(updates: Dict[str, Any]) -> None:
    # Try to use global_config helper first; fallback to direct JSON edit
    try:
        from utils.global_config import update_config_key, CONFIG_FILE_PATH
    except Exception:
        update_config_key = None
        CONFIG_FILE_PATH = os.path.join(os.path.dirname(__file__), "control.json")

    if update_config_key is not None:
        for k, v in updates.items():
            try:
                update_config_key(k, v)
            except Exception:
                pass  # fallback below for unknown keys

    # Fallback: merge into control.json directly
    try:
        path = CONFIG_FILE_PATH
        with open(path, "r", encoding="utf-8") as f:
            current = json.load(f)
    except Exception:
        current = {}
        path = os.path.join(os.path.dirname(__file__), "control.json")
        try:
            with open(path, "r", encoding="utf-8") as f:
                current = json.load(f)
        except Exception:
            current = {}

    current.update(updates)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(current, f, indent=2)


def cmd_set_roi(args, address: str):
    x1, y1, x2, y2 = args.x1, args.y1, args.x2, args.y2
    params = {"x1": x1, "y1": y1, "x2": x2, "y2": y2}
    _send_push("SET_ROI_ABSOLUTE", params, address)
    # Persist ROI to control.json in a single vector for broader app
    _update_control_json({"ROI_COORDINATES": [x1, y1, x2, y2]})
    print(f"[OK] ROI pushed and saved: {x1},{y1},{x2},{y2}")


def cmd_set_telemetry(args, address: str):
    on = args.state.lower() in ("on", "1", "true", "yes")
    params = {"telemetry_enabled": bool(on)}
    _send_push("UPDATE_OCR_PREPROCESSING_FULL", params, address)
    _update_control_json({"telemetry_enabled": bool(on)})
    print(f"[OK] Telemetry set to: {on}")


def cmd_set_opencv(args, address: str):
    params: Dict[str, Any] = {}
    if args.threads is not None:
        params["opencv_threads"] = int(args.threads)
    if args.optimized is not None:
        params["opencv_optimized"] = bool(int(args.optimized))
    if args.log_level:
        params["opencv_log_level"] = args.log_level
    if not params:
        print("No OpenCV parameters provided", file=sys.stderr)
        sys.exit(2)
    _send_push("UPDATE_OCR_PREPROCESSING_FULL", params, address)
    _update_control_json(params)
    print(f"[OK] OpenCV params pushed and saved: {params}")


def cmd_set_params(args, address: str):
    params: Dict[str, Any] = {}
    for kv in args.kv:
        if "=" not in kv:
            print(f"Invalid param '{kv}', expected key=value", file=sys.stderr)
            sys.exit(2)
        k, v = kv.split("=", 1)
        # Try to coerce numeric/bool
        val: Any = v
        if v.lower() in ("true", "false"):
            val = v.lower() == "true"
        else:
            try:
                if "." in v:
                    val = float(v)
                else:
                    val = int(v)
            except ValueError:
                val = v
        params[k] = val

    _send_push("UPDATE_OCR_PREPROCESSING_FULL", params, address)
    _update_control_json(params)
    print(f"[OK] Parameters pushed and saved: {params}")


def main():
    ap = argparse.ArgumentParser(description="OCR Control Publisher (ZMQ PUSH + control.json updater)")
    ap.add_argument("--addr", default=os.environ.get("OCR_ZMQ_PUSH_ADDR", "tcp://127.0.0.1:5559"), help="ZMQ PUSH connect address")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s1 = sub.add_parser("set-roi", help="Set ROI absolute coordinates")
    s1.add_argument("x1", type=int)
    s1.add_argument("y1", type=int)
    s1.add_argument("x2", type=int)
    s1.add_argument("y2", type=int)
    s1.set_defaults(func=cmd_set_roi)

    s2 = sub.add_parser("set-telemetry", help="Enable/disable cold-path telemetry")
    s2.add_argument("state", choices=["on", "off", "1", "0", "true", "false"])
    s2.set_defaults(func=cmd_set_telemetry)

    s3 = sub.add_parser("set-opencv", help="Set OpenCV runtime parameters")
    s3.add_argument("--threads", type=int)
    s3.add_argument("--optimized", choices=["0", "1"])  # use 0/1 for easy CLI
    s3.add_argument("--log-level", choices=["silent", "fatal", "error", "warning", "info", "debug"])
    s3.set_defaults(func=cmd_set_opencv)

    s4 = sub.add_parser("set-params", help="Set generic parameters (key=value ...)")
    s4.add_argument("kv", nargs="+")
    s4.set_defaults(func=cmd_set_params)

    args = ap.parse_args()
    args.func(args, args.addr)


if __name__ == "__main__":
    main()

