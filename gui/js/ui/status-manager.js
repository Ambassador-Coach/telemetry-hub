// ui/status-manager.js - Status pills and health monitoring
export class StatusManager {
    constructor(dom, state) {
        this.dom = dom;
        this.state = state;
        this.statusPills = new Map();
        this.coreStatusManager = null;
        this.apiStatusManager = null;
    }

    initialize() {
        this.createStatusPills();
        this.initializeCoreStatusManager();
        this.initializeAPIStatusManager();
        this.initializeDefaultStates();
        console.log('StatusManager initialized');
    }

    initializeDefaultStates() {
        // Set initial states for all pills
        this.updateStatusPill('api', 'error', 'API: Disconnected');
        this.updateStatusPill('core', 'error', 'Core: Unknown');
        this.updateStatusPill('redis', 'error', 'Redis: Unknown');
        this.updateStatusPill('babysitter', 'error', 'Babysitter: Unknown');
        this.updateStatusPill('zmq', 'error', 'ZMQ: Unknown');
        this.updateLatency(null);
    }

    createStatusPills() {
        const pillTypes = ['core', 'api', 'redis', 'babysitter', 'zmq'];
        
        pillTypes.forEach(type => {
            this.statusPills.set(type, new StatusPill(type, type.toUpperCase()));
        });
    }

    initializeCoreStatusManager() {
        this.coreStatusManager = new CoreStatusManager(this.state);
        this.coreStatusManager.start();
    }

    initializeAPIStatusManager() {
        this.apiStatusManager = new APIStatusManager(this.state);
    }

    updateHealthStatus(data) {
        if (!data.component) return;

        const component = data.component.toLowerCase();
        const status = this.mapHealthStatus(data.status);

        if (component === 'core' || component === 'applicationcore') {
            if (status === 'healthy') {
                this.coreStatusManager.updateHealth();
                this.updateStatusPill('core', status, `Core: ${data.status}`);
            } else {
                this.updateStatusPill('core', status, `Core: ${data.status}`);
            }
        } else if (this.statusPills.has(component)) {
            this.updateStatusPill(component, status, `${component}: ${data.status}`);
        }
    }

    updateCoreStatus(data) {
        if (data.status_event) {
            const event = data.status_event;
            const status = this.mapCoreEventToStatus(event);
            
            if (status === 'healthy') {
                this.coreStatusManager.updateHealth();
                this.updateStatusPill('core', status, `Core: ${event}`);
            } else {
                this.updateStatusPill('core', status, `Core: ${event}`);
            }
        }
    }

    updateStatusPill(type, status, tooltip) {
        const pill = this.statusPills.get(type);
        if (pill) {
            pill.update(status, tooltip);
        }
    }

    mapHealthStatus(status) {
        switch (status?.toLowerCase()) {
            case 'healthy':
            case 'running':
                return 'healthy';
            case 'degraded':
                return 'warning';
            default:
                return 'error';
        }
    }

    mapCoreEventToStatus(event) {
        if (event.includes('RUNNING') || event.includes('STARTED') || event === 'OCR_ACTIVE') {
            return 'healthy';
        } else if (event.includes('STOPPING') || event.includes('DEGRADED')) {
            return 'warning';
        } else {
            return 'error';
        }
    }

    setAPIStatus(connected) {
        this.apiStatusManager.setConnected(connected);
        // Update the API status pill
        const status = connected ? 'healthy' : 'error';
        const tooltip = connected ? 'API: Connected' : 'API: Disconnected';
        this.updateStatusPill('api', status, tooltip);
    }

    onReconnectAttempt() {
        this.apiStatusManager.onReconnectAttempt();
        this.updateStatusPill('api', 'warning', 'API: Reconnecting...');
    }

    // Method to update Redis status
    updateRedisStatus(connected) {
        const status = connected ? 'healthy' : 'error';
        const tooltip = connected ? 'Redis: Connected' : 'Redis: Disconnected';
        this.updateStatusPill('redis', status, tooltip);
    }

    // Method to update Babysitter status
    updateBabysitterStatus(status, message = '') {
        const mappedStatus = this.mapHealthStatus(status);
        const tooltip = `Babysitter: ${message || status}`;
        this.updateStatusPill('babysitter', mappedStatus, tooltip);
    }

    // Method to update ZMQ status
    updateZMQStatus(connected) {
        const status = connected ? 'healthy' : 'error';
        const tooltip = connected ? 'ZMQ: Connected' : 'ZMQ: Disconnected';
        this.updateStatusPill('zmq', status, tooltip);
    }

    // Method to update latency display
    updateLatency(latencyMs) {
        const latencyDisplay = document.getElementById('latencyDisplay');
        if (latencyDisplay) {
            if (latencyMs !== null && latencyMs !== undefined) {
                latencyDisplay.textContent = `${Math.round(latencyMs)}ms`;
                
                // Color code based on latency
                if (latencyMs < 100) {
                    latencyDisplay.style.color = 'var(--accent-primary)';
                } else if (latencyMs < 500) {
                    latencyDisplay.style.color = 'var(--accent-warning)';
                } else {
                    latencyDisplay.style.color = 'var(--accent-danger)';
                }
            } else {
                latencyDisplay.textContent = '--ms';
                latencyDisplay.style.color = 'var(--text-muted)';
            }
        }
    }
}

class StatusPill {
    constructor(pillId, name) {
        this.pillId = pillId;
        this.name = name;
        this.element = document.getElementById(`${pillId}StatusDot`);
        this.lastStatus = null;
        this.lastUpdate = 0;
        this.updateTimer = null;
    }

    update(status, tooltip = '', immediate = false) {
        if (this.updateTimer) {
            clearTimeout(this.updateTimer);
            this.updateTimer = null;
        }

        const now = Date.now();
        if (this.lastStatus === status && (now - this.lastUpdate) < 1000 && !immediate) {
            return;
        }

        const delay = immediate ? 0 : 100;

        this.updateTimer = setTimeout(() => {
            this._applyUpdate(status, tooltip);
            this.lastStatus = status;
            this.lastUpdate = now;
            this.updateTimer = null;
        }, delay);
    }

    _applyUpdate(status, tooltip) {
        if (!this.element) return;

        const color = this._getStatusColor(status);
        this.element.style.background = color;

        if (tooltip && this.element.parentElement) {
            this.element.parentElement.title = tooltip;
        }
    }

    _getStatusColor(status) {
        switch (status) {
            case 'healthy': return 'var(--accent-primary)';
            case 'warning': return 'var(--accent-warning)';
            case 'error': return 'var(--accent-danger)';
            default: return 'var(--accent-danger)';
        }
    }
}

class CoreStatusManager {
    constructor(state) {
        this.state = state;
        this.lastHealthUpdate = Date.now(); // Initialize to current time
        this.timeoutMs = 15000;
        this.checkInterval = null;
    }

    start() {
        this.checkInterval = setInterval(() => {
            this.checkStatus();
        }, 10000);
        setTimeout(() => this.checkStatus(), 1000);
    }

    stop() {
        if (this.checkInterval) {
            clearInterval(this.checkInterval);
            this.checkInterval = null;
        }
    }

    updateHealth() {
        this.lastHealthUpdate = Date.now();
        this.state.setLastHealthUpdate(this.lastHealthUpdate);
    }

    checkStatus() {
        const now = Date.now();
        const timeSinceLastUpdate = now - this.lastHealthUpdate;

        if (timeSinceLastUpdate > this.timeoutMs) {
            // Core is considered unhealthy
            console.warn(`Core unhealthy: ${Math.round(timeSinceLastUpdate / 1000)}s since last update`);
        }
    }
}

class APIStatusManager {
    constructor(state) {
        this.state = state;
        this.connected = false;
        this.reconnectAttempts = 0;
    }

    setConnected(connected) {
        this.connected = connected;
        this.state.setConnected(connected);
        
        if (connected) {
            this.reconnectAttempts = 0;
        }
    }

    onReconnectAttempt() {
        this.reconnectAttempts++;
    }
}