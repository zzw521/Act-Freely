// ============================================================
// src/main/bridge/agent-bridge.ts — Agent subprocess manager
// ============================================================

import { spawn, type ChildProcess } from 'child_process';
import { EventEmitter } from 'events';
import net from 'net';
import Logger from '../util/logger';
import { HttpClient } from './http-client';
import { SSEParser, type AgentEvent } from './sse-parser';
import { ConfigInjector, type WorkspaceConfig } from './config-injector';
import { VersionAdapter } from './version-adapter';
import { detectPython, detectAgent, ensureWorkspaceDir, getAgentStartCommand } from './python-env';

// ── Types ────────────────────────────────────────────────────

export type InstanceStatus = 'starting' | 'ready' | 'busy' | 'stopped' | 'error';

export interface AgentInstance {
  workspaceId: string;
  process: ChildProcess;
  httpPort: number;
  status: InstanceStatus;
  config: WorkspaceConfig;
  httpClient: HttpClient;
  versionAdapter: VersionAdapter;
  startedAt: Date;
  healthFailCount: number;
}

export interface ToolResult {
  success: boolean;
  output: string;
  error?: string;
  durationMs: number;
}

export interface BridgeEvents {
  'instance-status': (workspaceId: string, status: InstanceStatus) => void;
  'instance-error': (workspaceId: string, error: string) => void;
  'agent-event': (workspaceId: string, event: AgentEvent) => void;
}

// ── Constants ────────────────────────────────────────────────

const HEALTH_CHECK_INTERVAL_MS = 10_000;
const HEALTH_CHECK_MAX_FAILURES = 3;
const GRACEFUL_SHUTDOWN_TIMEOUT_MS = 5_000;
const HEALTH_CHECK_POLL_MS = 500;
const HEALTH_CHECK_MAX_WAIT_MS = 30_000;

const PORT_RANGE_START = 18000;
const PORT_RANGE_END = 19000;

const logger = Logger.getInstance();

// ── Port Allocation ──────────────────────────────────────────

const usedPorts = new Set<number>();

async function findFreePort(): Promise<number> {
  for (let port = PORT_RANGE_START; port < PORT_RANGE_END; port++) {
    if (usedPorts.has(port)) continue;

    const isFree = await new Promise<boolean>((resolve) => {
      const server = net.createServer();
      server.once('error', () => resolve(false));
      server.listen(port, '127.0.0.1', () => {
        server.close(() => resolve(true));
      });
    });

    if (isFree) {
      usedPorts.add(port);
      return port;
    }
  }
  throw new Error('No free ports available in range');
}

function releasePort(port: number): void {
  usedPorts.delete(port);
}

// ── Bridge ───────────────────────────────────────────────────

/**
 * Agent Bridge — manages Agent Python subprocess lifecycle
 * and HTTP/SSE communication.
 *
 * Each workspace gets its own isolated Agent instance with a
 * unique port and working directory.
 */
export class AgentBridge extends EventEmitter {
  private instances = new Map<string, AgentInstance>();
  private configInjector = new ConfigInjector();
  private healthCheckTimers = new Map<string, NodeJS.Timeout>();
  private pythonPath: string | null = null;

  constructor() {
    super();
  }

  /**
   * Initialize the bridge — detect Python and Agent installation.
   * Must be called once at app startup.
   */
  async initialize(): Promise<void> {
    const python = await detectPython();
    if (!python) {
      logger.error('Python not found on system', 'bridge');
      throw new Error('BRIDGE_PYTHON_NOT_FOUND: Python 3.10+ is required');
    }

    const agent = await detectAgent(python.path);
    if (!agent.installed) {
      logger.error('Agent not installed', 'bridge');
      throw new Error('BRIDGE_AGENT_NOT_FOUND: pip install agent is required');
    }

    this.pythonPath = python.path;
    logger.info(
      `Bridge initialized — Python ${python.version}, Agent ${agent.version || 'unknown'}`,
      'bridge',
    );
  }

  /**
   * Start an Agent instance for the given workspace.
   */
  async startWorkspace(workspaceId: string, config: WorkspaceConfig): Promise<AgentInstance> {
    if (!this.pythonPath) {
      throw new Error('Bridge not initialized — call initialize() first');
    }

    // Prevent duplicate instances
    const existing = this.instances.get(workspaceId);
    if (existing && (existing.status === 'ready' || existing.status === 'starting')) {
      logger.warn(`Instance already exists for workspace ${workspaceId}`, 'bridge');
      return existing;
    }

    // Allocate port
    const port = await findFreePort();

    // Ensure working directory
    const workDir = await ensureWorkspaceDir(workspaceId);
    config.workDir = workDir;
    config.workspaceId = workspaceId;

    // Write config file
    const configPath = await this.configInjector.writeConfigFile(config);

    // Create HTTP client
    const httpClient = new HttpClient('127.0.0.1', port);

    // Create version adapter
    const versionAdapter = new VersionAdapter();

    // Spawn Agent subprocess
    const args = getAgentStartCommand(this.pythonPath, port, workDir);
    const [cmd, ...cmdArgs] = args;

    logger.info(`Spawning Agent for workspace ${workspaceId} on port ${port}`, 'bridge');

    const child = spawn(cmd, [...cmdArgs, '--config', configPath], {
      cwd: workDir,
      env: {
        ...process.env,
        PYTHONUNBUFFERED: '1',
      },
      stdio: ['pipe', 'pipe', 'pipe'],
    });

    const instance: AgentInstance = {
      workspaceId,
      process: child,
      httpPort: port,
      status: 'starting',
      config,
      httpClient,
      versionAdapter,
      startedAt: new Date(),
      healthFailCount: 0,
    };

    this.instances.set(workspaceId, instance);
    this.emit('instance-status', workspaceId, 'starting');

    // Pipe stdout/stderr to logger
    child.stdout?.on('data', (data: Buffer) => {
      const msg = data.toString().trim();
      if (msg) logger.debug(`[Agent ${workspaceId}] ${msg}`, 'bridge:stdout');
    });

    child.stderr?.on('data', (data: Buffer) => {
      const msg = data.toString().trim();
      if (msg) logger.warn(`[Agent ${workspaceId}] ${msg}`, 'bridge:stderr');
    });

    // Handle process exit
    child.on('exit', (code, signal) => {
      logger.info(
        `Agent process exited for workspace ${workspaceId} (code=${code}, signal=${signal})`,
        'bridge',
      );
      this.handleProcessExit(workspaceId, code, signal);
    });

    child.on('error', (err) => {
      logger.error(`Agent process error for workspace ${workspaceId}`, 'bridge', {
        error: err.message,
      });
      this.setInstanceStatus(instance, 'error');
      this.emit('instance-error', workspaceId, err.message);
    });

    // Wait for health check to pass
    try {
      await this.waitForReady(httpClient);
      // Detect version
      await versionAdapter.detect(httpClient);

      if (!versionAdapter.isCompatible()) {
        logger.warn(
          `Agent version incompatible for workspace ${workspaceId}`,
          'bridge',
        );
        this.emit('instance-error', workspaceId, 'Agent engine version incompatible');
      }

      this.setInstanceStatus(instance, 'ready');
      this.startHealthCheck(workspaceId);

      logger.info(`Workspace ${workspaceId} is ready on port ${port}`, 'bridge');
    } catch (err) {
      logger.error(
        `Workspace ${workspaceId} failed to become ready`,
        'bridge',
        { error: err instanceof Error ? err.message : String(err) },
      );
      this.setInstanceStatus(instance, 'error');
      this.emit('instance-error', workspaceId, 'Failed to start: health check timeout');
    }

    return instance;
  }

  /**
   * Stop an Agent instance for the given workspace.
   * Sends SIGTERM, waits up to 5s, then SIGKILL.
   */
  async stopWorkspace(workspaceId: string): Promise<void> {
    const instance = this.instances.get(workspaceId);
    if (!instance) {
      logger.warn(`No instance found for workspace ${workspaceId}`, 'bridge');
      return;
    }

    // Stop health check
    this.stopHealthCheck(workspaceId);

    const { process: child, httpPort } = instance;

    if (child.exitCode !== null || child.killed) {
      // Already dead
      this.cleanupInstance(workspaceId);
      return;
    }

    logger.info(`Stopping workspace ${workspaceId}...`, 'bridge');

    // Graceful shutdown
    return new Promise<void>((resolve) => {
      const killTimer = setTimeout(() => {
        logger.warn(`Force killing workspace ${workspaceId} after timeout`, 'bridge');
        try {
          child.kill('SIGKILL');
        } catch {
          // Process may already be dead
        }
      }, GRACEFUL_SHUTDOWN_TIMEOUT_MS);

      child.once('exit', () => {
        clearTimeout(killTimer);
        this.cleanupInstance(workspaceId);
        logger.info(`Workspace ${workspaceId} stopped, port ${httpPort} released`, 'bridge');
        resolve();
      });

      try {
        child.kill('SIGTERM');
      } catch {
        clearTimeout(killTimer);
        this.cleanupInstance(workspaceId);
        resolve();
      }
    });
  }

  /**
   * Send a message to the Agent instance and return an SSE event stream.
   */
  async sendMessage(workspaceId: string, message: string): Promise<AsyncIterable<AgentEvent>> {
    const instance = this.getInstance(workspaceId);
    const { httpClient, versionAdapter } = instance;

    // Mark as busy
    this.setInstanceStatus(instance, 'busy');

    const chatPath = versionAdapter.getChatPath();
    const payload = versionAdapter.adaptChatRequest(message);

    try {
      const stream = await httpClient.stream(chatPath, payload);
      const parser = new SSEParser();

      parser.consume(stream, {
        onEvent: (event) => {
          this.emit('agent-event', workspaceId, event);
        },
        onEnd: () => {
          // Return to ready after stream ends
          if (instance.status === 'busy') {
            this.setInstanceStatus(instance, 'ready');
          }
        },
        onError: (err) => {
          logger.error(`SSE stream error for workspace ${workspaceId}`, 'bridge', {
            error: err.message,
          });
          if (instance.status === 'busy') {
            this.setInstanceStatus(instance, 'ready');
          }
        },
      });

      return parser;
    } catch (err) {
      // Return to ready on error
      if (instance.status === 'busy') {
        this.setInstanceStatus(instance, 'ready');
      }
      throw err;
    }
  }

  /**
   * Call a tool on the Agent instance.
   */
  async callTool(
    workspaceId: string,
    toolName: string,
    params: Record<string, unknown>,
  ): Promise<ToolResult> {
    const instance = this.getInstance(workspaceId);
    const { httpClient, versionAdapter } = instance;

    const toolPath = versionAdapter.getToolCallPath(toolName);
    const payload = versionAdapter.adaptToolCallRequest(params);

    const start = Date.now();
    try {
      const resp = await httpClient.post(toolPath, payload, {
        timeoutMs: 30_000,
        retries: 0, // No retry for tool calls
      });

      const durationMs = Date.now() - start;

      if (resp.statusCode >= 200 && resp.statusCode < 300) {
        const body = JSON.parse(resp.body);
        return {
          success: true,
          output: typeof body.output === 'string' ? body.output : JSON.stringify(body),
          durationMs,
        };
      }

      return {
        success: false,
        output: '',
        error: `Tool call failed with status ${resp.statusCode}: ${resp.body.slice(0, 500)}`,
        durationMs,
      };
    } catch (err) {
      return {
        success: false,
        output: '',
        error: err instanceof Error ? err.message : String(err),
        durationMs: Date.now() - start,
      };
    }
  }

  /**
   * Get the status of a workspace instance.
   */
  getStatus(workspaceId: string): InstanceStatus {
    const instance = this.instances.get(workspaceId);
    return instance?.status ?? 'stopped';
  }

  /**
   * Run a health check on a workspace instance.
   */
  async healthCheck(workspaceId: string): Promise<{ ok: boolean; latencyMs: number }> {
    const instance = this.instances.get(workspaceId);
    if (!instance) {
      return { ok: false, latencyMs: 0 };
    }
    return instance.httpClient.healthCheck();
  }

  /**
   * Hot-update the configuration for a running workspace.
   */
  async updateConfig(workspaceId: string, config: Partial<WorkspaceConfig>): Promise<boolean> {
    const instance = this.getInstance(workspaceId);
    const mergedConfig: WorkspaceConfig = { ...instance.config, ...config };
    return this.configInjector.hotUpdate(instance.httpClient, mergedConfig);
  }

  /**
   * Stop all running instances. Called during app shutdown.
   */
  async shutdownAll(): Promise<void> {
    const workspaceIds = Array.from(this.instances.keys());
    logger.info(`Shutting down ${workspaceIds.length} workspace instances`, 'bridge');

    await Promise.all(workspaceIds.map((id) => this.stopWorkspace(id)));

    // Clear all timers
    for (const timer of this.healthCheckTimers.values()) {
      clearInterval(timer);
    }
    this.healthCheckTimers.clear();
    this.instances.clear();
  }

  /**
   * Get all active instance IDs.
   */
  getActiveWorkspaceIds(): string[] {
    return Array.from(this.instances.keys()).filter((id) => {
      const inst = this.instances.get(id);
      return inst && inst.status !== 'stopped' && inst.status !== 'error';
    });
  }

  // ── Private ────────────────────────────────────────────────

  private getInstance(workspaceId: string): AgentInstance {
    const instance = this.instances.get(workspaceId);
    if (!instance) {
      throw new Error(`No instance found for workspace ${workspaceId}`);
    }
    if (instance.status === 'error' || instance.status === 'stopped') {
      throw new Error(`Instance for workspace ${workspaceId} is not available (status: ${instance.status})`);
    }
    return instance;
  }

  private setInstanceStatus(instance: AgentInstance, status: InstanceStatus): void {
    instance.status = status;
    this.emit('instance-status', instance.workspaceId, status);
  }

  private async waitForReady(httpClient: HttpClient): Promise<void> {
    const deadline = Date.now() + HEALTH_CHECK_MAX_WAIT_MS;

    while (Date.now() < deadline) {
      const { ok } = await httpClient.healthCheck(3_000);
      if (ok) return;
      await sleep(HEALTH_CHECK_POLL_MS);
    }

    throw new Error('Health check timeout: instance did not become ready');
  }

  private startHealthCheck(workspaceId: string): void {
    this.stopHealthCheck(workspaceId);

    const timer = setInterval(async () => {
      const instance = this.instances.get(workspaceId);
      if (!instance || instance.status === 'stopped') {
        this.stopHealthCheck(workspaceId);
        return;
      }

      const { ok } = await instance.httpClient.healthCheck();
      if (!ok) {
        instance.healthFailCount++;
        logger.warn(
          `Health check failed for workspace ${workspaceId} (${instance.healthFailCount}/${HEALTH_CHECK_MAX_FAILURES})`,
          'bridge',
        );

        if (instance.healthFailCount >= HEALTH_CHECK_MAX_FAILURES) {
          logger.error(`Workspace ${workspaceId} marked unavailable after ${HEALTH_CHECK_MAX_FAILURES} health failures`, 'bridge');
          this.setInstanceStatus(instance, 'error');
          this.emit('instance-error', workspaceId, 'Health check failed repeatedly');
          this.stopHealthCheck(workspaceId);
        }
      } else {
        // Reset counter on success
        if (instance.healthFailCount > 0) {
          instance.healthFailCount = 0;
        }
      }
    }, HEALTH_CHECK_INTERVAL_MS);

    this.healthCheckTimers.set(workspaceId, timer);
  }

  private stopHealthCheck(workspaceId: string): void {
    const timer = this.healthCheckTimers.get(workspaceId);
    if (timer) {
      clearInterval(timer);
      this.healthCheckTimers.delete(workspaceId);
    }
  }

  private handleProcessExit(workspaceId: string, code: number | null, signal: string | null): void {
    const instance = this.instances.get(workspaceId);
    if (!instance) return;

    this.stopHealthCheck(workspaceId);

    if (code !== 0 && code !== null) {
      // Abnormal exit
      logger.error(
        `Agent process crashed for workspace ${workspaceId} (exit code: ${code})`,
        'bridge',
      );
      this.setInstanceStatus(instance, 'error');
      this.emit('instance-error', workspaceId, `Process crashed with exit code ${code}`);
    } else {
      // Normal exit or signal
      this.setInstanceStatus(instance, 'stopped');
    }

    // Release port
    releasePort(instance.httpPort);
  }

  private cleanupInstance(workspaceId: string): void {
    const instance = this.instances.get(workspaceId);
    if (instance) {
      releasePort(instance.httpPort);
      this.setInstanceStatus(instance, 'stopped');
    }
    this.instances.delete(workspaceId);
  }
}

// ── Helpers ──────────────────────────────────────────────────

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// ── Singleton ────────────────────────────────────────────────

let bridgeInstance: AgentBridge | null = null;

export function getBridge(): AgentBridge {
  if (!bridgeInstance) {
    bridgeInstance = new AgentBridge();
  }
  return bridgeInstance;
}
