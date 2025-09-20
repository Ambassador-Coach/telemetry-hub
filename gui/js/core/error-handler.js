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

    cleanup() {
        this.errorHistory = [];
        this.errorCount = 0;
    }
}