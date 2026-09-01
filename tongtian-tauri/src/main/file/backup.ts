// ═══════════════════════════════════════════════════════════
// src/main/file/backup.ts — 安全备份与回滚机制
// ═══════════════════════════════════════════════════════════

import path from 'path';
import fs from 'fs';
import crypto from 'crypto';
import { app } from 'electron';
import Logger from '../util/logger';
import Database from '../store/db';

// ── Types ─────────────────────────────────────────────────

export interface BackupRecord {
  id: string;
  taskId: string;
  stepId: string;
  filePath: string;
  sha256: string;
  backupPath: string;
  status: 'pending' | 'success' | 'failed' | 'rolled_back';
  createdAt: string;
}

export interface BackupOptions {
  taskId: string;
  stepId: string;
  filePath: string;
}

// ── Constants ─────────────────────────────────────────────

const BACKUP_DIR_NAME = '.tongtian-backups';
const CLEANUP_INTERVAL_MS = 60 * 60 * 1000; // 1 hour
const BACKUP_RETENTION_MS = 24 * 60 * 60 * 1000; // 24 hours

// ── BackupManager ─────────────────────────────────────────

export class BackupManager {
  private static instance: BackupManager | null = null;
  private logger = Logger.getInstance();
  private cleanupTimer: NodeJS.Timeout | null = null;

  static getInstance(): BackupManager {
    if (!BackupManager.instance) {
      BackupManager.instance = new BackupManager();
    }
    return BackupManager.instance;
  }

  static resetInstance(): void {
    if (BackupManager.instance?.cleanupTimer) {
      clearInterval(BackupManager.instance.cleanupTimer);
    }
    BackupManager.instance = null;
  }

  // ── Initialization ────────────────────────────────────

  initialize(): void {
    this.startCleanupSchedule();
    this.logger.info('BackupManager initialized', { module: 'backup' });
  }

  // ── Core Operations ───────────────────────────────────

  /**
   * 计算文件 SHA256 指纹
   */
  calculateSHA256(filePath: string): string {
    try {
      const content = fs.readFileSync(filePath);
      return crypto.createHash('sha256').update(content).digest('hex');
    } catch (error: any) {
      if (error.code === 'ENOENT') {
        // File doesn't exist yet
        return '';
      }
      throw error;
    }
  }

  /**
   * 创建文件备份
   */
  async backup(options: BackupOptions): Promise<BackupRecord> {
    const { taskId, stepId, filePath } = options;
    const id = this.generateId();
    const backupDir = this.getBackupDir(taskId, stepId);
    const backupPath = path.join(backupDir, path.basename(filePath));

    // Ensure backup directory exists
    if (!fs.existsSync(backupDir)) {
      fs.mkdirSync(backupDir, { recursive: true });
    }

    // Calculate SHA256 before backup
    const sha256 = this.calculateSHA256(filePath);

    // Create backup record
    const record: BackupRecord = {
      id,
      taskId,
      stepId,
      filePath,
      sha256,
      backupPath,
      status: 'pending',
      createdAt: new Date().toISOString(),
    };

    try {
      // Copy file if it exists
      if (fs.existsSync(filePath)) {
        fs.copyFileSync(filePath, backupPath);
        this.logger.debug('File backed up', {
          module: 'backup',
          filePath,
          backupPath,
          sha256,
        });
      } else {
        this.logger.debug('File does not exist, skipping backup', {
          module: 'backup',
          filePath,
        });
      }

      record.status = 'success';
      this.saveRecord(record);
      return record;
    } catch (error: any) {
      this.logger.error('Backup failed', {
        module: 'backup',
        filePath,
        error: error.message,
      });
      record.status = 'failed';
      this.saveRecord(record);
      throw error;
    }
  }

  /**
   * 从备份恢复文件（回滚）
   */
  async rollback(backupId: string): Promise<boolean> {
    const record = this.getRecord(backupId);
    if (!record) {
      this.logger.warn('Backup record not found', { module: 'backup', backupId });
      return false;
    }

    try {
      // Restore file from backup
      if (fs.existsSync(record.backupPath)) {
        fs.copyFileSync(record.backupPath, record.filePath);
        this.logger.info('File rolled back', {
          module: 'backup',
          filePath: record.filePath,
          backupPath: record.backupPath,
        });
      } else if (record.sha256 === '') {
        // File didn't exist before, delete it
        if (fs.existsSync(record.filePath)) {
          fs.unlinkSync(record.filePath);
        }
      }

      // Update status
      record.status = 'rolled_back';
      this.updateRecord(record);
      return true;
    } catch (error: any) {
      this.logger.error('Rollback failed', {
        module: 'backup',
        backupId,
        error: error.message,
      });
      return false;
    }
  }

  /**
   * 标记备份为失败状态
   */
  markFailed(backupId: string): void {
    const record = this.getRecord(backupId);
    if (record) {
      record.status = 'failed';
      this.updateRecord(record);
    }
  }

  // ── Query Operations ──────────────────────────────────

  /**
   * 获取备份记录
   */
  getRecord(id: string): BackupRecord | null {
    const db = Database.getInstance().getDb();
    const stmt = db.prepare('SELECT * FROM file_backups WHERE id = ?');
    const row = stmt.get(id) as any;
    return row ? this.mapRowToRecord(row) : null;
  }

  /**
   * 获取任务的所有备份
   */
  getTaskBackups(taskId: string): BackupRecord[] {
    const db = Database.getInstance().getDb();
    const stmt = db.prepare('SELECT * FROM file_backups WHERE task_id = ? ORDER BY created_at DESC');
    const rows = stmt.all(taskId) as any[];
    return rows.map(this.mapRowToRecord);
  }

  /**
   * 获取 Step 的所有备份
   */
  getStepBackups(stepId: string): BackupRecord[] {
    const db = Database.getInstance().getDb();
    const stmt = db.prepare('SELECT * FROM file_backups WHERE step_id = ? ORDER BY created_at DESC');
    const rows = stmt.all(stepId) as any[];
    return rows.map(this.mapRowToRecord);
  }

  // ── Cleanup ───────────────────────────────────────────

  /**
   * 清理过期备份（> 24h）
   */
  cleanupExpiredBackups(): number {
    const cutoff = Date.now() - BACKUP_RETENTION_MS;
    const cutoffDate = new Date(cutoff).toISOString();

    const db = Database.getInstance().getDb();
    const stmt = db.prepare('SELECT * FROM file_backups WHERE created_at < ?');
    const oldRecords = stmt.all(cutoffDate) as any[];

    let cleanedCount = 0;
    for (const row of oldRecords) {
      const record = this.mapRowToRecord(row);

      // Delete backup file
      try {
        if (fs.existsSync(record.backupPath)) {
          fs.unlinkSync(record.backupPath);
        }
      } catch (error: any) {
        this.logger.warn('Failed to delete backup file', {
          module: 'backup',
          backupPath: record.backupPath,
          error: error.message,
        });
      }

      // Delete database record
      const deleteStmt = db.prepare('DELETE FROM file_backups WHERE id = ?');
      deleteStmt.run(record.id);
      cleanedCount++;
    }

    if (cleanedCount > 0) {
      this.logger.info('Cleaned up expired backups', {
        module: 'backup',
        count: cleanedCount,
      });
    }

    return cleanedCount;
  }

  /**
   * 启动定时清理任务
   */
  private startCleanupSchedule(): void {
    this.cleanupTimer = setInterval(() => {
      this.cleanupExpiredBackups();
    }, CLEANUP_INTERVAL_MS);
  }

  // ── Private Helpers ───────────────────────────────────

  private getBackupDir(taskId: string, stepId: string): string {
    const userDataPath = app.getPath('userData');
    return path.join(userDataPath, BACKUP_DIR_NAME, taskId, stepId);
  }

  private generateId(): string {
    return `backup_${Date.now()}_${Math.random().toString(36).substring(2, 9)}`;
  }

  private saveRecord(record: BackupRecord): void {
    const db = Database.getInstance().getDb();
    const stmt = db.prepare(`
      INSERT INTO file_backups (id, task_id, step_id, file_path, sha256, backup_path, status, created_at)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    `);
    stmt.run(
      record.id,
      record.taskId,
      record.stepId,
      record.filePath,
      record.sha256,
      record.backupPath,
      record.status,
      record.createdAt,
    );
  }

  private updateRecord(record: BackupRecord): void {
    const db = Database.getInstance().getDb();
    const stmt = db.prepare(`
      UPDATE file_backups
      SET status = ?
      WHERE id = ?
    `);
    stmt.run(record.status, record.id);
  }

  private mapRowToRecord(row: any): BackupRecord {
    return {
      id: row.id,
      taskId: row.task_id,
      stepId: row.step_id,
      filePath: row.file_path,
      sha256: row.sha256,
      backupPath: row.backup_path,
      status: row.status,
      createdAt: row.created_at,
    };
  }
}

export default BackupManager;
