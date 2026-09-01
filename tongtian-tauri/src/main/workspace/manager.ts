// ═══════════════════════════════════════════════════════════
// src/main/workspace/manager.ts — 工作区生命周期管理器
// ═══════════════════════════════════════════════════════════

import path from 'path';
import fs from 'fs';
import { app } from 'electron';
import Logger from '../util/logger';
import Database from '../store/db';
import { AgentBridge } from '../bridge/agent-bridge';
import type { WorkspaceConfig } from '../bridge/config-injector';
import { WorkspaceStatus } from '../../shared/types';
import type { Workspace } from '../../shared/types';
import type { WorkspaceCreateReq } from '../../shared/contracts';

// ── Types ─────────────────────────────────────────────────

export interface WorkspaceRuntime {
  workspaceId: string;
  config: WorkspaceConfig;
  httpPort: number;
  startedAt: Date;
}

export interface WorkspaceCreateInput extends WorkspaceCreateReq {}

// ── Constants ─────────────────────────────────────────────

const ARCHIVE_DIR_NAME = '.tongtian-archives';

// ── WorkspaceManager ──────────────────────────────────────

export class WorkspaceManager {
  private static instance: WorkspaceManager | null = null;
  private logger = Logger.getInstance();
  private bridge: AgentBridge;
  private runtimes = new Map<string, WorkspaceRuntime>();

  static getInstance(): WorkspaceManager {
    if (!WorkspaceManager.instance) {
      WorkspaceManager.instance = new WorkspaceManager();
    }
    return WorkspaceManager.instance;
  }

  static resetInstance(): void {
    WorkspaceManager.instance = null;
  }

  constructor() {
    this.bridge = AgentBridge.getInstance();
  }

  // ── Create ────────────────────────────────────────────

  async create(input: WorkspaceCreateInput): Promise<Workspace> {
    const id = this.generateId();
    const memoryStoreId = `mem_${id}`;
    const now = new Date().toISOString();

    const agentConfigJson = JSON.stringify(input.agentConfig);
    const skillSetJson = JSON.stringify(input.skillIds || []);
    const mcpToolsJson = JSON.stringify(input.mcpServerIds || []);

    const workspace: Workspace = {
      id,
      name: input.name,
      agentConfigJson,
      modelBinding: input.modelBinding || '',
      status: WorkspaceStatus.Active,
      memoryStoreId,
      skillSetJson,
      mcpToolsJson,
      createdAt: now,
      updatedAt: now,
    };

    // Persist to DB
    const db = Database.getInstance().getDb();
    const stmt = db.prepare(`
      INSERT INTO workspaces (id, name, agent_config_json, model_binding, status, memory_store_id, skill_set_json, mcp_tools_json, created_at, updated_at)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    `);
    stmt.run(
      workspace.id,
      workspace.name,
      workspace.agentConfigJson,
      workspace.modelBinding,
      workspace.status,
      workspace.memoryStoreId,
      workspace.skillSetJson,
      workspace.mcpToolsJson,
      workspace.createdAt,
      workspace.updatedAt,
    );

    // Start Agent subprocess
    try {
      const config = this.buildWorkspaceConfig(workspace);
      await this.bridge.startWorkspace(id, config);

      const instance = this.bridge.getStatus(id);
      this.runtimes.set(id, {
        workspaceId: id,
        config,
        httpPort: 0, // Port managed by bridge
        startedAt: new Date(),
      });

      this.logger.info('Workspace created and started', {
        module: 'workspace',
        workspaceId: id,
        name: workspace.name,
      });
    } catch (error: any) {
      this.logger.error('Failed to start workspace subprocess', {
        module: 'workspace',
        workspaceId: id,
        error: error.message,
      });
      // Workspace is created in DB but subprocess failed — mark as suspended
      this.updateStatus(id, WorkspaceStatus.Suspended);
      workspace.status = WorkspaceStatus.Suspended;
    }

    return workspace;
  }

  // ── List ──────────────────────────────────────────────

  list(): Workspace[] {
    const db = Database.getInstance().getDb();
    const stmt = db.prepare('SELECT * FROM workspaces ORDER BY created_at DESC');
    const rows = stmt.all() as any[];
    return rows.map(this.mapRowToWorkspace);
  }

  // ── Get by ID ─────────────────────────────────────────

  getById(id: string): Workspace | null {
    const db = Database.getInstance().getDb();
    const stmt = db.prepare('SELECT * FROM workspaces WHERE id = ?');
    const row = stmt.get(id) as any;
    return row ? this.mapRowToWorkspace(row) : null;
  }

  // ── Switch (set active workspace) ─────────────────────

  switch(workspaceId: string): { ok: boolean } {
    const ws = this.getById(workspaceId);
    if (!ws) {
      throw new Error('WORKSPACE_NOT_FOUND');
    }

    if (ws.status !== WorkspaceStatus.Active) {
      throw new Error('WORKSPACE_SUSPENDED');
    }

    // TODO: Persist "current workspace" preference
    this.logger.info('Workspace switched', {
      module: 'workspace',
      workspaceId,
    });

    return { ok: true };
  }

  // ── Suspend ───────────────────────────────────────────

  async suspend(workspaceId: string): Promise<{ ok: boolean }> {
    const ws = this.getById(workspaceId);
    if (!ws) {
      throw new Error('WORKSPACE_NOT_FOUND');
    }

    if (ws.status === WorkspaceStatus.Archived) {
      throw new Error('WORKSPACE_SUSPENDED');
    }

    // Stop Agent subprocess gracefully
    try {
      await this.bridge.stopWorkspace(workspaceId);
    } catch (error: any) {
      this.logger.warn('Error stopping workspace subprocess', {
        module: 'workspace',
        workspaceId,
        error: error.message,
      });
    }

    this.runtimes.delete(workspaceId);
    this.updateStatus(workspaceId, WorkspaceStatus.Suspended);

    this.logger.info('Workspace suspended', {
      module: 'workspace',
      workspaceId,
    });

    return { ok: true };
  }

  // ── Resume ────────────────────────────────────────────

  async resume(workspaceId: string): Promise<{ ok: boolean }> {
    const ws = this.getById(workspaceId);
    if (!ws) {
      throw new Error('WORKSPACE_NOT_FOUND');
    }

    if (ws.status === WorkspaceStatus.Active) {
      return { ok: true }; // Already running
    }

    if (ws.status === WorkspaceStatus.Archived) {
      throw new Error('WORKSPACE_SUSPENDED');
    }

    // Restart Agent subprocess
    const config = this.buildWorkspaceConfig(ws);
    try {
      await this.bridge.startWorkspace(workspaceId, config);
      this.runtimes.set(workspaceId, {
        workspaceId,
        config,
        httpPort: 0,
        startedAt: new Date(),
      });
    } catch (error: any) {
      this.logger.error('Failed to resume workspace subprocess', {
        module: 'workspace',
        workspaceId,
        error: error.message,
      });
      throw error;
    }

    this.updateStatus(workspaceId, WorkspaceStatus.Active);

    this.logger.info('Workspace resumed', {
      module: 'workspace',
      workspaceId,
    });

    return { ok: true };
  }

  // ── Archive ───────────────────────────────────────────

  async archive(workspaceId: string): Promise<{ ok: boolean }> {
    const ws = this.getById(workspaceId);
    if (!ws) {
      throw new Error('WORKSPACE_NOT_FOUND');
    }

    // Stop subprocess if running
    if (ws.status === WorkspaceStatus.Active) {
      try {
        await this.bridge.stopWorkspace(workspaceId);
      } catch (error: any) {
        this.logger.warn('Error stopping workspace during archive', {
          module: 'workspace',
          workspaceId,
          error: error.message,
        });
      }
      this.runtimes.delete(workspaceId);
    }

    // Pack workspace data to archive directory
    const archiveDir = this.getArchiveDir(workspaceId);
    if (!fs.existsSync(archiveDir)) {
      fs.mkdirSync(archiveDir, { recursive: true });
    }

    // Save workspace metadata to archive
    const archiveMeta = {
      workspaceId: ws.id,
      name: ws.name,
      archivedAt: new Date().toISOString(),
      agentConfig: JSON.parse(ws.agentConfigJson),
      modelBinding: ws.modelBinding,
    };
    fs.writeFileSync(
      path.join(archiveDir, 'metadata.json'),
      JSON.stringify(archiveMeta, null, 2),
      'utf-8',
    );

    this.updateStatus(workspaceId, WorkspaceStatus.Archived);

    this.logger.info('Workspace archived', {
      module: 'workspace',
      workspaceId,
      archiveDir,
    });

    return { ok: true };
  }

  // ── Destroy ───────────────────────────────────────────

  async destroy(workspaceId: string): Promise<{ ok: boolean }> {
    const ws = this.getById(workspaceId);
    if (!ws) {
      throw new Error('WORKSPACE_NOT_FOUND');
    }

    // Stop subprocess if running
    if (ws.status === WorkspaceStatus.Active) {
      try {
        await this.bridge.stopWorkspace(workspaceId);
      } catch (error: any) {
        this.logger.warn('Error stopping workspace during destroy', {
          module: 'workspace',
          workspaceId,
          error: error.message,
        });
      }
      this.runtimes.delete(workspaceId);
    }

    // Delete from DB
    const db = Database.getInstance().getDb();
    const stmt = db.prepare('DELETE FROM workspaces WHERE id = ?');
    stmt.run(workspaceId);

    // Clean up archive if exists
    const archiveDir = this.getArchiveDir(workspaceId);
    if (fs.existsSync(archiveDir)) {
      fs.rmSync(archiveDir, { recursive: true, force: true });
    }

    this.logger.info('Workspace destroyed', {
      module: 'workspace',
      workspaceId,
    });

    return { ok: true };
  }

  // ── Runtime Info ──────────────────────────────────────

  getRuntime(workspaceId: string): WorkspaceRuntime | null {
    return this.runtimes.get(workspaceId) || null;
  }

  getActiveWorkspaceIds(): string[] {
    return Array.from(this.runtimes.keys());
  }

  // ── Private Helpers ───────────────────────────────────

  private buildWorkspaceConfig(ws: Workspace): WorkspaceConfig {
    const agentConfig = JSON.parse(ws.agentConfigJson);
    const userDataPath = app.getPath('userData');
    return {
      workspaceId: ws.id,
      workDir: path.join(userDataPath, 'workspaces', ws.id),
      agentConfig: {
        role: agentConfig.role || 'assistant',
        systemPrompt: agentConfig.systemPrompt || '',
      },
      skillIds: JSON.parse(ws.skillSetJson || '[]'),
      mcpServers: JSON.parse(ws.mcpToolsJson || '[]'),
    };
  }

  private updateStatus(workspaceId: string, status: WorkspaceStatus): void {
    const db = Database.getInstance().getDb();
    const now = new Date().toISOString();
    const stmt = db.prepare('UPDATE workspaces SET status = ?, updated_at = ? WHERE id = ?');
    stmt.run(status, now, workspaceId);
  }

  private getArchiveDir(workspaceId: string): string {
    const userDataPath = app.getPath('userData');
    return path.join(userDataPath, ARCHIVE_DIR_NAME, workspaceId);
  }

  private generateId(): string {
    return `ws_${Date.now()}_${Math.random().toString(36).substring(2, 9)}`;
  }

  private mapRowToWorkspace(row: any): Workspace {
    return {
      id: row.id,
      name: row.name,
      agentConfigJson: row.agent_config_json,
      modelBinding: row.model_binding,
      status: row.status,
      memoryStoreId: row.memory_store_id,
      skillSetJson: row.skill_set_json,
      mcpToolsJson: row.mcp_tools_json,
      createdAt: row.created_at,
      updatedAt: row.updated_at,
    };
  }
}

export default WorkspaceManager;
