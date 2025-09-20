ProcessOrchestrator Health Checks for OCR Telemetry
===================================================

Purpose
- Monitor the OCR daemon’s telemetry health using lightweight Windows primitives.
- React quickly to stalls without invasive coupling to the EXE.

Signals to watch
- Event: `Local\testrade_telemetry_stuck` — set by the EXE when the cold-path spinlock is busy too long.
- SHM sequence: `Local\testrade_ocr_cold` — `sequence` grows when new frames are emitted (cold path enabled).

Event-based health check (recommended)
```python
import win32event

def check_telemetry_health_event() -> bool:
    evt = win32event.OpenEvent(win32event.EVENT_ALL_ACCESS, False, "Local\\testrade_telemetry_stuck")
    if not evt:
        return True  # event not present; treat as healthy
    result = win32event.WaitForSingleObject(evt, 0)
    return result != win32event.WAIT_OBJECT_0
```

Sequence-based health (optional, complementary)
```python
import mmap, struct, time

def check_telemetry_progress(timeout_sec: float = 5.0) -> bool:
    # Map minimal region; actual layout: ColdPathHeader with sequence at offset 16
    shm = mmap.mmap(-1, 4096, tagname="Local\\testrade_ocr_cold")
    try:
        off_seq = 16
        last = struct.unpack_from('<Q', shm, off_seq)[0]
        time.sleep(timeout_sec)
        cur = struct.unpack_from('<Q', shm, off_seq)[0]
        return cur != last
    finally:
        shm.close()
```

Integration sketch
```python
def health_check() -> bool:
    # Fail-fast on event
    if not check_telemetry_health_event():
        return False
    # Optionally also ensure progress
    # return check_telemetry_progress(5.0)
    return True

def on_unhealthy():
    # Restart telemetry consumer or the whole OCR telemetry process
    # e.g., orchestrator.restart_process("OCRTelemetryPYD")
    pass
```

Launcher env suggestions
- `DXGI_MAX_WIDTH=800`
- `DXGI_MAX_HEIGHT=400`
- `UPSCALE_MAX=3.0`
- `CONTROL_ROI_MIN_MS=100` (coalesce high-frequency ROI updates)

Notes
- The EXE remains self-contained: it sets the stuck event and continues processing (skipping a frame) — no hard locks.
- Use the event health check in ProcessOrchestrator to complete the self-healing loop.
