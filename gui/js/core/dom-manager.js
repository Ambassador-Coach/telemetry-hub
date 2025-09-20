// core/dom-manager.js - DOM element caching and management
export class DOMManager {
    constructor() {
        this.elements = new Map();
        this.cacheElements();
    }
    
    get(elementId) {
        // Return cached element if available, otherwise lookup and cache
        let element = this.elements.get(elementId);
        if (!element) {
            element = document.getElementById(elementId);
            if (element) {
                this.elements.set(elementId, element);
            }
        }
        return element;
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