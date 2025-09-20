# Mission Control Deep Dive Report

## Executive Summary

Mission Control is a sophisticated distributed process management and orchestration system for the TESTRADE/TANK trading platform. It provides unified control over multiple machines, intelligent error detection, and real-time monitoring capabilities across a heterogeneous Windows/Linux environment.

## System Architecture Overview

### Core Design Philosophy
Mission Control follows a **distributed microservices architecture** with centralized orchestration:
- **Hub-and-spoke model**: TELEMETRY machine acts as the control hub
- **Cross-platform support**: Manages Windows (BEAST) and Linux (TELEMETRY) processes
- **Real-time communication**: WebSocket-based streaming for instant updates
- **Fault tolerance**: Automatic recovery and intelligent error handling

### Technology Stack
- **Backend**: FastAPI (Python 3.11) with async/await patterns
- **Real-time**: WebSocket protocol for bidirectional communication
- **Frontend**: React + Vite for responsive UI
- **Process Control**: psutil for cross-platform process management
- **Data Store**: Redis for telemetry and state management
- **Configuration**: JSON-based declarative configuration

## Component Architecture

### 1. Server Components (`/mission_control/server/`)

#### Main Server (`main.py`)
- **Purpose**: Core FastAPI application and orchestration hub
- **Key Features**:
  - WebSocket management for multiple concurrent clients
  - REST API endpoints for control operations
  - CORS middleware for cross-origin requests
  - Event-driven architecture with startup/shutdown hooks
- **Architecture Pattern**: Singleton server pattern with dependency injection

#### Process Monitor (`process_monitor.py`)
- **Purpose**: Lifecycle management of all TESTRADE processes
- **Capabilities**:
  - Platform-aware process spawning (Windows/Linux)
  - Resource monitoring (CPU, memory)
  - Health checks and heartbeat monitoring
  - Automatic restart with exponential backoff
  - Process dependency resolution
- **Design Pattern**: State machine for process lifecycle

#### Distributed Process Monitor (`distributed_process_monitor.py`)
- **Purpose**: Extends ProcessMonitor for multi-machine orchestration
- **Key Features**:
  - SSH-based remote process control on BEAST
  - Local process management on TELEMETRY
  - Cross-machine health monitoring
  - Network fault detection and recovery
- **Architecture**: Master-slave pattern with TELEMETRY as master

#### Mode Manager (`mode_manager.py`)
- **Purpose**: Configuration management for different operational modes
- **Supported Modes**:
  - **LIVE**: Production trading with real money
  - **TANK_SEALED**: Isolated testing environment
  - **TANK_BROADCASTING**: Testing with full telemetry
  - **REPLAY**: Historical data replay
  - **PAPER**: Simulated trading
  - **SAFE_MODE**: Emergency OCR-only mode
- **Implementation**: Strategy pattern for mode-specific configurations

#### Console Aggregator (`console_aggregator.py`)
- **Purpose**: Centralized log collection and streaming
- **Features**:
  - Multi-process output capture
  - Log level parsing and color coding
  - Circular buffer for history
  - Real-time streaming to WebSocket clients
- **Pattern**: Observer pattern for log distribution

#### Error Detector (`error_detector.py`)
- **Purpose**: Intelligent error analysis and recovery suggestions
- **Capabilities**:
  - Pattern-based error recognition
  - Context-aware solution recommendations
  - Root cause analysis
  - Automated recovery action suggestions
- **AI/ML Integration**: Rule-based expert system with pattern matching

#### Zombie Killer (`zombie_killer.py`)
- **Purpose**: Clean up orphaned processes and resources
- **Functions**:
  - Port conflict resolution
  - Orphaned process termination
  - Shared memory cleanup
  - Lock file removal
- **Safety**: Multiple confirmation layers before termination

### 2. Configuration Architecture

#### Mission Control Config (`mission_control_config.json`)
```json
{
  "mission_control_distributed": {
    "architecture": "DISTRIBUTED",
    "machines": {
      "BEAST": {
        "ip": "192.168.50.100",
        "role": "TRADING_ENGINE",
        "services": ["Main", "OCRProcess"]
      },
      "TELEMETRY": {
        "ip": "192.168.50.101",
        "role": "TELEMETRY_HUB",
        "services": ["Redis", "TelemetryService", "BabysitterService", "GUIBackend"]
      }
    }
  }
}
```

### 3. Process Definitions and Dependencies

#### Service Topology
```
Redis (TELEMETRY)
├── BabysitterService (TELEMETRY)
├── TelemetryService (TELEMETRY)
│   ├── Main (BEAST)
│   └── OCRProcess (BEAST)
└── GUIBackend (TELEMETRY)
```

#### Startup Sequence
1. **Phase 1**: Infrastructure (Redis on TELEMETRY)
2. **Phase 2**: Telemetry services (Babysitter, Telemetry)
3. **Phase 3**: Trading engine (Main on BEAST)
4. **Phase 4**: Support services (OCR, GUI)

## Communication Architecture

### Inter-Process Communication
- **ZMQ Endpoints**:
  - Port 5555: OCR bulk data
  - Port 5556: Trading signals
  - Port 5557: System telemetry
- **Protocol**: MessagePack serialization over ZMQ

### WebSocket Protocol
```javascript
// Client -> Server
{
  "type": "launch",
  "mode": "TANK_SEALED",
  "options": {}
}

// Server -> Client
{
  "type": "status_update",
  "process": "Main",
  "state": "running",
  "metrics": {
    "cpu": 15.2,
    "memory_mb": 512
  }
}
```

## Operational Modes Deep Dive

### LIVE Mode
- **Purpose**: Production trading
- **Configuration Changes**:
  - Enables broker connections
  - Sets production risk limits
  - Activates all telemetry
  - Disables development features
- **Required Services**: All services active
- **Safety Features**: Pre-launch validation, risk parameter confirmation

### TANK_SEALED Mode
- **Purpose**: Isolated testing
- **Configuration**:
  - Disables all external connections
  - Uses synthetic data
  - No telemetry broadcast
  - Development mode enabled
- **Minimal Services**: Redis + Main only
- **Use Case**: Algorithm testing without side effects

### SAFE_MODE
- **Purpose**: Emergency operations
- **Features**:
  - OCR continues functioning
  - Trading disabled
  - Read-only market data
  - Full logging enabled
- **Recovery**: Allows diagnosis while preventing trading losses

## Error Detection and Recovery

### Error Pattern Database
```python
error_patterns = {
    "Port already in use": {
        "severity": "HIGH",
        "solution": "Kill existing process or change port",
        "auto_recovery": "kill_port_and_retry"
    },
    "ModuleNotFoundError": {
        "severity": "CRITICAL",
        "solution": "Install missing dependencies",
        "auto_recovery": "pip_install_and_retry"
    }
}
```

### Recovery Strategies
1. **Automatic**: Restart with backoff
2. **Guided**: Step-by-step instructions
3. **Manual**: Alert and wait for intervention
4. **Escalation**: Notify senior operators

## Resource Management

### Memory Management
- Process memory limits enforced
- Automatic garbage collection triggers
- Memory leak detection and alerting
- Shared memory cleanup on termination

### CPU Management
- Process priority settings
- CPU affinity for critical processes
- Load balancing across cores
- Throttling for non-critical services

## Security Considerations

### Access Control
- Local-only API access by default
- SSH key-based remote execution
- No hardcoded credentials
- Configuration file encryption support

### Audit Trail
- All control actions logged
- Process lifecycle events recorded
- Configuration changes tracked
- Error events with full context

## Monitoring and Observability

### Health Checks
```python
health_checks = {
    "Redis": "redis-cli ping",
    "Main": "HTTP GET /health",
    "OCRProcess": "shared_memory_heartbeat"
}
```

### Metrics Collection
- Process-level: CPU, memory, file handles
- System-level: Disk I/O, network traffic
- Application-level: Trade count, OCR latency
- Business-level: P&L, risk exposure

## Deployment Architecture

### Local Development
- Single machine mode
- Mock external services
- Reduced resource requirements
- Fast iteration cycle

### Production Deployment
- Multi-machine distributed mode
- High availability configuration
- Automatic failover support
- Blue-green deployment capability

## Future Architecture Considerations

### Planned Enhancements
1. **Kubernetes Integration**: Container orchestration
2. **Service Mesh**: Istio/Linkerd for microservices
3. **Distributed Tracing**: OpenTelemetry integration
4. **Machine Learning**: Predictive failure detection
5. **Cloud Native**: AWS/GCP deployment options

### Scalability Path
- Horizontal scaling of telemetry services
- Load balancing for OCR processing
- Database sharding for historical data
- Multi-region deployment support

## Integration Points

### External Systems
1. **Trading Infrastructure**:
   - Broker APIs (Interactive Brokers)
   - Market data feeds
   - Order management systems

2. **Monitoring Systems**:
   - Grafana dashboards
   - Prometheus metrics
   - PagerDuty alerts

3. **Development Tools**:
   - Git hooks for deployment
   - CI/CD pipelines
   - Automated testing frameworks

## Performance Characteristics

### Latency Targets
- Process startup: < 5 seconds
- Status update: < 100ms
- Error detection: < 500ms
- Recovery initiation: < 2 seconds

### Throughput
- Console logs: 10,000 lines/second
- WebSocket messages: 1,000/second
- Process operations: 100/minute
- Concurrent clients: 50+

## Operational Procedures

### Standard Operating Procedures
1. **Daily Startup**:
   - Check system resources
   - Verify network connectivity
   - Launch in TANK mode first
   - Validate all services
   - Switch to LIVE mode

2. **Emergency Shutdown**:
   - Activate SAFE_MODE
   - Cancel all pending orders
   - Dump state to disk
   - Graceful service shutdown

3. **Disaster Recovery**:
   - State restoration from backup
   - Service health verification
   - Incremental service activation
   - Full system validation

## Conclusion

Mission Control represents a robust, scalable, and intelligent process orchestration system designed for high-frequency trading operations. Its distributed architecture, comprehensive error handling, and real-time monitoring capabilities make it suitable for mission-critical financial applications.

The system successfully abstracts the complexity of managing a heterogeneous, distributed trading infrastructure while providing operators with powerful tools for monitoring, debugging, and control. The modular design ensures extensibility for future requirements while maintaining operational stability.

---
*Report Generated: 2025-09-19*
*System Version: Mission Control v2.0 (Distributed Architecture)*
*Environment: TELEMETRY Hub - telemetry-hub container*