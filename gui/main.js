// main.js - Main entry point for TESTRADE GUI (CORRECTED for Redis Stream Isolation)
import { AppConfig } from './config/app-config.js';
import { StateManager } from './state/state-manager.js';
import { DOMManager } from './core/dom-manager.js';
import { ErrorHandler } from './core/error-handler.js';
import { PerformanceMonitor } from './core/performance-monitor.js';
import { MessageRouter } from './core/message-router.js';
import { WebSocketManager } from './websocket/websocket-manager.js';
import { RedisStreamClient } from './api/redis-stream-client.js'; // CORRECTED: Redis Stream Client
import { UIManager } from './ui/ui-manager.js';
import { StatusManager } from './ui/status-manager.js';
import { TradeManager } from './trading/trade-manager.js';
import { OCRManager } from './ocr/ocr-manager.js';
import { ConfigManager } from './config/config-manager.js';
import { EventManager } from './events/event-manager.js';
import { ImageManager } from './core/image-manager.js';

/**
 * TESTRADE GUI Application Class - CORRECTED FOR ISOLATION
 * 
 * CRITICAL: This version respects TESTRADE isolation rules:
 * - All commands go through Redis streams via RedisStreamClient
 * - No direct API calls to TESTRADE components
 * - Only allowed HTTP call is backend health check
 * - All communication is: GUI → Backend (Redis) → TESTRADE
 */
class TesttradeGUI {
    constructor() {
        this.config = new AppConfig();
        this.state = new StateManager();
        this.dom = new DOMManager();
        this.errorHandler = new ErrorHandler();
        this.performance = new PerformanceMonitor();
        this.imageManager = new ImageManager(this.config);
        
        // Core systems
        this.messageRouter = new MessageRouter();
        this.websocket = new WebSocketManager(this.config, this.state, this.messageRouter);
        
        // CORRECTED: Use Redis Stream Client instead of direct API client
        this.api = new RedisStreamClient(this.config, this.state);
        
        // UI systems
        this.ui = new UIManager(this.dom, this.state);
        this.status = new StatusManager(this.dom, this.state);
        
        // Feature systems
        this.trade = new TradeManager(this.state, this.api, this.ui);
        this.ocr = new OCRManager(this.state, this.api, this.ui, this.imageManager);
        this.configManager = new ConfigManager(this.api, this.ui);
        this.events = new EventManager(this.api, this.ui, this.trade, this.ocr, this.configManager);
        
        this.initialized = false;
    }

    /**
     * Initialize the application
     */
    async initialize() {
        try {
            this.performance.startTiming('initialization');
            console.log('🚀 Starting TESTRADE GUI initialization...');
            console.log('📡 Using Redis Stream Communication (TESTRADE Isolation Compliant)');

            // Initialize error handling first
            this.setupErrorHandling();

            // Initialize core systems
            await this.initializeCore();

            // Initialize UI systems
            await this.initializeUI();

            // Initialize feature systems
            await this.initializeFeatures();

            // Setup event listeners
            this.events.setupEventListeners();

            // Setup message routing
            this.setupMessageRouting();

            // Connect to backend
            await this.connectToBackend();

            this.performance.endTiming('initialization');
            this.initialized = true;

            console.log('✅ TESTRADE GUI initialization complete');
            this.ui.addSystemMessage('✅ TESTRADE Platform Ready (Redis Stream Mode)', 'success');

        } catch (error) {
            this.errorHandler.handleError(error, 'Application Initialization', 'critical');
            this.ui.addSystemMessage('❌ Critical initialization error - check console', 'error');
        }
    }

    /**
     * Setup global error handling
     */
    setupErrorHandling() {
        window.addEventListener('error', (event) => {
            this.errorHandler.handleError(event.error, 'Global Error', 'critical');
        });

        window.addEventListener('unhandledrejection', (event) => {
            this.errorHandler.handleError(event.reason, 'Unhandled Promise Rejection', 'critical');
        });
    }

    /**
     * Initialize core systems
     */
    async initializeCore() {
        // Refresh DOM cache
        this.dom.refreshCache();
        console.log('DOM cache refreshed');

        // Initialize status pills
        this.status.initialize();
        console.log('Status management initialized');

        // Initialize state
        this.state.initialize();
        console.log('State management initialized');
    }

    /**
     * Initialize UI systems
     */
    async initializeUI() {
        // Initialize UI components
        this.ui.initialize();
        
        // Setup image preview
        this.ui.initializePreview();
        
        // Load initial ROI via Redis stream
        await this.loadInitialROIViaStream();
        
        // Set initial button states
        this.ui.updateOCRButtonState(false);
        
        console.log('UI systems initialized');
    }

    /**
     * Load initial ROI via Redis stream (CORRECTED)
     */
    async loadInitialROIViaStream() {
        try {
            const result = await this.api.loadInitialROI();
            if (result.success && result.roi) {
                this.state.setCurrentROI(result.roi);
                this.ui.updateROIDisplay();
                this.ui.addSystemMessage(`✅ ROI loaded via Redis stream: [${result.roi.join(', ')}]`, 'success');
            } else {
                // Use fallback
                this.state.setCurrentROI([64, 159, 681, 296]);
                this.ui.updateROIDisplay();
                this.ui.addSystemMessage('Using default ROI coordinates', 'info');
            }
        } catch (error) {
            this.errorHandler.handleError(error, 'Load Initial ROI', 'error');
            // Use fallback
            this.state.setCurrentROI([64, 159, 681, 296]);
            this.ui.updateROIDisplay();
            this.ui.addSystemMessage('Using default ROI coordinates due to error', 'warning');
        }
    }

    /**
     * Initialize feature systems
     */
    async initializeFeatures() {
        // Initialize trading system
        this.trade.initialize();
        
        // Initialize OCR system
        this.ocr.initialize();
        
        // Configuration will be loaded after WebSocket connects
        console.log('Feature systems initialized');
    }

    /**
     * Setup WebSocket message routing
     */
    setupMessageRouting() {
        // Position updates
        this.messageRouter.registerHandler('position_update', (data) => {
            this.trade.updateSinglePosition(data);
        });

        // Trade history
        this.messageRouter.registerHandler('trade_history_update', (data) => {
            this.trade.updateTradeHistory(data.trades);
        });

        // Order updates
        this.messageRouter.registerHandler('order_update', (data) => {
            this.trade.updateOrderStatus(data);
        });

        // Order fills
        this.messageRouter.registerHandler('order_fill', (data) => {
            this.trade.handleOrderFill(data);
        });

        // Trade opened/closed
        this.messageRouter.registerHandler('trade_opened', (data) => {
            this.trade.handleTradeOpened(data);
        });

        this.messageRouter.registerHandler('trade_closed', (data) => {
            this.trade.handleTradeClosed(data);
        });

        // Account summary
        this.messageRouter.registerHandler('account_summary', (data) => {
            this.ui.updateAccountSummary(data);
        });

        // Market status
        this.messageRouter.registerHandler('market_status', (data) => {
            this.ui.updateMarketStatus(data);
        });

        // OCR updates
        this.messageRouter.registerHandler('raw_ocr', (data) => {
            this.ocr.updateRawOCRStream(data.text || '');
            this.ocr.updateRawOCRTable(data.snapshots || {});
            this.ocr.handleOCRUpdate(data);
        });

        this.messageRouter.registerHandler('processed_ocr', (data) => {
            this.ocr.updateProcessedOCRTable(data.snapshots || {}, data.timestamp);
        });

        // **REMOVED CONFLICTING HANDLERS** - MessageHandlers class now handles OCR/image routing

        // Status updates
        this.messageRouter.registerHandler('ocr_status', (data) => {
            this.state.setOCRActive(data.active);
            this.ui.updateOCRButtonState(data.active);
        });

        this.messageRouter.registerHandler('trading_status', (data) => {
            this.state.setTradingActive(data.active);
            this.ui.updateTradingButtonState(data.active);
        });

        // System messages
        this.messageRouter.registerHandler('system_message', (data) => {
            this.ui.addSystemMessage(data.message, data.type || 'info');
        });

        // Command responses from Redis streams
        this.messageRouter.registerHandler('command_response', (data) => {
            this.api.handleCommandResponse(data);
        });

        // Initial state from backend
        this.messageRouter.registerHandler('initial_state', (data) => {
            console.log('📦 Received initial state from backend:', data);
            
            if (data.data) {
                // Update financial data if provided
                if (data.data.financial_data) {
                    this.ui.updateAccountSummary(data.data.financial_data);
                }
                
                // Update active trades if provided
                if (data.data.active_trades) {
                    this.trade.updateActiveTrades(data.data.active_trades);
                }
                
                // Log successful bootstrap
                this.ui.addSystemMessage('✅ Initial state loaded from backend', 'success');
            }
        });

        // Listen for WebSocket connection events
        document.addEventListener('websocket:connected', async () => {
            console.log('🔗 WebSocket connected, loading configuration...');
            try {
                await this.configManager.loadConfiguration();
                this.ui.addSystemMessage('✅ Configuration loaded', 'success');
                
                // Now that we're connected, bootstrap data
                console.log('🔗 WebSocket ready, requesting initial state...');
                await this.bootstrapFromRedisStream();
            } catch (error) {
                console.warn('Configuration loading failed after connection:', error);
                this.ui.addSystemMessage('⚠️ Using default configuration', 'warning');
            }
        });

        console.log(`Message routing configured with ${this.messageRouter.getHandlerCount()} handlers`);
    }

    /**
     * Connect to backend services
     */
    async connectToBackend() {
        console.log('🔗 Connecting to backend services...');
        
        // Just connect WebSocket - bootstrap will happen after connection
        this.websocket.connect();
        this.ui.addSystemMessage('📡 Connecting to backend via WebSocket...', 'info');
        
        // Start monitoring
        this.startMonitoring();
    }

    /**
     * Determine if we should use Redis stream bootstrap (PREFERRED)
     */
    shouldUseStreamBootstrap() {
        return true; // Always prefer Redis streams for isolation
    }

    /**
     * Determine if we should use bootstrap API (DEPRECATED)
     */
    shouldUseBootstrapAPI() {
        return (typeof global_app_config !== 'undefined' &&
                global_app_config.FEATURE_FLAG_GUI_USE_XREAD_AND_BOOTSTRAP_API !== undefined)
                ? global_app_config.FEATURE_FLAG_GUI_USE_XREAD_AND_BOOTSTRAP_API
                : false; // Default to Redis streams
    }

    /**
     * PREFERRED: Bootstrap initial data via Redis stream
     */
    async bootstrapFromRedisStream() {
        this.ui.addSystemMessage('ℹ️ Bootstrap mode - requesting initial state via Redis streams', 'info');
        
        try {
            const bootstrapStart = performance.now();
            
            // Send single command to request all initial state
            const result = await this.api.requestInitialStateViaStream();
            
            if (result.success) {
                const cmdIdShort = result.data?.command_id?.substring(0,8) || 'N/A';
                this.ui.addSystemMessage(
                    `✅ Initial state request sent via Redis stream (CmdID: ${cmdIdShort})`, 
                    'success'
                );
                this.ui.addSystemMessage(
                    '📡 Waiting for bootstrap data via WebSocket streams...', 
                    'info'
                );
            } else {
                throw new Error(result.error || 'Failed to request initial state');
            }

            const bootstrapEnd = performance.now();
            this.ui.addSystemMessage(
                `🏁 Bootstrap request completed in ${(bootstrapEnd - bootstrapStart).toFixed(0)}ms`,
                'success'
            );

        } catch (error) {
            this.errorHandler.handleError(error, 'Redis Stream Bootstrap', 'critical');
            this.ui.addSystemMessage('❌ Redis stream bootstrap failed - falling back to WebSocket only', 'error');
        }
    }

    /**
     * DEPRECATED: Bootstrap initial data from API (VIOLATES ISOLATION)
     */
    async bootstrapFromAPI() {
        console.warn('⚠️ Using API bootstrap - THIS VIOLATES TESTRADE ISOLATION RULES');
        this.ui.addSystemMessage('⚠️ API bootstrap mode - consider migrating to Redis streams', 'warning');
        
        try {
            const bootstrapStart = performance.now();
            
            // These API calls go to backend's API layer, not directly to TESTRADE
            // But they still violate the isolation principle
            const [positions, account, orders, marketStatus, rejections] = await Promise.allSettled([
                this.api.fetchPositions(),
                this.api.fetchAccountSummary(), 
                this.api.fetchTodaysOrders(),
                this.api.fetchMarketStatus(),
                this.api.fetchTradeRejections()
            ]);

            // Process results
            this.processBootstrapResults({
                positions,
                account,
                orders,
                marketStatus,
                rejections
            });

            const bootstrapEnd = performance.now();
            this.ui.addSystemMessage(
                `🏁 API Bootstrap completed in ${(bootstrapEnd - bootstrapStart).toFixed(0)}ms (DEPRECATED)`,
                'warning'
            );

        } catch (error) {
            this.errorHandler.handleError(error, 'API Bootstrap Sequence', 'critical');
            this.ui.addSystemMessage('❌ API Bootstrap failed - some data may be missing', 'error');
        }
    }

    /**
     * Process bootstrap results (for API bootstrap only)
     */
    processBootstrapResults(results) {
        // Process positions
        if (results.positions.status === 'fulfilled') {
            this.trade.updateActiveTrades(results.positions.value);
            this.ui.addSystemMessage('✅ Positions loaded', 'success');
        }

        // Process account
        if (results.account.status === 'fulfilled') {
            this.ui.updateAccountSummary(results.account.value);
            this.ui.addSystemMessage('✅ Account summary loaded', 'success');
        }

        // Process orders
        if (results.orders.status === 'fulfilled') {
            this.trade.updateOpenOrders(results.orders.value);
            this.ui.addSystemMessage('✅ Orders loaded', 'success');
        }

        // Process market status
        if (results.marketStatus.status === 'fulfilled') {
            this.ui.updateMarketStatus(results.marketStatus.value);
            this.ui.addSystemMessage('✅ Market status loaded', 'success');
        }

        // Process rejections
        if (results.rejections.status === 'fulfilled') {
            this.trade.updateHistoricalTrades(results.rejections.value);
            this.ui.addSystemMessage('✅ Trade history loaded', 'success');
        }
    }

    /**
     * Start monitoring and periodic tasks
     */
    startMonitoring() {
        // Start time updates
        setInterval(() => this.ui.updateTime(), 1000);
        
        // Start performance monitoring in debug mode
        if (this.config.debugMode) {
            setInterval(() => {
                this.performance.logPerformanceReport();
            }, 30000);
        }
        
        // Start memory cleanup
        setInterval(() => {
            this.imageManager.performCleanup();
        }, 60000);

        // Clean up old pending Redis stream commands
        setInterval(() => {
            this.api.cleanupPendingCommands();
        }, 30000);
    }

    /**
     * Cleanup and shutdown
     */
    cleanup() {
        console.log('🧹 Starting application cleanup...');
        
        // Stop WebSocket
        this.websocket.disconnect();
        
        // Clear intervals and timeouts
        this.performance.cleanup();
        
        // Clear image memory
        this.imageManager.cleanup();
        
        // Reset state
        this.state.reset();
        
        console.log('✅ Application cleanup completed');
    }

    /**
     * Get public API for external access
     */
    getAPI() {
        return {
            // Core systems
            state: this.state,
            config: this.config,
            api: this.api,  // Expose Redis Stream Client
            websocket: this.websocket,  // Expose WebSocket for command sending
            
            // Feature APIs
            trade: this.trade.getPublicAPI(),
            ocr: this.ocr.getPublicAPI(),
            ui: this.ui.getPublicAPI(),
            
            // Redis Stream API
            sendCommand: this.api.sendCommand.bind(this.api),
            
            // Utility functions
            debug: {
                performance: () => this.performance.getReport(),
                state: () => this.state.getDebugInfo(),
                memory: () => this.imageManager.getMemoryInfo(),
                pendingCommands: () => this.api.getPendingCommands(),
                messageHandlers: () => this.messageRouter.getAllHandlers()
            }
        };
    }
}

// Global initialization
let app = null;

// Initialize when DOM is ready
document.addEventListener('DOMContentLoaded', async function() {
    try {
        app = new TesttradeGUI();
        await app.initialize();
        
        // Expose app to global scope for debugging
        window.TESTRADE = app.getAPI();
        
        // Add isolation compliance info to global
        window.TESTRADE.ISOLATION_INFO = {
            compliant: true,
            communication_method: 'redis_streams',
            allowed_http_calls: ['backend_health_only'],
            forbidden_calls: ['direct_testrade_api', 'direct_testrade_http'],
            architecture: 'GUI → Backend (Redis) → TESTRADE'
        };
        
        console.log('🛡️ TESTRADE Isolation compliance: ACTIVE');
        console.log('📡 All commands routed through Redis streams');
        
    } catch (error) {
        console.error('❌ Failed to initialize TESTRADE GUI:', error);
    }
});

// Cleanup on page unload
window.addEventListener('beforeunload', () => {
    if (app) {
        app.cleanup();
    }
});

// Handle visibility changes
document.addEventListener('visibilitychange', () => {
    if (app) {
        if (document.hidden) {
            console.log('Page hidden - reducing background activity');
        } else {
            console.log('Page visible - resuming normal activity');
            // Try to reconnect if needed
            if (!app.state.isConnected() && app.initialized) {
                setTimeout(() => app.websocket.connect(), 1000);
            }
        }
    }
});

export { TesttradeGUI };