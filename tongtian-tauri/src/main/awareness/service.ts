// ============================================================
// src/main/awareness/service.ts — 意识模块服务
// 千问对标整改 spec §5.2（协作风格/工作手册/自动记忆持久化）
// ============================================================

import Database from '../store/db';
import Logger from '../util/logger';
import type { AwarenessProfile, MemoryEntry } from '../../shared/contracts';

const logger = Logger.getInstance();

const DEFAULT_PROFILE: AwarenessProfile = {
  mode: true,
  autoMemory: true,
  style: 'professional',
  customPrompt: '',
  workManual: '',
  updatedAt: '',
};

class AwarenessService {
  private static instance: AwarenessService | null = null;

  static getInstance(): AwarenessService {
    if (!AwarenessService.instance) AwarenessService.instance = new AwarenessService();
    return AwarenessService.instance;
  }

  // ── 意识配置 ─────────────────────────────────────────────

  getProfile(): AwarenessProfile {
    try {
      const db = Database.getInstance().getDb();
      const row = db.prepare("SELECT * FROM awareness_profiles WHERE id = 'global'").get() as Record<string, unknown> | undefined;
      if (!row) return { ...DEFAULT_PROFILE };
      return {
        mode: Number(row.mode) === 1,
        autoMemory: Number(row.auto_memory) === 1,
        style: (row.style as AwarenessProfile['style']) ?? 'professional',
        customPrompt: String(row.custom_prompt ?? ''),
        workManual: String(row.work_manual ?? ''),
        updatedAt: String(row.updated_at ?? ''),
      };
    } catch (err) {
      logger.error('awareness profile load failed', 'awareness', { error: String(err) });
      return { ...DEFAULT_PROFILE };
    }
  }

  updateProfile(patch: Partial<AwarenessProfile>): AwarenessProfile {
    const current = this.getProfile();
    const next: AwarenessProfile = { ...current, ...patch };
    const db = Database.getInstance().getDb();
    db.prepare(
      `INSERT INTO awareness_profiles (id, mode, auto_memory, style, custom_prompt, work_manual, updated_at)
       VALUES ('global', ?, ?, ?, ?, ?, datetime('now'))
       ON CONFLICT(id) DO UPDATE SET
         mode = excluded.mode, auto_memory = excluded.auto_memory, style = excluded.style,
         custom_prompt = excluded.custom_prompt, work_manual = excluded.work_manual,
         updated_at = datetime('now')`,
    ).run(next.mode ? 1 : 0, next.autoMemory ? 1 : 0, next.style, next.customPrompt, next.workManual);
    logger.info('awareness profile updated', 'awareness');
    return this.getProfile();
  }

  /** 构造注入 system prompt 的前置层（供 Agent 引擎使用） */
  buildPromptLayer(): string {
    const p = this.getProfile();
    if (!p.mode) return '';
    const styleMap: Record<string, string> = {
      professional: '以专业严谨的语气回复，使用正式用语与结构化输出。',
      friendly: '以轻松友好的语气回复，自然对话。',
      concise: '回复简洁高效，直奔主题，减少冗余。',
    };
    const styleText = p.style === 'custom' ? p.customPrompt : styleMap[p.style] ?? '';
    const parts: string[] = [];
    if (styleText) parts.push(`【协作风格】${styleText}`);
    if (p.workManual.trim()) parts.push(`【工作手册】\n${p.workManual.trim()}`);
    return parts.join('\n\n');
  }

  // ── 记忆条目 ─────────────────────────────────────────────

  listMemories(source?: string): MemoryEntry[] {
    try {
      const db = Database.getInstance().getDb();
      const rows = source
        ? (db.prepare('SELECT * FROM memory_entries WHERE source = ? ORDER BY created_at DESC').all(source) as Record<string, unknown>[])
        : (db.prepare('SELECT * FROM memory_entries ORDER BY created_at DESC').all() as Record<string, unknown>[]);
      return rows.map((r) => ({
        id: String(r.id),
        content: String(r.content),
        source: (r.source as MemoryEntry['source']) ?? 'auto',
        sessionId: r.session_id ? String(r.session_id) : undefined,
        createdAt: String(r.created_at),
      }));
    } catch (err) {
      logger.error('memory list failed', 'awareness', { error: String(err) });
      return [];
    }
  }

  addMemory(content: string, source: MemoryEntry['source'] = 'manual', sessionId?: string): MemoryEntry {
    const id = `mem-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    const db = Database.getInstance().getDb();
    db.prepare(
      `INSERT INTO memory_entries (id, content, source, session_id, created_at) VALUES (?, ?, ?, ?, datetime('now'))`,
    ).run(id, content, source, sessionId ?? null);
    logger.info('memory added', 'awareness', { id, source });
    const rows = db.prepare('SELECT * FROM memory_entries WHERE id = ?').get(id) as Record<string, unknown>;
    return {
      id,
      content,
      source,
      sessionId,
      createdAt: String(rows.created_at),
    };
  }

  deleteMemory(id: string): boolean {
    const db = Database.getInstance().getDb();
    const result = db.prepare('DELETE FROM memory_entries WHERE id = ?').run(id);
    return result.changes > 0;
  }
}

export default AwarenessService;
