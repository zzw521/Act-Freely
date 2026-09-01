// ============================================================
// src/main/util/logger.ts — Structured JSON logger
// ============================================================

import fs from 'fs';
import path from 'path';
import { app } from 'electron';

// ── Types ────────────────────────────────────────────────────

type LogLevel = 'debug' | 'info' | 'warn' | 'error';

interface LogEntry {
  timestamp: string;
  level: LogLevel;
  message: string;
  module?: string;
  data?: Record<string, unknown>;
}

// ── Constants ────────────────────────────────────────────────

const LOG_LEVELS: Record<LogLevel, number> = {
  debug: 0,
  info: 1,
  warn: 2,
  error: 3,
};

const MAX_LOG_DAYS = 7;

// Patterns that should be redacted
const REDACT_PATTERNS: RegExp[] = [
  // API keys (sk-..., key-..., etc.)
  /(?:sk-[a-zA-Z0-9]{20,})/g,
  /(?:api[_-]?key["']?\s*[:=]\s*["']?)([a-zA-Z0-9]{16,})/gi,
  // Bearer tokens
  /(?:Bearer\s+)([a-zA-Z0-9\-._~+/]+=*)/gi,
  // Email addresses (PII)
  /([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})/g,
  // Phone numbers (basic pattern)
  /(\d{3}[-.]?\d{4}[-.]?\d{4})/g,
];

const REDACTED = '[REDACTED]';

// ── Helpers ──────────────────────────────────────────────────

function getLogDir(): string {
  try {
    return path.join(app.getPath('userData'), 'logs');
  } catch {
    const home = process.env.APPDATA
      || process.env.HOME
      || path.join(process.env.USERPROFILE || '', '.tongtian');
    return path.join(home, '.tongtian', 'logs');
  }
}

function getLogFileName(): string {
  const now = new Date();
  const dateStr = now.toISOString().slice(0, 10); // YYYY-MM-DD
  return `tongtian-${dateStr}.log`;
}

function sanitize(value: string): string {
  let result = value;
  for (const pattern of REDACT_PATTERNS) {
    // Reset regex state since they have /g flag
    pattern.lastIndex = 0;
    result = result.replace(pattern, (match) => {
      // For patterns with capture groups, preserve the prefix
      if (match.includes(':') || match.includes('=')) {
        const separatorIndex = Math.max(match.indexOf(':'), match.indexOf('='));
        const prefix = match.slice(0, separatorIndex + 1);
        return prefix + REDACTED;
      }
      return REDACTED;
    });
  }
  return result;
}

function cleanOldLogs(logDir: string): void {
  try {
    if (!fs.existsSync(logDir)) return;

    const files = fs.readdirSync(logDir);
    const now = Date.now();
    const maxAge = MAX_LOG_DAYS * 24 * 60 * 60 * 1000;

    for (const file of files) {
      if (!file.startsWith('tongtian-') || !file.endsWith('.log')) continue;

      const filePath = path.join(logDir, file);
      const stat = fs.statSync(filePath);

      if (now - stat.mtimeMs > maxAge) {
        fs.unlinkSync(filePath);
      }
    }
  } catch {
    // Silently ignore cleanup errors
  }
}

// ── Logger Class ─────────────────────────────────────────────

class Logger {
  private static instance: Logger | null = null;
  private logDir: string;
  private minLevel: LogLevel;
  private isDev: boolean;

  private constructor() {
    this.logDir = getLogDir();
    this.minLevel = (process.env.LOG_LEVEL as LogLevel) || 'debug';
    this.isDev = process.env.NODE_ENV !== 'production';

    fs.mkdirSync(this.logDir, { recursive: true });
    cleanOldLogs(this.logDir);
  }

  static getInstance(): Logger {
    if (!Logger.instance) {
      Logger.instance = new Logger();
    }
    return Logger.instance;
  }

  private shouldLog(level: LogLevel): boolean {
    return LOG_LEVELS[level] >= LOG_LEVELS[this.minLevel];
  }

  private write(entry: LogEntry): void {
    const logFile = path.join(this.logDir, getLogFileName());
    const sanitizedMessage = sanitize(entry.message);

    // Sanitize data fields
    const sanitizedData: Record<string, unknown> | undefined = entry.data
      ? this.sanitizeData(entry.data)
      : undefined;

    const line = JSON.stringify({
      ...entry,
      message: sanitizedMessage,
      data: sanitizedData,
    }) + '\n';

    try {
      fs.appendFileSync(logFile, line, 'utf8');
    } catch {
      // If file write fails, at least output to console in dev mode
    }

    if (this.isDev) {
      const prefix = `[${entry.timestamp}] [${entry.level.toUpperCase()}]`;
      const modulePrefix = entry.module ? `[${entry.module}]` : '';
      const consoleMsg = `${prefix} ${modulePrefix} ${sanitizedMessage}`;

      switch (entry.level) {
        case 'debug': console.debug(consoleMsg); break;
        case 'info':  console.info(consoleMsg);  break;
        case 'warn':  console.warn(consoleMsg);  break;
        case 'error': console.error(consoleMsg); break;
      }
    }
  }

  private sanitizeData(data: Record<string, unknown>): Record<string, unknown> {
    const result: Record<string, unknown> = {};
    for (const [key, value] of Object.entries(data)) {
      if (typeof value === 'string') {
        result[key] = sanitize(value);
      } else if (typeof value === 'object' && value !== null) {
        result[key] = this.sanitizeData(value as Record<string, unknown>);
      } else {
        result[key] = value;
      }
    }
    return result;
  }

  debug(message: string, module?: string, data?: Record<string, unknown>): void {
    if (!this.shouldLog('debug')) return;
    this.write({
      timestamp: new Date().toISOString(),
      level: 'debug',
      message,
      module,
      data,
    });
  }

  info(message: string, module?: string, data?: Record<string, unknown>): void {
    if (!this.shouldLog('info')) return;
    this.write({
      timestamp: new Date().toISOString(),
      level: 'info',
      message,
      module,
      data,
    });
  }

  warn(message: string, module?: string, data?: Record<string, unknown>): void {
    if (!this.shouldLog('warn')) return;
    this.write({
      timestamp: new Date().toISOString(),
      level: 'warn',
      message,
      module,
      data,
    });
  }

  error(message: string, module?: string, data?: Record<string, unknown>): void {
    if (!this.shouldLog('error')) return;
    this.write({
      timestamp: new Date().toISOString(),
      level: 'error',
      message,
      module,
      data,
    });
  }
}

export default Logger;
