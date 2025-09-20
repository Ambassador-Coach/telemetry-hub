// core/message-router.js - WebSocket message routing
export class MessageRouter {
    constructor() {
        this.handlers = new Map();
    }

    registerHandler(messageType, handler) {
        this.handlers.set(messageType, handler);
    }

    route(data) {
        const handler = this.handlers.get(data.type);
        if (handler) {
            try {
                handler(data);
                return true;
            } catch (error) {
                console.error(`MessageRouter error for ${data.type}:`, error);
                return false;
            }
        }
        
        if (data.type !== 'heartbeat') {
            console.warn(`MessageRouter: Unhandled message type: ${data.type}`);
        }
        return false;
    }

    getHandlerCount() {
        return this.handlers.size;
    }

    removeHandler(messageType) {
        return this.handlers.delete(messageType);
    }

    hasHandler(messageType) {
        return this.handlers.has(messageType);
    }

    getAllHandlers() {
        return Array.from(this.handlers.keys());
    }

    clearAllHandlers() {
        this.handlers.clear();
    }
}