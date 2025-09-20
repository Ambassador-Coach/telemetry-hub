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