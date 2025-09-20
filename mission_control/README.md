# TESTRADE Mission Control

A world-class unified launcher and process management system for TESTRADE.

## Features

### 🚀 Unified Launch Interface
- Single UI to manage all TESTRADE processes
- Multiple operational modes (LIVE, TANK, REPLAY, PAPER, SAFE_MODE)
- One-click launch with proper configuration

### 📊 Real-time Process Monitoring
- Live status of all processes (Running, Stopped, Failed)
- CPU and memory usage monitoring
- Process uptime tracking
- Automatic health checks

### 🔍 Intelligent Error Detection
- Analyzes startup failures and provides solutions
- Detects common issues (port conflicts, missing modules, connection errors)
- Actionable recovery suggestions
- Context-aware restart capabilities

### 📝 Consolidated Console Output
- All process logs in one place
- Filterable by process
- Searchable console history
- Color-coded log levels
- Real-time streaming via WebSocket

### ⚡ Process Management
- Start/Stop/Restart individual processes
- Dependency-aware startup ordering
- Graceful shutdown sequences
- Automatic process recovery

### 🛡️ Mode Management
- **LIVE**: Production trading with real money
- **TANK_SEALED**: Isolated testing, no external connections
- **TANK_BROADCASTING**: Testing with full telemetry
- **REPLAY**: Historical data replay
- **PAPER**: Paper trading with simulated money
- **SAFE_MODE**: Emergency mode - OCR only, no trading

## Architecture

```
Mission Control
├── Server (FastAPI + WebSocket)
│   ├── ProcessMonitor - Manages subprocess lifecycles
│   ├── ConsoleAggregator - Captures and parses logs
│   ├── ErrorDetector - Intelligent error analysis
│   └── ModeManager - Configuration management
└── Frontend (React + Vite)
    ├── Real-time WebSocket updates
    ├── Process control interface
    └── Console viewer
```

## Setup

### Prerequisites
- Python 3.8+
- Node.js 16+
- TESTRADE virtual environment activated

### Installation

1. **Install Python dependencies**:
   ```bash
   pip install fastapi uvicorn websockets psutil
   ```

2. **Install frontend dependencies**:
   ```bash
   cd mission_control/frontend
   npm install
   ```

## Usage

### Starting Mission Control

1. **Start the server** (in one terminal):
   ```bash
   start_mission_control.bat
   ```
   This will start the FastAPI server on http://localhost:8000

2. **Start the UI** (in another terminal):
   ```bash
   start_mission_control_ui.bat
   ```
   This will start the React UI on http://localhost:3000

3. **Open your browser** to http://localhost:3000

### Using Mission Control

1. **Select a Mode**: Choose your desired operational mode
2. **Launch**: Click the Launch button to start all processes
3. **Monitor**: Watch real-time status and console output
4. **Manage**: Start/stop/restart individual processes as needed

## Process Definitions

Processes are defined in `process_monitor.py` with platform-specific commands:

```python
"Redis": {
    "platform": {
        "windows": {
            "command": ["wsl", "-d", "Ubuntu", "--", "redis-server"]
        }
    }
}
```

## Mode Configurations

Mode configurations are defined in `mode_manager.py`:

```python
"LIVE": {
    "config_updates": {
        "enable_intellisense_logging": True,
        "enable_trading_signals": True,
        "risk_max_position_size": 10000
    },
    "processes": ["Redis", "BabysitterService", "Main", "GUI"]
}
```

## Error Detection

The system can detect and provide solutions for:
- Port conflicts
- Missing Python modules
- Connection failures (Redis, Broker)
- Configuration errors
- Memory issues
- Permission problems

## API Endpoints

- `GET /api/status` - Get current system status
- `POST /api/launch` - Launch TESTRADE in a specific mode
- `POST /api/process/{action}` - Control individual processes
- `POST /api/shutdown` - Shutdown all processes
- `WS /ws` - WebSocket for real-time updates

## WebSocket Events

- `status_update` - Process status changes
- `console_output` - New console log entries
- `alert` - System alerts and warnings

## Troubleshooting

### Server won't start
- Check if port 8000 is available
- Ensure Python virtual environment is activated
- Verify all dependencies are installed

### UI won't connect
- Ensure the server is running first
- Check browser console for errors
- Verify WebSocket connection at ws://localhost:8000/ws

### Process won't start
- Check the error analysis in the UI
- Verify all dependencies for that process
- Check system logs in `logs/` directory

## Future Enhancements

- [ ] Process resource limits
- [ ] Automated recovery policies
- [ ] Performance metrics dashboard
- [ ] Configuration validation
- [ ] Remote monitoring support
- [ ] Process log export
- [ ] Alert notifications (email/webhook)
- [ ] Dark/Light theme toggle

## Development

### Adding a New Process

1. Add definition to `process_definitions` in `process_monitor.py`
2. Add to appropriate mode in `mode_manager.py`
3. Define dependencies in `process_dependencies`

### Adding a New Mode

1. Define mode in `mode_configs` in `mode_manager.py`
2. Add mode info to frontend `MODE_INFO`
3. Test configuration updates

### Adding Error Patterns

1. Add pattern to `error_patterns` in `error_detector.py`
2. Add solutions to `solution_database`
3. Test with sample error text