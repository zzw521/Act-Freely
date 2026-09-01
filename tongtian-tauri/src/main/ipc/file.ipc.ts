// ═══════════════════════════════════════════════════════════
// src/main/ipc/file.ipc.ts — File IPC Handler
// ═══════════════════════════════════════════════════════════

import { ipcMain } from 'electron';
import { z } from 'zod';
import fs from 'fs';
import path from 'path';
import { IPC_CHANNELS } from '../../shared/contracts';
import type { FileInfo, FilePreview } from '../../shared/contracts';
import Logger from '../util/logger';
import { FormatConverter } from '../file/convert';
import type { FileFormat } from '../file/convert';

// ── Zod Schemas ─────────────────────────────────────────────

const fileBrowseSchema = z.object({
  dirPath: z.string(),
});

const fileReadSchema = z.object({
  filePath: z.string(),
});

const filePreviewSchema = z.object({
  filePaths: z.array(z.string()),
});

const fileConvertSchema = z.object({
  inputPath: z.string(),
  outputPath: z.string(),
  inputFormat: z.enum(['docx', 'xlsx', 'pdf', 'csv', 'txt', 'md']),
  outputFormat: z.enum(['docx', 'xlsx', 'pdf', 'csv', 'txt', 'md']),
});

// ── Types ───────────────────────────────────────────────────

type SafeHandler = (event: Electron.IpcMainInvokeEvent, ...args: any[]) => Promise<any>;

// ── Helpers ─────────────────────────────────────────────────

const logger = Logger.getInstance();

function safeHandler(handler: SafeHandler): SafeHandler {
  return async (event, ...args) => {
    try {
      return await handler(event, ...args);
    } catch (error: any) {
      logger.error('IPC handler error', {
        module: 'ipc/file',
        error: error.message,
        stack: error.stack,
      });
      throw error;
    }
  };
}

// ── Path Validation ─────────────────────────────────────────

/**
 * 路径白名单校验（防止路径遍历攻击）
 * TODO: 与 SandboxManager 集成获取工作区路径
 */
function isPathAllowed(targetPath: string): boolean {
  const normalizedPath = path.normalize(targetPath);
  
  // Disallow path traversal
  if (normalizedPath.includes('..')) {
    return false;
  }

  // TODO: Check against workspace directory
  // For now, allow all absolute paths
  return path.isAbsolute(normalizedPath);
}

// ── MIME Type Detection ─────────────────────────────────────

function getMimeType(filePath: string): string {
  const ext = path.extname(filePath).toLowerCase();
  const mimeTypes: Record<string, string> = {
    '.txt': 'text/plain',
    '.md': 'text/markdown',
    '.csv': 'text/csv',
    '.json': 'application/json',
    '.pdf': 'application/pdf',
    '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.gif': 'image/gif',
  };
  return mimeTypes[ext] || 'application/octet-stream';
}

// ── IPC Handlers ────────────────────────────────────────────

export function registerFileIpc(): () => void {
  const handlers: Array<{ channel: string; handler: SafeHandler }> = [];

  // ── file:browse ─────────────────────────────────────────

  const browseHandler: SafeHandler = async (_event, args: unknown) => {
    const { dirPath } = fileBrowseSchema.parse(args);

    if (!isPathAllowed(dirPath)) {
      throw new Error('PATH_NOT_ALLOWED');
    }

    if (!fs.existsSync(dirPath)) {
      throw new Error('DIRECTORY_NOT_FOUND');
    }

    const entries = fs.readdirSync(dirPath, { withFileTypes: true });
    const files: FileInfo[] = entries.map(entry => {
      const fullPath = path.join(dirPath, entry.name);
      const stats = fs.statSync(fullPath);

      return {
        name: entry.name,
        path: fullPath,
        isDirectory: entry.isDirectory(),
        size: stats.size,
        modifiedAt: stats.mtime.toISOString(),
      };
    });

    return files;
  };

  ipcMain.handle(IPC_CHANNELS.FILE_BROWSE, browseHandler);
  handlers.push({ channel: IPC_CHANNELS.FILE_BROWSE, handler: browseHandler });

  // ── file:read ───────────────────────────────────────────

  const readHandler: SafeHandler = async (_event, args: unknown) => {
    const { filePath } = fileReadSchema.parse(args);

    if (!isPathAllowed(filePath)) {
      throw new Error('PATH_NOT_ALLOWED');
    }

    if (!fs.existsSync(filePath)) {
      throw new Error('FILE_NOT_FOUND');
    }

    const content = fs.readFileSync(filePath, 'utf-8');
    return { content };
  };

  ipcMain.handle(IPC_CHANNELS.FILE_READ, readHandler);
  handlers.push({ channel: IPC_CHANNELS.FILE_READ, handler: readHandler });

  // ── file:preview ────────────────────────────────────────

  const previewHandler: SafeHandler = async (_event, args: unknown) => {
    const { filePaths } = filePreviewSchema.parse(args);

    const previews: FilePreview[] = [];

    for (const filePath of filePaths) {
      if (!isPathAllowed(filePath)) {
        logger.warn('Path not allowed for preview', { module: 'ipc/file', filePath });
        continue;
      }

      try {
        if (!fs.existsSync(filePath)) {
          continue;
        }

        const mimeType = getMimeType(filePath);
        
        // For text files, read content
        if (mimeType.startsWith('text/') || mimeType === 'application/json') {
          const content = fs.readFileSync(filePath, 'utf-8');
          previews.push({
            path: filePath,
            content,
            mimeType,
          });
        } else {
          // For binary files, return placeholder
          previews.push({
            path: filePath,
            content: `[Binary file - ${mimeType}]`,
            mimeType,
          });
        }
      } catch (error: any) {
        logger.error('Failed to preview file', {
          module: 'ipc/file',
          filePath,
          error: error.message,
        });
      }
    }

    return previews;
  };

  ipcMain.handle(IPC_CHANNELS.FILE_PREVIEW, previewHandler);
  handlers.push({ channel: IPC_CHANNELS.FILE_PREVIEW, handler: previewHandler });

  // ── file:convert ────────────────────────────────────────

  const convertHandler: SafeHandler = async (_event, args: unknown) => {
    const { inputPath, outputPath, inputFormat, outputFormat } = fileConvertSchema.parse(args);

    if (!isPathAllowed(inputPath) || !isPathAllowed(outputPath)) {
      throw new Error('PATH_NOT_ALLOWED');
    }

    const converter = FormatConverter.getInstance();
    const result = await converter.convert({
      inputPath,
      outputPath,
      inputFormat: inputFormat as FileFormat,
      outputFormat: outputFormat as FileFormat,
    });

    if (!result.success) {
      throw new Error(result.error || 'CONVERSION_FAILED');
    }

    return {
      outputPath: result.outputPath,
      durationMs: result.durationMs,
    };
  };

  ipcMain.handle(IPC_CHANNELS.FILE_CONVERT, safeHandler(convertHandler));
  handlers.push({ channel: IPC_CHANNELS.FILE_CONVERT, handler: convertHandler });

  // ── Cleanup ─────────────────────────────────────────────

  logger.info('File IPC handlers registered', { module: 'ipc/file' });

  return () => {
    for (const { channel } of handlers) {
      ipcMain.removeHandler(channel);
    }
    logger.info('File IPC handlers unregistered', { module: 'ipc/file' });
  };
}

export default registerFileIpc;
