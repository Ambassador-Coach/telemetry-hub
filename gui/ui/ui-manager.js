// ui/ui-manager.js - UI updates and display management
export class UIManager {
    constructor(dom, state) {
        this.dom = dom;
        this.state = state;
        this.hierarchyState = {
            expandedSections: new Set(),
            lastUpdateTime: Date.now()
        };
    }

    initialize() {
        console.log('UIManager initialized');
    }

    // System messages
    addSystemMessage(message, type = 'info') {
        const timestamp = this.formatTime();
        const color = this.getMessageColor(type);

        const messagesElement = this.dom.get('systemMessages');
        if (messagesElement) {
            messagesElement.innerHTML += `<span style="color: ${color}">[${timestamp}] ${message}</span><br>`;
            messagesElement.scrollTop = messagesElement.scrollHeight;
        }
    }

    // Update time display
    updateTime() {
        const el = this.dom.get('lastUpdate');
        if (el) el.textContent = this.formatTime();
    }

    // Initialize preview image
    initializePreview() {
        const previewImage = this.dom.get('previewImage');
        if (previewImage) {
            const canvas = document.createElement('canvas');
            canvas.width = 500;
            canvas.height = 120;
            const ctx = canvas.getContext('2d');

            ctx.fillStyle = '#1a1a2e';
            ctx.fillRect(0, 0, canvas.width, canvas.height);
            ctx.fillStyle = '#00ff88';
            ctx.font = '16px JetBrains Mono';
            ctx.textAlign = 'center';
            ctx.fillText('Waiting for OCR feed...', canvas.width/2, canvas.height/2 - 10);
            ctx.font = '12px JetBrains Mono';
            ctx.fillText('ROI controls active when connected.', canvas.width/2, canvas.height/2 + 15);

            previewImage.src = canvas.toDataURL();
        }
    }

    // Load initial ROI
    async loadInitialROI() {
        // This would be implemented to load ROI from backend
        // For now, just set default
        this.state.setCurrentROI([64, 159, 681, 296]);
        this.updateROIDisplay();
    }

    // Update ROI display
    updateROIDisplay() {
        const roiCoordsEl = document.getElementById('roiCoords');
        const currentROI = this.state.getCurrentROI();
        
        if (roiCoordsEl) {
            if (currentROI && currentROI.length === 4) {
                roiCoordsEl.textContent = currentROI.join(', ');
            } else {
                roiCoordsEl.textContent = 'N/A';
            }
        }

        const previewImage = this.dom.get('previewImage');
        const roiBox = this.dom.get('roiBox');

        if (previewImage && roiBox && previewImage.naturalWidth > 0 && currentROI?.length === 4) {
            const rect = previewImage.getBoundingClientRect();
            roiBox.style.left = '0px';
            roiBox.style.top = '0px';
            roiBox.style.width = `${rect.width}px`;
            roiBox.style.height = `${rect.height}px`;
            roiBox.style.border = '2px solid var(--accent-primary)';
            roiBox.style.boxShadow = '0 0 10px rgba(0, 255, 136, 0.3)';
            roiBox.style.backgroundColor = 'transparent';
        }
    }

    // Update OCR button state
    updateOCRButtonState(isActive) {
        const btn = document.getElementById('ocrToggle');
        if (!btn) return;

        this.state.setOCRActive(isActive);
        btn.disabled = false;

        if (isActive) {
            btn.textContent = 'Stop OCR';
            btn.className = 'btn btn-danger';
            btn.title = 'Click to stop OCR processing';
        } else {
            btn.textContent = 'Start OCR';
            btn.className = 'btn btn-primary';
            btn.title = 'Click to start OCR processing';
        }
    }

    // Update confidence display
    updateConfidence(confidence) {
        const confidencePercent = typeof confidence === 'number' ? Math.round(confidence * 100) / 100 : 0;
        
        const confidenceText = document.getElementById('confidenceText');
        const confidenceFill = document.getElementById('confidenceFill');
        const ocrConfidence = document.getElementById('ocrConfidence');

        if (confidenceText) confidenceText.textContent = `${confidencePercent}%`;
        if (confidenceFill) confidenceFill.style.width = `${Math.min(confidencePercent, 100)}%`;
        if (ocrConfidence) ocrConfidence.textContent = `${confidencePercent}%`;
    }

    // Update latency display
    updateLatencyDisplay(latencyMs) {
        const latencyEl = this.dom.get('latencyDisplay');
        if (!latencyEl || !latencyMs || latencyMs <= 0) return;

        const rounded = Math.round(latencyMs);
        latencyEl.textContent = `${rounded}ms`;

        // Color code based on latency
        if (latencyMs < 100) {
            latencyEl.style.color = 'var(--accent-success)';
        } else if (latencyMs < 500) {
            latencyEl.style.color = 'var(--accent-warning)';
        } else {
            latencyEl.style.color = 'var(--accent-danger)';
        }
    }

    // Update positions display (structural changes)
    updatePositionsDisplay(openTrades) {
        console.log(`Rebuilding positions UI for ${openTrades.length} trades`);
        this.rebuildPositionsDOM(openTrades);
    }

    // Update live data only (no DOM rebuild)
    updateLiveDataOnly(openTrades) {
        const startTime = performance.now();

        openTrades.forEach(position => {
            const symbol = position.symbol;
            const tradeId = position.trade_id;

            const card = document.querySelector(`[data-symbol="${symbol}"][data-trade-id="${tradeId}"]`);
            if (!card) return;

            // Update all live data elements
            this.updateLiveElement(card, 'quantity', this.formatNumber(Math.abs(position.quantity || 0)));
            this.updateLiveElement(card, 'last-price', this.formatCurrency(position.last_price || 0));
            this.updateLivePnLElement(card, 'unrealized-pnl', position.unrealized_pnl || 0);
            this.updateLivePnLElement(card, 'realized-pnl', position.realized_pnl || 0);
        });

        const endTime = performance.now();
        console.log(`Live data updated in ${(endTime - startTime).toFixed(2)}ms`);
    }

    // Rebuild positions DOM
    rebuildPositionsDOM(openTrades) {
        const container = document.getElementById('openPositionsContainer');
        const noOpenMessage = document.getElementById('noOpenPositionsMessage');

        if (!container) {
            console.error("'openPositionsContainer' not found!");
            return;
        }

        if (openTrades.length === 0) {
            container.innerHTML = '';
            if (noOpenMessage) noOpenMessage.style.display = 'block';
            return;
        }

        if (noOpenMessage) noOpenMessage.style.display = 'none';

        // Store current expanded states
        const expandedStates = new Set();
        document.querySelectorAll('.hierarchy-content:not(.collapsed)').forEach(el => {
            expandedStates.add(el.id);
        });

        // Clear and rebuild
        container.innerHTML = '';

        // Group trades by hierarchy
        const groupedTrades = this.groupTradesByHierarchy(openTrades);

        // Render each group
        Object.values(groupedTrades).forEach(group => {
            const parentPosition = group.parent || group.children[0];
            if (!parentPosition) return;

            const card = this.createPositionCard(parentPosition, group.children);
            container.appendChild(card);
        });

        // Restore expanded states
        setTimeout(() => {
            expandedStates.forEach(id => {
                const element = document.getElementById(id);
                if (element) {
                    element.classList.remove('collapsed');
                    const button = element.closest('.trade-hierarchy-section')?.querySelector('.hierarchy-toggle');
                    if (button) {
                        button.textContent = '▲';
                        button.setAttribute('aria-expanded', 'true');
                    }
                }
            });
        }, 0);
    }

    // Group trades by hierarchy
    groupTradesByHierarchy(trades) {
        const groups = {};

        trades.forEach(trade => {
            const isGroupParent = trade.is_parent || (!trade.parent_trade_id && !trade.is_child);
            const groupId = isGroupParent ? trade.trade_id : (trade.parent_trade_id || trade.trade_id);

            if (!groups[groupId]) {
                groups[groupId] = { parent: null, children: [] };
            }

            if (isGroupParent) {
                groups[groupId].parent = trade;
            } else {
                groups[groupId].children.push(trade);
            }
        });

        return groups;
    }

    // Create position card
    createPositionCard(position, childPositions) {
        const card = document.createElement('div');
        card.className = 'position-summary-card';
        card.setAttribute('data-symbol', position.symbol);
        card.setAttribute('data-trade-id', position.trade_id);

        // Add profit/loss styling
        if (position.unrealized_pnl > 0) {
            card.className += ' profit-card';
        } else if (position.unrealized_pnl < 0) {
            card.className += ' loss-card';
        }

        const hierarchyId = `pos-${position.symbol.toLowerCase()}-${position.trade_id.replace(/[^a-z0-9]/g, '')}-hierarchy`;
        const isExpanded = this.hierarchyState.expandedSections.has(hierarchyId);

        card.innerHTML = this.createPositionCardHTML(position, hierarchyId, isExpanded);

        return card;
    }

    // Create position card HTML
    createPositionCardHTML(position, hierarchyId, isExpanded) {
        const toggleText = isExpanded ? '▲' : '▼';
        const collapsedClass = isExpanded ? '' : 'collapsed';

        return `
            <div class="position-summary-header">
                <div class="summary-section symbol-section primary-section">
                    <div class="summary-section-label">SYMBOL</div>
                    <div class="summary-section-value">${position.symbol}</div>
                    <div class="summary-section-subtitle">${position.strategy || 'MANUAL'}</div>
                </div>
                <div class="summary-section primary-section">
                    <div class="summary-section-label">QUANTITY</div>
                    <div class="summary-section-value" data-live="quantity">${this.formatNumber(Math.abs(position.quantity || 0))}</div>
                    <div class="summary-section-subtitle">shares</div>
                </div>
                <div class="summary-section primary-section">
                    <div class="summary-section-label">AVG COST</div>
                    <div class="summary-section-value" data-live="avg-price">${this.formatCurrency(position.average_price || 0)}</div>
                    <div class="summary-section-subtitle">per share</div>
                </div>
                <div class="summary-section primary-section">
                    <div class="summary-section-label">LAST PRICE</div>
                    <div class="summary-section-value" data-live="last-price">${this.formatCurrency(position.last_price || 0)}</div>
                    <div class="summary-section-subtitle">current</div>
                </div>
                <div class="summary-section pnl-section primary-section">
                    <div class="summary-section-label">UNREALIZED P&L</div>
                    <div class="summary-section-value ${this.getPnLClass(position.unrealized_pnl || 0)}" data-live="unrealized-pnl">${this.formatCurrency(position.unrealized_pnl || 0)}</div>
                    <div class="summary-section-subtitle">current</div>
                </div>
                <div class="summary-section pnl-section primary-section">
                    <div class="summary-section-label">REALIZED P&L</div>
                    <div class="summary-section-value ${this.getPnLClass(position.realized_pnl || 0)}" data-live="realized-pnl">${this.formatCurrency(position.realized_pnl || 0)}</div>
                    <div class="summary-section-subtitle">session</div>
                </div>
            </div>

            <div class="trade-hierarchy-section">
                <div class="hierarchy-header" onclick="window.TESTRADE.ui.toggleHierarchy('${hierarchyId}', event)">
                    <span class="hierarchy-title">Order Details & Fills</span>
                    <button class="hierarchy-toggle" aria-expanded="${isExpanded}">${toggleText}</button>
                </div>
                <div id="${hierarchyId}" class="hierarchy-content ${collapsedClass}">
                    <!-- Child trades/orders would be inserted here -->
                </div>
            </div>
        `;
    }

    // Toggle hierarchy section
    toggleHierarchy(hierarchyId, event) {
        if (event) {
            event.preventDefault();
            event.stopPropagation();
        }

        const content = document.getElementById(hierarchyId);
        if (!content) return;

        const isCollapsed = content.classList.contains('collapsed');
        content.classList.toggle('collapsed');

        const section = content.closest('.trade-hierarchy-section');
        const button = section?.querySelector('.hierarchy-toggle');
        if (button) {
            button.textContent = isCollapsed ? '▲' : '▼';
            button.setAttribute('aria-expanded', !isCollapsed);
        }

        if (isCollapsed) {
            this.hierarchyState.expandedSections.add(hierarchyId);
        } else {
            this.hierarchyState.expandedSections.delete(hierarchyId);
        }
    }

    // Update account summary
    updateAccountSummary(account) {
        if (!account) return;

        this.state.setAccountValue(account.account_value || 0);
        this.state.setBuyingPower(account.buying_power || 0);
        this.state.setDayPnL(account.day_pnl || 0);

        const accountValueEl = this.dom.get('accountValue');
        if (accountValueEl && account.account_value !== undefined) {
            accountValueEl.textContent = this.formatCurrency(account.account_value);
        }

        const buyingPowerEl = this.dom.get('buyingPower');
        if (buyingPowerEl && account.buying_power !== undefined) {
            buyingPowerEl.textContent = this.formatCurrency(account.buying_power);
        }

        const dayPnLEl = this.dom.get('dayTotalPnL');
        if (dayPnLEl && account.day_pnl !== undefined) {
            dayPnLEl.innerHTML = this.formatPnLWithClass(account.day_pnl);
        }

        this.updateBuyingPowerUsage();
    }

    // Update position summary
    updatePositionSummary(summary) {
        // Implementation for position summary updates
        console.log('Position summary updated:', summary);
    }

    // Update market status
    updateMarketStatus(status) {
        // Implementation for market status updates
        console.log('Market status updated:', status);
    }

    // Update historical trades display
    updateHistoricalTradesDisplay() {
        const container = this.dom.get('historicalTradesContainer');
        if (!container) return;

        const trades = this.state.getHistoricalTrades();
        const activeTab = this.state.getActiveHistoryTab();

        // Filter trades based on active tab
        let filteredTrades = this.filterTradesForTab(trades, activeTab);

        // Clear and populate
        container.innerHTML = '';

        if (filteredTrades.length === 0) {
            container.innerHTML = '<div class="no-trades">No trades found</div>';
            return;
        }

        filteredTrades.forEach(trade => {
            const tradeElement = this.createHistoricalTradeElement(trade);
            container.appendChild(tradeElement);
        });
    }

    // Update position counts
    updatePositionCounts(counts) {
        const openCountEl = this.dom.get('guiOpenPositionsCount');
        if (openCountEl) openCountEl.textContent = counts.openPositions;

        const unrealizedEl = this.dom.get('guiUnrealizedPnlTotal');
        if (unrealizedEl) {
            unrealizedEl.innerHTML = `Unrealized: <strong>${this.formatCurrency(counts.unrealizedPnl)}</strong>`;
            unrealizedEl.className = this.getPnLClass(counts.unrealizedPnl);
        }
    }

    // Update closed position stats
    updateClosedPositionStats(stats) {
        const closedCountEl = document.getElementById('closedPositionsCount');
        if (closedCountEl) closedCountEl.textContent = stats.closedToday;

        const closedPnLEl = document.getElementById('closedPnL');
        if (closedPnLEl) {
            closedPnLEl.textContent = this.formatCurrency(stats.closedPnL);
            closedPnLEl.className = 'metric-value ' + this.getPnLClass(stats.closedPnL);
        }
    }

    // Update buying power usage
    updateBuyingPowerUsage() {
        const totalBP = this.state.getBuyingPower();
        const usedBP = this.calculateUsedBuyingPower();
        const availableBP = totalBP - usedBP;
        const usagePercent = totalBP > 0 ? (usedBP / totalBP) * 100 : 0;

        const bpAvailableEl = this.dom.get('buyingPowerLeft');
        if (bpAvailableEl) {
            bpAvailableEl.textContent = this.formatCurrency(Math.max(0, availableBP));
        }

        const bpUsageBar = this.dom.get('buyingPowerUsageBar');
        if (bpUsageBar) {
            bpUsageBar.style.width = Math.min(100, usagePercent) + '%';
        }
    }

    // Calculate used buying power
    calculateUsedBuyingPower() {
        const activeTrades = this.state.getActiveTrades();
        return activeTrades.reduce((total, trade) => {
            if (trade.is_open && trade.quantity && trade.average_price) {
                return total + (Math.abs(trade.quantity) * trade.average_price);
            }
            return total;
        }, 0);
    }

    // Helper methods
    updateLiveElement(card, dataType, newValue) {
        const element = card.querySelector(`[data-live="${dataType}"]`);
        if (element && element.textContent !== newValue) {
            element.textContent = newValue;
        }
    }

    updateLivePnLElement(card, dataType, value) {
        const element = card.querySelector(`[data-live="${dataType}"]`);
        if (element) {
            const formattedValue = this.formatCurrency(value);
            if (element.textContent !== formattedValue) {
                element.textContent = formattedValue;
                element.className = element.className.replace(/pnl-\w+/g, '') + ' ' + this.getPnLClass(value);
            }
        }
    }

    filterTradesForTab(trades, activeTab) {
        switch (activeTab) {
            case 'today':
                const today = new Date().toDateString();
                return trades.filter(trade => {
                    const tradeDate = new Date(trade.timestamp).toDateString();
                    return tradeDate === today;
                });
            case 'winners':
                return trades.filter(trade => (trade.pnl || trade.realized_pnl || 0) > 0);
            case 'losers':
                return trades.filter(trade => (trade.pnl || trade.realized_pnl || 0) < 0);
            case 'all_time':
            default:
                return trades;
        }
    }

    createHistoricalTradeElement(trade) {
        const element = document.createElement('div');
        element.className = 'historical-trade-item';
        element.innerHTML = `
            <div class="trade-symbol">${trade.symbol}</div>
            <div class="trade-action">${trade.action}</div>
            <div class="trade-quantity">${this.formatNumber(trade.quantity)}</div>
            <div class="trade-price">${this.formatCurrency(trade.price)}</div>
            <div class="trade-pnl ${this.getPnLClass(trade.pnl)}">${this.formatCurrency(trade.pnl)}</div>
            <div class="trade-time">${this.formatTime(trade.timestamp)}</div>
        `;
        return element;
    }

    // Formatting utilities
    formatTime(timestamp) {
        if (!timestamp) return new Date().toLocaleTimeString();
        return new Date(timestamp).toLocaleTimeString();
    }

    formatCurrency(value) {
        if (value === null || value === undefined || isNaN(parseFloat(value))) return '$0.00';
        const num = parseFloat(value);
        return `$${num.toFixed(2)}`;
    }

    formatNumber(value) {
        if (value === null || value === undefined || isNaN(parseFloat(value))) return '0';
        return parseFloat(value).toLocaleString();
    }

    formatPnLWithClass(value) {
        const formatted = this.formatCurrency(value);
        const className = this.getPnLClass(value);
        return `<span class="${className}">${formatted}</span>`;
    }

    getPnLClass(value) {
        const numValue = parseFloat(value) || 0;
        if (numValue > 0) return 'pnl-positive';
        if (numValue < 0) return 'pnl-negative';
        return 'pnl-neutral';
    }

    getMessageColor(type) {
        switch (type) {
            case 'error': return 'var(--accent-danger)';
            case 'warning': return 'var(--accent-warning)';
            case 'success': return 'var(--accent-primary)';
            default: return 'var(--text-secondary)';
        }
    }

    // Public API
    getPublicAPI() {
        return {
            addSystemMessage: this.addSystemMessage.bind(this),
            updateTime: this.updateTime.bind(this),
            updateOCRButtonState: this.updateOCRButtonState.bind(this),
            updateConfidence: this.updateConfidence.bind(this),
            updateLatencyDisplay: this.updateLatencyDisplay.bind(this),
            updatePositionsDisplay: this.updatePositionsDisplay.bind(this),
            updateLiveDataOnly: this.updateLiveDataOnly.bind(this),
            updateAccountSummary: this.updateAccountSummary.bind(this),
            updateHistoricalTradesDisplay: this.updateHistoricalTradesDisplay.bind(this),
            toggleHierarchy: this.toggleHierarchy.bind(this),
            formatCurrency: this.formatCurrency.bind(this),
            formatNumber: this.formatNumber.bind(this)
        };
    }
}