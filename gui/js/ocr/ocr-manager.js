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
        if (!imageData) {
            console.warn(`❌ OCR updatePreviewImage called with no imageData (type: ${imageType})`);
            return;
        }

        console.log(`🔄 OCR updatePreviewImage: ${imageType}, ${(imageData.length / 1024).toFixed(1)}KB`);

        // Store in image manager
        const imageKey = `${imageType}_${Date.now()}`;
        this.imageManager.addImage(imageKey, imageData, imageType);

        // Update state
        if (imageType === 'raw') {
            this.state.setLastRawImage(imageData);
        } else {
            this.state.setLastProcessedImage(imageData);
        }

        // Check current image mode
        const currentMode = this.state.getImageMode();
        console.log(`📺 Current image mode: ${currentMode}, incoming type: ${imageType}`);

        // Display if current mode matches
        if (currentMode === imageType) {
            console.log(`✅ Displaying ${imageType} image`);
            this.displayImage(imageData);
            
            // Update ROI display - but only for raw images (where ROI coordinates make sense)
            if (imageType === 'raw') {
                setTimeout(() => {
                    const previewImage = this.ui.dom.get('previewImage');
                    if (previewImage && previewImage.naturalWidth > 0) {
                        this.ui.updateROIDisplay();
                    }
                }, 50);
            }
        } else {
            console.log(`⏭️ Skipping display - mode mismatch (current: ${currentMode}, incoming: ${imageType})`);
        }
    }

    displayImage(imageData) {
        const previewImage = this.ui.dom.get('previewImage');
        
        console.log(`🖼️ displayImage called:`, {
            hasElement: !!previewImage,
            elementId: previewImage?.id,
            hasImageData: !!imageData,
            imageDataType: typeof imageData,
            imageDataLength: imageData?.length,
            startsWithDataImage: imageData?.startsWith('data:image')
        });

        if (!previewImage) {
            console.error(`❌ previewImage element not found in DOM!`);
            return;
        }

        if (!imageData) {
            console.error(`❌ No imageData provided to displayImage`);
            return;
        }

        try {
            const src = imageData.startsWith('data:image') ? imageData : `data:image/png;base64,${imageData}`;
            console.log(`🎯 Setting image src (length: ${src.length}, starts with: ${src.substring(0, 30)}...)`);
            
            previewImage.onload = () => {
                console.log(`✅ Image loaded successfully: ${previewImage.naturalWidth}x${previewImage.naturalHeight}`);
                // Update ROI display now that image has dimensions
                this.ui.updateROIDisplay();
            };
            
            previewImage.onerror = (error) => {
                console.error(`❌ Image failed to load:`, error);
            };
            
            previewImage.src = src;
            console.log(`📝 Image src set to previewImage element`);
            
            // Also trigger ROI update immediately for rapid image updates
            setTimeout(() => {
                if (previewImage.naturalWidth > 0) {
                    this.ui.updateROIDisplay();
                }
            }, 50);
            
        } catch (error) {
            console.error(`❌ Error in displayImage:`, error);
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

    updateRawOCRTable(snapshots) {
        const tableBody = document.getElementById('rawOcrTableBody');
        if (!tableBody) return;

        if (!snapshots || Object.keys(snapshots).length === 0) {
            tableBody.innerHTML = '<tr><td colspan="6" class="no-data">No OCR data</td></tr>';
            return;
        }

        tableBody.innerHTML = '';

        Object.entries(snapshots).forEach(([symbol, data]) => {
            if (symbol === 'ALL') {
                // Handle summary row
                const summaryBody = document.getElementById('rawOcrSummaryBody');
                if (summaryBody) {
                    summaryBody.innerHTML = `
                        <tr>
                            <td style="font-weight: 600;">TOTAL</td>
                            <td>${data.price_str || '0.00'}</td>
                            <td>-</td>
                            <td>-</td>
                            <td>-</td>
                            <td class="${this.ui.getPnLClass(data.pnl_str)}">${data.pnl_str || '0.00'}</td>
                        </tr>
                    `;
                }
            } else {
                // Regular symbol row
                const row = document.createElement('tr');
                row.innerHTML = `
                    <td style="font-weight: 600;">${symbol}</td>
                    <td>${data.price_str || '-'}</td>
                    <td>${data.quantity_str || '-'}</td>
                    <td>${data.action_intent_str || '-'}</td>
                    <td>${data.cost_basis_str || '-'}</td>
                    <td class="${this.ui.getPnLClass(data.pnl_str)}">${data.pnl_str || '-'}</td>
                `;
                tableBody.appendChild(row);
            }
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

        // Check for dropdown first
        const imageModeDropdown = document.getElementById('imageMode');
        if (imageModeDropdown && !targetMode) {
            newMode = imageModeDropdown.value;
        } else if (targetMode) {
            newMode = targetMode;
        } else {
            // Toggle between modes
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
        const toggleButton = document.getElementById('imageModeToggle') || 
                            document.getElementById('imageToggle') ||
                            document.getElementById('toggleImageMode');
        if (toggleButton) {
            toggleButton.textContent = newMode === 'raw' ? 'Raw' : 'Processed';
            toggleButton.title = `Currently showing ${newMode} image. Click to switch.`;
        }

        // Update dropdown if it exists
        if (imageModeDropdown) {
            imageModeDropdown.value = newMode;
        }
    }

    async toggleOCR() {
        const btn = document.getElementById('ocrToggle');
        
        // Prevent rapid clicking
        if (btn && btn.disabled) {
            this.ui.addSystemMessage('OCR command already in progress, please wait...', 'warning');
            return;
        }

        try {
            const command = this.state.isOCRActive() ? 'stop_ocr' : 'start_ocr';
            
            // Set button to loading state
            if (btn) {
                btn.disabled = true;
                btn.textContent = this.state.isOCRActive() ? 'Stopping...' : 'Starting...';
                btn.className = 'btn btn-warning';
            }

            const result = await this.api.sendCommand(command);
            
            if (result.success) {
                const action = this.state.isOCRActive() ? 'stop' : 'start';
                this.ui.addSystemMessage(`OCR ${action} command sent`, 'info');
            } else {
                throw new Error(result.error || 'Failed to toggle OCR');
            }
        } catch (error) {
            console.error('Failed to toggle OCR:', error);
            this.ui.addSystemMessage(`Failed to toggle OCR: ${error.message}`, 'error');
            
            // Reset button on error
            if (btn) {
                this.ui.updateOCRButtonState(this.state.isOCRActive());
            }
        }
    }

    async updateOCRSettings(settings) {
        try {
            console.log('Applying OCR settings:', settings);
            this.ui.addSystemMessage(`Applying OCR settings... (${Object.keys(settings).length} parameters)`, 'info');

            const result = await this.api.sendCommand('update_ocr_preprocessing_full', settings);
            
            if (result.success) {
                this.ui.addSystemMessage('✅ OCR settings updated successfully', 'success');
            } else {
                throw new Error(result.error || 'Failed to update OCR settings');
            }
        } catch (error) {
            console.error('Failed to update OCR settings:', error);
            this.ui.addSystemMessage('❌ Failed to apply OCR settings', 'error');
        }
    }

    async adjustROI(axis, direction, event = null) {
        const shiftHeld = event ? event.shiftKey : false;
        const stepSize = shiftHeld ? 5 : 1;

        console.log(`🔧 adjustROI called: ${axis} ${direction} (step: ${stepSize})`);

        try {
            console.log(`🚀 Sending command: roi_adjust`);
            const result = await this.api.sendCommand('roi_adjust', {
                edge: axis,
                direction: direction,
                step_size: stepSize,
                shift: shiftHeld
            });

            console.log(`📨 Command result:`, result);

            if (result.success) {
                this.ui.addSystemMessage(
                    `✅ ROI edge ${axis} adjusted ${direction} (${shiftHeld ? 'large step' : 'small step'})`,
                    'success'
                );
            } else {
                this.ui.addSystemMessage(`❌ ROI adjust failed: ${result.error}`, 'error');
            }
        } catch (error) {
            console.error('❌ Failed to adjust ROI:', error);
            this.ui.addSystemMessage(`❌ Failed to adjust ROI: ${error.message}`, 'error');
        }
    }

    async expandROI() {
        const newRoiCoords = [50, 20, 450, 100];
        
        try {
            const result = await this.api.sendCommand('set_roi_absolute', {
                x1: newRoiCoords[0],
                y1: newRoiCoords[1],
                x2: newRoiCoords[2],
                y2: newRoiCoords[3]
            });

            if (result.success) {
                this.ui.addSystemMessage('ROI expanded successfully', 'success');
            }
        } catch (error) {
            console.error('Failed to expand ROI:', error);
            this.ui.addSystemMessage(`Failed to expand ROI: ${error.message}`, 'error');
        }
    }

    getPublicAPI() {
        return {
            switchImageMode: this.switchImageMode.bind(this),
            toggleOCR: this.toggleOCR.bind(this),
            updateOCRSettings: this.updateOCRSettings.bind(this),
            adjustROI: this.adjustROI.bind(this),
            expandROI: this.expandROI.bind(this),
            getImageMode: () => this.state.getImageMode(),
            isOCRActive: () => this.state.isOCRActive()
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