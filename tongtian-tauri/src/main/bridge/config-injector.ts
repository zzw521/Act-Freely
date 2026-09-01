// ============================================================
// src/main/bridge/config-injector.ts — Agent config injection
// ============================================================

import fs from 'fs';
import path from 'path';
import Logger from '../util/logger';
import type { HttpClient } from './http-client';

// ── Types ────────────────────────────────────────────────────

export interface WorkspaceConfig {
  workspaceId: string;
  workDir: string;
  agentConfig: {
    role: string;
    systemPrompt: string;
  };
  modelBinding?: ModelBindingConfig;
  skillIds?: string[];
  mcpServers?: MCPServerConfig[];
  sandboxPolicy?: SandboxPolicyConfig;
}

export interface ModelBindingConfig {
  provider: string;
  baseUrl: string;
  apiKey: string;
  modelName: string;
  contextWindow?: number;
}

export interface MCPServerConfig {
  name: string;
  transport: 'stdio' | 'sse' | 'streamable-http';
  endpoint: string;
  authType?: 'none' | 'oauth2' | 'api_key';
  authConfig?: Record<string, unknown>;
}

export interface SandboxPolicyConfig {
  executionLevel: 'L0' | 'L1' | 'L2';
  fileAllowlist?: string[];
  fileBlocklist?: string[];
  toolAllowlist?: string[];
  toolBlocklist?: string[];
}

/**
 * The JSON structure written to disk / sent via POST /api/config.
 */
export interface AgentConfig {
  workspace_id: string;
  work_dir: string;
  agent: {
    role: string;
    system_prompt: string;
  };
  model?: {
    provider: string;
    base_url: string;
    api_key: string;
    model_name: string;
    context_window?: number;
  };
  skills?: string[];
  mcp_servers?: Array<{
    name: string;
    transport: string;
    endpoint: string;
    auth_type?: string;
    auth_config?: Record<string, unknown>;
  }>;
  sandbox?: {
    execution_level: string;
    file_allowlist?: string[];
    file_blocklist?: string[];
    tool_allowlist?: string[];
    tool_blocklist?: string[];
  };
}

// ── Constants ────────────────────────────────────────────────

const CONFIG_FILE_NAME = 'agent_config.json';
const logger = Logger.getInstance();

// ── Config Injector ──────────────────────────────────────────

/**
 * Generates and injects Agent configuration files based on
 * workspace settings. Supports both file-based injection (at startup)
 * and hot-reload via HTTP API (at runtime).
 */
export class ConfigInjector {
  /**
   * Generate an Agent config object from a WorkspaceConfig.
   */
  generate(config: WorkspaceConfig): AgentConfig {
    const result: AgentConfig = {
      workspace_id: config.workspaceId,
      work_dir: config.workDir,
      agent: {
        role: config.agentConfig.role,
        system_prompt: config.agentConfig.systemPrompt,
      },
    };

    if (config.modelBinding) {
      result.model = {
        provider: config.modelBinding.provider,
        base_url: config.modelBinding.baseUrl,
        api_key: config.modelBinding.apiKey,
        model_name: config.modelBinding.modelName,
        context_window: config.modelBinding.contextWindow,
      };
    }

    if (config.skillIds && config.skillIds.length > 0) {
      result.skills = config.skillIds;
    }

    if (config.mcpServers && config.mcpServers.length > 0) {
      result.mcp_servers = config.mcpServers.map((s) => ({
        name: s.name,
        transport: s.transport,
        endpoint: s.endpoint,
        auth_type: s.authType,
        auth_config: s.authConfig,
      }));
    }

    if (config.sandboxPolicy) {
      result.sandbox = {
        execution_level: config.sandboxPolicy.executionLevel,
        file_allowlist: config.sandboxPolicy.fileAllowlist,
        file_blocklist: config.sandboxPolicy.fileBlocklist,
        tool_allowlist: config.sandboxPolicy.toolAllowlist,
        tool_blocklist: config.sandboxPolicy.toolBlocklist,
      };
    }

    return result;
  }

  /**
   * Write the config file to the workspace directory.
   * Called during workspace startup before spawning the Agent process.
   */
  async writeConfigFile(config: WorkspaceConfig): Promise<string> {
    const agentConfig = this.generate(config);
    const configPath = path.join(config.workDir, CONFIG_FILE_NAME);

    await fs.promises.mkdir(config.workDir, { recursive: true });
    await fs.promises.writeFile(configPath, JSON.stringify(agentConfig, null, 2), 'utf8');

    logger.info('Config file written', 'bridge:config', { configPath });
    return configPath;
  }

  /**
   * Hot-update config via Agent HTTP API without restarting the process.
   * POST /api/config with the new config body.
   */
  async hotUpdate(httpClient: HttpClient, config: WorkspaceConfig): Promise<boolean> {
    const agentConfig = this.generate(config);

    try {
      const resp = await httpClient.post('/api/config', agentConfig, {
        timeoutMs: 10_000,
        retries: 1,
      });

      if (resp.statusCode >= 200 && resp.statusCode < 300) {
        logger.info('Config hot-updated via API', 'bridge:config', {
          workspaceId: config.workspaceId,
        });
        return true;
      }

      logger.warn('Config hot-update returned non-2xx', 'bridge:config', {
        statusCode: resp.statusCode,
        body: resp.body.slice(0, 200),
      });
      return false;
    } catch (err) {
      logger.error('Config hot-update failed', 'bridge:config', {
        error: err instanceof Error ? err.message : String(err),
      });
      return false;
    }
  }

  /**
   * Read an existing config file from disk (for debugging / verification).
   */
  async readConfigFile(workDir: string): Promise<AgentConfig | null> {
    const configPath = path.join(workDir, CONFIG_FILE_NAME);

    try {
      const content = await fs.promises.readFile(configPath, 'utf8');
      return JSON.parse(content) as AgentConfig;
    } catch {
      return null;
    }
  }

  /**
   * Remove the config file (cleanup on workspace destroy).
   */
  async removeConfigFile(workDir: string): Promise<void> {
    const configPath = path.join(workDir, CONFIG_FILE_NAME);

    try {
      await fs.promises.unlink(configPath);
      logger.info('Config file removed', 'bridge:config', { configPath });
    } catch {
      // File might not exist — that's fine
    }
  }
}
