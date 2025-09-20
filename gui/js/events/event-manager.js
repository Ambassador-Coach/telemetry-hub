// js/events/event-manager.js

export class EventManager {
    constructor(api, ui, trading, ocr, configManager) {
        this.api = api;
        this.ui = ui;
        this.trading = trading;
        this.ocr = ocr;
        this.configManager = configManager;
    }

    /**
     * The main entry point to set up all event listeners.
     * This is called once from main.js after the DOM is ready.
     */
    setupEventListeners() {
        console.log("Setting up all application event listeners...");

        // --- Core Action Buttons ---
        this.attachListener('ocrToggle', 'click', () => this.ocr.toggleOCR());
        this.attachListener('manualAddBtn', 'click', () => this.trading.executeManualTrade('add'));
        this.attachListener('forceCloseBtn', 'click', () => this.trading.executeForceCloseAll());
        this.attachListener('emergencyBtn', 'click', () => this.trading.executeEmergencyStop());
        
        // --- Image Mode Toggle ---
        this.attachListener('imageModeToggle', 'click', () => this.ocr.switchImageMode());
        
        // --- History Toggle ---
        this.attachListener('historyToggle', 'click', (e) => this.ui.toggleHistory(e));
        
        // --- Position Summary ---
        this.attachListener('requestPositionSummaryBtn', 'click', () => this.trading.requestPositionSummary());
        
        // --- Core Control Buttons ---
        this.attachListener('startCoreBtn', 'click', () => this.ui.startCore());
        this.attachListener('toggleRecordingBtn', 'click', () => this.ui.toggleRecording());
        this.attachListener('expandROIBtn', 'click', () => this.ocr.expandROI());
        this.attachListener('toggleAllSymbolsBtn', 'click', () => this.ui.toggleAllSymbols());
        
        // --- OCR Settings Buttons ---
        this.attachListener('applyOCRSettingsBtn', 'click', () => this.ocr.applyCurrentSettings());
        this.attachListener('resetOCRSettingsBtn', 'click', () => this.ocr.resetOCRSettings());
        this.attachListener('toggleConfidenceModeBtn', 'click', () => this.ocr.toggleConfidenceMode());
        this.attachListener('toggleVideoModeBtn', 'click', () => this.ocr.toggleVideoMode());
        
        // --- Development/Testing Buttons ---
        this.attachListener('togglePerfTrackingBtn', 'click', () => this.ui.togglePerfTracking());
        this.attachListener('exportPerfStatsBtn', 'click', () => this.ui.exportPerfStats());
        this.attachListener('resetTimeSyncBtn', 'click', () => this.ui.resetTimeSync());
        this.attachListener('testFunctionBtn', 'click', () => this.ui.testFunction());
        
        // --- Session Management Buttons ---
        this.attachListener('createSessionBtn', 'click', () => this.ui.createSession());
        this.attachListener('loadSessionDataBtn', 'click', () => this.ui.loadSessionData());
        this.attachListener('startReplayBtn', 'click', () => this.ui.startReplay());
        this.attachListener('stopReplayBtn', 'click', () => this.ui.stopReplay());
        
        // --- Configuration Buttons ---
        this.attachListener('saveConfigBtn', 'click', () => this.configManager.saveConfiguration());
        this.attachListener('loadConfigBtn', 'click', () => this.configManager.loadConfiguration());
        
        // --- ROI Control Buttons ---
        this.setupROIControls();
        
        // --- Auto-Save Listeners ---
        this.setupConfigAutoSave();
        this.setupOCRSliderListeners();
        
        // --- Keyboard Shortcuts ---
        this.setupKeyboardShortcuts();
        
        console.log('EventManager: Setup complete.');
    }

    /**
     * Sets up ROI control buttons
     */
    setupROIControls() {
        const roiButtons = document.querySelectorAll('.roi-btn-external');
        
        roiButtons.forEach(button => {
            button.addEventListener('click', (event) => {
                // Visual feedback
                button.style.transform = 'scale(0.95)';
                setTimeout(() => {
                    button.style.transform = 'scale(1)';
                }, 150);
                
                // ROI adjustment logic
                const axis = button.dataset.roi;
                const direction = button.dataset.direction;
                
                if (axis && direction) {
                    console.log(`🎯 ROI BUTTON: ${button.textContent} -> adjustROI(${axis}, ${direction})`);
                    console.log(`🔍 this.ocr exists:`, !!this.ocr);
                    console.log(`🔍 this.ocr.adjustROI exists:`, !!(this.ocr && this.ocr.adjustROI));
                    
                    if (this.ocr && this.ocr.adjustROI) {
                        this.ocr.adjustROI(axis, direction, event);
                    } else {
                        console.error(`❌ this.ocr or adjustROI method not available`);
                    }
                } else {
                    console.error(`❌ ROI BUTTON MISSING DATA: ${button.textContent}`);
                }
            });
        });
    }

    /**
     * Sets up listeners that automatically trigger a config save on change.
     */
    setupConfigAutoSave() {
        const tradingParamIds = ['initialShares', 'addType', 'reducePercent'];
        tradingParamIds.forEach(id => {
            this.attachListener(id, 'change', (event) => {
                const configKey = event.target.dataset.configKey;
                if(configKey) this.configManager.updateTradingParams({ [configKey]: event.target.value });
            });
        });

        const devSettingIds = ['developmentMode', 'enableImageRecording', 'enableRawOcrRecording', 'enableIntelliSenseLogging', 'enableObservabilityLogging', 'logLevel', 'enableOcrDebugLogging'];
        devSettingIds.forEach(id => {
            this.attachListener(id, 'change', (event) => {
                const el = event.target;
                const value = el.type === 'checkbox' ? el.checked : el.value;
                if(el.dataset.configKey) this.configManager.api.updateConfig(el.dataset.configKey, value);
            });
        });
    }

    /**
     * Sets up listeners for OCR sliders to provide real-time value feedback.
     */
    setupOCRSliderListeners() {
        // Map of slider IDs to their value display IDs
        const sliderMappings = {
            'upscaleFactor': 'upscaleFactorValue',
            'unsharpStrength': 'unsharpStrengthValue',
            'thresholdC': 'thresholdCValue',
            'redBoost': 'redBoostValue',
            'greenBoost': 'greenBoostValue',
            'textMaskMinContourArea': 'contourAreaValue',
            'textMaskMinWidth': 'maskWidthValue',
            'textMaskMinHeight': 'maskHeightValue',
            'symbolMaxHeight': 'symbolMaxHeightValue',
            'periodCommaRatioMin': 'periodRatioMinValue',
            'periodCommaRatioMax': 'periodRatioMaxValue'
        };

        Object.entries(sliderMappings).forEach(([sliderId, valueDisplayId]) => {
            const slider = document.getElementById(sliderId);
            const valueDisplay = document.getElementById(valueDisplayId);
            
            if (slider && valueDisplay) {
                // Update display on input
                slider.addEventListener('input', () => {
                    valueDisplay.textContent = slider.value;
                });
                // Apply settings on change (when mouse is released)
                slider.addEventListener('change', () => {
                    this.ocr.applyCurrentSettings();
                });
            }
        });
    }
    
    /**
     * Attaches keyboard shortcuts for power users.
     */
    setupKeyboardShortcuts() {
        document.addEventListener('keydown', (e) => {
            if (e.ctrlKey || e.metaKey) { // Handle Ctrl or Cmd key
                switch(e.key.toLowerCase()) {
                    case 'm': e.preventDefault(); this.trading.executeManualTrade('add'); break;
                    case 'f': e.preventDefault(); this.trading.executeForceCloseAll(); break;
                    case 'o': e.preventDefault(); this.ocr.toggleOCR(); break;
                    case 'e': e.preventDefault(); this.trading.executeEmergencyStop(); break;
                }
            }
            // Function keys without modifiers
            switch(e.key) {
                case 'F1': e.preventDefault(); this.trading.toggleTrading(); break;
                case 'F2': e.preventDefault(); this.ocr.toggleOCR(); break;
                case 'F3': e.preventDefault(); this.ocr.switchImageMode(); break;
            }
        });
    }

    /**
     * A robust helper to attach an event listener to an element by its ID.
     * Logs a warning if the element is not found, preventing crashes.
     */
    attachListener(elementId, eventType, handler) {
        const element = document.getElementById(elementId);
        if (element) {
            element.addEventListener(eventType, handler);
            console.log(`✅ EventManager: Attached ${eventType} listener to #${elementId}`);
        } else {
            // This warning helps identify missing elements
            console.warn(`⚠️ EventManager: Element with ID '${elementId}' not found. Listener not attached.`);
        }
    }

    cleanup() {
        // Optional: remove listeners if this manager is ever destroyed.
        console.log('EventManager: Cleanup called');
    }
}