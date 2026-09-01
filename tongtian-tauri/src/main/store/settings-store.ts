// ============================================================
// src/main/store/settings-store.ts — 应用设置持久化 + 系统行为落地
// 千问对标整改 spec §5.5 / §6.1
// ============================================================

import { app, powerSaveBlocker } from 'electron';
import Database from './db';
import Logger from '../util/logger';
import type { AppSettingsData } from '../../shared/contracts';

const logger = Logger.getInstance();

const SETTINGS_KEY = 'app_settings';

export const DEFAULT_SETTINGS: AppSettingsData = {
  language: '中文',
  theme: '亮色',
  uiStyle: '默认',
  chatFont: '无衬线',
  chatFontSize: 14,
  chatWidth: '标准',
  previewMode: '应用内预览',
  promptSuggestion: true,
  expandToolCalls: false,
  imShowToolProcess: false,
  autoStart: false,
  keepAwake: true,
  desktopNotify: true,
  badgeUnread: true,
  soundNotify: false,
  proxyMode: '跟随系统',
  privacyMode: false,
  voiceEnabled: false,
  snapshotEnabled: false,
  quickPickEnabled: false,
  computerUsePolicy: 'ask_every_time',
  secureEnv: false,
  workbenchModes: ['general', 'design', 'slides'],
  workbenchDefaultMode: 'general',
  experiments: {},
};

class SettingsStore {
  private static instance: SettingsStore | null = null;
  private cache: AppSettingsData | null = null;
  private wakeBlockerId: number | null = null;

  static getInstance(): SettingsStore {
    if (!SettingsStore.instance) {
      SettingsStore.instance = new SettingsStore();
    }
    return SettingsStore.instance;
  }

  /** 读取全部设置（内存缓存 + sqlite 持久层） */
  getAll(): AppSettingsData {
    if (this.cache) return { ...this.cache };

    try {
      const db = Database.getInstance().getDb();
      const row = db.prepare('SELECT value_json FROM app_settings WHERE key = ?').get(SETTINGS_KEY) as
        | { value_json: string }
        | undefined;
      const stored = row ? (JSON.parse(row.value_json) as Partial<AppSettingsData>) : {};
      this.cache = { ...DEFAULT_SETTINGS, ...stored };
    } catch (err) {
      logger.error('settings load failed, using defaults', 'settings', { error: String(err) });
      this.cache = { ...DEFAULT_SETTINGS };
    }
    return { ...this.cache };
  }

  /** 增量更新设置，并落地真实系统行为 */
  update(patch: Partial<AppSettingsData>): AppSettingsData {
    const current = this.getAll();
    const next: AppSettingsData = { ...current, ...patch };
    this.cache = next;

    try {
      const db = Database.getInstance().getDb();
      db.prepare(
        `INSERT INTO app_settings (key, value_json, updated_at) VALUES (?, ?, datetime('now'))
         ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, updated_at = datetime('now')`,
      ).run(SETTINGS_KEY, JSON.stringify(next));
    } catch (err) {
      logger.error('settings persist failed', 'settings', { error: String(err) });
    }

    this.applySystemEffects(current, next);
    logger.info('settings updated', 'settings', { keys: Object.keys(patch).join(',') });
    return { ...next };
  }

  /** 应用启动时按已保存设置恢复系统行为 */
  applyOnStartup(): void {
    const s = this.getAll();
    this.applyAutoStart(s.autoStart);
    this.applyKeepAwake(s.keepAwake);
  }

  // ── 真实系统行为 ─────────────────────────────────────────

  private applySystemEffects(prev: AppSettingsData, next: AppSettingsData): void {
    if (prev.autoStart !== next.autoStart) this.applyAutoStart(next.autoStart);
    if (prev.keepAwake !== next.keepAwake) this.applyKeepAwake(next.keepAwake);
  }

  /** 开机自启动（Electron 原生能力） */
  private applyAutoStart(enabled: boolean): void {
    try {
      app.setLoginItemSettings({ openAtLogin: enabled });
      logger.info(`autoStart applied: ${enabled}`, 'settings');
    } catch (err) {
      logger.error('setLoginItemSettings failed', 'settings', { error: String(err) });
    }
  }

  /** 保持系统唤醒（powerSaveBlocker） */
  private applyKeepAwake(enabled: boolean): void {
    try {
      if (enabled && this.wakeBlockerId === null) {
        this.wakeBlockerId = powerSaveBlocker.start('prevent-app-suspension');
        logger.info('keepAwake blocker started', 'settings');
      } else if (!enabled && this.wakeBlockerId !== null) {
        powerSaveBlocker.stop(this.wakeBlockerId);
        this.wakeBlockerId = null;
        logger.info('keepAwake blocker stopped', 'settings');
      }
    } catch (err) {
      logger.error('powerSaveBlocker failed', 'settings', { error: String(err) });
    }
  }
}

export default SettingsStore;
