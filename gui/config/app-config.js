// config/app-config.js - Application configuration
export class AppConfig {
    constructor() {
        this.config = {
            // API Configuration
            apiUrl: 'http://localhost:8001',
            wsUrl: 'ws://localhost:8001/ws',
            
            // Connection settings
            maxReconnectAttempts: 5,
            reconnectDelayMs: 2000,
            reconnectionTimeoutMs: 5000,
            
            // Health monitoring
            healthPollingIntervalMs: 5000,
            coreStatusCheckIntervalMs: 10000,
            healthUpdateTimeoutMs: 15000,
            
            // Memory management
            maxImageSizeBytes: 2000000, // 2MB
            maxImageHistoryCount: 5,
            memoryWarningThresholdMB: 50,
            maxImageHistoryItems: 100,
            
            // UI settings
            maxStreamLines: 30,
            buttonResetDelayMs: 2000,
            buttonErrorDelayMs: 3000,
            maxSharesLimit: 10000,
            
            // Performance
            imageUpdateDebounceMs: 50,
            
            // Debug mode
            debugMode: false
        };
    }

    get(key) {
        return this.config[key];
    }

    set(key, value) {
        this.config[key] = value;
    }

    getAll() {
        return { ...this.config };
    }

    // Getters for commonly used config
    get apiUrl() { return this.config.apiUrl; }
    get wsUrl() { return this.config.wsUrl; }
    get debugMode() { return this.config.debugMode; }
    get maxReconnectAttempts() { return this.config.maxReconnectAttempts; }
    get reconnectDelayMs() { return this.config.reconnectDelayMs; }
    get healthUpdateTimeoutMs() { return this.config.healthUpdateTimeoutMs; }
    get maxImageSizeBytes() { return this.config.maxImageSizeBytes; }
    get memoryWarningThresholdMB() { return this.config.memoryWarningThresholdMB; }
    get maxStreamLines() { return this.config.maxStreamLines; }
    get buttonResetDelayMs() { return this.config.buttonResetDelayMs; }
    get buttonErrorDelayMs() { return this.config.buttonErrorDelayMs; }
    get maxSharesLimit() { return this.config.maxSharesLimit; }
    get imageUpdateDebounceMs() { return this.config.imageUpdateDebounceMs; }
}