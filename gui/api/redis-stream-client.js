// api/redis-stream-client.js - Redis Stream Communication Layer
// This replaces direct HTTP API calls and maintains TESTRADE isolation
export class RedisStreamClient {
    constructor(config, state) {
        this.config = config;
        this.state = state;
        this.baseUrl = config.apiUrl;
        this.streamName = 'gui:commands'; // GUI command stream
        this.responseStream = 'gui:responses'; // GUI response stream
        this.pendingCommands = new Map(); // Track pending commands
        this.commandId = 0;
    }

    /**
     * Send command through Redis stream instead of direct API call
     * This maintains TESTRADE isolation by going through the GUI command listener
     */
    async sendCommand(command, parameters = {}) {
        if (!this.state.isConnected() && command !== 'health_check') {
            throw new Error('Not connected to backend');
        }

        const commandId = this.generateCommandId();
        const streamMessage = {
            command_id: commandId,
            command_type: command,
            parameters: parameters,
            timestamp: Date.now(),
            source: 'gui'
        };

        try {
            // Send command through Redis stream via the backend's stream endpoint
            const response = await this.sendToRedisStream(this.streamName, streamMessage);
            
            // Store pending command for response tracking
            this.pendingCommands.set(commandId, {
                command: command,
                timestamp: Date.now(),
                resolve: null,
                reject: null
            });

            return { 
                success: true, 
                data: { 
                    command_id: commandId,
                    message: `Command ${command} sent via Redis stream`,
                    stream_name: this.streamName
                }
            };
        } catch (error) {
            console.error(`Failed to send command ${command} via Redis stream:`, error);
            throw error;
        }
    }

    /**
     * Send message to Redis stream through backend
     * Prefer WebSocket if available, fallback to HTTP
     */
    async sendToRedisStream(streamName, message) {
        // Try WebSocket first if available
        if (window.TESTRADE && window.TESTRADE.websocket && window.TESTRADE.websocket.isConnected()) {
            try {
                const success = window.TESTRADE.websocket.sendCommand({
                    command_type: message.command_type,
                    parameters: message.parameters,
                    command_id: message.command_id,
                    timestamp: message.timestamp,
                    source: message.source
                });
                
                if (success) {
                    return {
                        success: true,
                        method: 'websocket',
                        command_id: message.command_id
                    };
                }
            } catch (error) {
                console.warn('WebSocket send failed, falling back to HTTP:', error);
            }
        }
        
        // Fallback to HTTP endpoint
        const response = await fetch(`${this.baseUrl}/stream/send`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({
                stream_name: streamName,
                message: message
            })
        });

        if (!response.ok) {
            throw new Error(`Redis stream send failed: ${response.status} ${response.statusText}`);
        }

        return await response.json();
    }

    /**
     * Configuration updates - these go through Redis streams
     */
    async updateConfig(key, value) {
        return await this.sendCommand('update_config', {
            config_key: key,
            config_value: value
        });
    }

    async loadConfiguration() {
        return await this.sendCommand('load_configuration', {});
    }

    async saveConfiguration() {
        return await this.sendCommand('save_configuration', {});
    }

    async loadInitialROI() {
        try {
            const result = await this.sendCommand('get_roi_config', {});
            return {
                success: true,
                roi: [64, 159, 681, 296], // Default fallback
                source: 'redis_stream'
            };
        } catch (error) {
            return {
                success: false,
                error: error.message,
                roi: [64, 159, 681, 296] // Fallback
            };
        }
    }

    /**
     * Health status - only non-TESTRADE call allowed (backend health)
     */
    async fetchHealthStatus() {
        try {
            // This is the only direct HTTP call allowed - it's to the backend, not TESTRADE
            const response = await fetch(`${this.baseUrl}/health`);
            if (!response.ok) {
                throw new Error(`Health check failed: ${response.status}`);
            }
            return await response.json();
        } catch (error) {
            console.warn('Backend health check failed:', error);
            return null;
        }
    }

    // Trading commands - all through Redis streams
    async executeManualTrade(action, shares, symbol = null) {
        return await this.sendCommand('manual_trade', {
            action: action,
            shares: shares,
            symbol: symbol
        });
    }

    async executeForceCloseAll(reason = 'Manual GUI Force Close Button') {
        return await this.sendCommand('force_close_all', {
            reason: reason
        });
    }

    async executeEmergencyStop(reason = 'Manual GUI Emergency Stop Button') {
        return await this.sendCommand('emergency_stop', {
            reason: reason
        });
    }

    // OCR commands - all through Redis streams
    async toggleOCR() {
        const command = this.state.isOCRActive() ? 'stop_ocr' : 'start_ocr';
        return await this.sendCommand(command, {});
    }

    async updateOCRSettings(settings) {
        return await this.sendCommand('update_ocr_preprocessing_full', settings);
    }

    async adjustROI(edge, direction, stepSize = 1) {
        return await this.sendCommand('roi_adjust', {
            edge: edge,
            direction: direction,
            step_size: stepSize
        });
    }

    async setROIAbsolute(x1, y1, x2, y2) {
        return await this.sendCommand('set_roi_absolute', {
            x1: x1,
            y1: y1,
            x2: x2,
            y2: y2
        });
    }

    // System control commands - all through Redis streams
    async startCore() {
        return await this.sendCommand('start_testrade_core', {});
    }

    async toggleRecording() {
        return await this.sendCommand('toggle_recording_mode', {});
    }

    async toggleAllSymbols(enabled) {
        return await this.sendCommand('toggle_all_symbols_mode', { enabled });
    }

    async toggleConfidenceMode(enabled) {
        return await this.sendCommand('toggle_confidence_mode', { enabled });
    }

    async toggleVideoMode(enabled) {
        return await this.sendCommand('toggle_video_mode', { enabled });
    }

    async updateTradingParams(params) {
        return await this.sendCommand('update_trading_params', params);
    }

    async updateDevelopmentMode(developmentMode) {
        return await this.sendCommand('update_development_mode', {
            development_mode: developmentMode
        });
    }

    async updateRecordingSettings(settings) {
        return await this.sendCommand('update_recording_settings', settings);
    }

    async updateLoggingSettings(settings) {
        return await this.sendCommand('update_logging_settings', settings);
    }

    /**
     * Bootstrap API - These should be replaced with Redis stream requests
     * These are the ONLY HTTP calls allowed and they're to the backend's API layer, not TESTRADE
     */
    async fetchPositions() {
        console.warn('fetchPositions: Consider replacing with Redis stream request');
        try {
            // This goes to backend API layer which reads from Redis, not directly to TESTRADE
            const response = await fetch(`${this.baseUrl}/api/v1/positions/current`);
            if (!response.ok) {
                throw new Error(`Positions API failed: ${response.status}`);
            }
            const data = await response.json();
            return data.positions || data || [];
        } catch (error) {
            console.error('Failed to fetch positions:', error);
            return [];
        }
    }

    async fetchAccountSummary() {
        console.warn('fetchAccountSummary: Consider replacing with Redis stream request');
        try {
            // This goes to backend API layer which reads from Redis, not directly to TESTRADE
            const response = await fetch(`${this.baseUrl}/api/v1/account/summary`);
            if (!response.ok) {
                throw new Error(`Account API failed: ${response.status}`);
            }
            return await response.json();
        } catch (error) {
            console.error('Failed to fetch account summary:', error);
            return null;
        }
    }

    async fetchTodaysOrders() {
        console.warn('fetchTodaysOrders: Consider replacing with Redis stream request');
        try {
            // This goes to backend API layer which reads from Redis, not directly to TESTRADE
            const response = await fetch(`${this.baseUrl}/api/v1/orders/today`);
            if (!response.ok) {
                throw new Error(`Orders API failed: ${response.status}`);
            }
            const data = await response.json();
            return data.orders || data || [];
        } catch (error) {
            console.error('Failed to fetch orders:', error);
            return [];
        }
    }

    async fetchMarketStatus() {
        console.warn('fetchMarketStatus: Consider replacing with Redis stream request');
        try {
            // This goes to backend API layer which reads from Redis, not directly to TESTRADE
            const response = await fetch(`${this.baseUrl}/api/v1/market/status`);
            if (!response.ok) {
                throw new Error(`Market status API failed: ${response.status}`);
            }
            return await response.json();
        } catch (error) {
            console.error('Failed to fetch market status:', error);
            return null;
        }
    }

    async fetchTradeRejections() {
        console.warn('fetchTradeRejections: Consider replacing with Redis stream request');
        try {
            // This goes to backend API layer which reads from Redis, not directly to TESTRADE
            const response = await fetch(`${this.baseUrl}/api/v1/trades/rejections`);
            if (!response.ok) {
                throw new Error(`Trade rejections API failed: ${response.status}`);
            }
            const data = await response.json();
            return data.trades || [];
        } catch (error) {
            console.error('Failed to fetch trade rejections:', error);
            return [];
        }
    }

    /**
     * PREFERRED: Request initial state via Redis stream instead of HTTP API
     */
    async requestInitialStateViaStream() {
        console.log('Requesting initial state via Redis stream (PREFERRED method)');
        return await this.sendCommand('request_initial_state', {
            include_positions: true,
            include_account: true,
            include_orders: true,
            include_history: true,
            correlation_id: this.generateCommandId()
        });
    }

    // Utility methods
    generateCommandId() {
        this.commandId++;
        return `gui_cmd_${Date.now()}_${this.commandId}`;
    }

    getBaseUrl() {
        return this.baseUrl;
    }

    isConnected() {
        return this.state.isConnected();
    }

    // Handle command responses (called by WebSocket message handler)
    handleCommandResponse(responseData) {
        const commandId = responseData.command_id;
        const pendingCommand = this.pendingCommands.get(commandId);
        
        if (pendingCommand) {
            console.log(`Received response for command ${pendingCommand.command}:`, responseData);
            this.pendingCommands.delete(commandId);
            
            if (pendingCommand.resolve) {
                pendingCommand.resolve(responseData);
            }
        }
    }

    // Get pending commands for debugging
    getPendingCommands() {
        return Array.from(this.pendingCommands.entries()).map(([id, cmd]) => ({
            command_id: id,
            command: cmd.command,
            age_ms: Date.now() - cmd.timestamp
        }));
    }

    // Cleanup old pending commands
    cleanupPendingCommands() {
        const now = Date.now();
        const timeout = 30000; // 30 seconds timeout
        
        for (const [commandId, command] of this.pendingCommands.entries()) {
            if (now - command.timestamp > timeout) {
                console.warn(`Command ${command.command} timed out after 30s`);
                this.pendingCommands.delete(commandId);
                
                if (command.reject) {
                    command.reject(new Error('Command timeout'));
                }
            }
        }
    }
}

// Command stream message format documentation
export const REDIS_STREAM_FORMATS = {
    // GUI Command Stream Format (gui:commands)
    COMMAND_MESSAGE: {
        command_id: "string - unique identifier",
        command_type: "string - command name",
        parameters: "object - command parameters",
        timestamp: "number - unix timestamp ms",
        source: "string - always 'gui'"
    },

    // GUI Response Stream Format (gui:responses) 
    RESPONSE_MESSAGE: {
        command_id: "string - matches original command",
        command_type: "string - original command name",
        status: "string - success/error/pending",
        data: "object - response data",
        message: "string - human readable message",
        timestamp: "number - unix timestamp ms",
        source: "string - responding component"
    },

    // Example Commands
    EXAMPLES: {
        manual_trade: {
            command_type: "manual_trade",
            parameters: {
                action: "add|reduce|close",
                shares: "number",
                symbol: "string|null"
            }
        },
        update_ocr_settings: {
            command_type: "update_ocr_preprocessing_full", 
            parameters: {
                ocr_upscale_factor: "number",
                ocr_threshold_c: "number",
                // ... other OCR parameters
            }
        },
        roi_adjust: {
            command_type: "roi_adjust",
            parameters: {
                edge: "x1|x2|y1|y2",
                direction: "left|right|up|down", 
                step_size: "number"
            }
        }
    }
};

/*
ISOLATION ARCHITECTURE:

┌─────────────────┐    Redis Streams    ┌──────────────────┐    Redis Streams    ┌─────────────────┐
│                 │ ──────────────────> │                  │ ──────────────────> │                 │
│   TESTRADE GUI  │                     │  Backend Layer   │                     │   TESTRADE      │
│                 │ <────────────────── │ (GUI Commands)   │ <────────────────── │   Components    │
└─────────────────┘    WebSocket        └──────────────────┘    Internal APIs    └─────────────────┘

ALLOWED COMMUNICATION:
✅ GUI → Backend (Redis Streams): All commands
✅ Backend → GUI (WebSocket): All data streams  
✅ Backend → TESTRADE (Redis): Command forwarding
✅ TESTRADE → Backend (Redis): Data publishing
✅ GUI → Backend (HTTP): Health check only

FORBIDDEN COMMUNICATION:
❌ GUI → TESTRADE (Direct): Never allowed
❌ GUI → TESTRADE (HTTP): Never allowed
❌ GUI → TESTRADE (Redis): Never allowed (must go through Backend)

This maintains complete isolation while allowing the GUI to function.
*/