// core/dom-manager.js - DOM element caching and management
export class DOMManager {
    constructor() {
        this.elements = new Map();
        this.cacheElements();
    }
    
    get(elementId) {
        return this.elements.get(elementId) || document.getElementById(elementId);
    }
    
    cacheElements() {
        const commonElements = [
            'previewImage', 'imageModeToggle', 'roiBox', 
            'apiStatusDot', 'coreStatusDot', 'redisStatusDot', 'babysitterStatusDot', 'zmqStatusDot',
            'latencyDisplay', 'confidenceDisplay', 'accountValue', 'dayTotalPnL', 'buyingPower', 
            'buyingPowerLeft', 'buyingPowerUsageBar', 'positionsContainer', 'historicalTradesContainer', 
            'systemMessages', 'manualTradeSymbol', 'manualTradeShares', 'manualTradeAction',
            'ocrModeDisplay', 'streamOutput', 'historyContent', 'historyToggle'
        ];
        
        commonElements.forEach(id => {
            const element = document.getElementById(id);
            if (element) {
                this.elements.set(id, element);
            }
        });
        
        console.log(`DOMManager: Cached ${this.elements.size} elements`);
    }
    
    refreshCache() {
        this.elements.clear();
        this.cacheElements();
    }
}

// core/error-handler.js - Centralized error handling
export class ErrorHandler {
    constructor() {
        this.errorCount = 0;
        this.errorHistory = [];
        this.maxHistorySize = 100;
    }

    handleError(error, context = '', severity = 'error') {
        this.errorCount++;

        const errorInfo = {
            id: this.errorCount,
            message: error.message || error,
            context: context,
            severity: severity,
            timestamp: new Date().toISOString(),
            stack: error.stack || null
        };

        this.errorHistory.push(errorInfo);

        if (this.errorHistory.length > this.maxHistorySize) {
            this.errorHistory.shift();
        }

        const logMethod = severity === 'warning' ? console.warn :
                         severity === 'info' ? console.info : console.error;

        logMethod(`[${severity.toUpperCase()}] ${context}: ${errorInfo.message}`, error);

        this.updateErrorDisplay(errorInfo);

        if (severity === 'critical') {
            this.reportCriticalError(errorInfo);
        }
    }

    updateErrorDisplay(errorInfo) {
        // Update error indicator if it exists
        const errorIndicator = document.getElementById('errorIndicator');
        if (errorIndicator) {
            errorIndicator.textContent = `Errors: ${this.errorCount}`;
            errorIndicator.className = `error-indicator ${errorInfo.severity}`;
        }
    }

    reportCriticalError(errorInfo) {
        console.error('CRITICAL ERROR REPORTED:', errorInfo);
    }

    getErrorSummary() {
        const recent = this.errorHistory.slice(-10);
        const byCategory = recent.reduce((acc, error) => {
            acc[error.context] = (acc[error.context] || 0) + 1;
            return acc;
        }, {});

        return {
            total: this.errorCount,
            recent: recent.length,
            categories: byCategory
        };
    }
}

// core/performance-monitor.js - Performance monitoring
export class PerformanceMonitor {
    constructor() {
        this.metrics = new Map();
        this.isEnabled = false;
        this.intervals = [];
    }

    enable() {
        this.isEnabled = true;
    }

    disable() {
        this.isEnabled = false;
    }

    startTiming(label) {
        if (!this.isEnabled) return;

        this.metrics.set(label, {
            start: performance.now(),
            memory: this.getMemoryUsage()
        });
    }

    endTiming(label) {
        if (!this.isEnabled) return;

        const metric = this.metrics.get(label);
        if (!metric) return;

        const duration = performance.now() - metric.start;
        const memoryAfter = this.getMemoryUsage();

        console.log(`⏱️ ${label}: ${duration.toFixed(2)}ms, Memory: ${memoryAfter.used - metric.memory.used}MB`);

        this.metrics.delete(label);
    }

    getMemoryUsage() {
        if (performance.memory) {
            return {
                used: Math.round(performance.memory.usedJSHeapSize / 1024 / 1024),
                total: Math.round(performance.memory.totalJSHeapSize / 1024 / 1024),
                limit: Math.round(performance.memory.jsHeapSizeLimit / 1024 / 1024)
            };
        }
        return { used: 0, total: 0, limit: 0 };
    }

    logPerformanceReport() {
        if (!this.isEnabled) return;

        const memory = this.getMemoryUsage();
        console.group('📊 Performance Report');
        console.log(`Memory Usage: ${memory.used}MB / ${memory.total}MB (Limit: ${memory.limit}MB)`);
        console.groupEnd();
    }

    getReport() {
        return {
            memory: this.getMemoryUsage(),
            enabled: this.isEnabled,
            activeMetrics: this.metrics.size
        };
    }

    cleanup() {
        this.intervals.forEach(interval => clearInterval(interval));
        this.intervals = [];
        this.metrics.clear();
    }
}

// core/image-manager.js - Image memory management
export class ImageManager {
    constructor(config) {
        this.config = config;
        this.imageCache = new Map();
        this.maxCacheSize = 10;
        this.totalMemoryUsed = 0;
        this.maxMemoryMB = config.memoryWarningThresholdMB || 50;
    }

    addImage(key, imageData, type = 'raw') {
        if (this.imageCache.size >= this.maxCacheSize) {
            this.cleanupOldestImages(3);
        }

        const estimatedSize = imageData.length * 0.75;

        if (this.totalMemoryUsed + estimatedSize > this.maxMemoryMB * 1024 * 1024) {
            this.forceCleanup();
        }

        this.imageCache.set(key, {
            data: imageData,
            type: type,
            timestamp: Date.now(),
            size: estimatedSize
        });

        this.totalMemoryUsed += estimatedSize;
    }

    cleanupOldestImages(count) {
        const sortedEntries = Array.from(this.imageCache.entries())
            .sort((a, b) => a[1].timestamp - b[1].timestamp);

        for (let i = 0; i < Math.min(count, sortedEntries.length); i++) {
            const [key, entry] = sortedEntries[i];
            this.totalMemoryUsed -= entry.size;
            this.imageCache.delete(key);
        }
    }

    forceCleanup() {
        console.warn('Force cleaning image cache due to memory pressure');
        this.cleanupOldestImages(Math.floor(this.imageCache.size / 2));
    }

    getImage(key) {
        return this.imageCache.get(key);
    }

    getMemoryUsage() {
        return {
            totalMB: this.totalMemoryUsed / 1024 / 1024,
            cacheSize: this.imageCache.size,
            maxCacheSize: this.maxCacheSize
        };
    }

    performCleanup() {
        const memoryInfo = this.getMemoryUsage();
        if (memoryInfo.totalMB > this.maxMemoryMB * 0.8) {
            this.cleanupOldestImages(5);
            console.log(`🧹 Periodic cleanup: ${memoryInfo.totalMB.toFixed(1)}MB → ${this.getMemoryUsage().totalMB.toFixed(1)}MB`);
        }
    }

    cleanup() {
        this.imageCache.clear();
        this.totalMemoryUsed = 0;
    }

    getMemoryInfo() {
        return this.getMemoryUsage();
    }
}

// core/message-router.js - WebSocket message routing
export class MessageRouter {
    constructor() {
        this.handlers = new Map();
    }

    registerHandler(messageType, handler) {
        this.handlers.set(messageType, handler);
    }

    route(data) {
        const handler = this.handlers.get(data.type);
        if (handler) {
            try {
                handler(data);
                return true;
            } catch (error) {
                console.error(`MessageRouter error for ${data.type}:`, error);
                return false;
            }
        }
        
        if (data.type !== 'heartbeat') {
            console.warn(`MessageRouter: Unhandled message type: ${data.type}`);
        }
        return false;
    }

    getHandlerCount() {
        return this.handlers.size;
    }
}