import React, { useState, useEffect, useRef } from 'react';
import axios from 'axios';
import toast, { Toaster } from 'react-hot-toast';
import { 
  Play, Square, RotateCcw, AlertCircle, CheckCircle, 
  XCircle, Clock, Cpu, HardDrive, Activity, Terminal,
  Shield, Zap, Database, Eye, Layout, Minimize2, Maximize2
} from 'lucide-react';
import './App.css';

const API_BASE = 'http://localhost:8000';
const WS_URL = 'ws://localhost:8000/ws';

// Process icon mapping
const PROCESS_ICONS = {
  'Redis': Database,
  'BabysitterService': Shield,
  'TelemetryService': Activity,
  'Main': Zap,
  'GUI': Layout
};

// Mode descriptions
const MODE_INFO = {
  'LIVE': { color: 'red', emoji: '💰' },
  'TANK_SEALED': { color: 'gray', emoji: '🔒' },
  'TANK_BROADCASTING': { color: 'blue', emoji: '📡' },
  'REPLAY': { color: 'purple', emoji: '📊' },
  'PAPER': { color: 'green', emoji: '📝' },
  'SAFE_MODE': { color: 'orange', emoji: '🛡️' }
};

function App() {
  const [mode, setMode] = useState('STOPPED');
  const [selectedMode, setSelectedMode] = useState('TANK_SEALED');
  const [processes, setProcesses] = useState({});
  const [consoleLogs, setConsoleLogs] = useState([]);
  const [selectedProcess, setSelectedProcess] = useState('ALL');
  const [alerts, setAlerts] = useState([]);
  const [loading, setLoading] = useState(false);
  const [consoleMinimized, setConsoleMinimized] = useState(false);
  const wsRef = useRef(null);
  const consoleEndRef = useRef(null);

  // Initialize WebSocket connection
  useEffect(() => {
    connectWebSocket();
    fetchStatus();

    return () => {
      if (wsRef.current) {
        wsRef.current.close();
      }
    };
  }, []);

  // Auto-scroll console
  useEffect(() => {
    consoleEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [consoleLogs]);

  const connectWebSocket = () => {
    wsRef.current = new WebSocket(WS_URL);

    wsRef.current.onopen = () => {
      console.log('WebSocket connected');
      toast.success('Connected to Mission Control');
    };

    wsRef.current.onmessage = (event) => {
      const data = JSON.parse(event.data);
      handleWebSocketMessage(data);
    };

    wsRef.current.onerror = (error) => {
      console.error('WebSocket error:', error);
      toast.error('Connection error');
    };

    wsRef.current.onclose = () => {
      console.log('WebSocket disconnected');
      toast.error('Disconnected from Mission Control');
      // Reconnect after 3 seconds
      setTimeout(connectWebSocket, 3000);
    };
  };

  const handleWebSocketMessage = (data) => {
    switch (data.type) {
      case 'status_update':
        setMode(data.data.mode);
        setProcesses(data.data.processes);
        setAlerts(data.data.alerts || []);
        break;
      
      case 'console_output':
        setConsoleLogs(prev => [...prev.slice(-500), data.data]);
        break;
      
      case 'alert':
        setAlerts(prev => [data.data, ...prev.slice(0, 9)]);
        toast(data.data.message, {
          icon: data.data.severity === 'critical' ? '🚨' : '⚠️',
          duration: 5000
        });
        // Play sound for critical alerts
        if (data.data.severity === 'critical') {
          try {
            // Simple beep sound using Audio API
            const audio = new Audio('data:audio/wav;base64,UklGRnoGAABXQVZFZm10IBAAAAABAAEARKwAAIhYAQACABAAZGF0YQoGAACBhYqFbF1fdJivrJBhNjVgodDbq2EcBj+a2/LDciUFLIHO8tiJNwgZaLvt559NEAxQp+PwtmMcBjiR1/LMeSwFJHfH8N2QQAoUXrTp66hVFApGn+DyvmwhBSt9y+/XkEEKFl+06+qoVxQKRJ7h8b9uIAYnfMvv15BACBZitOrqq1YUCEKZ4vG/bSAHKHzL79aQQQgWYrTq6qpXFApCmuLxv2wgByd8y+/WhDMIHGW36OeeWBYLTKLf8bllHQUvgdDy0YY2CBdnv+jkoVYTCEui3/G5ZB0GL4HQ8tGGNwgZZ7/o5KFWEwhLot/xuWQdBi+B0PLRhjcKGWW36OegWBkNTKLf8bllHQUvgdDy0YY2CBdnv+jkoVYTCEui3/G5ZB0GL4HQ8tGGNwgZZ7/o5KFWEwhLot/xuWQdBi+B0PLRhjcKGWS36OaiWBwMTKLf8bllHQUvgdDy0YY2CBdnv+jkoVYTCEui3/G5ZB0GL4HQ8tGGNwgZZ7/o5KFWEwhLot/xuWQdBi+B0PLRhjcKGWS36OajWBwMTKPf8bllHQUvgdDy0YY2CBdnv+jkoVYTCEui3/G5ZB0GL4HQ8tGGNwgZZ7/o5KFWEwhLot/xuWQdBi+B0PLRhjcKGWS36OajWBwMTKPf8bllHQUvgdDy0YY2CBdnv+jkoVYTCEui3/G5ZB0GL4HQ8tGGNwgZZ7/o5KFWEwhLot/xuWQdBi+B0PLRhjcKGWS36OajWBwMTKPf8bllHQUvgdDy0YY2CBdnv+jkoVYTCEui3/G5ZB0GL4HQ8tGGNwgZZ7/o5KFWEwhLot/xuWQdBi+B0PLRhjcKGWS36OajWBwMTKPf8bllHQUvgdDy0YY2CBdnv+jkoVYTCEui3/G5ZB0GL4HQ8tGGNwgZZ7/o5KFWEwhLot/xuWQdBi+B0PLRhjcKGWS36OajWBwMTKPf8bllHQUvgdDy0YY2CBdnv+jkoVYTCEui3/G5ZB0GL4HQ8tGGNwgZZ7/o5KFWEwhLot/xuWQdBi+B0PLRhjcKGWS36OajWBwMTKPf8bllHQUvgdDy0YY2CBdnv+jkoVYTCEui3/G5ZB0GL4HQ8tGGNwgZZ7/o5KFWEwhLot/xuWQdBi+B0PLRhjcKGWS36OajWBwMTKPf8bllHQUvgdDy0YY2CBdnv+jkoVYTCEui3/G5ZB0GL4HQ8tGGNwgZZ7/o5KFWEwhLot/xuWQdBi+B0PLRhjcKGWS36OajWBwMTKPf8bllHQUvgdDy0YY2CBdnv+jkoVYTCEui3/G5ZB0GL4HQ8tGGNwgZZ7/o5KFWEwhLot/xuWQdBi+B0PLRhjcKGWS36OajWBwMTKPf8bllHQUvgdDy0YY2CBdnv+jkoVYTCEui3/G5ZB0GL4HQ8tGGNwgZZ7/o5KFWEwhLot/xuWQdBi+B0PLRhjcKGWS36OajWBwMTKPf8bllHgg=');
            audio.play().catch(e => console.log('Could not play alert sound:', e));
          } catch (e) {
            console.log('Alert sound failed:', e);
          }
        }
        break;
      
      default:
        console.log('Unknown message type:', data.type);
    }
  };

  const fetchStatus = async () => {
    try {
      const response = await axios.get(`${API_BASE}/api/status`);
      setMode(response.data.mode);
      setProcesses(response.data.processes);
      setAlerts(response.data.alerts || []);
    } catch (error) {
      console.error('Failed to fetch status:', error);
    }
  };

  const launchMode = async () => {
    setLoading(true);
    try {
      const response = await axios.post(`${API_BASE}/api/launch`, {
        mode: selectedMode
      });
      
      if (response.data.status === 'success') {
        toast.success(`Launched in ${selectedMode} mode`);
        
        // Check if GUI URL is available
        if (response.data.gui_url) {
          setTimeout(() => {
            toast(
              <div>
                <strong>Trading GUI Ready!</strong>
                <button 
                  onClick={() => window.open(response.data.gui_url, '_blank')}
                  style={{
                    display: 'block',
                    marginTop: '8px',
                    padding: '4px 8px',
                    background: '#059669',
                    color: 'white',
                    border: 'none',
                    borderRadius: '4px',
                    cursor: 'pointer'
                  }}
                >
                  Open Trading GUI
                </button>
              </div>,
              { duration: 10000 }
            );
          }, 2000);
        }
      }
    } catch (error) {
      const errorData = error.response?.data?.detail;
      if (errorData?.analysis) {
        // Show detailed error analysis
        toast.error(
          <div>
            <strong>{errorData.analysis.summary}</strong>
            <ul className="mt-2 text-sm">
              {errorData.analysis.solutions.slice(0, 2).map((solution, idx) => (
                <li key={idx}>• {solution}</li>
              ))}
            </ul>
          </div>,
          { duration: 10000 }
        );
      } else {
        toast.error(`Launch failed: ${error.message}`);
      }
    } finally {
      setLoading(false);
    }
  };

  const processAction = async (processName, action) => {
    try {
      const response = await axios.post(`${API_BASE}/api/process/${action}`, {
        process_name: processName,
        action: action
      });
      
      if (response.data.analysis && action === 'restart') {
        // Show restart analysis
        toast(
          <div>
            <strong>Restart Analysis</strong>
            <p className="text-sm mt-1">{response.data.analysis.summary}</p>
          </div>,
          { duration: 5000 }
        );
      } else {
        toast.success(`${processName} ${action} successful`);
      }
    } catch (error) {
      toast.error(`Failed to ${action} ${processName}`);
    }
  };

  const shutdownAll = async () => {
    if (!window.confirm('Are you sure you want to shutdown all processes?')) {
      return;
    }
    
    setLoading(true);
    try {
      await axios.post(`${API_BASE}/api/shutdown`);
      toast.success('All processes stopped');
    } catch (error) {
      toast.error('Shutdown failed');
    } finally {
      setLoading(false);
    }
  };

  const getProcessIcon = (processName) => {
    const Icon = PROCESS_ICONS[processName] || Activity;
    return <Icon size={16} />;
  };

  const getProcessStateColor = (state) => {
    switch (state) {
      case 'running': return 'text-green-500';
      case 'stopped': return 'text-gray-500';
      case 'starting': return 'text-yellow-500';
      case 'failed': return 'text-red-500';
      default: return 'text-gray-500';
    }
  };

  const filteredLogs = selectedProcess === 'ALL' 
    ? consoleLogs 
    : consoleLogs.filter(log => log.process === selectedProcess);

  // Calculate process summary
  const processCount = Object.keys(processes).length;
  const runningCount = Object.values(processes).filter(p => p.state === 'running').length;
  const failedCount = Object.values(processes).filter(p => p.state === 'failed').length;

  return (
    <div className="app">
      <Toaster position="top-right" />
      
      <header className="app-header">
        <h1>🚀 TESTRADE Mission Control</h1>
        <div className="header-status">
          <span className={`mode-badge ${mode === 'STOPPED' ? 'mode-stopped' : 'mode-active'}`}>
            {MODE_INFO[mode]?.emoji} {mode}
          </span>
          {mode !== 'STOPPED' && (
            <button onClick={shutdownAll} className="btn btn-danger" disabled={loading}>
              <Square size={16} /> Shutdown All
            </button>
          )}
        </div>
      </header>

      {/* Process Status Bar */}
      {mode !== 'STOPPED' && (
        <div className="status-bar">
          <div className="status-item">
            <Activity size={16} />
            <span>Processes: {processCount}</span>
          </div>
          <div className="status-item status-running">
            <CheckCircle size={16} />
            <span>Running: {runningCount}</span>
          </div>
          {failedCount > 0 && (
            <div className="status-item status-failed">
              <XCircle size={16} />
              <span>Failed: {failedCount}</span>
            </div>
          )}
          <div className="status-summary">
            {Object.entries(processes).map(([name, info]) => (
              <div 
                key={name} 
                className={`status-dot ${info.state}`} 
                title={`${info.display_name}: ${info.state}`}
              />
            ))}
          </div>
        </div>
      )}

      <div className="app-content">
        <div className="control-panel">
          {/* Mode Selection */}
          <div className="panel">
            <h2>Launch Mode</h2>
            <div className="mode-selector">
              {Object.entries(MODE_INFO).map(([modeName, info]) => (
                <label key={modeName} className="mode-option">
                  <input
                    type="radio"
                    name="mode"
                    value={modeName}
                    checked={selectedMode === modeName}
                    onChange={(e) => setSelectedMode(e.target.value)}
                    disabled={mode !== 'STOPPED'}
                  />
                  <span className={`mode-label mode-${info.color}`}>
                    {info.emoji} {modeName}
                  </span>
                </label>
              ))}
            </div>
            <button 
              onClick={launchMode} 
              className="btn btn-primary btn-large"
              disabled={mode !== 'STOPPED' || loading}
            >
              <Play size={20} /> Launch {selectedMode}
            </button>
          </div>

          {/* Process Status */}
          <div className="panel">
            <h2>Process Status</h2>
            <div className="process-grid">
              {Object.entries(processes).map(([name, info]) => (
                <div key={name} className="process-card">
                  <div className="process-header">
                    <span className="process-name">
                      {getProcessIcon(name)} {info.display_name}
                    </span>
                    <span className={`process-state ${getProcessStateColor(info.state)}`}>
                      {info.state === 'running' ? <CheckCircle size={16} /> : 
                       info.state === 'failed' ? <XCircle size={16} /> :
                       info.state === 'starting' ? <Clock size={16} /> :
                       <Circle size={16} />}
                      {info.state}
                    </span>
                  </div>
                  
                  {info.state === 'running' && (
                    <div className="process-stats">
                      <span><Cpu size={14} /> {info.cpu_percent?.toFixed(1)}%</span>
                      <span><HardDrive size={14} /> {info.memory_mb?.toFixed(0)}MB</span>
                      {info.uptime && (
                        <span><Clock size={14} /> {Math.floor(info.uptime / 60)}m</span>
                      )}
                    </div>
                  )}
                  
                  <div className="process-actions">
                    {info.state === 'stopped' ? (
                      <button 
                        onClick={() => processAction(name, 'start')}
                        className="btn btn-sm btn-success"
                      >
                        <Play size={14} /> Start
                      </button>
                    ) : info.state === 'running' ? (
                      <>
                        <button 
                          onClick={() => processAction(name, 'restart')}
                          className="btn btn-sm btn-warning"
                        >
                          <RotateCcw size={14} /> Restart
                        </button>
                        <button 
                          onClick={() => processAction(name, 'stop')}
                          className="btn btn-sm btn-danger"
                        >
                          <Square size={14} /> Stop
                        </button>
                      </>
                    ) : null}
                  </div>
                  
                  {info.last_error && (
                    <div className="process-error">
                      <AlertCircle size={14} /> {info.last_error}
                    </div>
                  )}
                </div>
              ))}
            </div>
          </div>
        </div>

        {/* Console Output */}
        <div className={`console-panel ${consoleMinimized ? 'minimized' : ''}`}>
          <div className="console-header">
            <h2><Terminal size={20} /> Console Output</h2>
            <div className="console-controls">
              <select 
                value={selectedProcess} 
                onChange={(e) => setSelectedProcess(e.target.value)}
                className="process-filter"
              >
                <option value="ALL">All Processes</option>
                {Object.keys(processes).map(name => (
                  <option key={name} value={name}>{name}</option>
                ))}
              </select>
              <button 
                onClick={() => setConsoleMinimized(!consoleMinimized)}
                className="btn btn-sm minimize-btn"
                title={consoleMinimized ? "Expand console" : "Minimize console"}
              >
                {consoleMinimized ? <Maximize2 size={16} /> : <Minimize2 size={16} />}
              </button>
            </div>
          </div>
          
          {!consoleMinimized && (
            <div className="console-output">
              {filteredLogs.map((log, idx) => (
                <div 
                  key={idx} 
                  className={`console-line level-${log.level?.toLowerCase() || 'info'}`}
                >
                  <span className="console-time">{new Date(log.timestamp).toLocaleTimeString()}</span>
                  <span className="console-process">[{log.process}]</span>
                  <span className="console-message">{log.message}</span>
                </div>
              ))}
              <div ref={consoleEndRef} />
            </div>
          )}
        </div>

        {/* Alerts */}
        {alerts.length > 0 && (
          <div className="alerts-panel">
            <div className="alerts-header">
              <h3><AlertCircle size={16} /> Alerts ({alerts.length})</h3>
              <button 
                onClick={() => setAlerts([])} 
                className="btn btn-sm"
                title="Clear all alerts"
              >
                <XCircle size={14} />
              </button>
            </div>
            {alerts.slice(0, 5).map((alert, idx) => (
              <div key={idx} className={`alert alert-${alert.severity}`}>
                <span className="alert-time">{new Date(alert.timestamp).toLocaleTimeString()}</span>
                <span className="alert-message">{alert.message}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

// Fix for missing Circle icon
const Circle = ({ size }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
    <circle cx="12" cy="12" r="10"></circle>
  </svg>
);

export default App;