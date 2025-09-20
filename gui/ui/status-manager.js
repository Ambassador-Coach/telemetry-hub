// ui/status-manager.js - Status pills and health monitoring
export class StatusManager {
    constructor(dom, state) {
        this.dom = dom;
        this.state = state;
        this.statusPills = new Map();
        this.coreStatusManager = null;
        this.apiStatusManager = null;
    }

    initialize() {
        this.createStatusPills();
        this.initializeCoreStatusManager();
        this.initializeAPIStatusManager();
        console.log('StatusManager initialized');
    }

    createStatusPills() {
        const pillTypes = ['core', 'api', 'redis', 'babysitter', 'zmq'];
        
        pillTypes.forEach(type => {
            this.statusPills.set(type, new StatusPill(type, type.toUpperCase()));
        });
    }

    initializeCoreStatusManager() {
        this.coreStatusManager = new CoreStatusManager(this.state);
        this.coreStatusManager.start();
    }

    initializeAPIStatusManager() {
        this.apiStatusManager = new APIStatusManager(this.state);
    }

    updateHealthStatus(data) {
        if (!data.component) return;

        const component = data.component.toLowerCase();
        const status = this.mapHealthStatus(data.status);

        if (component === 'core' || component === 'applicationcore') {
            if (status === 'healthy') {
                this.coreStatusManager.updateHealth();
            } else {
                this.updateStatusPill('core', status, `Core: ${data.status}`);
            }
        } else if (this.statusPills.has(component)) {
            this.updateStatusPill(component, status, `${component}: ${data.status}`);
        }
    }

    updateCoreStatus(data) {
        if (data.status_event) {
            const event = data.status_event;
            const status = this.mapCoreEventToStatus(event);
            
            if (status === 'healthy') {
                this.coreStatusManager.updateHealth();
            } else {
                this.updateStatusPill('core', status, `Core: ${event}`);
            }
        }
    }

    updateStatusPill(type, status, tooltip) {
        const pill = this.statusPills.get(type);
        if (pill) {
            pill.update(status, tooltip);
        }
    }

    mapHealthStatus(status) {
        switch (status?.toLowerCase()) {
            case 'healthy':
            case 'running':
                return 'healthy';
            case 'degraded':
                return 'warning';
            default:
                return 'error';
        }
    }

    mapCoreEventToStatus(event) {
        if (event.includes('RUNNING') || event.includes('STARTED') || event === 'OCR_ACTIVE') {
            return 'healthy';
        } else if (event.includes('STOPPING') || event.includes('DEGRADED')) {
            return 'warning';
        } else {
            return 'error';
        }
    }

    setAPIStatus(connected) {
        this.apiStatusManager.setConnected(connected);
    }

    onReconnectAttempt() {
        this.apiStatusManager.onReconnectAttempt();
    }
}

class StatusPill {
    constructor(pillId, name) {
        this.pillId = pillId;
        this.name = name;
        this.element = document.getElementById(`${pillId}StatusDot`);
        this.lastStatus = null;
        this.lastUpdate = 0;
        this.updateTimer = null;
    }

    update(status, tooltip = '', immediate = false) {
        if (this.updateTimer) {
            clearTimeout(this.updateTimer);
            this.updateTimer = null;
        }

        const now = Date.now();
        if (this.lastStatus === status && (now - this.lastUpdate) < 1000 && !immediate) {
            return;
        }

        const delay = immediate ? 0 : 100;

        this.updateTimer = setTimeout(() => {
            this._applyUpdate(status, tooltip);
            this.lastStatus = status;
            this.lastUpdate = now;
            this.updateTimer = null;
        }, delay);
    }

    _applyUpdate(status, tooltip) {
        if (!this.element) return;

        const color = this._getStatusColor(status);
        this.element.style.background = color;

        if (tooltip && this.element.parentElement) {
            this.element.parentElement.title = tooltip;
        }
    }

    _getStatusColor(status) {
        switch (status) {
            case 'healthy': return 'var(--accent-primary)';
            case 'warning': return 'var(--accent-warning)';
            case 'error': return 'var(--accent-danger)';
            default: return 'var(--accent-danger)';
        }
    }
}

class CoreStatusManager {
    constructor(state) {
        this.state = state;
        this.lastHealthUpdate = Date.now(); // Initialize to current time
        this.timeoutMs = 15000;
        this.checkInterval = null;
    }

    start() {
        this.checkInterval = setInterval(() => {
            this.checkStatus();
        }, 10000);
        setTimeout(() => this.checkStatus(), 1000);
    }

    stop() {
        if (this.checkInterval) {
            clearInterval(this.checkInterval);
            this.checkInterval = null;
        }
    }

    updateHealth() {
        this.lastHealthUpdate = Date.now();
        this.state.setLastHealthUpdate(this.lastHealthUpdate);
    }

    checkStatus() {
        const now = Date.now();
        const timeSinceLastUpdate = now - this.lastHealthUpdate;

        if (timeSinceLastUpdate > this.timeoutMs) {
            // Core is considered unhealthy
            console.warn(`Core unhealthy: ${Math.round(timeSinceLastUpdate / 1000)}s since last update`);
        }
    }
}

class APIStatusManager {
    constructor(state) {
        this.state = state;
        this.connected = false;
        this.reconnectAttempts = 0;
    }

    setConnected(connected) {
        this.connected = connected;
        this.state.setConnected(connected);
        
        if (connected) {
            this.reconnectAttempts = 0;
        }
    }

    onReconnectAttempt() {
        this.reconnectAttempts++;
    }
}

// ocr/ocr-manager.js - OCR operations and image management
export class OCRManager {
    constructor(state, api, ui, imageManager) {
        this.state = state;
        this.api = api;
        this.ui = ui;
        this.imageManager = imageManager;
        this.latencyTracker = new LatencyTracker();
    }

    initialize() {
        console.log('OCRManager initialized');
    }

    updatePreviewImage(imageData, imageType = 'raw') {
        if (!imageData) return;

        console.log(`OCR image update: ${imageType}, ${(imageData.length / 1024).toFixed(1)}KB`);

        // Store in image manager
        const imageKey = `${imageType}_${Date.now()}`;
        this.imageManager.addImage(imageKey, imageData, imageType);

        // Update state
        if (imageType === 'raw') {
            this.state.setLastRawImage(imageData);
        } else {
            this.state.setLastProcessedImage(imageData);
        }

        // Display if current mode matches
        if (this.state.getImageMode() === imageType) {
            this.displayImage(imageData);
        }
    }

    displayImage(imageData) {
        const previewImage = this.ui.dom.get('previewImage');
        if (previewImage && imageData) {
            const src = imageData.startsWith('data:image') ? imageData : `data:image/png;base64,${imageData}`;
            previewImage.src = src;
        }
    }

    updateRawOCRStream(content) {
        const rawOcrStream = document.getElementById('rawOcrStream');
        if (rawOcrStream) {
            rawOcrStream.textContent = content;
        }
    }

    updateProcessedOCRTable(snapshots, timestamp) {
        const tableBody = document.getElementById('processedOcrTableBody');
        if (!tableBody) return;

        if (!snapshots || Object.keys(snapshots).length === 0) {
            tableBody.innerHTML = '<tr><td colspan="6" style="text-align: center; color: #888;">No trade data detected</td></tr>';
            return;
        }

        tableBody.innerHTML = '';

        Object.entries(snapshots).forEach(([symbol, data]) => {
            const row = document.createElement('tr');
            row.innerHTML = `
                <td>${data.strategy || 'UNKNOWN'}</td>
                <td>${symbol}</td>
                <td>${data.quantity || 0}</td>
                <td>${this.ui.formatCurrency(data.avg_price || 0)}</td>
                <td class="${this.ui.getPnLClass(data.realized_pnl || 0)}">${this.ui.formatCurrency(data.realized_pnl || 0)}</td>
                <td style="font-size: 10px; color: var(--text-secondary);">${this.ui.formatTime(timestamp)}</td>
            `;
            tableBody.appendChild(row);
        });
    }

    clearProcessedOCRTable() {
        this.updateProcessedOCRTable({});
    }

    handleOCRUpdate(data) {
        // Track frame for synchronization if needed
        if (data.frame_number !== undefined && data.timestamp) {
            console.log(`OCR frame ${data.frame_number} at ${new Date(data.timestamp).toLocaleTimeString()}`);
        }

        // Update latency tracking
        this.latencyTracker.trackMessage();
    }

    switchImageMode(targetMode = null) {
        let newMode;

        if (targetMode) {
            newMode = targetMode;
        } else {
            newMode = this.state.getImageMode() === 'raw' ? 'processed' : 'raw';
        }

        this.state.setImageMode(newMode);

        // Display appropriate image
        const imageData = newMode === 'raw' ? 
            this.state.getLastRawImage() : 
            this.state.getLastProcessedImage();

        if (imageData) {
            this.displayImage(imageData);
            this.ui.addSystemMessage(`Switched to ${newMode} image mode`, 'info');
        } else {
            this.ui.addSystemMessage(`Switched to ${newMode} image mode (no image available)`, 'warning');
        }

        // Update toggle button if it exists
        const toggleButton = document.getElementById('imageModeToggle');
        if (toggleButton) {
            toggleButton.textContent = newMode === 'raw' ? 'Raw' : 'Processed';
        }
    }

    async toggleOCR() {
        try {
            const result = await this.api.toggleOCR();
            
            if (result.success) {
                const action = this.state.isOCRActive() ? 'stop' : 'start';
                this.ui.addSystemMessage(`OCR ${action} command sent`, 'info');
            } else {
                throw new Error(result.error || 'Failed to toggle OCR');
            }
        } catch (error) {
            console.error('Failed to toggle OCR:', error);
            this.ui.addSystemMessage(`Failed to toggle OCR: ${error.message}`, 'error');
        }
    }

    async updateOCRSettings(settings) {
        try {
            const result = await this.api.updateOCRSettings(settings);
            
            if (result.success) {
                this.ui.addSystemMessage('OCR settings updated', 'success');
            } else {
                throw new Error(result.error || 'Failed to update OCR settings');
            }
        } catch (error) {
            console.error('Failed to update OCR settings:', error);
            this.ui.addSystemMessage(`Failed to update OCR settings: ${error.message}`, 'error');
        }
    }

    getPublicAPI() {
        return {
            switchImageMode: this.switchImageMode.bind(this),
            toggleOCR: this.toggleOCR.bind(this),
            updateOCRSettings: this.updateOCRSettings.bind(this),
            getImageMode: () => this.state.getImageMode()
        };
    }
}

class LatencyTracker {
    constructor() {
        this.lastMessageTime = 0;
        this.messageCount = 0;
        this.latencySamples = [];
        this.maxSamples = 10;
    }

    trackMessage() {
        const now = performance.now();
        if (this.lastMessageTime > 0) {
            const timeSinceLastMessage = now - this.lastMessageTime;
            if (timeSinceLastMessage < 5000) {
                this.latencySamples.push(timeSinceLastMessage);
                if (this.latencySamples.length > this.maxSamples) {
                    this.latencySamples.shift();
                }
            }
        }
        this.lastMessageTime = now;
        this.messageCount++;
    }

    getAverageLatency() {
        if (this.latencySamples.length === 0) return null;
        return this.latencySamples.reduce((a, b) => a + b, 0) / this.latencySamples.length;
    }
}

// config/config-manager.js - Configuration management
export class ConfigManager {
    constructor(api, ui) {
        this.api = api;
        this.ui = ui;
    }

    async loadConfiguration() {
        try {
            const config = await this.api.loadConfiguration();
            
            if (config) {
                this.applyConfigurationToUI(config);
                this.ui.addSystemMessage('✅ Configuration loaded', 'success');
            }
            
            return config;
        } catch (error) {
            console.error('Failed to load configuration:', error);
            this.ui.addSystemMessage('❌ Failed to load configuration', 'error');
            return null;
        }
    }

    async saveConfiguration() {
        try {
            const result = await this.api.saveConfiguration();
            
            if (result.success) {
                this.ui.addSystemMessage('✅ Configuration saved', 'success');
            } else {
                throw new Error(result.error || 'Save failed');
            }
            
            return result;
        } catch (error) {
            console.error('Failed to save configuration:', error);
            this.ui.addSystemMessage('❌ Failed to save configuration', 'error');
            return { success: false, error: error.message };
        }
    }

    applyConfigurationToUI(config) {
        // Apply trading settings
        if (config.initial_share_size !== undefined) {
            this.setElementValue('initialShares', config.initial_share_size);
        }
        if (config.add_type !== undefined) {
            this.setElementValue('addType', config.add_type);
        }
        if (config.reduce_percentage !== undefined) {
            this.setElementValue('reducePercent', config.reduce_percentage);
        }
        if (config.manual_shares !== undefined) {
            this.setElementValue('manualShares', config.manual_shares);
        }

        // Apply development settings
        if (config.development_mode !== undefined) {
            this.setElementValue('developmentMode', config.development_mode);
        }

        // Apply OCR settings
        this.applyOCRSettings(config);
    }

    applyOCRSettings(config) {
        const ocrMappings = {
            'upscaleFactor': 'ocr_upscale_factor',
            'unsharpStrength': 'ocr_unsharp_strength',
            'thresholdC': 'ocr_threshold_c',
            'redBoost': 'ocr_red_boost',
            'greenBoost': 'ocr_green_boost',
            'forceBlackText': 'ocr_force_black_text_on_white',
            'textMaskCleaning': 'ocr_apply_text_mask_cleaning',
            'enhanceSmallSymbols': 'ocr_enhance_small_symbols'
        };

        Object.entries(ocrMappings).forEach(([elementId, configKey]) => {
            if (config[configKey] !== undefined) {
                this.setElementValue(elementId, config[configKey]);
                
                // Update display values for sliders
                if (elementId.includes('Factor') || elementId.includes('Strength') || elementId.includes('Boost')) {
                    const displayElement = document.getElementById(elementId.replace(/([A-Z])/g, '$1') + 'Value');
                    if (displayElement) {
                        displayElement.textContent = config[configKey];
                    }
                }
            }
        });
    }

    setElementValue(elementId, value) {
        const element = document.getElementById(elementId);
        if (element) {
            if (element.type === 'checkbox') {
                element.checked = Boolean(value);
            } else {
                element.value = value;
            }
        }
    }

    async updateConfigValue(key, value) {
        try {
            await this.api.updateConfig(key, value);
            this.ui.addSystemMessage(`✅ ${key} updated`, 'success');
        } catch (error) {
            console.error(`Failed to update ${key}:`, error);
            this.ui.addSystemMessage(`❌ Failed to update ${key}`, 'error');
        }
    }
}

// events/event-manager.js - Event handling and keyboard shortcuts
export class EventManager {
    constructor(app) {
        this.app = app;
    }

    setupEventListeners() {
        this.setupKeyboardShortcuts();
        this.setupConfigAutoSave();
        this.setupOCRSliderListeners();
        console.log('Event listeners initialized');
    }

    setupKeyboardShortcuts() {
        document.addEventListener('keydown', (e) => {
            if (e.ctrlKey || e.metaKey) {
                switch(e.key.toLowerCase()) {
                    case 'm': 
                        e.preventDefault(); 
                        this.handleManualAdd(); 
                        break;
                    case 'f': 
                        e.preventDefault(); 
                        this.handleForceClose(); 
                        break;
                    case 'o': 
                        e.preventDefault(); 
                        this.app.ocr.toggleOCR(); 
                        break;
                    case 'e': 
                        e.preventDefault(); 
                        this.handleEmergencyStop(); 
                        break;
                    case 'i': 
                        e.preventDefault(); 
                        this.app.ocr.switchImageMode(); 
                        break;
                }
            }
        });
    }

    setupConfigAutoSave() {
        // Trading configuration
        ['initialShares', 'addType', 'reducePercent', 'manualShares'].forEach(id => {
            const element = document.getElementById(id);
            if (element) {
                element.addEventListener('change', () => this.onConfigChange(id));
            }
        });

        // Development settings
        ['developmentMode', 'enableImageRecording'].forEach(id => {
            const element = document.getElementById(id);
            if (element) {
                element.addEventListener('change', () => this.onDevelopmentSettingChange(id));
            }
        });
    }

    setupOCRSliderListeners() {
        const sliders = [
            'upscaleFactor', 'unsharpStrength', 'thresholdC', 
            'redBoost', 'greenBoost'
        ];

        sliders.forEach(id => {
            const slider = document.getElementById(id);
            if (slider) {
                slider.addEventListener('input', () => {
                    const valueDisplay = document.getElementById(id + 'Value');
                    if (valueDisplay) {
                        valueDisplay.textContent = slider.value;
                    }
                });
                
                slider.addEventListener('change', () => this.onOCRSettingChange());
            }
        });
    }

    async handleManualAdd() {
        const sharesEl = document.getElementById('manualShares');
        if (!sharesEl) return;

        const shares = parseInt(sharesEl.value);
        if (isNaN(shares) || shares <= 0) {
            this.app.ui.addSystemMessage('Invalid share quantity', 'error');
            return;
        }

        try {
            const result = await this.app.api.executeManualTrade('add', shares);
            if (result.success) {
                this.app.ui.addSystemMessage(`✅ Manual ADD trade sent: ${shares} shares`, 'success');
            } else {
                throw new Error(result.error || 'Trade failed');
            }
        } catch (error) {
            console.error('Manual trade error:', error);
            this.app.ui.addSystemMessage(`❌ Manual trade failed: ${error.message}`, 'error');
        }
    }

    async handleForceClose() {
        if (!confirm('Are you sure you want to force close all positions?')) return;

        try {
            const result = await this.app.api.executeForceCloseAll();
            if (result.success) {
                this.app.ui.addSystemMessage('✅ Force close command sent', 'success');
            } else {
                throw new Error(result.error || 'Force close failed');
            }
        } catch (error) {
            console.error('Force close error:', error);
            this.app.ui.addSystemMessage(`❌ Force close failed: ${error.message}`, 'error');
        }
    }

    async handleEmergencyStop() {
        if (!confirm('EMERGENCY STOP - Are you absolutely sure?')) return;

        try {
            const result = await this.app.api.executeEmergencyStop();
            if (result.success) {
                this.app.ui.addSystemMessage('🚨 EMERGENCY STOP INITIATED', 'error');
            } else {
                throw new Error(result.error || 'Emergency stop failed');
            }
        } catch (error) {
            console.error('Emergency stop error:', error);
            this.app.ui.addSystemMessage(`❌ Emergency stop failed: ${error.message}`, 'error');
        }
    }

    onConfigChange(elementId) {
        const element = document.getElementById(elementId);
        if (!element) return;

        const configMappings = {
            'initialShares': 'initial_share_size',
            'addType': 'add_type',
            'reducePercent': 'reduce_percentage',
            'manualShares': 'manual_shares'
        };

        const configKey = configMappings[elementId];
        if (configKey) {
            const value = element.type === 'number' ? parseInt(element.value) : element.value;
            this.app.configManager.updateConfigValue(configKey, value);
        }
    }

    onDevelopmentSettingChange(elementId) {
        const element = document.getElementById(elementId);
        if (!element) return;

        if (elementId === 'developmentMode') {
            this.app.api.updateDevelopmentMode(element.checked);
        }
    }

    onOCRSettingChange() {
        // Collect all OCR settings and update
        const settings = this.collectOCRSettings();
        this.app.ocr.updateOCRSettings(settings);
    }

    collectOCRSettings() {
        return {
            ocr_upscale_factor: parseFloat(document.getElementById('upscaleFactor')?.value || 3.0),
            ocr_unsharp_strength: parseFloat(document.getElementById('unsharpStrength')?.value || 1.7),
            ocr_threshold_c: parseInt(document.getElementById('thresholdC')?.value || -6),
            ocr_red_boost: parseFloat(document.getElementById('redBoost')?.value || 1.0),
            ocr_green_boost: parseFloat(document.getElementById('greenBoost')?.value || 1.0),
            ocr_force_black_text_on_white: document.getElementById('forceBlackText')?.checked || true,
            ocr_apply_text_mask_cleaning: document.getElementById('textMaskCleaning')?.checked || true,
            ocr_enhance_small_symbols: document.getElementById('enhanceSmallSymbols')?.checked || true
        };
    }
}