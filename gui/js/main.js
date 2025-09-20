// js/main.js - Main entry point for TESTRADE GUI

import { AppConfig } from './config/app-config.js';
import { StateManager } from './state/state-manager.js';
import { DOMManager } from './core/dom-manager.js';
import { ErrorHandler } from './core/error-handler.js';
import { PerformanceMonitor } from './core/performance-monitor.js';
import { ImageManager } from './core/image-manager.js';
import { MessageRouter } from './core/message-router.js';
import { WebSocketManager } from './websocket/websocket-manager.js';
import { MessageHandlers } from './websocket/message-handlers.js';
import { RedisStreamClient } from './api/redis-stream-client.js';
import { UIManager } from './ui/ui-manager.js';
import { StatusManager } from './ui/status-manager.js';
import { TradeManager } from './trading/trade-manager.js';
import { OCRManager } from './ocr/ocr-manager.js';
import { ConfigManager } from './config/config-manager.js';
import { EventManager } from './events/event-manager.js';

class TesttradeGUI {
    constructor() {
        // --- 1. Core Systems (No Dependencies) ---
        this.config = new AppConfig();
        this.state = new StateManager(this.config);
        this.dom = new DOMManager();
        this.errorHandler = new ErrorHandler();
        this.performance = new PerformanceMonitor();
        this.imageManager = new ImageManager(this.config);
        this.messageRouter = new MessageRouter();

        // --- 2. Services & Managers (Depend on Core Systems) ---
        this.api = new RedisStreamClient(this.config, this.state);
        this.ui = new UIManager(this.dom, this.state);
        this.status = new StatusManager(this.dom, this.state);
        this.websocket = new WebSocketManager(this.config, this.state, this.messageRouter, this.status);

        // --- 3. Feature Managers (Depend on Services & UI) ---
        this.trade = new TradeManager(this.state, this.api, this.ui);
        this.ocr = new OCRManager(this.state, this.api, this.ui, this.imageManager);
        this.configManager = new ConfigManager(this.api, this.ui);
        this.events = new EventManager(this.api, this.ui, this.trade, this.ocr, this.configManager);
        this.messageHandlers = new MessageHandlers(this.state, this.ui, this.trade, this.ocr, this.status);

        this.initialized = false;
    }

    async initialize() {
        if (this.initialized) return;
        this.performance.startTiming('initialization');
        console.log('🚀 Starting TESTRADE GUI initialization...');

        this.setupErrorHandling();
        this.status.initialize();
        this.ui.initialize();
        this.trade.initialize();
        this.ocr.initialize();
        this.events.setupEventListeners();
        this.messageHandlers.registerHandlers(this.messageRouter);

        this.websocket.connect();

        // Wait for WebSocket connection before bootstrap
        await this.waitForConnection();
        await this.bootstrap();

        this.performance.endTiming('initialization');
        this.initialized = true;
        this.ui.addSystemMessage('✅ TESTRADE Platform Ready', 'success');
    }
    
    setupErrorHandling() {
        window.addEventListener('error', (event) => this.errorHandler.handleError(event.error, 'Global Error', 'critical'));
        window.addEventListener('unhandledrejection', (event) => this.errorHandler.handleError(event.reason, 'Unhandled Promise Rejection', 'critical'));
    }

    async waitForConnection() {
        return new Promise((resolve) => {
            if (this.websocket.isConnected()) {
                resolve();
                return;
            }

            const checkConnection = () => {
                if (this.websocket.isConnected()) {
                    resolve();
                } else {
                    setTimeout(checkConnection, 100);
                }
            };

            checkConnection();
        });
    }

    async bootstrap() {
        this.ui.addSystemMessage('ℹ️ Bootstrap mode enabled. Fetching initial state...', 'info');
        try {
            const bootstrapStart = performance.now();
            await this.configManager.loadConfiguration();
            
            // The API client now uses Redis streams for all commands
            const result = await this.api.requestInitialStateViaStream();

            if (result.success) {
                 this.ui.addSystemMessage(`✅ Initial state request sent (CmdID: ${result.data.command_id.substring(0,8)})`, 'success');
            } else {
                 throw new Error(result.error || 'Failed to request initial state');
            }

            const bootstrapEnd = performance.now();
            this.ui.addSystemMessage(`🏁 Bootstrap request sent in ${(bootstrapEnd - bootstrapStart).toFixed(0)} ms. Waiting for data streams...`, 'info');
        } catch (error) {
            this.errorHandler.handleError(error, 'GUI Bootstrap', 'critical');
        }
    }

    cleanup() {
        console.log('🧹 Cleaning up application...');
        this.websocket.disconnect();
        this.events.cleanup();
        this.performance.cleanup();
        this.imageManager.cleanup();
        this.errorHandler.cleanup();
    }

    getPublicAPI() {
        return {
            state: this.state,
            config: this.config,
            api: this.api,
            trade: this.trade.getPublicAPI(),
            ocr: this.ocr.getPublicAPI(),
            ui: this.ui.getPublicAPI(),
            debug: {
                performance: () => this.performance.getReport(),
                state: () => this.state.getDebugInfo(),
                memory: () => this.imageManager.getMemoryInfo(),
                pendingCommands: () => this.api.getPendingCommands(),
                handlers: () => this.messageRouter.getAllHandlers(),
            }
        };
    }
}

// --- Global Initialization ---
window.addEventListener('DOMContentLoaded', async () => {
    try {
        const app = new TesttradeGUI();
        window.TESTRADE = app; // Expose for debugging
        await app.initialize();
    } catch (error) {
        console.error('❌ Critical initialization error:', error);
        document.body.innerHTML = `<div style="padding: 20px; color: red;"><h1>Initialization Failed</h1><p>${error.message}</p><pre>${error.stack}</pre></div>`;
    }
});

window.addEventListener('beforeunload', () => {
    if (window.TESTRADE) {
        window.TESTRADE.cleanup();
    }
});