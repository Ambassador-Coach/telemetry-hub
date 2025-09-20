// api/api-client.js - API client that uses Redis streams for TESTRADE isolation
import { RedisStreamClient } from './redis-stream-client.js';

export class APIClient extends RedisStreamClient {
    constructor(config, state) {
        super(config, state);
        
        // Start periodic cleanup of pending commands
        setInterval(() => this.cleanupPendingCommands(), 5000);
    }

    /**
     * Legacy request method - redirects to Redis stream
     * Kept for compatibility but logs deprecation warning
     */
    async request(endpoint, options = {}) {
        console.warn(`DEPRECATED: Direct HTTP request to ${endpoint}. Should use Redis streams.`);
        
        // Only allow specific backend endpoints, never TESTRADE
        const allowedEndpoints = ['/health', '/stream/send', '/ws'];
        const isAllowed = allowedEndpoints.some(allowed => endpoint.startsWith(allowed));
        
        if (!isAllowed) {
            throw new Error(`Direct HTTP request to ${endpoint} not allowed. Use Redis streams.`);
        }

        const url = `${this.baseUrl}${endpoint}`;
        const defaultOptions = {
            headers: {
                'Content-Type': 'application/json',
            }
        };

        const requestOptions = { ...defaultOptions, ...options };

        try {
            const response = await fetch(url, requestOptions);
            
            if (!response.ok) {
                throw new Error(`HTTP ${response.status}: ${response.statusText}`);
            }

            return await response.json();
        } catch (error) {
            console.error(`API request failed: ${endpoint}`, error);
            throw error;
        }
    }

    /**
     * Override sendCommand to ensure it uses Redis streams
     * This maintains the same interface but routes through streams
     */
    async sendCommand(command, parameters = {}) {
        // Let parent RedisStreamClient handle it
        return await super.sendCommand(command, parameters);
    }

    /**
     * Trading API - uses Redis streams
     */
    async toggleTrading() {
        const command = this.state.isTradingActive() ? 'stop_trading' : 'start_trading';
        return await this.sendCommand(command);
    }

    /**
     * Get WebSocket URL for GUI
     * This is one of the few allowed direct connections
     */
    getWebSocketUrl() {
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const host = this.config.apiUrl.replace(/^https?:\/\//, '').replace(/\/$/, '');
        return `${protocol}//${host}/ws`;
    }

    /**
     * Bootstrap methods that use backend API layer
     * These read from Redis through the backend, not directly from TESTRADE
     */
    async fetchInitialState() {
        // Prefer Redis stream method
        if (this.config.useRedisStreamBootstrap) {
            return await this.requestInitialStateViaStream();
        }
        
        // Fallback to HTTP API (still goes through backend, not TESTRADE)
        return {
            positions: await this.fetchPositions(),
            account: await this.fetchAccountSummary(),
            orders: await this.fetchTodaysOrders(),
            marketStatus: await this.fetchMarketStatus(),
            rejections: await this.fetchTradeRejections()
        };
    }

    /**
     * Configuration API - uses Redis streams
     */
    async updateConfiguration(config) {
        const updates = [];
        
        for (const [key, value] of Object.entries(config)) {
            updates.push(this.updateConfig(key, value));
        }
        
        return await Promise.all(updates);
    }

    /**
     * OCR API - uses Redis streams
     */
    async updateOCRPreprocessing(settings) {
        return await this.updateOCRSettings(settings);
    }

    /**
     * Trading parameters - uses Redis streams
     */
    async updateTradingParameters(params) {
        return await this.updateTradingParams(params);
    }

    /**
     * Development settings - uses Redis streams
     */
    async updateDevelopmentSettings(settings) {
        const updates = [];
        
        if (settings.development_mode !== undefined) {
            updates.push(this.updateDevelopmentMode(settings.development_mode));
        }
        
        if (settings.recording !== undefined) {
            updates.push(this.updateRecordingSettings(settings.recording));
        }
        
        if (settings.logging !== undefined) {
            updates.push(this.updateLoggingSettings(settings.logging));
        }
        
        return await Promise.all(updates);
    }

    /**
     * System control - uses Redis streams
     */
    async systemControl(action, params = {}) {
        switch (action) {
            case 'start_core':
                return await this.startCore();
            case 'toggle_recording':
                return await this.toggleRecording();
            case 'toggle_all_symbols':
                return await this.toggleAllSymbols(params.enabled);
            case 'toggle_confidence_mode':
                return await this.toggleConfidenceMode(params.enabled);
            case 'toggle_video_mode':
                return await this.toggleVideoMode(params.enabled);
            default:
                throw new Error(`Unknown system control action: ${action}`);
        }
    }

    /**
     * Get pending command count for UI display
     */
    getPendingCommandCount() {
        return this.pendingCommands.size;
    }

    /**
     * Check if any commands are pending
     */
    hasPendingCommands() {
        return this.pendingCommands.size > 0;
    }
}