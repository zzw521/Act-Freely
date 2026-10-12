//! 数据分级检测（PRD §4.7.1 数据分级与出域合规）
//!
//! 纯本地规则引擎，不引入正则依赖（保持随包体积与离线可用性）：
//! - 关键词命中：证件/资质 → 商业机密 → 内部标记（仅命中其一即取最高档）
//! - 数值型 PII 扫描：身份证号 / 手机号 / 银行卡号（容忍空格、连字符分组）
//! - 邮箱扫描
//!
//! 规则表与前端 `src/shared/sensitivity-rules.ts` 同源，两端漂移由单元测试守护
//! （`rules_match_frontend_table` 直接读取 TS 文件比对关键词字面量）。

use serde::{Deserialize, Serialize};

/// 分级取值（与前端 DataSensitivity 枚举一致）
pub const PUBLIC: &str = "public";
pub const INTERNAL: &str = "internal";
pub const CONFIDENTIAL: &str = "confidential";

/// 证件/资质类关键词 → confidential（severity high）
pub const CERT_KEYWORDS: &[&str] = &["身份证", "护照", "营业执照", "驾驶证", "社保卡", "银行卡"];
/// 商业机密类关键词 → confidential（severity medium）
pub const COMMERCIAL_KEYWORDS: &[&str] = &[
    "合同",
    "协议",
    "薪资",
    "报价",
    "招标",
    "投标",
    "财务报表",
    "客户名单",
];
/// 内部标记类关键词 → internal（severity low）
pub const INTERNAL_KEYWORDS: &[&str] = &["内部", "机密", "绝密", "仅限内网"];

/// 命中的单条特征
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ScanFinding {
    /// 前端契约字段名为 `type`：pii / keyword
    #[serde(rename = "type")]
    pub kind: String,
    /// 命中模式（关键词字面量或特征名）
    pub pattern: String,
    /// high / medium / low
    pub severity: String,
    pub description: String,
}

/// 分级检测结果（与前端 `DataScanResult` 契约一致，附加 pii/force_local 供路由决策）
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ScanResult {
    pub sensitivity: String,
    pub confidence: f64,
    pub findings: Vec<ScanFinding>,
    pub summary: String,
    /// 命中个人身份信息
    pub pii: bool,
    /// 是否要求强制本地通道（PII 或机密命中即为真，PRD §4.2.3 按数据敏感度自动切换）
    pub force_local: bool,
}

// ── 扫描工具（按字符类别切词，附带字节偏移以便脱敏时原样替换） ──────

/// 按谓词切分词元，返回 (起始字节, 结束字节, 词元)
fn word_runs(text: &str, pred: impl Fn(char) -> bool) -> Vec<(usize, usize, String)> {
    let mut out = Vec::new();
    let mut start: Option<usize> = None;
    for (off, ch) in text.char_indices() {
        if pred(ch) {
            if start.is_none() {
                start = Some(off);
            }
        } else if let Some(s) = start.take() {
            out.push((s, off, text[s..off].to_string()));
        }
    }
    if let Some(s) = start {
        out.push((s, text.len(), text[s..].to_string()));
    }
    out
}

/// 数字串扫描：单个空格/连字符若两侧均为数字则并入同一数字串
/// （覆盖 `6222 0212 3456 7890` 这类分组写法）。
/// 17 位数字后紧跟校验位 `X`/`x` 时一并纳入（18 位身份证号）。
fn digit_runs(text: &str) -> Vec<(usize, usize, String)> {
    let chars: Vec<(usize, char)> = text.char_indices().collect();
    let mut out = Vec::new();
    let mut i = 0usize;
    while i < chars.len() {
        if !chars[i].1.is_ascii_digit() {
            i += 1;
            continue;
        }
        let start = chars[i].0;
        let mut digits = String::new();
        let mut end = start;
        let mut j = i;
        while j < chars.len() {
            let (off, ch) = chars[j];
            if ch.is_ascii_digit() {
                digits.push(ch);
                end = off + ch.len_utf8();
                j += 1;
            } else if matches!(ch, ' ' | '-')
                && j + 1 < chars.len()
                && chars[j + 1].1.is_ascii_digit()
                && !digits.is_empty()
            {
                j += 1; // 分隔符：跳过但计入同一数字串
            } else {
                break;
            }
        }
        // 身份证校验位：17 位数字 + X/x
        if digits.len() == 17 {
            if let Some(&(off, ch)) = chars.get(j) {
                if ch == 'X' || ch == 'x' {
                    digits.push(ch);
                    end = off + ch.len_utf8();
                }
            }
        }
        out.push((start, end, digits));
        i = j;
    }
    out
}

fn is_email_char(c: char) -> bool {
    c.is_alphanumeric() || matches!(c, '@' | '.' | '_' | '-' | '+')
}

fn is_secret_char(c: char) -> bool {
    c.is_alphanumeric() || matches!(c, '-' | '_')
}

/// 18 位身份证号：前 17 位为数字，末位为数字或校验字母 X
pub fn is_id_card(s: &str) -> bool {
    let chars: Vec<char> = s.chars().collect();
    chars.len() == 18
        && chars[..17].iter().all(|c| c.is_ascii_digit())
        && (chars[17].is_ascii_digit() || chars[17] == 'X' || chars[17] == 'x')
}

/// 11 位手机号：1 开头、次位 3-9、全数字
pub fn is_phone(s: &str) -> bool {
    let bytes = s.as_bytes();
    bytes.len() == 11
        && bytes[0] == b'1'
        && (b'3'..=b'9').contains(&bytes[1])
        && bytes.iter().all(|b| b.is_ascii_digit())
}

/// 16-19 位纯数字：银行卡号形态
pub fn is_bank_card(s: &str) -> bool {
    (16..=19).contains(&s.chars().count()) && s.chars().all(|c| c.is_ascii_digit())
}

/// 邮箱形态：`local@domain.tld`，单 @、域名含点且点不在首尾
fn is_email(t: &str) -> bool {
    let parts: Vec<&str> = t.split('@').collect();
    if parts.len() != 2 {
        return false;
    }
    let (local, domain) = (parts[0], parts[1]);
    !local.is_empty()
        && domain.len() >= 3
        && domain.contains('.')
        && !domain.starts_with('.')
        && !domain.ends_with('.')
}

/// 明文密钥形态：`sk-` / `sk_` 前缀 + 至少 8 位密钥字符（OpenAI 兼容 Key 的通用形态）。
/// 非该形态的令牌（如自定义 Header）不会被识别，属已知边界。
fn is_secret(t: &str) -> bool {
    t.chars().count() >= 11 && (t.starts_with("sk-") || t.starts_with("sk_"))
}

// ── 脱敏（PRD §4.7.4：敏感字段自动脱敏） ─────────────────────────

/// 掩码：保留首 3 / 末 2 位，其余以 * 代替；过短则整体打码
fn mask(seg: &str) -> String {
    let n = seg.chars().count();
    if n <= 5 {
        return "*".repeat(n.max(1));
    }
    let head: String = seg.chars().take(3).collect();
    let tail: String = seg.chars().skip(n - 2).collect();
    format!("{head}{}{tail}", "*".repeat(n - 5))
}

/// 文本脱敏：PII（证件号/手机号/银行卡/邮箱）与明文密钥替换为掩码
pub fn redact(text: &str) -> String {
    let mut ranges: Vec<(usize, usize)> = Vec::new();
    for (s, e, digits) in digit_runs(text) {
        if is_id_card(&digits) || is_phone(&digits) || is_bank_card(&digits) {
            ranges.push((s, e));
        }
    }
    for (s, e, t) in word_runs(text, is_email_char) {
        if is_email(&t) {
            ranges.push((s, e));
        }
    }
    for (s, e, t) in word_runs(text, is_secret_char) {
        if is_secret(&t) {
            ranges.push((s, e));
        }
    }
    apply_masks(text, ranges)
}

/// 脱敏文件路径中的用户名：`C:\Users\alice\x` → `C:\Users\***\x`
pub fn redact_paths(text: &str) -> String {
    const MARKERS: [&str; 3] = ["\\Users\\", "/Users/", "/home/"];
    let mut out = String::with_capacity(text.len());
    let mut cursor = 0usize;
    while cursor < text.len() {
        let found = MARKERS
            .iter()
            .filter_map(|m| text[cursor..].find(m).map(|p| cursor + p + m.len()))
            .min();
        let Some(name_start) = found else { break };
        out.push_str(&text[cursor..name_start]);
        let rest = &text[name_start..];
        if rest.is_empty() {
            cursor = name_start; // 标记在末尾（用户名位为空）：已并入输出，避免重复追加
            break;
        }
        let name_end = rest.find(['\\', '/']).unwrap_or(rest.len());
        if name_end == 0 {
            // 用户名位为空（异常路径）：跳过一个分隔符，避免原地打转
            out.push_str(&text[name_start..name_start + 1]);
            cursor = name_start + 1;
        } else {
            out.push_str("***");
            cursor = name_start + name_end;
        }
    }
    out.push_str(&text[cursor..]);
    out
}

/// 合并重叠区间后统一掩码（同一片段被多类特征命中时只掩一次）
fn apply_masks(text: &str, mut ranges: Vec<(usize, usize)>) -> String {
    ranges.sort_unstable();
    let mut merged: Vec<(usize, usize)> = Vec::new();
    for (s, e) in ranges {
        match merged.last_mut() {
            Some(last) if s < last.1 => last.1 = last.1.max(e),
            _ => merged.push((s, e)),
        }
    }
    let mut out = String::with_capacity(text.len());
    let mut cursor = 0usize;
    for (s, e) in merged {
        if s < cursor {
            continue;
        }
        out.push_str(&text[cursor..s]);
        out.push_str(&mask(&text[s..e]));
        cursor = e;
    }
    out.push_str(&text[cursor..]);
    out
}

// ── 分级判定 ────────────────────────────────────────────────────

fn push_finding(out: &mut Vec<ScanFinding>, kind: &str, pattern: &str, severity: &str, desc: &str) {
    if out.iter().any(|f| f.kind == kind && f.pattern == pattern) {
        return; // 去重：同一特征重复命中只记一条
    }
    out.push(ScanFinding {
        kind: kind.to_string(),
        pattern: pattern.to_string(),
        severity: severity.to_string(),
        description: desc.to_string(),
    });
}

/// 单份文本分级检测（空文本按 public 返回，不报错）
pub fn classify(text: &str) -> ScanResult {
    let mut findings: Vec<ScanFinding> = Vec::new();
    let mut pii = false;

    for (_, _, digits) in digit_runs(text) {
        if is_id_card(&digits) {
            pii = true;
            push_finding(&mut findings, "pii", "18 位身份证号", "high", "个人身份证号");
        } else if is_phone(&digits) {
            pii = true;
            push_finding(&mut findings, "pii", "11 位手机号", "high", "个人手机号");
        } else if is_bank_card(&digits) {
            pii = true;
            push_finding(&mut findings, "pii", "16-19 位银行卡号", "high", "银行账户信息");
        }
    }

    if word_runs(text, is_email_char).into_iter().any(|(_, _, t)| is_email(&t)) {
        pii = true;
        push_finding(&mut findings, "pii", "邮箱地址", "high", "个人邮箱地址");
    }

    for kw in CERT_KEYWORDS {
        if text.contains(kw) {
            push_finding(&mut findings, "keyword", kw, "high", "个人证件/资质信息");
        }
    }
    for kw in COMMERCIAL_KEYWORDS {
        if text.contains(kw) {
            push_finding(&mut findings, "keyword", kw, "medium", "商业机密文件");
        }
    }
    for kw in INTERNAL_KEYWORDS {
        if text.contains(kw) {
            push_finding(&mut findings, "keyword", kw, "low", "内部敏感标记");
        }
    }

    let has = |sev: &str| findings.iter().any(|f| f.severity == sev);
    // 数值型 PII 为强特征（置信度最高），关键词按严重度递减
    let (sensitivity, confidence) = if pii {
        (CONFIDENTIAL, 0.95)
    } else if has("high") {
        (CONFIDENTIAL, 0.9)
    } else if has("medium") {
        (CONFIDENTIAL, 0.85)
    } else if has("low") {
        (INTERNAL, 0.7)
    } else {
        (PUBLIC, 0.5)
    };

    let summary = match sensitivity {
        CONFIDENTIAL => "检测到高度敏感信息，建议仅使用本地模型处理",
        INTERNAL => "检测到内部敏感内容，建议限制使用范围",
        _ => "未检测到敏感信息，可正常使用",
    }
    .to_string();

    ScanResult {
        sensitivity: sensitivity.to_string(),
        confidence,
        findings,
        summary,
        pii,
        force_local: sensitivity == CONFIDENTIAL,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn kinds(r: &ScanResult) -> Vec<String> {
        r.findings.iter().map(|f| f.pattern.clone()).collect()
    }

    #[test]
    fn test_clean_text_is_public() {
        let r = classify("把桌面上的待办清单按优先级排序");
        assert_eq!(r.sensitivity, PUBLIC);
        assert!(!r.pii);
        assert!(!r.force_local);
        assert!(r.findings.is_empty());
        assert!(r.summary.contains("未检测到敏感信息"));
    }

    #[test]
    fn test_empty_text_is_public_not_error() {
        let r = classify("");
        assert_eq!(r.sensitivity, PUBLIC);
        assert_eq!(r.confidence, 0.5);
    }

    #[test]
    fn test_id_card_confidential_pii() {
        let r = classify("客户身份证 11010119900307123X 请本地处理");
        assert_eq!(r.sensitivity, CONFIDENTIAL);
        assert!(r.pii);
        assert!(r.force_local);
        assert!(kinds(&r).contains(&"18 位身份证号".to_string()));
        assert_eq!(r.confidence, 0.95);
    }

    #[test]
    fn test_phone_confidential() {
        let r = classify("联系电话 13800138000");
        assert_eq!(r.sensitivity, CONFIDENTIAL);
        assert!(kinds(&r).contains(&"11 位手机号".to_string()));
    }

    #[test]
    fn test_non_phone_11_digits_not_flagged() {
        // 次位不在 3-9（如快递单号片段）不按手机号处理
        let r = classify("单号 12000138000");
        assert!(!kinds(&r).contains(&"11 位手机号".to_string()));
    }

    #[test]
    fn test_bank_card_grouped_with_spaces() {
        let r = classify("卡号 6222 0212 3456 7890 已核对");
        assert!(r.pii);
        assert!(kinds(&r).contains(&"16-19 位银行卡号".to_string()));
    }

    /// 混合样例：与前端 `tests/unit/security/security.test.ts` 的黄金样例逐字对齐，
    /// 守护「每串数字只出一类结论」（身份证尾字母不得再被判为银行卡）。
    #[test]
    fn test_golden_mixed_sample() {
        let r = classify("客户身份证 11010119900307123X，联系方式 13800138000，附采购合同一份");
        assert_eq!(
            kinds(&r),
            vec![
                "18 位身份证号".to_string(),
                "11 位手机号".to_string(),
                "身份证".to_string(),
                "合同".to_string(),
            ],
            "混合样例的命中集合必须与前端本地通道一致"
        );
        assert_eq!(r.sensitivity, CONFIDENTIAL);
        assert_eq!(r.confidence, 0.95);
        assert!(r.pii);
        assert!(r.force_local);
    }

    #[test]
    fn test_email_confidential() {
        let r = classify("发到 zhangsan@example.com");
        assert!(r.pii);
        assert!(kinds(&r).contains(&"邮箱地址".to_string()));
        // 非邮箱的 @ 形态不误报
        let r2 = classify("user@ 不是邮箱");
        assert!(!r2.pii);
    }

    #[test]
    fn test_keyword_tiers() {
        let cert = classify("这是营业执照扫描件");
        assert_eq!(cert.sensitivity, CONFIDENTIAL);
        assert!(!cert.pii);
        assert!(kinds(&cert).contains(&"营业执照".to_string()));

        let commercial = classify("请整理本季度采购合同");
        assert_eq!(commercial.sensitivity, CONFIDENTIAL);
        assert_eq!(commercial.confidence, 0.85);

        let internal = classify("内部通知：周五团建");
        assert_eq!(internal.sensitivity, INTERNAL);
        assert_eq!(internal.confidence, 0.7);
        assert!(!internal.force_local);
    }

    #[test]
    fn test_findings_deduplicated() {
        let r = classify("合同 合同 合同");
        assert_eq!(r.findings.len(), 1);
    }

    #[test]
    fn test_redact_masks_pii_and_secret() {
        let out = redact("手机 13800138000 密钥 sk-abcdefghijklmn 邮箱 a@b.com");
        assert!(!out.contains("13800138000"));
        assert!(!out.contains("sk-abcdefghijklmn"));
        assert!(!out.contains("a@b.com"));
        assert!(out.contains("138"));
        assert!(out.contains("手机"));
        assert!(out.contains("邮箱"));
    }

    #[test]
    fn test_redact_keeps_plain_text() {
        let src = "整理下载文件夹";
        assert_eq!(redact(src), src);
    }

    #[test]
    fn test_redact_handles_overlapping_email_and_digits() {
        // 同一片段被数字串与邮箱同时命中：只掩一次，不产生重复替换
        let out = redact("13800138000@example.com");
        assert!(!out.contains("example.com"));
        assert!(out.starts_with("138"));
    }

    #[test]
    fn test_redact_paths_windows_and_unix() {
        assert_eq!(
            redact_paths(r"C:\Users\alice\Documents\报表.xlsx"),
            r"C:\Users\***\Documents\报表.xlsx"
        );
        assert_eq!(redact_paths("/home/bob/x.md"), "/home/***/x.md");
        assert_eq!(redact_paths("/Users/carol/x.md"), "/Users/***/x.md");
    }

    #[test]
    fn test_redact_paths_without_marker_unchanged() {
        assert_eq!(redact_paths("D:/work/report.md"), "D:/work/report.md");
        // 标记在末尾（用户名位为空）：不 panic，原样返回
        assert_eq!(redact_paths(r"C:\Users\"), r"C:\Users\");
    }

    /// 两端规则表一致性门禁：前端 TS 表里的关键词必须都在 Rust 侧存在
    #[test]
    fn test_rules_match_frontend_table() {
        let ts_path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../src/shared/sensitivity-rules.ts");
        let ts = std::fs::read_to_string(&ts_path)
            .unwrap_or_else(|e| panic!("读取前端规则表失败 {}: {e}", ts_path.display()));
        let all: Vec<&str> = CERT_KEYWORDS
            .iter()
            .chain(COMMERCIAL_KEYWORDS.iter())
            .chain(INTERNAL_KEYWORDS.iter())
            .copied()
            .collect();
        for kw in &all {
            assert!(
                ts.contains(&format!("'{kw}'")),
                "前端规则表缺少关键词 {kw}（两端规则已漂移）"
            );
        }
        // 反向：TS 侧字面量也不能多出 Rust 未覆盖的关键词
        for line in ts.lines() {
            let line = line.trim();
            if !(line.starts_with('\'') && line.contains('\'')) {
                continue;
            }
            if let Some(end) = line[1..].find('\'') {
                let kw = &line[1..1 + end];
                if kw.is_empty() || kw.starts_with("//") {
                    continue;
                }
                assert!(
                    all.contains(&kw),
                    "Rust 规则表缺少前端关键词 {kw}（两端规则已漂移）"
                );
            }
        }
    }

    #[test]
    fn test_char_class_helpers() {
        assert!(is_email_char('@'));
        assert!(is_email_char('.'));
        assert!(is_email_char('-'));
        assert!(!is_email_char('/'));
        assert!(is_secret_char('-'));
        assert!(is_secret_char('_'));
        assert!(!is_secret_char('@'));
    }
}