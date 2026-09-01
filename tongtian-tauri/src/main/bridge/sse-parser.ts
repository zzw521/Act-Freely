// ============================================================
// src/main/bridge/sse-parser.ts — SSE event stream parser
// ============================================================

import type { IncomingMessage } from 'http';
import Logger from '../util/logger';

// ── Types ────────────────────────────────────────────────────

export interface AgentEvent {
  type: 'plan' | 'step_start' | 'step_progress' | 'step_done' | 'step_failed' | 'deliver' | 'done' | 'error';
  data: Record<string, unknown>;
}

export interface SSEParseOptions {
  /** Called when an event is successfully parsed. */
  onEvent?: (event: AgentEvent) => void;
  /** Called when the stream ends (server closes connection). */
  onEnd?: () => void;
  /** Called on stream error. */
  onError?: (error: Error) => void;
}

// ── Constants ────────────────────────────────────────────────

const logger = Logger.getInstance();

// ── SSE Parser ───────────────────────────────────────────────

/**
 * Parse a Server-Sent Events stream from a Node IncomingMessage.
 *
 * QwenPaw SSE format:
 *   event: <type>
 *   data: <JSON>
 *   (blank line separates events)
 *
 * Returns an AsyncIterable<AgentEvent> that yields parsed events.
 */
export class SSEParser {
  private buffer = '';
  private closed = false;
  private error: Error | null = null;
  private eventQueue: AgentEvent[] = [];
  private waitResolve: ((value: IteratorResult<AgentEvent>) => void) | null = null;

  /**
   * Start consuming the SSE stream from the given HTTP response.
   */
  consume(stream: IncomingMessage, options?: SSEParseOptions): void {
    stream.setEncoding('utf8');

    stream.on('data', (chunk: string) => {
      this.buffer += chunk;
      this.processBuffer(options);
    });

    stream.on('end', () => {
      this.closed = true;
      // Process any remaining data in buffer
      if (this.buffer.trim()) {
        this.processBuffer(options);
      }
      options?.onEnd?.();
      this.flushWaiter();
      logger.debug('SSE stream ended', 'bridge:sse');
    });

    stream.on('error', (err) => {
      this.error = err;
      this.closed = true;
      options?.onError?.(err);
      this.flushWaiter();
      logger.error('SSE stream error', 'bridge:sse', { error: err.message });
    });

    stream.on('close', () => {
      if (!this.closed) {
        this.closed = true;
        options?.onEnd?.();
        this.flushWaiter();
        logger.debug('SSE stream closed', 'bridge:sse');
      }
    });
  }

  /**
   * AsyncIterable interface — allows `for await (const event of parser)`.
   */
  [Symbol.asyncIterator](): AsyncIterableIterator<AgentEvent> {
    return {
      next: (): Promise<IteratorResult<AgentEvent>> => {
        // If there are queued events, return the next one
        if (this.eventQueue.length > 0) {
          const event = this.eventQueue.shift()!;
          return Promise.resolve({ value: event, done: false });
        }

        // If stream is closed or errored, signal done
        if (this.closed || this.error) {
          return Promise.resolve({ value: undefined as unknown as AgentEvent, done: true });
        }

        // Otherwise, wait for the next event
        return new Promise((resolve) => {
          this.waitResolve = resolve;
        });
      },
      [Symbol.asyncIterator]() {
        return this;
      },
    };
  }

  /**
   * Collect all events into an array (useful for non-streaming scenarios).
   */
  async collectAll(): Promise<AgentEvent[]> {
    const events: AgentEvent[] = [];
    for await (const event of this) {
      events.push(event);
    }
    return events;
  }

  // ── Private ────────────────────────────────────────────────

  private processBuffer(options?: SSEParseOptions): void {
    // SSE events are separated by double newlines
    const parts = this.buffer.split('\n\n');

    // Keep the last (potentially incomplete) part in the buffer
    this.buffer = parts.pop() || '';

    for (const part of parts) {
      const event = this.parseEvent(part);
      if (event) {
        this.eventQueue.push(event);
        options?.onEvent?.(event);
        this.flushWaiter();
      }
    }
  }

  private parseEvent(raw: string): AgentEvent | null {
    const lines = raw.split('\n');
    let eventType = '';
    let eventData = '';

    for (const line of lines) {
      if (line.startsWith('event:')) {
        eventType = line.slice(6).trim();
      } else if (line.startsWith('data:')) {
        // Accumulate data lines (multi-line data)
        eventData += (eventData ? '\n' : '') + line.slice(5).trim();
      }
      // Ignore 'id:', 'retry:', and comment lines starting with ':'
    }

    if (!eventType && !eventData) {
      return null;
    }

    // Parse JSON data
    let data: Record<string, unknown> = {};
    if (eventData) {
      try {
        data = JSON.parse(eventData);
      } catch (err) {
        logger.warn('Failed to parse SSE event data', 'bridge:sse', {
          eventType,
          eventData: eventData.slice(0, 200),
          error: err instanceof Error ? err.message : String(err),
        });
        // Try to treat raw string as the data
        data = { raw: eventData };
      }
    }

    // Map event type to our AgentEvent type
    const type = this.mapEventType(eventType, data);
    if (!type) {
      logger.debug(`Ignoring unknown SSE event type: ${eventType}`, 'bridge:sse');
      return null;
    }

    return { type, data };
  }

  private mapEventType(
    rawType: string,
    data: Record<string, unknown>,
  ): AgentEvent['type'] | null {
    const mapping: Record<string, AgentEvent['type']> = {
      plan: 'plan',
      step_start: 'step_start',
      step_progress: 'step_progress',
      step_done: 'step_done',
      step_failed: 'step_failed',
      deliver: 'deliver',
      done: 'done',
      error: 'error',
    };

    // Direct mapping
    if (mapping[rawType]) {
      return mapping[rawType];
    }

    // Infer from data if type is missing
    if (!rawType && data.type && typeof data.type === 'string') {
      return mapping[data.type] || null;
    }

    return null;
  }

  private flushWaiter(): void {
    if (this.waitResolve) {
      if (this.eventQueue.length > 0) {
        const event = this.eventQueue.shift()!;
        this.waitResolve({ value: event, done: false });
      } else if (this.closed || this.error) {
        this.waitResolve({ value: undefined as unknown as AgentEvent, done: true });
      }
      this.waitResolve = null;
    }
  }
}

/**
 * Create an SSE parser and start consuming the given stream.
 * Convenience factory for one-shot usage.
 */
export function createSSEParser(stream: IncomingMessage, options?: SSEParseOptions): SSEParser {
  const parser = new SSEParser();
  parser.consume(stream, options);
  return parser;
}
