// websocket/websocket-manager.js - WebSocket connection management
export class WebSocketManager {
    constructor(config, state, messageRouter, statusManager = null) {
        this.config = config;
        this.state = state;
        this.messageRouter = messageRouter;
        this.statusManager = statusManager;
        this.ws = null;
        this.reconnectAttempts = 0;
        this.reconnectTimeout = null;
        this.lastMessageTime = 0;
        this.latencySamples = [];
        this.maxLatencySamples = 10;
    }

    // Method to set status manager after initialization
    setStatusManager(statusManager) {
        this.statusManager = statusManager;
    }

    connect() {
        if (this.ws && (this.ws.readyState === WebSocket.OPEN || this.ws.readyState === WebSocket.CONNECTING)) {
            console.log('WebSocket connection already active');
            return;
        }

        try {
            this.ws = new WebSocket(this.config.wsUrl);
            console.log(`Connecting to WebSocket: ${this.config.wsUrl}`);

            this.ws.onopen = this.handleOpen.bind(this);
            this.ws.onmessage = this.handleMessage.bind(this);
            this.ws.onclose = this.handleClose.bind(this);
            this.ws.onerror = this.handleError.bind(this);

        } catch (error) {
            console.error('Failed to create WebSocket:', error);
            this.scheduleReconnect();
        }
    }

    handleOpen() {
        console.log('WebSocket connected successfully');
        this.state.setConnected(true);
        this.state.setWebSocket(this.ws);
        this.reconnectAttempts = 0;
        
        // Update API status through StatusManager
        if (this.statusManager) {
            this.statusManager.setAPIStatus(true);
        }
        
        // Emit connection event
        this.emitEvent('connected');
    }

    handleMessage(event) {
        try {
            const data = JSON.parse(event.data);
            
            // Update last update time and track latency
            this.updateLastUpdateTime();
            this.trackLatency();
            
            // Special handling for command responses
            if (data.type === 'command_response' && data.command_id) {
                // Forward to API client for pending command resolution
                if (window.TESTRADE && window.TESTRADE.api && window.TESTRADE.api.handleCommandResponse) {
                    window.TESTRADE.api.handleCommandResponse(data);
                }
                return;
            }
            
            // Route message
            const handled = this.messageRouter.route(data);
            
            if (!handled && this.config.debugMode) {
                console.log('Unhandled WebSocket message:', data.type);
            }
            
        } catch (error) {
            console.error('WebSocket message parse error:', error);
        }
    }

    handleClose(event) {
        console.log('WebSocket disconnected:', event.code, event.reason);
        this.state.setConnected(false);
        this.state.setWebSocket(null);
        this.ws = null;
        
        // Update API status through StatusManager
        if (this.statusManager) {
            this.statusManager.setAPIStatus(false);
        }
        
        // Emit disconnection event
        this.emitEvent('disconnected', { code: event.code, reason: event.reason });
        
        // Schedule reconnection
        if (!this.state.isReconnecting()) {
            this.scheduleReconnect();
        }
    }

    handleError(error) {
        console.error('WebSocket error:', error);
        this.updateAPIStatus('error');
        this.emitEvent('error', error);
    }

    scheduleReconnect() {
        if (this.reconnectAttempts >= this.config.maxReconnectAttempts) {
            console.error('Max reconnection attempts reached');
            this.emitEvent('maxReconnectAttemptsReached');
            return;
        }

        this.state.setReconnecting(true);
        this.reconnectAttempts++;
        
        // Update status to show reconnecting
        if (this.statusManager) {
            this.statusManager.onReconnectAttempt();
        }
        
        console.log(`Scheduling reconnection attempt ${this.reconnectAttempts}/${this.config.maxReconnectAttempts}`);
        
        this.reconnectTimeout = setTimeout(() => {
            this.state.setReconnecting(false);
            this.connect();
        }, this.config.reconnectDelayMs);
    }

    disconnect() {
        if (this.reconnectTimeout) {
            clearTimeout(this.reconnectTimeout);
            this.reconnectTimeout = null;
        }

        if (this.ws) {
            this.ws.close();
            this.ws = null;
        }

        this.state.setConnected(false);
        this.state.setReconnecting(false);
        this.state.setWebSocket(null);
    }

    send(data) {
        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
            try {
                this.ws.send(JSON.stringify(data));
                return true;
            } catch (error) {
                console.error('Failed to send WebSocket message:', error);
                return false;
            }
        }
        return false;
    }

    isConnected() {
        return this.ws && this.ws.readyState === WebSocket.OPEN;
    }

    getState() {
        return {
            connected: this.state.isConnected(),
            reconnecting: this.state.isReconnecting(),
            reconnectAttempts: this.reconnectAttempts,
            readyState: this.ws ? this.ws.readyState : WebSocket.CLOSED
        };
    }

    /**
     * Send command through WebSocket
     * Used by RedisStreamClient for GUI commands
     */
    sendCommand(command) {
        if (!this.isConnected()) {
            throw new Error('WebSocket not connected');
        }

        try {
            const message = {
                type: 'command',
                ...command
            };
            
            this.ws.send(JSON.stringify(message));
            return true;
        } catch (error) {
            console.error('Failed to send WebSocket command:', error);
            return false;
        }
    }

    updateAPIStatus(status) {
        // This would trigger status pill updates
        this.emitEvent('statusUpdate', { component: 'api', status });
    }

    updateLastUpdateTime() {
        const lastUpdateEl = document.getElementById('lastUpdate');
        if (lastUpdateEl) {
            lastUpdateEl.textContent = new Date().toLocaleTimeString();
        }
    }

    trackLatency() {
        const now = performance.now();
        
        if (this.lastMessageTime > 0) {
            const timeSinceLastMessage = now - this.lastMessageTime;
            
            // Only track reasonable latencies (< 5 seconds)
            if (timeSinceLastMessage < 5000) {
                this.latencySamples.push(timeSinceLastMessage);
                
                // Keep only the most recent samples
                if (this.latencySamples.length > this.maxLatencySamples) {
                    this.latencySamples.shift();
                }
                
                // Calculate average and update status
                const avgLatency = this.latencySamples.reduce((a, b) => a + b, 0) / this.latencySamples.length;
                
                if (this.statusManager) {
                    this.statusManager.updateLatency(avgLatency);
                }
            }
        }
        
        this.lastMessageTime = now;
    }

    emitEvent(eventName, data = {}) {
        // Emit custom events for other components to listen to
        const event = new CustomEvent(`websocket:${eventName}`, {
            detail: { ...data, timestamp: Date.now() }
        });
        document.dispatchEvent(event);
    }
}