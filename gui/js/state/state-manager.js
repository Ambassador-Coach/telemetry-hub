// state/state-manager.js - Centralized state management
export class StateManager {
    constructor() {
        this.state = {
            // Connection state
            connected: false,
            reconnecting: false,
            
            // OCR state
            ocrActive: false,
            imageMode: 'raw', // 'raw' or 'processed'
            lastRawImage: null,
            lastProcessedImage: null,
            currentROI: [64, 159, 681, 296],
            
            // UI state
            ocrSettingsExpanded: false,
            developmentSettingsExpanded: false,
            
            // Feature flags
            allSymbols: false,
            confidenceMode: false,
            videoMode: false,
            perfTracking: false,
            usePreprocessedImage: false,
            
            // Health monitoring
            lastHealthUpdate: 0,
            coreConnected: false,
            
            // Trading state
            activeTrades: [],
            active_trades: [], // Alternative property name
            
            // WebSocket
            ws: null,
            wsUrl: null,
            
            // Image management
            rawImageHistory: [],
            processedImageHistory: [],
            imageUpdateTimeout: null,
            roiAdjustmentMode: false,
            lockedImage: null
        };

        this.tradeState = {
            openPositions: [],
            historicalTrades: [],
            openOrders: [],
            totalPnL: 0,
            dayPnL: 0,
            accountValue: 0,
            buyingPower: 0
        };

        this.uiState = {
            historyExpanded: false,
            activeHistoryTab: 'today'
        };
    }

    initialize() {
        // Set initial values
        this.state.wsUrl = this.state.apiUrl ? this.state.apiUrl.replace('http', 'ws') + '/ws' : null;
        
        // Initialize arrays if needed
        this.state.activeTrades = this.state.activeTrades || [];
        this.state.active_trades = this.state.active_trades || [];
        this.tradeState.openOrders = this.tradeState.openOrders || [];
        this.tradeState.historicalTrades = this.tradeState.historicalTrades || [];
    }

    // Connection state
    isConnected() {
        return this.state.connected;
    }

    setConnected(connected) {
        this.state.connected = connected;
    }

    isReconnecting() {
        return this.state.reconnecting;
    }

    setReconnecting(reconnecting) {
        this.state.reconnecting = reconnecting;
    }

    // OCR state
    isOCRActive() {
        return this.state.ocrActive;
    }

    setOCRActive(active) {
        this.state.ocrActive = active;
    }

    getImageMode() {
        return this.state.imageMode;
    }

    setImageMode(mode) {
        this.state.imageMode = mode;
    }

    getLastRawImage() {
        return this.state.lastRawImage;
    }

    setLastRawImage(image) {
        this.state.lastRawImage = image;
    }

    getLastProcessedImage() {
        return this.state.lastProcessedImage;
    }

    setLastProcessedImage(image) {
        this.state.lastProcessedImage = image;
    }

    getCurrentROI() {
        return this.state.currentROI;
    }

    setCurrentROI(roi) {
        this.state.currentROI = roi;
    }

    // Trading state
    getActiveTrades() {
        return this.state.activeTrades;
    }

    setActiveTrades(trades) {
        this.state.activeTrades = trades;
        this.state.active_trades = trades; // Keep both in sync
    }

    getTradeState() {
        return this.tradeState;
    }

    getOpenOrders() {
        return this.tradeState.openOrders;
    }

    setOpenOrders(orders) {
        this.tradeState.openOrders = orders;
    }

    getHistoricalTrades() {
        return this.tradeState.historicalTrades;
    }

    setHistoricalTrades(trades) {
        this.tradeState.historicalTrades = trades;
    }

    addHistoricalTrade(trade) {
        this.tradeState.historicalTrades.unshift(trade);
    }

    // UI state
    getUIState() {
        return this.uiState;
    }

    isHistoryExpanded() {
        return this.uiState.historyExpanded;
    }

    setHistoryExpanded(expanded) {
        this.uiState.historyExpanded = expanded;
    }

    getActiveHistoryTab() {
        return this.uiState.activeHistoryTab;
    }

    setActiveHistoryTab(tab) {
        this.uiState.activeHistoryTab = tab;
    }

    // Health state
    getLastHealthUpdate() {
        return this.state.lastHealthUpdate;
    }

    setLastHealthUpdate(timestamp) {
        this.state.lastHealthUpdate = timestamp;
    }

    isCoreConnected() {
        return this.state.coreConnected;
    }

    setCoreConnected(connected) {
        this.state.coreConnected = connected;
    }

    // WebSocket state
    getWebSocket() {
        return this.state.ws;
    }

    setWebSocket(ws) {
        this.state.ws = ws;
    }

    // Account state
    getAccountValue() {
        return this.tradeState.accountValue;
    }

    setAccountValue(value) {
        this.tradeState.accountValue = value;
    }

    getBuyingPower() {
        return this.tradeState.buyingPower;
    }

    setBuyingPower(power) {
        this.tradeState.buyingPower = power;
    }

    getDayPnL() {
        return this.tradeState.dayPnL;
    }

    setDayPnL(pnl) {
        this.tradeState.dayPnL = pnl;
    }

    // Image history
    getRawImageHistory() {
        return this.state.rawImageHistory;
    }

    addRawImage(image) {
        this.state.rawImageHistory.push(image);
    }

    getProcessedImageHistory() {
        return this.state.processedImageHistory;
    }

    addProcessedImage(image) {
        this.state.processedImageHistory.push(image);
    }

    clearImageHistory() {
        this.state.rawImageHistory = [];
        this.state.processedImageHistory = [];
    }

    // Feature flags
    isAllSymbolsEnabled() {
        return this.state.allSymbols;
    }

    setAllSymbolsEnabled(enabled) {
        this.state.allSymbols = enabled;
    }

    isConfidenceModeEnabled() {
        return this.state.confidenceMode;
    }

    setConfidenceModeEnabled(enabled) {
        this.state.confidenceMode = enabled;
    }

    isVideoModeEnabled() {
        return this.state.videoMode;
    }

    setVideoModeEnabled(enabled) {
        this.state.videoMode = enabled;
    }

    // Direct state access (for backward compatibility)
    get() {
        return this.state;
    }

    // Reset state
    reset() {
        // Reset connection state
        this.state.connected = false;
        this.state.reconnecting = false;
        this.state.ws = null;
        
        // Clear images
        this.state.lastRawImage = null;
        this.state.lastProcessedImage = null;
        this.clearImageHistory();
        
        // Clear timeouts
        if (this.state.imageUpdateTimeout) {
            clearTimeout(this.state.imageUpdateTimeout);
            this.state.imageUpdateTimeout = null;
        }
        
        // Reset trading state
        this.state.activeTrades = [];
        this.state.active_trades = [];
        this.tradeState.openOrders = [];
        this.tradeState.historicalTrades = [];
    }

    // Debug information
    getDebugInfo() {
        return {
            connection: {
                connected: this.state.connected,
                reconnecting: this.state.reconnecting,
                lastHealthUpdate: this.state.lastHealthUpdate,
                coreConnected: this.state.coreConnected
            },
            trading: {
                activeTrades: this.state.activeTrades.length,
                openOrders: this.tradeState.openOrders.length,
                historicalTrades: this.tradeState.historicalTrades.length,
                accountValue: this.tradeState.accountValue,
                buyingPower: this.tradeState.buyingPower
            },
            images: {
                mode: this.state.imageMode,
                hasRaw: !!this.state.lastRawImage,
                hasProcessed: !!this.state.lastProcessedImage,
                rawHistory: this.state.rawImageHistory.length,
                processedHistory: this.state.processedImageHistory.length
            },
            ui: {
                historyExpanded: this.uiState.historyExpanded,
                activeTab: this.uiState.activeHistoryTab,
                ocrSettingsExpanded: this.state.ocrSettingsExpanded
            }
        };
    }
}