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

    // Check memory pressure and warn if needed
    checkMemoryPressure() {
        const usage = this.getMemoryUsage();
        if (usage.totalMB > this.maxMemoryMB * 0.9) {
            console.warn(`High memory usage: ${usage.totalMB.toFixed(1)}MB`);
            return true;
        }
        return false;
    }

    // Set cache size limits
    setCacheSize(maxSize) {
        this.maxCacheSize = maxSize;
        if (this.imageCache.size > maxSize) {
            this.cleanupOldestImages(this.imageCache.size - maxSize);
        }
    }

    // Get cache statistics
    getCacheStats() {
        const entries = Array.from(this.imageCache.values());
        const typeStats = entries.reduce((acc, entry) => {
            acc[entry.type] = (acc[entry.type] || 0) + 1;
            return acc;
        }, {});

        return {
            totalImages: this.imageCache.size,
            typeBreakdown: typeStats,
            memoryUsage: this.getMemoryUsage(),
            oldestImage: entries.length > 0 ? Math.min(...entries.map(e => e.timestamp)) : null,
            newestImage: entries.length > 0 ? Math.max(...entries.map(e => e.timestamp)) : null
        };
    }
}