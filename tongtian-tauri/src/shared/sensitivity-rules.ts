// src/shared/sensitivity-rules.ts — 数据分级规则表（前端侧）
//
// 与 Rust 侧 `src-tauri/src/security/classify.rs` 同源：
// 桌面应用（联调/生产）走 Rust command `security_scan_data`，Rust 为唯一权威实现；
// 本文件仅供开发阶段的两条 Mock 通道使用（VITE_MOCK 开发自测 / 浏览器直连引擎），
// 保证 Mock 开关规范下安全页在没有 Rust 后端时依然可自测。
//
// 两端关键词表漂移由 Rust 单元测试 `rules_match_frontend_table` 守护：
// 该测试会读取本文件比对字面量，「CERT/COMMERCIAL/INTERNAL_KEYWORDS」三张表
// 必须逐字一致 —— 因此下面关键词数组的每个元素独占一行且以引号开头。

import { DataSensitivity } from './types';

/** 证件/资质类关键词 → confidential（high） */
export const CERT_KEYWORDS = [
  '身份证',
  '护照',
  '营业执照',
  '驾驶证',
  '社保卡',
  '银行卡',
];

/** 商业机密类关键词 → confidential（medium） */
export const COMMERCIAL_KEYWORDS = [
  '合同',
  '协议',
  '薪资',
  '报价',
  '招标',
  '投标',
  '财务报表',
  '客户名单',
];

/** 内部标记类关键词 → internal（low） */
export const INTERNAL_KEYWORDS = [
  '内部',
  '机密',
  '绝密',
  '仅限内网',
];

/** 命中特征（与 Rust ScanFinding 契约一致） */
export interface ScanFinding {
  type: string;
  pattern: string;
  severity: 'high' | 'medium' | 'low';
  description: string;
}

/** 分级结果（与 Rust ScanResult 契约一致） */
export interface LocalScanResult {
  sensitivity: DataSensitivity;
  confidence: number;
  findings: ScanFinding[];
  summary: string;
  pii: boolean;
  force_local: boolean;
}

/** 关键词规则（供设置页展示与本地检测复用） */
export interface SensitivityRule {
  level: DataSensitivity;
  severity: 'high' | 'medium' | 'low';
  description: string;
  keywords: string[];
}

export const SENSITIVITY_RULES: SensitivityRule[] = [
  ...CERT_KEYWORDS.map((kw) => ({
    level: DataSensitivity.CONFIDENTIAL,
    severity: 'high' as const,
    description: '个人证件/资质信息',
    keywords: [kw],
  })),
  ...COMMERCIAL_KEYWORDS.map((kw) => ({
    level: DataSensitivity.CONFIDENTIAL,
    severity: 'medium' as const,
    description: '商业机密文件',
    keywords: [kw],
  })),
  ...INTERNAL_KEYWORDS.map((kw) => ({
    level: DataSensitivity.INTERNAL,
    severity: 'low' as const,
    description: '内部敏感标记',
    keywords: [kw],
  })),
];

// ── 本地检测（仅开发 Mock 通道使用） ────────────────────────────
//
// 判定链与 Rust `security::classify` 逐条对齐（同一数字串只出一类结论：
// 身份证 → 手机号 → 银行卡），避免「18 位身份证尾字母被同时判成银行卡」这类漂移。

const ID_CARD_RE = /^\d{17}[\dXx]$/;
const PHONE_RE = /^1[3-9]\d{9}$/;
const BANK_CARD_RE = /^\d{16,19}$/;

/** ASCII 数字判定：避免对 CJK 全角数字误判（Rust 侧同为 is_ascii_digit） */
function isAsciiDigit(ch: string | undefined): boolean {
  return ch !== undefined && ch >= '0' && ch <= '9';
}

/** 邮箱字符判定：镜像 Rust `char::is_alphanumeric`（含 CJK 等 Unicode 字母数字） */
function isEmailChar(ch: string): boolean {
  return /[\p{L}\p{N}@._+-]/u.test(ch);
}

/**
 * 数字串扫描（镜像 Rust `digit_runs`）：
 * 单个空格/连字符两侧均为数字时并入同一串；17 位数字后紧跟 `X`/`x` 一并纳入。
 */
export function digitRuns(text: string): string[] {
  const out: string[] = [];
  let i = 0;
  while (i < text.length) {
    if (!isAsciiDigit(text[i])) {
      i += 1;
      continue;
    }
    let digits = '';
    let j = i;
    while (j < text.length) {
      const ch = text[j];
      if (isAsciiDigit(ch)) {
        digits += ch;
        j += 1;
      } else if (
        (ch === ' ' || ch === '-') &&
        j + 1 < text.length &&
        isAsciiDigit(text[j + 1]) &&
        digits.length > 0
      ) {
        j += 1; // 分隔符：跳过但计入同一数字串
      } else {
        break;
      }
    }
    if (digits.length === 17 && (text[j] === 'X' || text[j] === 'x')) {
      digits += text[j];
      j += 1;
    }
    out.push(digits);
    i = j;
  }
  return out;
}

export function isIdCard(s: string): boolean {
  return ID_CARD_RE.test(s);
}

export function isPhone(s: string): boolean {
  return PHONE_RE.test(s);
}

export function isBankCard(s: string): boolean {
  return BANK_CARD_RE.test(s);
}

/** 邮箱形态（镜像 Rust `is_email`）：单 `@`、域名含点且点不在首尾、域名长度 ≥3 */
export function isEmail(t: string): boolean {
  const parts = t.split('@');
  if (parts.length !== 2) return false;
  const [local, domain] = parts;
  return (
    local.length > 0 &&
    domain.length >= 3 &&
    domain.includes('.') &&
    !domain.startsWith('.') &&
    !domain.endsWith('.')
  );
}

/** 按 Rust `word_runs` 语义切出连续「邮箱字符」片段 */
function wordRuns(text: string): string[] {
  const out: string[] = [];
  let cur = '';
  for (const ch of text) {
    if (isEmailChar(ch)) {
      cur += ch;
    } else if (cur) {
      out.push(cur);
      cur = '';
    }
  }
  if (cur) out.push(cur);
  return out;
}

function pushFinding(
  out: ScanFinding[],
  kind: string,
  pattern: string,
  severity: ScanFinding['severity'],
  description: string,
): void {
  if (out.some((f) => f.type === kind && f.pattern === pattern)) return;
  out.push({ type: kind, pattern, severity, description });
}

/**
 * 本地规则检测（与 Rust `classify::classify` 判定顺序一致）：
 * 数值型 PII（身份证→手机号→银行卡，每串只取一类）→ 邮箱 → 证件关键词
 * → 商业机密关键词 → 内部标记，取命中最高档为最终分级。
 */
export function classifyText(text: string): LocalScanResult {
  const findings: ScanFinding[] = [];
  let pii = false;

  for (const digits of digitRuns(text)) {
    if (isIdCard(digits)) {
      pii = true;
      pushFinding(findings, 'pii', '18 位身份证号', 'high', '个人身份证号');
    } else if (isPhone(digits)) {
      pii = true;
      pushFinding(findings, 'pii', '11 位手机号', 'high', '个人手机号');
    } else if (isBankCard(digits)) {
      pii = true;
      pushFinding(findings, 'pii', '16-19 位银行卡号', 'high', '银行账户信息');
    }
  }

  if (wordRuns(text).some(isEmail)) {
    pii = true;
    pushFinding(findings, 'pii', '邮箱地址', 'high', '个人邮箱地址');
  }

  for (const kw of CERT_KEYWORDS) {
    if (text.includes(kw)) pushFinding(findings, 'keyword', kw, 'high', '个人证件/资质信息');
  }
  for (const kw of COMMERCIAL_KEYWORDS) {
    if (text.includes(kw)) pushFinding(findings, 'keyword', kw, 'medium', '商业机密文件');
  }
  for (const kw of INTERNAL_KEYWORDS) {
    if (text.includes(kw)) pushFinding(findings, 'keyword', kw, 'low', '内部敏感标记');
  }

  const has = (sev: ScanFinding['severity']) => findings.some((f) => f.severity === sev);
  let sensitivity = DataSensitivity.PUBLIC;
  let confidence = 0.5;
  if (pii) {
    sensitivity = DataSensitivity.CONFIDENTIAL;
    confidence = 0.95;
  } else if (has('high')) {
    sensitivity = DataSensitivity.CONFIDENTIAL;
    confidence = 0.9;
  } else if (has('medium')) {
    sensitivity = DataSensitivity.CONFIDENTIAL;
    confidence = 0.85;
  } else if (has('low')) {
    sensitivity = DataSensitivity.INTERNAL;
    confidence = 0.7;
  }

  const summary =
    sensitivity === DataSensitivity.CONFIDENTIAL
      ? '检测到高度敏感信息，建议仅使用本地模型处理'
      : sensitivity === DataSensitivity.INTERNAL
        ? '检测到内部敏感内容，建议限制使用范围'
        : '未检测到敏感信息，可正常使用';

  return {
    sensitivity,
    confidence,
    findings,
    summary,
    pii,
    force_local: sensitivity === DataSensitivity.CONFIDENTIAL,
  };
}