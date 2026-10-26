// tests/unit/bridge/skill-scan-contract.test.ts — 技能静态风险扫描契约
//
// SKILL.md 是整段交给模型的指令，等同于「用户允许它指导 Agent」。扫描器必须
// 在安装、启用、运行时三层都生效，并且高风险内容不能从任何入口（清单/use_skill/
// 显式 /技能 引用）泄露；扫描只是启发式，不能宣称「安全认证」。
// 规则细节与真实触发由 src-tauri/src/security/skill_scan.rs 的单元测试验证。

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

function read(rel: string): string {
  return readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8').replace(/\r\n/g, '\n');
}

const scanner = () => read('../../../src-tauri/src/security/skill_scan.rs');
const securityMod = () => read('../../../src-tauri/src/security/mod.rs');
const skillMod = () => read('../../../src-tauri/src/skill/mod.rs');
const suiteMod = () => read('../../../src-tauri/src/suite/mod.rs');
const marketMod = () => read('../../../src-tauri/src/market/mod.rs');
const engineRuntime = () => read('../../../engine/runtime_context.py');
const contracts = () => read('../../../src/shared/contracts.ts');
const errorMessages = () => read('../../../src/shared/error-messages.ts');
const extensionsPage = () => read('../../../src/renderer/pages/Extensions/index.tsx');

describe('技能静态扫描：安装 → 启用 → 运行时', () => {
  it('扫描器覆盖可执行危害，不把「文档提到 curl」直接判死', () => {
    const src = scanner();
    for (const code of [
      'prompt_injection',
      'prompt_injection_zh',
      'hide_from_user',
      'hide_from_user_en',
      'bypass_guard',
      'bypass_guard_en',
      'destructive_disk',
      'pipe_to_shell',
      'encoded_exec',
      'credential_exfil',
      'hidden_unicode',
    ]) {
      expect(src).toContain(`"${code}"`);
    }
    // 下载/删除/密钥提及保留为提醒级，避免整包误杀
    expect(src).toContain('"network_download"');
    expect(src).toContain('"network_upload"');
    expect(src).toContain('"secret_reference"');
    expect(src).toMatch(/"network_download",\s+"warn"/);
    expect(src).toContain('static SECRET: OnceLock<Regex>');
  });

  it('报告结构可序列化到前端，且拦截原因不回显整篇正文', () => {
    const src = scanner();
    expect(src).toContain('pub struct Finding');
    expect(src).toContain('pub struct ScanReport');
    expect(src).toContain('pub fn scan(text: &str) -> ScanReport');
    expect(src).toContain('pub fn block_reason');
    expect(src).toContain('MAX_FINDINGS');
    expect(src).toContain('EXCERPT_CHARS');
    expect(securityMod()).toContain('pub mod skill_scan;');
  });

  it('安装口是主闸门：高风险直接拒绝，没有强制安装旁路', () => {
    const src = skillMod();
    const install = src.slice(src.indexOf('fn write_new_skill'), src.indexOf('fn uninstall_in'));
    expect(install).toContain('skill_scan::scan(content)');
    expect(install).toContain('if report.blocked()');
    expect(install).toContain('SKILL_SCAN_BLOCKED: 技能「{id}」未安装');
    // 扫描必须在创建暂存目录之前
    expect(install.indexOf('skill_scan::scan')).toBeLessThan(install.indexOf('create_dir_all(&staging)'));
    expect(install).not.toContain('force');
  });

  it('列表展示扫描结论，运行时正文被清空并带可见原因', () => {
    const src = skillMod();
    expect(src).toContain('let scan = text.as_deref().map(skill_scan::scan);');
    expect(src).toContain('"scan": scan,');
    expect(src).toContain('obj.insert("content".to_string(), Value::String(String::new()));');
    expect(src).toContain('obj.insert("scanBlocked".to_string(), Value::Bool(true));');
    expect(src).toContain('静态安全扫描判定为高风险，正文未进入执行面');
    // 磁盘存量正文不被改写，只是不进入执行面
    expect(src).toContain('历史技能即使仍在启用清单里，也不把高风险正文送进模型');
  });

  it('启用开关不能把历史高风险技能重新放进来', () => {
    const src = skillMod();
    expect(src).toContain('fn ensure_enable_allowed');
    expect(src).toContain('ensure_enable_allowed(&dir, &id)?;');
    expect(src).toContain('SKILL_SCAN_BLOCKED: 技能「{id}」被安全扫描拦截，不能启用');
    // 安装被拦截要审计
    expect(src).toContain('if err.starts_with("SKILL_SCAN_BLOCKED")');
    expect(src).toContain('"skill_scan"');
  });

  it('套件包不能绕过技能扫描：安装拦 SKILL.md，运行拦技能/指令正文', () => {
    const src = suiteMod();
    expect(src).toContain('use crate::security::skill_scan;');
    expect(src).toContain('SKILL_SCAN_BLOCKED: 套件「{name}」中的');
    expect(src).toContain('let report = skill_scan::scan(&text);');
    // 历史套件 + 指令正文在装配前再扫一次
    expect(src).toContain('历史套件可能早于扫描器安装');
    expect(src).toContain('指令「{}」被安全扫描判定为高风险，本轮未装配');
    // 市场下载的套件与技能仍走同一落盘闸门
    const market = marketMod();
    expect(market).toContain('skill::install_content_in(base, &item.id, &content)');
    expect(market).toContain('suite::install_bytes_in(base, &bytes)');
  });

  it('引擎把 scanBlocked 当成硬边界：清单、use_skill、显式引用都不放行', () => {
    const src = engineRuntime();
    expect(src).toContain('"scanBlocked": item.get("scanBlocked") is True');
    expect(src).toContain('skills = [s for s in skills if not s.get("scanBlocked")]');
    expect(src).toContain('技能被安全扫描判定为高风险，正文未进入执行面');
    // 即使调用方还带着正文，use_skill 也只返回拒绝
    expect(src).toContain('被安全扫描判定为高风险，正文不可用');
    expect(src).toContain('本轮未内联正文');
  });

  it('前端契约、错误文案与列表徽标一致', () => {
    expect(contracts()).toContain("SKILL_SCAN_BLOCKED = 'SKILL_SCAN_BLOCKED'");
    expect(contracts()).toContain('export interface SkillScanFinding');
    expect(contracts()).toContain('export interface SkillScanReport');
    expect(contracts()).toContain('level: \'clean\' | \'warn\' | \'block\'');
    expect(contracts()).toContain('scanBlocked?: boolean;');
    expect(errorMessages()).toContain('技能被安全扫描拦截');
    const page = extensionsPage();
    expect(page).toContain("s.scan?.level === 'block'");
    expect(page).toContain('安全风险');
    expect(page).toContain('提醒 ${s.scan.findings.length}');
    // 被拦截且当前禁用的技能不能通过开关启用
    expect(page).toContain("(s.scan?.level === 'block' && !s.enabled)");
  });
});
