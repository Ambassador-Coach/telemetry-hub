// js/websocket/message-handlers.js

export class MessageHandlers {
    constructor(state, ui, trade, ocr, status) {
        this.state = state;
        this.ui = ui;
        this.trade = trade;
        this.ocr = ocr;
        this.status = status;
    }

    registerHandlers(messageRouter) {
        // --- Register a handler for every message type ---

        // Health & Status
        messageRouter.registerHandler('health_update', data => this.status.updateHealthStatus(data.payload || data));
        messageRouter.registerHandler('core_status_update', data => this.status.updateCoreStatus(data));
        messageRouter.registerHandler('babysitter_health', data => this.status.updateBabysitterStatus(data.status, data.message));
        messageRouter.registerHandler('redis_health', data => this.status.updateRedisStatus(data.connected || data.status === 'healthy'));
        messageRouter.registerHandler('zmq_status', data => this.status.updateZMQStatus(data.connected || data.status === 'healthy'));
        messageRouter.registerHandler('system_message', data => this.ui.addSystemMessage(data.message, data.level || 'info'));
        messageRouter.registerHandler('initial_state', data => this.handleInitialState(data));
        
        // Trading & Positions
        messageRouter.registerHandler('trades_update', data => {
            const trades = data.payload?.trades || data.trades || [];
            this.trade.updateActiveTrades(trades);
        });
        messageRouter.registerHandler('position_update', data => this.trade.updateSinglePosition(data));
        messageRouter.registerHandler('trade_opened', data => this.trade.handleTradeOpened(data.trade));
        messageRouter.registerHandler('trade_closed', data => this.trade.handleTradeClosed(data.trade));
        messageRouter.registerHandler('trade_history_update', data => this.trade.updateTradeHistory(data.trades));

        // Orders
        messageRouter.registerHandler('orders_update', data => this.trade.updateOpenOrders(data.orders));
        messageRouter.registerHandler('order_status_update', data => this.trade.updateOrderStatus(data.payload));
        messageRouter.registerHandler('order_fill_update', data => this.trade.handleOrderFill(data.fill || data.order));
        messageRouter.registerHandler('order_rejection', data => this.trade.handleOrderRejection(data.payload));
        
        // Account & Summaries
        messageRouter.registerHandler('account_summary_update', data => this.ui.updateAccountSummary(data.account));
        messageRouter.registerHandler('position_summary_update', data => this.ui.updatePositionSummary(data.summary));
        messageRouter.registerHandler('daily_summary_update', data => this.ui.updateDailySummary(data.summary));

        // OCR & Images
        messageRouter.registerHandler('image_grab', data => {
            console.log('🖼️ image_grab received:', {
                hasImageData: !!data.image_data,
                imageSize: data.image_data ? `${(data.image_data.length / 1024).toFixed(1)}KB` : 'N/A',
                timestamp: new Date().toLocaleTimeString()
            });
            this.ocr.updatePreviewImage(data.image_data, 'raw');
        });
        messageRouter.registerHandler('preview_frame', data => {
            console.log('🖼️ preview_frame received:', {
                hasImageData: !!data.image_data,
                imageSize: data.image_data ? `${(data.image_data.length / 1024).toFixed(1)}KB` : 'N/A',
                hasContent: !!data.content,
                confidence: data.confidence,
                timestamp: new Date().toLocaleTimeString()
            });
            this.ocr.updatePreviewImage(data.image_data, 'processed');
            if (data.content) this.ocr.updateRawOCRStream(data.content);
            if (data.confidence !== undefined) this.ui.updateConfidence(data.confidence);
        });
        messageRouter.registerHandler('raw_ocr', data => {
            if (data.content) this.ocr.updateRawOCRStream(data.content);
            if (data.confidence) this.ui.updateConfidence(data.confidence);
        });
        messageRouter.registerHandler('processed_ocr', data => {
            if (data.no_trade_data) this.ocr.clearProcessedOCRTable();
            else if (data.snapshots) this.ocr.updateProcessedOCRTable(data.snapshots, data.timestamp);
        });
        
        // Other
        messageRouter.registerHandler('roi_update', data => {
            if (data.roi) this.state.setCurrentROI(data.roi);
            this.ui.updateROIDisplay();
        });
        messageRouter.registerHandler('command_response_v2', data => this.ui.handleCommandResponse(data));
        messageRouter.registerHandler('command_response', data => {
            // Handle ROI updates from command responses (like monolith does)
            if (data.command_type === 'SET_ROI_ABSOLUTE' && data.status === 'success') {
                if (data.data) {
                    if (Array.isArray(data.data.roi) && data.data.roi.length === 4) {
                        this.state.setCurrentROI(data.data.roi);
                        this.ui.updateROIDisplay();
                        this.ui.addSystemMessage('✅ ROI updated from command response', 'success');
                    } else if (data.data.x1 !== undefined && data.data.y1 !== undefined && data.data.x2 !== undefined && data.data.y2 !== undefined) {
                        this.state.setCurrentROI([data.data.x1, data.data.y1, data.data.x2, data.data.y2]);
                        this.ui.updateROIDisplay();
                        this.ui.addSystemMessage('✅ ROI updated from command response', 'success');
                    }
                }
            }
            // Also handle ROI_ADJUST responses  
            if (data.command_type === 'ROI_ADJUST' && data.status === 'success') {
                if (data.data) {
                    if (Array.isArray(data.data.roi) && data.data.roi.length === 4) {
                        this.state.setCurrentROI(data.data.roi);
                        this.ui.updateROIDisplay();
                        this.ui.addSystemMessage('✅ ROI adjusted successfully', 'success');
                    }
                }
            }
        });
        
        // Performance & Latency
        messageRouter.registerHandler('latency_update', data => {
            if (data.latency_ms !== undefined) {
                this.status.updateLatency(data.latency_ms);
            }
        });
        
        console.log(`Message Handlers registered: ${messageRouter.getHandlerCount()} types.`);
    }

    handleInitialState(data) {
        console.log('Received initial state:', data);
        this.ui.addSystemMessage('🚀 Initial state received from backend', 'success');
        
        // Process different parts of the initial state
        if (data.trades) {
            this.trade.updateActiveTrades(data.trades);
            this.ui.addSystemMessage(`📊 Loaded ${data.trades.length} active positions`, 'info');
        }
        
        if (data.orders) {
            this.trade.updateOpenOrders(data.orders);
            this.ui.addSystemMessage(`📋 Loaded ${data.orders.length} open orders`, 'info');
        }
        
        if (data.account) {
            this.ui.updateAccountSummary(data.account);
            this.ui.addSystemMessage('💰 Account summary loaded', 'info');
        }
        
        if (data.config) {
            // Update configuration from backend
            this.ui.addSystemMessage('⚙️ Configuration synchronized', 'info');
        }
        
        if (data.health) {
            this.status.updateHealthStatus(data.health);
            this.ui.addSystemMessage('💚 System health status updated', 'info');
        }
    }
}