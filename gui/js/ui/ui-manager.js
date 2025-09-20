// ui/ui-manager.js - Clean version without debug spam
export class UIManager {
    constructor(dom, state) {
        this.dom = dom;
        this.state = state;
    }

    initialize() {
        this.initializePreview();
        this.loadInitialROI();
    }

    initializePreview() {
        const previewImage = this.dom.get('previewImage');
        if (previewImage) {
            const canvas = document.createElement('canvas');
            canvas.width = 600;
            canvas.height = 120;
            const ctx = canvas.getContext('2d');
            
            ctx.fillStyle = '#1a1a1a';
            ctx.fillRect(0, 0, canvas.width, canvas.height);
            ctx.fillStyle = '#666';
            ctx.font = '16px JetBrains Mono';
            ctx.textAlign = 'center';
            ctx.fillText('Waiting for OCR feed...', canvas.width/2, canvas.height/2 - 10);
            ctx.font = '12px JetBrains Mono';
            ctx.fillText('ROI controls active when connected.', canvas.width/2, canvas.height/2 + 15);

            previewImage.src = canvas.toDataURL();
        }
    }

    async loadInitialROI() {
        this.state.setCurrentROI([62, 158, 668, 277]);
        this.updateROIDisplay();
    }

    // Update ROI display - CLEAN VERSION
    updateROIDisplay() {
        const roiCoordsEl = document.getElementById('roiCoords');
        const currentROI = this.state.getCurrentROI();
        
        // Update ROI coordinates display
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
            // Ensure container positioning is correct
            const previewContainer = document.querySelector('.preview-canvas-container');
            if (previewContainer) {
                previewContainer.style.position = 'relative';
                previewContainer.style.overflow = 'visible';
            }
            
            // Apply green border around entire image
            roiBox.style.cssText = `
                position: absolute !important;
                left: 0px !important;
                top: 0px !important;
                width: 100% !important;
                height: 100% !important;
                border: 3px solid var(--accent-primary) !important;
                background: rgba(0, 255, 136, 0.02) !important;
                display: block !important;
                z-index: 15 !important;
                border-radius: 4px !important;
                box-shadow: 0 0 10px rgba(0, 255, 136, 0.4) !important;
            `;
        } else {
            // Hide ROI box if conditions not met
            if (roiBox) {
                roiBox.style.display = 'none';
            }
        }
    }

    // Update OCR button state
    updateOCRButtonState(isActive) {
        const btn = document.getElementById('ocrToggle');
        if (!btn) return;

        this.state.setOCRActive(isActive);
        
        if (isActive) {
            btn.textContent = 'Stop OCR';
            btn.classList.remove('btn-success');
            btn.classList.add('btn-danger');
        } else {
            btn.textContent = 'Start OCR';
            btn.classList.remove('btn-danger');
            btn.classList.add('btn-success');
        }
    }

    addSystemMessage(message, level = 'info') {
        const systemMessages = document.getElementById('systemMessages');
        if (!systemMessages) return;

        const messageEl = document.createElement('div');
        messageEl.className = `system-message system-message-${level}`;
        messageEl.innerHTML = `
            <span class="timestamp">${new Date().toLocaleTimeString()}</span>
            <span class="message">${message}</span>
        `;

        systemMessages.appendChild(messageEl);
        systemMessages.scrollTop = systemMessages.scrollHeight;

        // Clean up old messages
        const messages = systemMessages.querySelectorAll('.system-message');
        if (messages.length > 50) {
            messages[0].remove();
        }
    }

    updateConfidence(confidence) {
        const confidenceText = document.getElementById('confidenceText');
        const confidenceFill = document.getElementById('confidenceFill');
        
        if (confidenceText && confidenceFill) {
            confidenceText.textContent = `${confidence.toFixed(1)}%`;
            confidenceFill.style.width = `${confidence}%`;
            
            if (confidence >= 80) {
                confidenceFill.style.backgroundColor = 'var(--accent-primary)';
            } else if (confidence >= 60) {
                confidenceFill.style.backgroundColor = 'var(--accent-warning)';
            } else {
                confidenceFill.style.backgroundColor = 'var(--accent-danger)';
            }
        }
    }

    // Utility methods used by OCR manager
    formatCurrency(value) {
        if (typeof value !== 'number') return '$0.00';
        return new Intl.NumberFormat('en-US', {
            style: 'currency',
            currency: 'USD',
            minimumFractionDigits: 2
        }).format(value);
    }

    getPnLClass(pnl) {
        if (pnl > 0) return 'positive';
        if (pnl < 0) return 'negative';
        return 'neutral';
    }

    formatTime(timestamp) {
        if (!timestamp) return '--:--:--';
        return new Date(timestamp * 1000).toLocaleTimeString();
    }
}