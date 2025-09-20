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
                this.ui.addSystemMessage('✅ Configuration loaded from control.json', 'success');
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
                if (result.data && result.data.message) {
                    this.ui.addSystemMessage(`✅ ${result.data.message}`, 'success');
                } else {
                    this.ui.addSystemMessage('✅ Configuration saved', 'success');
                }

                // Show changes if available
                if (result.data && result.data.data && result.data.data.changed_count > 0) {
                    this.ui.addSystemMessage(
                        `📝 ${result.data.data.changed_count} parameters changed: ${result.data.data.changed_params.join(', ')}`,
                        'info'
                    );
                }
            } else {
                const errorMsg = result.data?.message || result.data?.detail || result.error || 'Unknown error';
                throw new Error(errorMsg);
            }
            
            return result;
        } catch (error) {
            console.error('Failed to save configuration:', error);
            this.ui.addSystemMessage(`❌ Failed to save configuration: ${error.message}`, 'error');
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
        if (config.enable_image_recording !== undefined) {
            this.setElementValue('enableImageRecording', config.enable_image_recording);
        }
        if (config.enable_raw_ocr_recording !== undefined) {
            this.setElementValue('enableRawOcrRecording', config.enable_raw_ocr_recording);
        }
        if (config.enable_intellisense_logging !== undefined) {
            this.setElementValue('enableIntelliSenseLogging', config.enable_intellisense_logging);
        }
        if (config.enable_observability_logging !== undefined) {
            this.setElementValue('enableObservabilityLogging', config.enable_observability_logging);
        }
        if (config.log_level !== undefined) {
            this.setElementValue('logLevel', config.log_level);
        }
        if (config.enable_ocr_debug_logging !== undefined) {
            this.setElementValue('enableOcrDebugLogging', config.enable_ocr_debug_logging);
        }

        // Apply OCR settings
        this.applyOCRSettings(config);

        // Update status displays
        this.updateStatusDisplays();
    }

    applyOCRSettings(config) {
        const ocrMappings = {
            'upscaleFactor': 'ocr_upscale_factor',
            'unsharpStrength': 'ocr_unsharp_strength',
            'thresholdC': 'ocr_threshold_c',
            'redBoost': 'ocr_red_boost',
            'greenBoost': 'ocr_green_boost',
            'textMaskMinContourArea': 'ocr_text_mask_min_contour_area',
            'textMaskMinWidth': 'ocr_text_mask_min_width',
            'textMaskMinHeight': 'ocr_text_mask_min_height',
            'symbolMaxHeight': 'ocr_symbol_max_height',
            'periodCommaRatioMin': 'ocr_period_comma_ratio_min',
            'periodCommaRatioMax': 'ocr_period_comma_ratio_max',
            'periodCommaRadius': 'ocr_period_comma_radius',
            'hyphenMinRatio': 'ocr_hyphen_min_ratio',
            'hyphenMinHeight': 'ocr_hyphen_min_height',
            'forceBlackText': 'ocr_force_black_text_on_white',
            'textMaskCleaning': 'ocr_apply_text_mask_cleaning',
            'enhanceSmallSymbols': 'ocr_enhance_small_symbols'
        };

        Object.entries(ocrMappings).forEach(([elementId, configKey]) => {
            if (config[configKey] !== undefined) {
                this.setElementValue(elementId, config[configKey]);
                
                // Update display values for sliders
                this.updateSliderDisplay(elementId, config[configKey]);
            }
        });
    }

    updateSliderDisplay(elementId, value) {
        const displayMappings = {
            'upscaleFactor': 'upscaleValue',
            'unsharpStrength': 'unsharpValue',
            'thresholdC': 'thresholdCValue',
            'redBoost': 'redBoostValue',
            'greenBoost': 'greenBoostValue',
            'textMaskMinContourArea': 'contourAreaValue',
            'textMaskMinWidth': 'maskWidthValue',
            'textMaskMinHeight': 'maskHeightValue',
            'symbolMaxHeight': 'symbolMaxHeightValue',
            'periodCommaRatioMin': 'periodRatioMinValue',
            'periodCommaRatioMax': 'periodRatioMaxValue',
            'periodCommaRadius': 'periodRadiusValue',
            'hyphenMinRatio': 'hyphenRatioValue',
            'hyphenMinHeight': 'hyphenHeightValue'
        };

        const displayElementId = displayMappings[elementId];
        if (displayElementId) {
            const displayElement = document.getElementById(displayElementId);
            if (displayElement) {
                displayElement.textContent = value;
            }
        }
    }

    updateStatusDisplays() {
        this.updateDevelopmentStatusDisplays();
        this.updateRecordingStatusDisplays();
        this.updateLoggingStatusDisplays();
    }

    updateDevelopmentStatusDisplays() {
        const developmentMode = document.getElementById('developmentMode')?.checked;
        const statusText = document.getElementById('developmentStatusText');
        
        if (statusText) {
            if (developmentMode) {
                statusText.textContent = 'Active: 90% less storage, all tools still work';
                statusText.style.color = 'var(--accent-primary)';
            } else {
                statusText.textContent = 'Disabled: Full logging/storage (Large files!)';
                statusText.style.color = 'var(--accent-warning)';
            }
        }
    }

    updateRecordingStatusDisplays() {
        const imageRecording = document.getElementById('enableImageRecording')?.checked;
        const rawOcrRecording = document.getElementById('enableRawOcrRecording')?.checked;
        const intelliSenseLogging = document.getElementById('enableIntelliSenseLogging')?.checked;
        const observabilityLogging = document.getElementById('enableObservabilityLogging')?.checked;

        const statusText = document.getElementById('recordingStatusText');
        if (statusText) {
            const activeSettings = [];
            if (imageRecording) activeSettings.push('Images');
            if (rawOcrRecording) activeSettings.push('Raw OCR');
            if (intelliSenseLogging) activeSettings.push('IntelliSense');
            if (observabilityLogging) activeSettings.push('Observability');

            if (activeSettings.length === 0) {
                statusText.textContent = 'Development Mode: No Recording';
                statusText.style.color = 'var(--accent-primary)';
            } else {
                statusText.textContent = `Recording: ${activeSettings.join(', ')}`;
                statusText.style.color = observabilityLogging ? 'var(--accent-danger)' :
                                        (imageRecording ? 'var(--accent-warning)' : 'var(--text-muted)');
            }
        }
    }

    updateLoggingStatusDisplays() {
        const logLevel = document.getElementById('logLevel')?.value;
        const ocrDebugLogging = document.getElementById('enableOcrDebugLogging')?.checked;

        const statusText = document.getElementById('loggingStatusText');
        if (statusText) {
            statusText.textContent = `Current: ${logLevel} level, OCR Debug ${ocrDebugLogging ? 'ON' : 'OFF'}`;

            if (logLevel === 'DEBUG' || ocrDebugLogging) {
                statusText.style.color = 'var(--accent-warning)';
            } else if (logLevel === 'ERROR') {
                statusText.style.color = 'var(--accent-primary)';
            } else {
                statusText.style.color = 'var(--text-muted)';
            }
        }
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

    async updateTradingParams(params) {
        try {
            const result = await this.api.sendCommand('update_trading_params', params);
            
            if (result.success) {
                const paramNames = Object.keys(params);
                this.ui.addSystemMessage(`✅ Trading parameters updated: ${paramNames.join(', ')}`, 'success');
            } else {
                throw new Error(result.error || 'Failed to update trading parameters');
            }
        } catch (error) {
            console.error('Failed to update trading parameters:', error);
            this.ui.addSystemMessage(`❌ Failed to update trading parameters: ${error.message}`, 'error');
        }
    }

    setCorrectDefaults() {
        // Set development settings to correct defaults
        this.setElementValue('developmentMode', true);
        this.setElementValue('enableRawOcrRecording', false);
        this.setElementValue('logLevel', 'ERROR');

        // Update status displays
        this.updateStatusDisplays();

        this.ui.addSystemMessage('✅ Applied correct default settings', 'info');
    }
}