// events/event-manager.js - Event handling and keyboard shortcuts
export class EventManager {
    constructor(api, ui, trading, ocr, config) {
        this.api = api;
        this.ui = ui;
        this.trading = trading;
        this.ocr = ocr;
        this.config = config;
        this.keydownHandler = null;
        this.autoSaveTimer = null;
    }

    setupGlobalEventListeners() {
        // Auto-save configuration on changes
        this.setupAutoSave();

        // Keyboard shortcuts
        this.setupKeyboardShortcuts();

        // UI event handlers
        this.setupUIHandlers();

        // Trading control events
        this.setupTradingControls();

        // OCR control events
        this.setupOCRControls();

        // Configuration events
        this.setupConfigurationHandlers();

        console.log('EventManager: All event listeners attached');
    }

    setupAutoSave() {
        const autoSaveInputs = [
            'initialShares', 'addType', 'reducePercent', 'manualShares',
            'developmentMode', 'enableImageRecording', 'enableRawOcrRecording',
            'enableIntelliSenseLogging', 'enableObservabilityLogging',
            'logLevel', 'enableOcrDebugLogging'
        ];

        autoSaveInputs.forEach(id => {
            const element = document.getElementById(id);
            if (element) {
                element.addEventListener('change', () => this.triggerAutoSave());
            }
        });
    }

    triggerAutoSave() {
        if (this.autoSaveTimer) {
            clearTimeout(this.autoSaveTimer);
        }

        this.autoSaveTimer = setTimeout(() => {
            this.config.saveConfiguration();
        }, 1500);
    }

    setupKeyboardShortcuts() {
        this.keydownHandler = (e) => {
            // F1: Toggle Trading
            if (e.key === 'F1') {
                e.preventDefault();
                this.trading.toggleTrading();
            }
            // F2: Toggle OCR
            else if (e.key === 'F2') {
                e.preventDefault();
                this.ocr.toggleOCR();
            }
            // F3: Switch image mode
            else if (e.key === 'F3') {
                e.preventDefault();
                this.ocr.switchImageMode();
            }
            // F9: Force save configuration
            else if (e.key === 'F9') {
                e.preventDefault();
                this.config.saveConfiguration();
            }
            // Ctrl+R: Reload without cache
            else if (e.ctrlKey && e.key === 'r') {
                e.preventDefault();
                location.reload(true);
            }
        };

        document.addEventListener('keydown', this.keydownHandler);
    }

    setupUIHandlers() {
        // Clear console button
        const clearConsoleBtn = document.getElementById('clearConsole');
        if (clearConsoleBtn) {
            clearConsoleBtn.addEventListener('click', () => {
                const consoleDiv = document.getElementById('console');
                if (consoleDiv) {
                    consoleDiv.innerHTML = '';
                    this.ui.addSystemMessage('Console cleared', 'info');
                }
            });
        }

        // Image mode toggle
        const imageModeToggle = document.getElementById('imageModeToggle');
        if (imageModeToggle) {
            imageModeToggle.addEventListener('click', () => {
                this.ocr.switchImageMode();
            });
        }

        // Image mode dropdown
        const imageModeDropdown = document.getElementById('imageMode');
        if (imageModeDropdown) {
            imageModeDropdown.addEventListener('change', (e) => {
                this.ocr.switchImageMode(e.target.value);
            });
        }
    }

    setupTradingControls() {
        // Trading toggle
        const tradingToggle = document.getElementById('tradingToggle');
        if (tradingToggle) {
            tradingToggle.addEventListener('click', () => {
                this.trading.toggleTrading();
            });
        }

        // Manual trade buttons
        const manualBuyBtn = document.getElementById('manualBuy');
        if (manualBuyBtn) {
            manualBuyBtn.addEventListener('click', () => {
                this.trading.executeManualTrade('BUY');
            });
        }

        const manualSellBtn = document.getElementById('manualSell');
        if (manualSellBtn) {
            manualSellBtn.addEventListener('click', () => {
                this.trading.executeManualTrade('SELL');
            });
        }

        // Trading parameter changes
        const tradingParams = ['initialShares', 'addType', 'reducePercent'];
        tradingParams.forEach(param => {
            const element = document.getElementById(param);
            if (element) {
                element.addEventListener('change', async () => {
                    const params = {};
                    params[param] = element.type === 'checkbox' ? element.checked : element.value;
                    await this.config.updateTradingParams(params);
                });
            }
        });
    }

    setupOCRControls() {
        // OCR toggle
        const ocrToggle = document.getElementById('ocrToggle');
        if (ocrToggle) {
            ocrToggle.addEventListener('click', () => {
                this.ocr.toggleOCR();
            });
        }

        // OCR preprocessing sliders
        const ocrSliders = document.querySelectorAll('.ocr-slider');
        ocrSliders.forEach(slider => {
            slider.addEventListener('input', (e) => {
                const displayId = e.target.id.replace(/([A-Z])/g, '-$1').toLowerCase() + '-value';
                const display = document.getElementById(displayId);
                if (display) {
                    display.textContent = e.target.value;
                }
            });

            slider.addEventListener('change', () => {
                this.applyOCRSettings();
            });
        });

        // ROI adjustment buttons
        this.setupROIControls();

        // Apply OCR settings button
        const applyOcrBtn = document.getElementById('applyOcrSettings');
        if (applyOcrBtn) {
            applyOcrBtn.addEventListener('click', () => {
                this.applyOCRSettings();
            });
        }
    }

    setupROIControls() {
        const roiButtons = {
            'roiLeft': () => this.ocr.adjustROI('x1', 'decrease'),
            'roiRight': () => this.ocr.adjustROI('x1', 'increase'),
            'roiTop': () => this.ocr.adjustROI('y1', 'decrease'),
            'roiBottom': () => this.ocr.adjustROI('y1', 'increase'),
            'roiExpand': () => this.ocr.expandROI()
        };

        Object.entries(roiButtons).forEach(([id, handler]) => {
            const button = document.getElementById(id);
            if (button) {
                button.addEventListener('click', handler);
            }
        });
    }

    setupConfigurationHandlers() {
        // Save configuration button
        const saveConfigBtn = document.getElementById('saveConfig');
        if (saveConfigBtn) {
            saveConfigBtn.addEventListener('click', () => {
                this.config.saveConfiguration();
            });
        }

        // Load configuration button
        const loadConfigBtn = document.getElementById('loadConfig');
        if (loadConfigBtn) {
            loadConfigBtn.addEventListener('click', () => {
                this.config.loadConfiguration();
            });
        }

        // Development mode changes
        const devMode = document.getElementById('developmentMode');
        if (devMode) {
            devMode.addEventListener('change', () => {
                this.config.updateDevelopmentStatusDisplays();
            });
        }

        // Recording toggles
        const recordingToggles = [
            'enableImageRecording', 
            'enableRawOcrRecording',
            'enableIntelliSenseLogging',
            'enableObservabilityLogging'
        ];

        recordingToggles.forEach(id => {
            const element = document.getElementById(id);
            if (element) {
                element.addEventListener('change', () => {
                    this.config.updateRecordingStatusDisplays();
                });
            }
        });
    }

    applyOCRSettings() {
        const settings = {
            ocr_upscale_factor: parseFloat(document.getElementById('upscaleFactor')?.value || 2),
            ocr_unsharp_strength: parseFloat(document.getElementById('unsharpStrength')?.value || 0.5),
            ocr_threshold_c: parseInt(document.getElementById('thresholdC')?.value || 25),
            ocr_red_boost: parseFloat(document.getElementById('redBoost')?.value || 1.2),
            ocr_green_boost: parseFloat(document.getElementById('greenBoost')?.value || 1.0),
            ocr_text_mask_min_contour_area: parseInt(document.getElementById('textMaskMinContourArea')?.value || 50),
            ocr_text_mask_min_width: parseInt(document.getElementById('textMaskMinWidth')?.value || 5),
            ocr_text_mask_min_height: parseInt(document.getElementById('textMaskMinHeight')?.value || 5),
            ocr_symbol_max_height: parseInt(document.getElementById('symbolMaxHeight')?.value || 50),
            ocr_period_comma_ratio_min: parseFloat(document.getElementById('periodCommaRatioMin')?.value || 0.5),
            ocr_period_comma_ratio_max: parseFloat(document.getElementById('periodCommaRatioMax')?.value || 1.5),
            ocr_period_comma_radius: parseInt(document.getElementById('periodCommaRadius')?.value || 3),
            ocr_hyphen_min_ratio: parseFloat(document.getElementById('hyphenMinRatio')?.value || 2.0),
            ocr_hyphen_min_height: parseInt(document.getElementById('hyphenMinHeight')?.value || 5),
            ocr_force_black_text_on_white: document.getElementById('forceBlackText')?.checked || false,
            ocr_apply_text_mask_cleaning: document.getElementById('textMaskCleaning')?.checked || false,
            ocr_enhance_small_symbols: document.getElementById('enhanceSmallSymbols')?.checked || false
        };

        this.ocr.updateOCRSettings(settings);
    }

    cleanup() {
        if (this.keydownHandler) {
            document.removeEventListener('keydown', this.keydownHandler);
        }

        if (this.autoSaveTimer) {
            clearTimeout(this.autoSaveTimer);
        }
    }
}