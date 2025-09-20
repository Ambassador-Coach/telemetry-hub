# IntelliSense Data Loading Investigation Report
**Date**: June 29, 2025
**Issue**: Sessions failing at load_data and start_replay steps

## Key Findings

### 1. Session State Machine
The proper state transitions for a test session are:
- `PENDING` - Session created, not yet started
- `DATA_LOADING` - Data is being loaded 
- `DATA_LOADED_PENDING_SYNC` - Data loaded, ready for timeline sync (NOT `READY`)
- `RUNNING` - Test session is actively replaying/injecting
- `COMPLETED` - Session finished normally
- `STOPPED` - Session was manually stopped
- `FAILED` - Session failed due to test logic or data issues

### 2. Required Data Files
The data source factory (`intellisense/factories/datasource_factory.py`) requires:

1. **OCR Data** (`controlled_trade_log_path`):
   - Expected format: JSON Lines correlation log file
   - Used by `IntelliSenseOCRReplaySource`
   - If empty/missing, OCR engine setup fails

2. **Price Data** (`historical_price_data_path_sync`):
   - Expected format: JSON file with price data
   - Available: `/mnt/c/TESTRADE/data/friday_may30_2025/all_trades.json`
   - Used by `IntelliSensePriceReplaySource`

3. **Broker Data** (`broker_response_log_path`):
   - Expected format: Broker response log
   - Used by `IntelliSenseBrokerReplaySource`

### 3. Data Loading Failure Cause
When using placeholder "test" paths:
```python
# In DataSourceFactory.create_ocr_data_source():
if session_config.controlled_trade_log_path:
    return IntelliSenseOCRReplaySource(
        replay_file_path=session_config.controlled_trade_log_path,
        ocr_config=session_config.ocr_config,
        session_config=session_config
    )
else:
    logger.error("OCR replay path not found in session config.")
    return None
```

The OCR replay source then checks if the file exists:
```python
if not os.path.exists(self.replay_file_path):
    logger.error(f"OCR replay file not found: {self.replay_file_path}")
    return False
```

### 4. Proper Workflow Sequence
1. **Create Session** (`POST /sessions/`)
   - Creates session in PENDING state
   - Stores in master controller's session_registry

2. **Load Data** (`POST /sessions/{id}/load_data`)
   - Changes state to DATA_LOADING
   - Calls `ocr_engine.setup_for_session()`
   - If successful, state becomes DATA_LOADED_PENDING_SYNC
   - If fails, state becomes FAILED

3. **Start Replay** (`POST /sessions/{id}/start_replay`)
   - Only works if state is DATA_LOADED_PENDING_SYNC
   - Changes state to RUNNING
   - Starts replay thread

### 5. Solutions
To make the system work with test data:

1. **Create sample OCR data file** in JSON Lines format
2. **Use existing price data** from `/mnt/c/TESTRADE/data/friday_may30_2025/`
3. **Create empty broker log** or use empty string
4. **Or modify the data source factory** to handle missing files gracefully

### 6. Test Script
Created `test_intellisense_workflow.py` to demonstrate the complete workflow with proper error handling and state transitions.

## Next Steps
1. Create sample OCR correlation log file
2. Test with valid data paths
3. Verify Redis injection works end-to-end
4. Document the data format requirements