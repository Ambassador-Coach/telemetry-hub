# IntelliSense Minimal Test Data Creation
**Date**: June 29, 2025
**Objective**: Create minimal test data files to enable end-to-end workflow testing

## Test Data Files Created

### 1. OCR Correlation Log (`/test_data/ocr_correlation_log.jsonl`)
JSON Lines format with 3 OCR events simulating:
- Frame 1001: NVDA Long position with 100 shares @ $450.50
- Frame 1002: NVDA Long position increased to 150 shares @ $451.00
- Frame 1003: NVDA position closed

**Required fields**:
- `global_sequence_id`: Unique event ID
- `epoch_timestamp_s`: Unix timestamp
- `perf_timestamp_ns`: Performance counter timestamp
- `source_sense`: "OCR"
- `event_payload`: Contains OCR snapshot data

### 2. Price Data (`/test_data/price_data.jsonl`)
JSON Lines format with 5 price events:
- Mix of TRADE and QUOTE events for NVDA
- Prices ranging from $450.25 to $452.00
- Includes volume, bid/ask prices and sizes

**Required fields**:
- `global_sequence_id`: Unique event ID
- `epoch_timestamp_s`: Unix timestamp
- `perf_timestamp_ns`: Performance counter timestamp
- `source_sense`: "PRICE"
- `event_payload`: Contains price data with `price_event_type`

### 3. Broker Response Log (`/test_data/broker_response_log.jsonl`)
JSON Lines format with 3 broker events:
- ORDER_REQUEST: Buy 100 shares of NVDA
- ACK: Order acknowledged by broker
- FILL: Order filled at $450.50

**Required fields**:
- `global_sequence_id`: Unique event ID
- `epoch_timestamp_s`: Unix timestamp
- `perf_timestamp_ns`: Performance counter timestamp
- `source_sense`: "BROKER"
- `event_type`: Broker event type
- `event_payload`: Contains broker response data

## Test Scripts Created

### 1. `test_intellisense_workflow_with_data.py`
Python script that:
- Checks API health
- Creates session with real data paths
- Loads data (should succeed)
- Starts replay (should succeed)
- Monitors progress
- Stops replay
- Gets results
- Deletes session

### 2. `test_intellisense_workflow.bat`
Windows batch file to easily run the test from Windows

## Running the Test

1. **Start the API server** (from Windows):
   ```
   cd C:\TESTRADE
   .venv\Scripts\python.exe -m intellisense.api.main
   ```

2. **Run the test** (from another terminal):
   ```
   C:\TESTRADE\test_intellisense_workflow.bat
   ```
   
   Or directly:
   ```
   cd C:\TESTRADE
   .venv\Scripts\python.exe test_intellisense_workflow_with_data.py
   ```

## Expected Results

With the minimal test data files:
1. Session creation: SUCCESS
2. Data loading: SUCCESS (all 3 data sources load)
3. Start replay: SUCCESS (state becomes RUNNING)
4. OCR events injected via Redis
5. Timeline synchronized across all data sources
6. Results available after completion

## Redis Communication

When replay runs, the system will:
1. Read events from the data files
2. Sort them by timestamp
3. Inject OCR snapshots via Redis commands
4. TESTRADE will receive these injections
5. Complete isolation maintained

## Next Steps

1. Monitor Redis streams during test execution
2. Verify OCR injection commands in Redis
3. Check TESTRADE logs for received events
4. Expand test data for more complex scenarios