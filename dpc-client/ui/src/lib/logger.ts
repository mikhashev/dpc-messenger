/**
 * Frontend logger — captures ALL console output and mirrors it to ~/.dpc/logs/ui.log
 * via the backend WebSocket.
 *
 * Call setupConsoleRelay() once at app startup (done in +layout.svelte).
 * Logs that arrive before the WebSocket connects are buffered and flushed automatically.
 *
 * Named log API (optional, for new code):
 *   import { log } from '$lib/logger';
 *   log.info('coreService', 'WebSocket connected');
 */

type LogLevel = 'debug' | 'info' | 'warn' | 'error';
type SendFn = (level: LogLevel, context: string, message: string) => void;

// Injected by coreService once the WebSocket opens.
let _sendFn: SendFn | null = null;

// Buffer for log entries that arrive before the socket is ready.
const MAX_BUFFER = 500;
const _buffer: Array<{ level: LogLevel; context: string; message: string }> = [];

function relay(level: LogLevel, context: string, message: string) {
    if (_sendFn) {
        try { _sendFn(level, context, message); } catch { /* ignore */ }
    } else {
        if (_buffer.length < MAX_BUFFER) {
            _buffer.push({ level, context, message });
        }
    }
}

/** Called by coreService when the WebSocket is open. Flushes any buffered entries. */
export function setLogSender(fn: SendFn) {
    _sendFn = fn;
    // Flush pre-connection buffer
    for (const entry of _buffer.splice(0)) {
        try { fn(entry.level, entry.context, entry.message); } catch { /* ignore */ }
    }
}

// ── Global console intercept ──────────────────────────────────────────────────
// Wraps console.log/warn/error/debug so ALL existing calls are captured without
// any code changes.  The originals are still called so devtools work normally.

const _orig = {
    log:   console.log.bind(console),
    warn:  console.warn.bind(console),
    error: console.error.bind(console),
    debug: console.debug.bind(console),
};

function intercept(level: LogLevel, args: unknown[]) {
    // Extract a simple context string from the first argument if it looks like "[Tag]"
    let context = 'ui';
    let rest = args;
    if (args.length > 0 && typeof args[0] === 'string') {
        const m = args[0].match(/^\[([^\]]+)\]/);
        if (m) {
            context = m[1];
            rest = [args[0].slice(m[0].length).trimStart(), ...args.slice(1)];
        }
    }
    const message = rest
        .map(a => {
            if (a === null) return 'null';
            if (a === undefined) return 'undefined';
            if (typeof a === 'object') { try { return JSON.stringify(a); } catch { return String(a); } }
            return String(a);
        })
        .join(' ');
    relay(level, context, message);
}

/** Install global console intercept. Safe to call multiple times. */
export function setupConsoleRelay() {
    if ((console as any).__dpc_relay_installed) return;
    (console as any).__dpc_relay_installed = true;

    console.log   = (...args: unknown[]) => { _orig.log(...args);   intercept('info',  args); };
    console.warn  = (...args: unknown[]) => { _orig.warn(...args);  intercept('warn',  args); };
    console.error = (...args: unknown[]) => { _orig.error(...args); intercept('error', args); };
    console.debug = (...args: unknown[]) => { _orig.debug(...args); intercept('debug', args); };
}

// ── Named log API (for new code) ─────────────────────────────────────────────
export const log = {
    debug: (context: string, ...args: unknown[]) => {
        _orig.debug(`[${context}]`, ...args);
        intercept('debug', [`[${context}]`, ...args]);
    },
    info: (context: string, ...args: unknown[]) => {
        _orig.log(`[${context}]`, ...args);
        intercept('info', [`[${context}]`, ...args]);
    },
    warn: (context: string, ...args: unknown[]) => {
        _orig.warn(`[${context}]`, ...args);
        intercept('warn', [`[${context}]`, ...args]);
    },
    error: (context: string, ...args: unknown[]) => {
        _orig.error(`[${context}]`, ...args);
        intercept('error', [`[${context}]`, ...args]);
    },
};
