// websocket/websocket-manager.js - WebSocket connection management
export class WebSocketManager {
    constructor(config, state, messageRouter) {
        this.config = config;
        this.state = state;
        this.messageRouter = messageRouter;
        this.ws = null;
        this.reconnectAttempts = 0;
        this.reconnectTimeout = null;
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
        
        // Update API status
        this.updateAPIStatus('healthy');
        
        // Emit connection event
        this.emitEvent('connected');
    }

    handleMessage(event) {
        try {
            const data = JSON.parse(event.data);
            
            // Update last update time
            this.updateLastUpdateTime();
            
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
        
        // Update API status
        this.updateAPIStatus('error');
        
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

    emitEvent(eventName, data = {}) {
        // Emit custom events for other components to listen to
        const event = new CustomEvent(`websocket:${eventName}`, {
            detail: { ...data, timestamp: Date.now() }
        });
        document.dispatchEvent(event);
    }
}

// websocket/message-handlers.js - Specific message handlers
export class MessageHandlers {
    constructor(state, ui, trade, ocr, status) {
        this.state = state;
        this.ui = ui;
        this.trade = trade;
        this.ocr = ocr;
        this.status = status;
    }

    registerHandlers(messageRouter) {
        // Health and status messages
        messageRouter.registerHandler('health_update', this.handleHealthUpdate.bind(this));
        messageRouter.registerHandler('core_status_update', this.handleCoreStatusUpdate.bind(this));
        messageRouter.registerHandler('system_message', this.handleSystemMessage.bind(this));
        
        // Trading messages
        messageRouter.registerHandler('trades_update', this.handleTradesUpdate.bind(this));
        messageRouter.registerHandler('position_update', this.handlePositionUpdate.bind(this));
        messageRouter.registerHandler('orders_update', this.handleOrdersUpdate.bind(this));
        messageRouter.registerHandler('order_status_update', this.handleOrderStatusUpdate.bind(this));
        messageRouter.registerHandler('order_fill_update', this.handleOrderFillUpdate.bind(this));
        messageRouter.registerHandler('trade_opened', this.handleTradeOpened.bind(this));
        messageRouter.registerHandler('trade_closed', this.handleTradeClosed.bind(this));
        messageRouter.registerHandler('trade_history_update', this.handleTradeHistoryUpdate.bind(this));
        
        // Account messages
        messageRouter.registerHandler('account_summary_update', this.handleAccountSummaryUpdate.bind(this));
        messageRouter.registerHandler('position_summary_update', this.handlePositionSummaryUpdate.bind(this));
        
        // OCR messages
        messageRouter.registerHandler('image_grab', this.handleImageGrab.bind(this));
        messageRouter.registerHandler('preview_frame', this.handlePreviewFrame.bind(this));
        messageRouter.registerHandler('raw_ocr', this.handleRawOCR.bind(this));
        messageRouter.registerHandler('processed_ocr', this.handleProcessedOCR.bind(this));
        messageRouter.registerHandler('ocr_update', this.handleOCRUpdate.bind(this));
        
        // Other messages
        messageRouter.registerHandler('roi_update', this.handleROIUpdate.bind(this));
        messageRouter.registerHandler('command_response', this.handleCommandResponse.bind(this));
    }

    handleHealthUpdate(data) {
        this.status.updateHealthStatus(data);
        
        if (data.metrics && data.metrics.gui_latency_ms) {
            this.ui.updateLatencyDisplay(data.metrics.gui_latency_ms);
        }
    }

    handleCoreStatusUpdate(data) {
        this.status.updateCoreStatus(data);
        
        if (data.status_event) {
            if (data.status_event.includes("OCR_STARTED") || data.status_event === "OCR_ACTIVE") {
                this.ui.updateOCRButtonState(true);
            } else if (data.status_event.includes("OCR_STOPPED") || data.status_event === "OCR_INACTIVE") {
                this.ui.updateOCRButtonState(false);
            }
        }
    }

    handleSystemMessage(data) {
        this.ui.addSystemMessage(data.message, data.level || 'info');
    }

    handleTradesUpdate(data) {
        if (data.payload && data.payload.trades) {
            this.trade.updateActiveTrades(data.payload.trades);
            
            if (data.payload.is_bootstrap) {
                this.ui.addSystemMessage(`🚀 Bootstrap: Loaded ${data.payload.trades.length} positions`, 'success');
            }
        }
    }

    handlePositionUpdate(data) {
        if (data.symbol && data.position) {
            this.trade.updateSinglePosition(data);
        }
    }

    handleOrdersUpdate(data) {
        if (data.orders) {
            this.trade.updateOpenOrders(data.orders);
            
            if (data.is_bootstrap) {
                this.ui.addSystemMessage(`📋 Bootstrap: Loaded ${data.orders.length} orders`, 'success');
            }
        }
    }

    handleOrderStatusUpdate(data) {
        if (data.payload) {
            this.trade.updateOrderStatus(data.payload);
        }
    }

    handleOrderFillUpdate(data) {
        if (data.fill || data.order) {
            this.trade.handleOrderFill(data.fill || data.order);
        }
    }

    handleTradeOpened(data) {
        if (data.trade) {
            this.trade.handleTradeOpened(data.trade);
        }
    }

    handleTradeClosed(data) {
        if (data.trade) {
            this.trade.handleTradeClosed(data.trade);
        }
    }

    handleTradeHistoryUpdate(data) {
        if (data.trades) {
            this.trade.updateTradeHistory(data.trades);
        }
    }

    handleAccountSummaryUpdate(data) {
        if (data.account) {
            this.ui.updateAccountSummary(data.account);
            
            if (data.account.is_bootstrap) {
                this.ui.addSystemMessage('🚀 Bootstrap: Account summary loaded', 'success');
            }
        }
    }

    handlePositionSummaryUpdate(data) {
        if (data.summary) {
            this.ui.updatePositionSummary(data.summary);
            
            if (data.summary.is_bootstrap) {
                this.ui.addSystemMessage('🚀 Bootstrap: Position summary loaded', 'success');
            }
        }
    }

    handleImageGrab(data) {
        if (data.image_data) {
            this.ocr.updatePreviewImage(data.image_data, 'raw');
        }
        
        if (data.timestamp_backend_sent_ns) {
            const latency = (performance.now() - data.timestamp_backend_sent_ns / 1000000);
            if (latency > 0 && latency < 10000) {
                this.ui.updateLatencyDisplay(latency);
            }
        }
    }

    handlePreviewFrame(data) {
        if (data.image_data) {
            this.ocr.updatePreviewImage(data.image_data, 'processed');
        }
        
        if (data.content) {
            this.ocr.updateRawOCRStream(data.content);
        }
        
        if (data.confidence !== undefined) {
            this.ui.updateConfidence(data.confidence);
        }
    }

    handleRawOCR(data) {
        if (data.content) {
            this.ocr.updateRawOCRStream(data.content);
        }
        
        if (data.confidence) {
            this.ui.updateConfidence(data.confidence);
        }
    }

    handleProcessedOCR(data) {
        if (data.no_trade_data) {
            this.ocr.clearProcessedOCRTable();
        } else if (data.snapshots) {
            this.ocr.updateProcessedOCRTable(data.snapshots, data.timestamp);
        }
    }

    handleOCRUpdate(data) {
        // Generic OCR update handler
        this.ocr.handleOCRUpdate(data);
    }

    handleROIUpdate(data) {
        if (data.roi) {
            this.state.setCurrentROI(data.roi);
            this.ui.updateROIDisplay();
        }
    }

    handleCommandResponse(data) {
        const level = data.status === 'success' ? 'success' : 'error';
        this.ui.addSystemMessage(
            `Command ${data.command_type}: ${data.status} - ${data.message || ''}`,
            level
        );
        
        // Handle specific command responses
        if (data.command_type === 'SET_ROI_ABSOLUTE' && data.status === 'success' && data.data) {
            if (data.data.roi) {
                this.state.setCurrentROI(data.data.roi);
                this.ui.updateROIDisplay();
            }
        }
    }
}