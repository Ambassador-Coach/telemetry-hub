// trading/trade-manager.js - Trading operations and state management
export class TradeManager {
    constructor(state, api, ui) {
        this.state = state;
        this.api = api;
        this.ui = ui;
        this.updateStrategy = {
            lastStructuralHash: null,
            lastDataUpdate: 0
        };
    }

    initialize() {
        console.log('TradeManager initialized');
    }

    // Main method to update active trades
    updateActiveTrades(trades) {
        try {
            if (!Array.isArray(trades)) {
                console.warn('updateActiveTrades called with non-array:', trades);
                trades = [];
            }

            const normalizedTrades = trades
                .map(trade => this.normalizeTradeData(trade))
                .filter(trade => trade !== null);

            const openTrades = normalizedTrades.filter(trade => this.isTradeOpen(trade));
            const newStructuralHash = this.createStructuralHash(openTrades);

            // Update state
            this.state.setActiveTrades(openTrades);

            // Check if structural change occurred
            if (this.updateStrategy.lastStructuralHash !== newStructuralHash) {
                console.log(`Structural change detected. Rebuilding UI for ${openTrades.length} trades.`);
                this.ui.updatePositionsDisplay(openTrades);
                this.updateStrategy.lastStructuralHash = newStructuralHash;
            } else {
                // Just update live data
                this.ui.updateLiveDataOnly(openTrades);
            }

            this.updatePositionTotals(openTrades);
            this.updateStrategy.lastDataUpdate = Date.now();

        } catch (error) {
            console.error('Error updating active trades:', error);
        }
    }

    // Update a single position
    updateSinglePosition(data) {
        if (!data.symbol || !data.position) return;

        const activeTrades = this.state.getActiveTrades();
        const existingIndex = activeTrades.findIndex(t => t.symbol === data.symbol);

        if (data.is_open) {
            if (existingIndex >= 0) {
                // Update existing position but preserve market data
                const existing = activeTrades[existingIndex];
                data.position.bid_price = data.position.bid_price || existing.bid_price;
                data.position.ask_price = data.position.ask_price || existing.ask_price;
                data.position.last_price = data.position.last_price || existing.last_price;
                activeTrades[existingIndex] = data.position;
            } else {
                // Add new position
                activeTrades.push(data.position);
            }
        } else {
            // Remove closed position
            if (existingIndex >= 0) {
                activeTrades.splice(existingIndex, 1);
            }
        }

        this.updateActiveTrades(activeTrades);
    }

    // Update open orders
    updateOpenOrders(orders) {
        if (Array.isArray(orders)) {
            const normalizedOrders = orders.map(order => this.normalizeOrderData(order));
            this.state.setOpenOrders(normalizedOrders);
            console.log(`Updated open orders: ${normalizedOrders.length} orders`);
        }
    }

    // Update order status
    updateOrderStatus(orderData) {
        const orders = this.state.getOpenOrders();
        const orderId = orderData.order_id || orderData.local_order_id;
        const existingIndex = orders.findIndex(order => 
            order.order_id === orderId || order.local_order_id === orderId
        );

        const classification = this.classifyTradeStatus(orderData);

        if (classification.shouldShowInOpen) {
            if (existingIndex >= 0) {
                orders[existingIndex] = { ...orders[existingIndex], ...orderData };
            } else {
                orders.push(orderData);
            }
        } else {
            // Remove closed order
            if (existingIndex >= 0) {
                orders.splice(existingIndex, 1);
            }
        }

        this.state.setOpenOrders(orders);
    }

    // Handle order fill
    handleOrderFill(fillData) {
        // Update order status
        this.updateOrderStatus(fillData);
        
        // Add to historical trades
        const historicalTrade = this.convertFillToHistoricalTrade(fillData);
        this.state.addHistoricalTrade(historicalTrade);
        
        const fillQty = fillData.filled_quantity || fillData.quantity || 0;
        const fillPrice = fillData.fill_price || fillData.price || 0;
        this.ui.addSystemMessage(
            `💰 Order fill: ${fillData.symbol} ${fillQty} @ ${this.ui.formatCurrency(fillPrice)}`,
            'info'
        );
    }

    // Handle trade opened
    handleTradeOpened(tradeData) {
        const historicalTrade = this.convertTradeToHistoricalTrade(tradeData, 'OPEN');
        this.state.addHistoricalTrade(historicalTrade);
        
        this.ui.addSystemMessage(
            `Trade opened: ${tradeData.symbol} ${tradeData.side} ${tradeData.quantity}`,
            'success'
        );
    }

    // Handle trade closed
    handleTradeClosed(tradeData) {
        const historicalTrade = this.convertTradeToHistoricalTrade(tradeData, 'CLOSED');
        this.state.addHistoricalTrade(historicalTrade);
        
        const pnl = tradeData.realized_pnl || 0;
        this.ui.addSystemMessage(
            `Trade closed: ${tradeData.symbol} P&L ${this.ui.formatCurrency(pnl)}`,
            pnl >= 0 ? 'success' : 'warning'
        );
    }

    // Update trade history
    updateTradeHistory(trades) {
        if (Array.isArray(trades)) {
            if (trades.length <= 2) {
                // Incremental update
                trades.forEach(trade => {
                    const normalizedTrade = this.normalizeHistoricalTrade(trade);
                    this.state.addHistoricalTrade(normalizedTrade);
                });
            } else {
                // Bulk update
                const normalizedTrades = trades.map(trade => this.normalizeHistoricalTrade(trade));
                this.state.setHistoricalTrades(normalizedTrades);
            }
            
            this.ui.updateHistoricalTradesDisplay();
        }
    }

    // Update historical trades
    updateHistoricalTrades(trades) {
        this.updateTradeHistory(trades);
    }

    // Normalize trade data
    normalizeTradeData(rawTrade) {
        if (!rawTrade || typeof rawTrade !== 'object') {
            console.warn('Invalid trade data received:', rawTrade);
            return null;
        }

        return {
            // Core identifiers
            symbol: (rawTrade.symbol || 'UNKNOWN').toString().toUpperCase(),
            trade_id: rawTrade.trade_id || rawTrade.position_uuid || this.generateTradeId(),
            parent_trade_id: rawTrade.parent_trade_id,

            // Quantities and pricing
            quantity: parseFloat(rawTrade.quantity || 0),
            average_price: parseFloat(rawTrade.average_price || rawTrade.avg_price || rawTrade.price || 0),
            last_price: parseFloat(rawTrade.last_price || rawTrade.current_price || 0),
            bid_price: parseFloat(rawTrade.bid_price || rawTrade.bid || 0),
            ask_price: parseFloat(rawTrade.ask_price || rawTrade.ask || 0),

            // P&L
            realized_pnl: parseFloat(rawTrade.realized_pnl || 0),
            unrealized_pnl: parseFloat(rawTrade.unrealized_pnl || 0),
            pnl_per_share: parseFloat(rawTrade.pnl_per_share || 0),

            // Status and flags
            status: (rawTrade.status || 'UNKNOWN').toString().toUpperCase(),
            is_open: rawTrade.is_open !== undefined ? rawTrade.is_open : true,
            side: (rawTrade.side || rawTrade.action || 'UNKNOWN').toString().toUpperCase(),

            // Metadata
            strategy: rawTrade.strategy || 'UNKNOWN',
            timestamp: this.normalizeTimestamp(rawTrade.timestamp || Date.now()),
            is_parent: Boolean(rawTrade.is_parent),
            is_child: Boolean(rawTrade.is_child),

            // Market data availability flags
            market_data_available: Boolean(rawTrade.bid_price || rawTrade.ask_price || rawTrade.last_price),
            hierarchy_data_available: Boolean(rawTrade.parent_trade_id || rawTrade.is_parent || rawTrade.child_trades?.length)
        };
    }

    // Normalize order data
    normalizeOrderData(order) {
        return {
            local_id: order.local_id || 0,
            order_id: order.order_id || order.local_order_id,
            local_order_id: order.local_order_id,
            parent_trade_id: order.parent_trade_id,
            symbol: order.symbol || 'UNKNOWN',
            side: order.side || 'UNKNOWN',
            quantity: order.requested_shares || order.quantity || 0,
            price: order.avg_fill_price || order.requested_lmt_price || 0,
            status: order.ls_status || order.status || 'UNKNOWN',
            filled_quantity: order.filled_quantity || 0,
            remaining_quantity: order.leftover_shares || 0,
            order_type: order.order_type || 'LMT',
            timestamp: order.timestamp_requested || Date.now() / 1000,
            is_child: order.is_child || false
        };
    }

    // Normalize historical trade
    normalizeHistoricalTrade(trade) {
        const timestamp = trade.timestamp || 0;
        return {
            ...trade,
            timestamp: timestamp > 2000000000000 ? timestamp : timestamp * 1000, // Convert to ms if needed
            pnl: trade.pnl || trade.realized_pnl || 0
        };
    }

    // Convert fill to historical trade
    convertFillToHistoricalTrade(fillData) {
        const timestamp = fillData.fill_timestamp || fillData.timestamp || Date.now() / 1000;
        return {
            trade_id: fillData.order_id || fillData.trade_id || `fill-${Date.now()}`,
            symbol: fillData.symbol,
            action: (fillData.side || 'FILL').toUpperCase(),
            status: 'FILLED',
            quantity: fillData.filled_quantity || fillData.quantity || 0,
            price: fillData.fill_price || fillData.price || 0,
            timestamp: timestamp > 2000000000000 ? timestamp : timestamp * 1000,
            pnl: fillData.realized_pnl || 0,
            is_parent: fillData.is_parent || false,
            is_child: fillData.is_child !== undefined ? fillData.is_child : true,
            parent_trade_id: fillData.parent_trade_id,
            strategy: fillData.strategy,
            commission: fillData.commission || 0
        };
    }

    // Convert trade to historical trade
    convertTradeToHistoricalTrade(tradeData, actionType) {
        const timestamp = tradeData.close_timestamp || tradeData.open_timestamp || tradeData.timestamp || Date.now() / 1000;
        return {
            trade_id: tradeData.trade_id || `${actionType.toLowerCase()}-${Date.now()}`,
            symbol: tradeData.symbol,
            action: actionType,
            status: actionType === 'OPEN' ? 'OPEN' : 'CLOSED',
            quantity: tradeData.quantity,
            price: tradeData.close_price || tradeData.open_price || tradeData.price || 0,
            timestamp: timestamp > 2000000000000 ? timestamp : timestamp * 1000,
            pnl: tradeData.realized_pnl || 0,
            is_parent: tradeData.is_parent,
            is_child: tradeData.is_child,
            parent_trade_id: tradeData.parent_trade_id,
            strategy: tradeData.strategy || 'N/A',
            commission: tradeData.commission || 0
        };
    }

    // Check if trade is open
    isTradeOpen(trade) {
        if (trade.is_open !== undefined) {
            return trade.is_open === true;
        }
        
        const closedStatuses = ['CLOSED', 'FILLED', 'CANCELLED', 'REJECTED', 'FAILED', 'EXPIRED'];
        return !closedStatuses.includes((trade.status || 'UNKNOWN').toUpperCase());
    }

    // Classify trade status
    classifyTradeStatus(trade) {
        const status = (trade.status || '').toUpperCase();
        const isOpen = trade.is_open;

        const closedStatuses = ['CLOSED', 'FILLED', 'CANCELLED', 'REJECTED', 'FAILED', 'EXPIRED'];
        const openStatuses = ['OPEN', 'ACTIVE', 'PARTIALLY_FILLED', 'PENDING', 'SUBMITTED'];

        if (closedStatuses.includes(status)) {
            return {
                isOpen: false,
                category: 'closed',
                displayStatus: status,
                shouldShowInOpen: false,
                shouldShowInHistory: true
            };
        }

        if (openStatuses.includes(status)) {
            return {
                isOpen: true,
                category: 'open',
                displayStatus: status,
                shouldShowInOpen: true,
                shouldShowInHistory: false
            };
        }

        // Fallback to is_open flag
        if (isOpen === false) {
            return {
                isOpen: false,
                category: 'closed',
                displayStatus: status || 'CLOSED',
                shouldShowInOpen: false,
                shouldShowInHistory: true
            };
        }

        return {
            isOpen: true,
            category: 'open',
            displayStatus: status || 'OPEN',
            shouldShowInOpen: true,
            shouldShowInHistory: false
        };
    }

    // Create structural hash for change detection
    createStructuralHash(trades) {
        if (!trades || !Array.isArray(trades) || trades.length === 0) return 'empty';

        return trades.map(trade => {
            if (!trade) return 'null';

            return [
                trade.symbol || 'UNKNOWN',
                trade.trade_id || 'unknown',
                Math.round(trade.quantity || 0),
                trade.is_open !== undefined ? trade.is_open : true,
                trade.strategy || 'UNKNOWN',
                trade.is_parent || false,
                trade.is_child || false,
                trade.parent_trade_id || 'none'
            ].join('-');
        }).sort().join('|');
    }

    // Update position totals
    updatePositionTotals(openTrades) {
        const totalUnrealizedPnl = openTrades.reduce((sum, trade) => sum + (trade.unrealized_pnl || 0), 0);
        const openPositionsCount = openTrades.length;
        const totalShares = openTrades.reduce((sum, trade) => sum + Math.abs(trade.quantity || 0), 0);

        // Update UI elements
        this.ui.updatePositionCounts({
            openPositions: openPositionsCount,
            totalShares: totalShares,
            unrealizedPnl: totalUnrealizedPnl
        });

        // Update closed position stats
        this.updateClosedPositionStats();
    }

    // Update closed position statistics
    updateClosedPositionStats() {
        const historicalTrades = this.state.getHistoricalTrades();
        const todayTrades = historicalTrades.filter(trade => {
            const tradeDate = new Date(trade.timestamp);
            const today = new Date();
            return tradeDate.toDateString() === today.toDateString() && 
                   (trade.status === 'CLOSED' || trade.status === 'FILLED');
        });

        const stats = {
            closedToday: todayTrades.length,
            totalTrades: historicalTrades.length,
            closedPnL: todayTrades.reduce((sum, trade) => sum + (trade.realized_pnl || trade.pnl || 0), 0)
        };

        this.ui.updateClosedPositionStats(stats);
    }

    // Utility methods
    normalizeTimestamp(timestamp) {
        if (!timestamp) return Date.now();
        const ts = parseFloat(timestamp);
        return ts < 4000000000 ? ts * 1000 : ts; // Convert seconds to milliseconds if needed
    }

    generateTradeId() {
        return `trade_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`;
    }

    // Public API for external access
    getPublicAPI() {
        return {
            updateActiveTrades: this.updateActiveTrades.bind(this),
            updateSinglePosition: this.updateSinglePosition.bind(this),
            updateOpenOrders: this.updateOpenOrders.bind(this),
            updateTradeHistory: this.updateTradeHistory.bind(this),
            getActiveTrades: () => this.state.getActiveTrades(),
            getOpenOrders: () => this.state.getOpenOrders(),
            getHistoricalTrades: () => this.state.getHistoricalTrades()
        };
    }
}