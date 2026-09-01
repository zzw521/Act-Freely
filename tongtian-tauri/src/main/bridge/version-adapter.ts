// ============================================================
// src/main/bridge/version-adapter.ts — Agent version adapter
// ============================================================

import Logger from '../util/logger';
import type { HttpClient } from './http-client';

// ── Types ────────────────────────────────────────────────────

export interface AgentVersionInfo {
  version: string;
  apiVersion: string;
  compatible: boolean;
}

export interface HealthResponse {
  status: string;
  version?: string;
  api_version?: string;
  uptime?: number;
}

// ── Constants ────────────────────────────────────────────────

/** Compatible Agent version range */
const MIN_VERSION = '1.1.0';
const MAX_VERSION = '1.99.99';

const logger = Logger.getInstance();

// ── Helpers ──────────────────────────────────────────────────

/**
 * Compare two semver strings.
 * Returns -1 if a < b, 0 if a === b, 1 if a > b.
 */
function compareSemver(a: string, b: string): -1 | 0 | 1 {
  const pa = a.split('.').map(Number);
  const pb = b.split('.').map(Number);

  for (let i = 0; i < 3; i++) {
    const na = pa[i] || 0;
    const nb = pb[i] || 0;
    if (na < nb) return -1;
    if (na > nb) return 1;
  }
  return 0;
}

function isVersionInRange(version: string, min: string, max: string): boolean {
  // Handle pre-release suffixes (e.g., 1.1.0-beta.1)
  const cleanVersion = version.replace(/-.*$/, '');
  return compareSemver(cleanVersion, min) >= 0 && compareSemver(cleanVersion, max) <= 0;
}

// ── Version Adapter ──────────────────────────────────────────

/**
 * Detects Agent API version and provides an adaptation layer
 * to shield upstream API changes from the rest of the application.
 *
 * Only version-adapter should know about Agent's raw API shape;
 * all other modules use the normalized interface exposed here.
 */
export class VersionAdapter {
  private versionInfo: AgentVersionInfo | null = null;

  /**
   * Detect the Agent version by querying GET /health.
   * Returns version info and compatibility status.
   */
  async detect(httpClient: HttpClient): Promise<AgentVersionInfo> {
    try {
      const resp = await httpClient.get('/health', { timeoutMs: 5_000, retries: 1 });

      if (resp.statusCode !== 200) {
        throw new Error(`Health check returned ${resp.statusCode}`);
      }

      const health: HealthResponse = JSON.parse(resp.body);
      const version = health.version || '0.0.0';
      const apiVersion = health.api_version || '0.0.0';
      const compatible = isVersionInRange(version, MIN_VERSION, MAX_VERSION);

      this.versionInfo = { version, apiVersion, compatible };

      if (!compatible) {
        logger.warn(
          `Agent version ${version} is outside compatible range [${MIN_VERSION}, ${MAX_VERSION}]`,
          'bridge:version',
        );
      } else {
        logger.info(
          `Agent version detected: ${version} (API ${apiVersion})`,
          'bridge:version',
        );
      }

      return this.versionInfo;
    } catch (err) {
      const info: AgentVersionInfo = {
        version: '0.0.0',
        apiVersion: '0.0.0',
        compatible: false,
      };
      this.versionInfo = info;

      logger.error('Failed to detect Agent version', 'bridge:version', {
        error: err instanceof Error ? err.message : String(err),
      });
      return info;
    }
  }

  /**
   * Get the cached version info (call detect() first).
   */
  getVersionInfo(): AgentVersionInfo | null {
    return this.versionInfo;
  }

  /**
   * Check if the detected version is compatible.
   */
  isCompatible(): boolean {
    return this.versionInfo?.compatible ?? false;
  }

  // ── API Adaptation Methods ────────────────────────────────
  // These methods normalize API paths/payloads across Agent versions.

  /**
   * Get the chat API path (adapts if path changes across versions).
   */
  getChatPath(): string {
    // v1.1.x: POST /api/chat
    return '/api/chat';
  }

  /**
   * Get the tool call API path.
   */
  getToolCallPath(toolName: string): string {
    // v1.1.x: POST /api/tools/{tool_name}
    return `/api/tools/${encodeURIComponent(toolName)}`;
  }

  /**
   * Get the config update API path.
   */
  getConfigPath(): string {
    // v1.1.x: POST /api/config
    return '/api/config';
  }

  /**
   * Get the cron/schedule API paths.
   */
  getCronPaths(): { list: string; create: string; delete: string } {
    return {
      list: '/api/cron/list',
      create: '/api/cron/create',
      delete: '/api/cron/delete',
    };
  }

  /**
   * Get the skill management API paths.
   */
  getSkillPaths(): { list: string; install: string; uninstall: string } {
    return {
      list: '/api/skills/list',
      install: '/api/skills/install',
      uninstall: '/api/skills/uninstall',
    };
  }

  /**
   * Get the MCP management API paths.
   */
  getMCPPaths(): { list: string; connect: string; disconnect: string; tools: string } {
    return {
      list: '/api/mcp/list',
      connect: '/api/mcp/connect',
      disconnect: '/api/mcp/disconnect',
      tools: '/api/mcp/tools',
    };
  }

  /**
   * Adapt the chat request payload if needed for the current version.
   */
  adaptChatRequest(message: string, context?: Record<string, unknown>): Record<string, unknown> {
    // v1.1.x: { message, ...context }
    return {
      message,
      ...context,
    };
  }

  /**
   * Adapt the tool call request payload.
   */
  adaptToolCallRequest(params: Record<string, unknown>): Record<string, unknown> {
    // v1.1.x: pass params directly
    return params;
  }
}
