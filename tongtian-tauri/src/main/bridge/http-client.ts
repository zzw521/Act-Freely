// ============================================================
// src/main/bridge/http-client.ts — HTTP client with timeout & retry
// ============================================================

import http from 'http';
import Logger from '../util/logger';

// ── Types ────────────────────────────────────────────────────

export interface HttpResponse {
  statusCode: number;
  headers: http.IncomingHttpHeaders;
  body: string;
}

export interface HttpRequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'DELETE' | 'PATCH';
  path: string;
  body?: string | object;
  headers?: Record<string, string>;
  timeoutMs?: number;
  retries?: number;
  retryBaseDelayMs?: number;
}

// ── Constants ────────────────────────────────────────────────

const DEFAULT_TIMEOUT_MS = 30_000;
const DEFAULT_RETRIES = 3;
const DEFAULT_RETRY_BASE_DELAY_MS = 1_000;

const logger = Logger.getInstance();

// ── HTTP Client ──────────────────────────────────────────────

/**
 * Lightweight HTTP client with timeout control and exponential-backoff retry.
 * Used exclusively for communicating with Agent child-process HTTP APIs.
 */
export class HttpClient {
  private host: string;
  private port: number;

  constructor(host: string, port: number) {
    this.host = host;
    this.port = port;
  }

  /**
   * Update the target port (e.g. after workspace restart on a new port).
   */
  setPort(port: number): void {
    this.port = port;
  }

  /**
   * Send an HTTP request with timeout and optional retry.
   */
  async request(options: HttpRequestOptions): Promise<HttpResponse> {
    const {
      method = 'GET',
      path: reqPath,
      body,
      headers = {},
      timeoutMs = DEFAULT_TIMEOUT_MS,
      retries = DEFAULT_RETRIES,
      retryBaseDelayMs = DEFAULT_RETRY_BASE_DELAY_MS,
    } = options;

    let lastError: Error | null = null;

    for (let attempt = 0; attempt <= retries; attempt++) {
      try {
        const response = await this.doRequest(method, reqPath, body, headers, timeoutMs);
        return response;
      } catch (err) {
        lastError = err instanceof Error ? err : new Error(String(err));

        // Only retry on network / timeout errors, not on 4xx responses
        if (attempt < retries) {
          const delay = retryBaseDelayMs * Math.pow(2, attempt);
          logger.warn(
            `HTTP ${method} ${reqPath} failed (attempt ${attempt + 1}/${retries + 1}), retrying in ${delay}ms`,
            'bridge:http',
            { error: lastError.message },
          );
          await sleep(delay);
        }
      }
    }

    logger.error(
      `HTTP ${method} ${reqPath} failed after ${retries + 1} attempts`,
      'bridge:http',
      { error: lastError?.message },
    );
    throw lastError;
  }

  /**
   * Convenience: GET request.
   */
  async get(path: string, options?: Partial<HttpRequestOptions>): Promise<HttpResponse> {
    return this.request({ ...options, method: 'GET', path });
  }

  /**
   * Convenience: POST request with JSON body.
   */
  async post(path: string, body?: object, options?: Partial<HttpRequestOptions>): Promise<HttpResponse> {
    return this.request({
      ...options,
      method: 'POST',
      path,
      body: body !== undefined ? JSON.stringify(body) : undefined,
      headers: { 'Content-Type': 'application/json', ...(options?.headers || {}) },
    });
  }

  /**
   * Health check: GET /health
   */
  async healthCheck(timeoutMs = 5_000): Promise<{ ok: boolean; latencyMs: number }> {
    const start = Date.now();
    try {
      const resp = await this.get('/health', { timeoutMs, retries: 0 });
      const latencyMs = Date.now() - start;
      return { ok: resp.statusCode === 200, latencyMs };
    } catch {
      return { ok: false, latencyMs: Date.now() - start };
    }
  }

  /**
   * Stream a POST request and return the raw Node IncomingMessage for SSE parsing.
   * No retry on streams — caller is responsible for reconnection.
   */
  async stream(
    path: string,
    body?: object,
    timeoutMs = DEFAULT_TIMEOUT_MS,
  ): Promise<http.IncomingMessage> {
    return new Promise((resolve, reject) => {
      const payload = body !== undefined ? JSON.stringify(body) : undefined;

      const req = http.request(
        {
          hostname: this.host,
          port: this.port,
          path,
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'Accept': 'text/event-stream',
            ...(payload ? { 'Content-Length': Buffer.byteLength(payload) } : {}),
          },
          timeout: timeoutMs,
        },
        (res) => {
          if (res.statusCode && res.statusCode >= 400) {
            let errorBody = '';
            res.on('data', (chunk) => { errorBody += chunk; });
            res.on('end', () => {
              reject(new Error(`HTTP ${res.statusCode}: ${errorBody.slice(0, 500)}`));
            });
            return;
          }
          resolve(res);
        },
      );

      req.on('timeout', () => {
        req.destroy();
        reject(new Error(`Stream timeout after ${timeoutMs}ms`));
      });

      req.on('error', (err) => {
        reject(err);
      });

      if (payload) {
        req.write(payload);
      }
      req.end();
    });
  }

  // ── Private ────────────────────────────────────────────────

  private doRequest(
    method: string,
    reqPath: string,
    body: string | object | undefined,
    headers: Record<string, string>,
    timeoutMs: number,
  ): Promise<HttpResponse> {
    return new Promise((resolve, reject) => {
      const payload = typeof body === 'object' ? JSON.stringify(body) : body;

      const req = http.request(
        {
          hostname: this.host,
          port: this.port,
          path: reqPath,
          method,
          headers: {
            ...(payload ? { 'Content-Length': Buffer.byteLength(payload) } : {}),
            ...headers,
          },
          timeout: timeoutMs,
        },
        (res) => {
          const chunks: Buffer[] = [];
          res.on('data', (chunk: Buffer) => chunks.push(chunk));
          res.on('end', () => {
            const responseBody = Buffer.concat(chunks).toString('utf8');
            resolve({
              statusCode: res.statusCode || 0,
              headers: res.headers,
              body: responseBody,
            });
          });
          res.on('error', reject);
        },
      );

      req.on('timeout', () => {
        req.destroy();
        reject(new Error(`Request timeout after ${timeoutMs}ms`));
      });

      req.on('error', (err) => {
        reject(err);
      });

      if (payload) {
        req.write(payload);
      }
      req.end();
    });
  }
}

// ── Helpers ──────────────────────────────────────────────────

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
