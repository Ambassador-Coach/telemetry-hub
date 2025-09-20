# MOC Injection Plans Documentation

## Overview

This directory contains InjectionPlans for MillisecondOptimizationCapture (MOC) designed for end-to-end testing of IntelliSense and generating data to fine-tune TESTRADE. These plans utilize the "Replayable Trinity" (Processed ROI Images, Price Data, Broker Replies) by referencing data logged by CorrelationLogger from Redis streams.

## General Principles

### Targeted Testing
Each plan focuses on a specific aspect of TESTRADE:
- OCR accuracy impact on signal generation
- Order execution latency and reliability  
- Risk rule triggering and validation
- Configuration and state change behavior

### Correlated Events
Each InjectionStep generates a unique `master_correlation_id` (called `step_corr_id` by MOC) for:
- Stimulus injection tracking
- Subsequent event tracing
- Cross-referencing with ProcessedImageFrameTimelineEvent for visual context

### Validated Outcomes
Each step includes clear ValidationPoints to:
- Assert expected outcomes
- Measure performance metrics
- Verify system behavior under various conditions

### Contextual Reporting
Reports enable easy cross-referencing to logged events via `master_correlation_id` for visual context, especially for OCR-related steps.

## Injection Plan Categories

### 1. OCR Accuracy & Signal Generation Robustness

**File**: `IP_OCR_ACC_001_SignalGen.json`

**Objective**: Test how variations in OCR quality affect signal generation and downstream order processing.

**Stimulus Type**: `ocr_event` using `EnhancedOCRDataConditioningService.inject_simulated_raw_ocr_input`

**Test Cases**:
- **High Confidence Clear Signal**: 95% confidence, clear AAPL LONG signal
- **Low Confidence Ambiguous Signal**: 60% confidence with typos ("LUNG" instead of "LONG")
- **Medium Confidence Partial Signal**: 78% confidence with missing quantity data
- **Noise Rejection**: High confidence but irrelevant text content

**Key Validations**:
- Pipeline completion status
- Signal generation accuracy (LONG → BUY conversion)
- Processing latency thresholds (< 200-300ms)
- Proper noise filtering and rejection

### 2. Order Execution Path Latency & Reliability

**File**: `IP_ORD_EXEC_001_MktOrder.json`

**Objective**: Measure end-to-end latency from order request to fill and validate broker interaction.

**Stimulus Type**: `trade_command` via `_inject_trade_command` publishing `OrderRequestEvent`

**Test Cases**:
- **Market Buy Order**: AAPL market buy with full fill validation
- **Limit Sell Order**: TSLA limit sell with ACK validation
- **Partial Fill Scenario**: NVDA order with partial fill handling
- **Order Rejection**: Invalid symbol rejection testing
- **Rapid Sequence**: Throughput testing with multiple rapid orders

**Key Validations**:
- End-to-end execution latency (< 500ms)
- Order-to-ACK latency (< 100ms)
- Position manager state updates
- Fill quantity and price accuracy
- Rejection handling and error codes

### 3. Risk Management Rule Validation

**File**: `IP_RISK_001_MaxPos.json`

**Objective**: Test TESTRADE's risk rules (max position size, P&L limits) trigger correctly.

**Stimulus Type**: `trade_command` sequences designed to breach risk limits

**Test Cases**:
- **Max Position Setup**: Establish position near limit (90/100 shares)
- **Position Limit Breach**: Attempt to exceed maximum position size
- **P&L Limit Setup**: Establish position for loss limit testing
- **P&L Limit Trigger**: Price movement triggering daily loss limits
- **Concentration Risk**: Multi-symbol concentration limit testing
- **Risk Recovery**: System recovery after limit reset

**Key Validations**:
- Risk rejection messages and reasons
- Position manager state preservation
- Trading state changes (ACTIVE → RISK_PAUSED)
- Portfolio risk metrics and warnings
- Recovery and resume functionality

### 4. Contextual Event Validation (Config Changes, Trading State)

**File**: `IP_CONTEXT_001_ConfigState.json`

**Objective**: Verify system behavior when configuration or trading state changes.

**Stimulus Types**: `config_change`, `trading_state_change`, `ocr_event`

**Test Cases**:
- **Baseline Performance**: Establish normal operation metrics
- **Config Change Trigger**: Modify OCR confidence thresholds
- **Post-Config Performance**: Verify behavior with new configuration
- **Trading State Pause**: Test paused state behavior
- **Paused State Behavior**: OCR processing continues, no orders generated
- **Trading State Resume**: Return to active trading
- **Post-Resume Verification**: Confirm normal operation restored

**Key Validations**:
- Configuration change logging
- Latency impact analysis (< 20% increase)
- State transition accuracy
- Behavior consistency during state changes
- Performance comparison across configurations

## Validation Point Types

### Pipeline Validation
- `pipeline_completion_status`: Ensures pipelines complete successfully
- `pipeline_latency`: Validates processing time thresholds
- `latency_comparison`: Compares performance across scenarios

### Event Log Validation
- `specific_log_event_field`: Validates specific fields in logged events
- `correlation_log_event_count`: Counts events matching criteria
- `event_criteria`: Filters events by source_sense, event_type, correlation_id

### System State Validation
- `position_manager_state`: Validates position quantities and values
- `portfolio_risk_metric`: Checks risk calculations and limits
- `trading_state`: Verifies system state transitions

### Operators
- `==`: Exact equality
- `<=`, `>=`: Numeric comparisons
- `contains_ignore_case`: Case-insensitive substring matching
- `contains`: Exact substring matching

## Usage Guidelines

### File Naming Convention
- `IP_[CATEGORY]_[NUMBER]_[DESCRIPTION].json`
- Examples: `IP_OCR_ACC_001_SignalGen.json`, `IP_RISK_001_MaxPos.json`

### Correlation ID Patterns
- Use descriptive prefixes: `moc_ocr_hc_001`, `moc_trade_mkt_001`
- Include scenario type and sequence number
- Ensure uniqueness across all test steps

### Timing Considerations
- `wait_after_injection_s`: Allow sufficient time for pipeline completion
- Consider downstream processing delays
- Account for broker response times in execution tests

### Validation Strategy
- Start with pipeline completion validation
- Add specific field validations for critical data
- Include latency thresholds appropriate for production
- Validate both positive and negative test cases

## Integration with TESTRADE

### Data Sources
Plans reference data from TESTRADE Redis streams:
- `testrade:processed-roi-image-frames`
- `testrade:raw-ocr-events`
- `testrade:cleaned-ocr-snapshots`
- `testrade:market-data:trades`
- `testrade:market-data:quotes`
- `testrade:broker-*` streams

### Stimulus Injection
- OCR events via `EnhancedOCRDataConditioningService`
- Trade commands via `OrderRequestEvent` publishing
- Price events via market data injection
- Config/state changes via TESTRADE APIs

### Result Analysis
- Cross-reference with `ProcessedImageFrameTimelineEvent` for visual context
- Analyze pipeline latencies for performance optimization
- Validate risk rule effectiveness
- Measure system resilience under various conditions

## Future Enhancements

### Advanced Scenarios
- Multi-symbol concurrent processing
- Market volatility simulation
- Network latency injection
- Broker connectivity issues

### Enhanced Validation
- Statistical analysis of latency distributions
- Correlation analysis between OCR confidence and accuracy
- Risk model validation under stress conditions
- Performance regression detection

### Automation
- Automated plan generation based on historical data
- Dynamic threshold adjustment based on system performance
- Continuous integration with TESTRADE builds
- Automated report generation and analysis
