/**
 * 定时任务编辑接口契约测试（任务 #17）
 *
 * `schedule:update` 曾只是一个死常量：契约里有通道名、preload 里放行，
 * 但 Rust 侧没有 command、前端三条通道也没有分支，页面更没有编辑入口。
 * 本文件从源码层面锁定链路完整，避免再次退化成「常量存在但无人实现」：
 * - Rust：`UpdateSchedule` 结构 + `update()` + command 注册
 * - 通道：tauri 分支 / 本地台账路由 / store 调用常量
 * - 页面：任务行有编辑入口，弹窗区分新建与编辑
 * - 口径：TS 与 Rust 对「缺省字段沿用原值」「禁用任务不排期」的实现一致
 */

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { ErrorCode } from '../../../src/shared/contracts';
import { errorMessageOf } from '../../../src/shared/error-messages';

function read(rel: string): string {
  return readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8');
}

const rustSchedule = () => read('../../../src-tauri/src/task/schedule.rs');
const rustLib = () => read('../../../src-tauri/src/lib.rs');
const tauriApi = () => read('../../../src/renderer/lib/tauri-api.ts');
const scheduleLocal = () => read('../../../src/renderer/lib/schedule-local.ts');
const store = () => read('../../../src/renderer/stores/useScheduleStore.ts');
const page = () => read('../../../src/renderer/pages/ScheduleView/index.tsx');

describe('Rust 侧编辑接口', () => {
  it('UpdateSchedule 字段与 TS ScheduleUpdateReq 同名（camelCase 序列化）', () => {
    const src = rustSchedule();
    expect(src).toMatch(/rename_all\s*=\s*"camelCase"/);
    expect(src).toMatch(/pub struct UpdateSchedule/);
    for (const field of ['schedule_id', 'name', 'description', 'freq', 'time_of_day', 'weekday', 'workspace_id']) {
      expect(src).toContain(`pub ${field}:`);
    }
    expect(src).toMatch(/pub fn update\(\s*store: &Store,\s*req: &UpdateSchedule,\s*now_ms: i64/);
  });

  it('编辑走单行参数化 UPDATE，不触碰存量行的其他字段', () => {
    const body = rustSchedule().split('pub fn update(')[1].split('/// 删除定时任务')[0];
    expect(body).toContain('UPDATE scheduled_tasks SET');
    expect(body).toContain('WHERE schedule_id = ?1');
    // SQL 由字符串拼接会让任务名里的引号变成注入点
    expect(body).not.toMatch(/execute\(\s*format!/);
    expect(body).toContain('rusqlite::params!');
  });

  it('command 已注册且禁用中的任务编辑后仍不排期', () => {
    expect(rustLib()).toContain('schedule_update,');
    const body = rustSchedule().split('pub fn update(')[1].split('/// 删除定时任务')[0];
    expect(body).toContain('if cur.enabled');
  });
});

describe('前端三通道与页面入口', () => {
  it('Tauri 通道把 schedule:update 映射到 schedule_update command', () => {
    expect(tauriApi()).toContain("if (channel === 'schedule:update') return invoke('schedule_update'");
  });

  it('浏览器通道（Mock / 直连引擎）由本地台账接管 schedule:update', () => {
    const src = scheduleLocal();
    expect(src).toContain("if (channel === 'schedule:update')");
    expect(src).toContain('export function updateLocalSchedule(');
  });

  it('store 经 IPC 常量调用编辑（不再存在无人使用的 SCHEDULE_UPDATE）', () => {
    expect(store()).toContain('IPC_CHANNELS.SCHEDULE_UPDATE');
    expect(store()).toMatch(/update: async \(req\) =>/);
  });

  it('页面有编辑入口，且新建与编辑复用同一弹窗但文案区分', () => {
    const src = page();
    expect(src).toContain('const openEditDialog =');
    expect(src).toContain('onClick={() => openEditDialog(t)}');
    expect(src).toContain('编辑定时任务');
    expect(src).toContain('editingScheduleId ? \'保存修改\'');
  });
});

describe('错误码契约', () => {
  it('Rust 编辑链路的错误码在 TS 文案表中都有对应', () => {
    const src = rustSchedule() + rustLib();
    const codes = [...src.matchAll(/(?<=")SCHEDULE_[A-Z][A-Z_]*/g)].map((m) => m[0]);
    expect(codes).toContain('SCHEDULE_INVALID_ID');
    for (const code of new Set(codes)) {
      // 未登记的码会原样回显内部错误，用户看到的是英文大写串
      expect(errorMessageOf(code), `错误码 ${code} 缺少可读文案`).not.toContain(code);
    }
  });

  it('SCHEDULE_INVALID_ID 文案指向可操作下一步（不暴露内部错误码）', () => {
    const text = errorMessageOf(ErrorCode.SCHEDULE_INVALID_ID);
    expect(text).toContain('定时任务');
    expect(text).not.toContain('SCHEDULE_INVALID_ID');
  });
});
