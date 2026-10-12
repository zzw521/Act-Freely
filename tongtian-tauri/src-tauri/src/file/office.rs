//! Office / PDF 正文提取（file:preview 真实化）
//!
//! 离线构建无 zip / pdf 第三方 crate，故本模块自带最小解析：
//! - ZIP：EOCD → 中央目录 → 本地头；STORE 直取，DEFLATE 交 flate2 解码，CRC 可校验
//! - OOXML（docx / xlsx / pptx）：部件 XML 标签扫描，只取正文文本节点，不还原排版
//! - PDF：版本 / 页数 / 信息字典 + 内容流（FlateDecode）里的文本操作符
//!
//! 失败统一返回 `FILE_CONVERSION_FAILED: <原因>`；能提取但不完整时用 note 说明，
//! 绝不静默返回空正文（前端把 note 原样展示给用户）。

use flate2::read::DeflateDecoder;
use std::io::Read;

/// 容器（zip）单部件解压上限，防 zip 炸弹
const MAX_ENTRY_BYTES: u64 = 64 * 1024 * 1024;
/// 预览正文上限（字符数）；超出显式标注截断，不静默
const MAX_PREVIEW_CHARS: usize = 60_000;
/// 单个工作表最多渲染行数
const MAX_SHEET_ROWS: usize = 300;

const SIG_EOCD: u32 = 0x0605_4B50;
const SIG_CENTRAL: u32 = 0x0201_4B50;
const SIG_LOCAL: u32 = 0x0403_4B50;

const MIME_DOCX: &str = "application/vnd.openxmlformats-officedocument.wordprocessingml.document";
const MIME_XLSX: &str = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";
const MIME_PPTX: &str = "application/vnd.openxmlformats-officedocument.presentationml.presentation";

/// 解析失败统一错误码前缀（前端按 `CODE: detail` 拆词展示）
macro_rules! conv_err {
    ($($arg:tt)*) => {
        format!("FILE_CONVERSION_FAILED: {}", format!($($arg)*))
    };
}

/// 提取结果。`supported=false` 表示正文不可信，仅给出元信息 + note 说明原因
#[derive(Debug)]
pub struct Extracted {
    pub content: String,
    pub supported: bool,
    pub note: Option<String>,
}

/// 按 MIME 抽取正文文本；不支持的格式与解析失败均返回 `FILE_CONVERSION_FAILED: <原因>`
pub fn extract(mime: &str, bytes: &[u8]) -> Result<Extracted, String> {
    let (content, note) = match mime {
        MIME_DOCX => (docx_text(bytes)?, None),
        MIME_XLSX => (xlsx_text(bytes)?, None),
        MIME_PPTX => (pptx_text(bytes)?, None),
        "application/pdf" => pdf_preview(bytes),
        m if m.starts_with("application/msword") || m.starts_with("application/vnd.ms-") => {
            return Err(conv_err!(
                "旧版二进制 Office 格式（.doc/.xls/.ppt）暂无解析能力，请另存为 docx/xlsx/pptx 后预览"
            ))
        }
        m => return Err(conv_err!("不支持预览的格式（{m}），文档类请另存为 docx/xlsx/pptx/pdf")),
    };
    let (content, truncated) = truncate_preview(content);
    let note = match (note, truncated) {
        (Some(n), Some(t)) => Some(format!("{n}；{t}")),
        (Some(n), None) => Some(n),
        (None, Some(t)) => Some(t),
        (None, None) => None,
    };
    let supported = !content.trim().is_empty();
    Ok(Extracted { content, supported, note })
}


// ── ZIP 最小读取 ───────────────────────────────────────────

#[derive(Debug, Clone)]
pub struct ZipEntry {
    pub name: String,
    pub method: u16,
    pub crc: u32,
    pub comp_size: u64,
    pub uncomp_size: u64,
    pub data_offset: u64,
}

fn u16le(b: &[u8], at: usize) -> u16 {
    u16::from_le_bytes([b[at], b[at + 1]])
}

fn u32le(b: &[u8], at: usize) -> u32 {
    u32::from_le_bytes([b[at], b[at + 1], b[at + 2], b[at + 3]])
}

/// 解析压缩包条目清单（中央目录顺序）
pub fn zip_entries(bytes: &[u8]) -> Result<Vec<ZipEntry>, String> {
    let len = bytes.len();
    if len < 22 {
        return Err(conv_err!("文件过小，不是有效的 Office 压缩包"));
    }
    // EOCD 固定 22 字节起，尾部注释最长 65535 字节
    let floor = len.saturating_sub(22 + 0xFFFF);
    let mut eocd = None;
    let mut i = len - 22;
    loop {
        if u32le(bytes, i) == SIG_EOCD {
            eocd = Some(i);
            break;
        }
        if i == 0 || i <= floor + 1 {
            break;
        }
        i -= 1;
    }
    let eocd = eocd.ok_or_else(|| conv_err!("未找到压缩包目录（EOCD），文件可能已损坏或不完整"))?;
    let cd_count = u16le(bytes, eocd + 10) as usize;
    let cd_offset = u32le(bytes, eocd + 16) as usize;
    if cd_offset == u32::MAX as usize || cd_count == 0xFFFF {
        return Err(conv_err!("暂不支持 ZIP64 压缩包（条目数或目录偏移超出 32 位）"));
    }
    let mut out = Vec::new();
    let mut p = cd_offset;
    for _ in 0..cd_count {
        if p + 46 > len || u32le(bytes, p) != SIG_CENTRAL {
            return Err(conv_err!("压缩包目录损坏（第 {} 项偏移 {} 无效）", out.len() + 1, p));
        }
        let method = u16le(bytes, p + 10);
        let crc = u32le(bytes, p + 16);
        let comp_size = u32le(bytes, p + 20) as u64;
        let uncomp_size = u32le(bytes, p + 24) as u64;
        let name_len = u16le(bytes, p + 28) as usize;
        let extra_len = u16le(bytes, p + 30) as usize;
        let comment_len = u16le(bytes, p + 32) as usize;
        let local_offset = u32le(bytes, p + 42) as usize;
        let name_end = p + 46 + name_len;
        if name_end > len {
            return Err(conv_err!("压缩包目录损坏（文件名越界）"));
        }
        let name = String::from_utf8_lossy(&bytes[p + 46..name_end]).replace('\\', "/");
        // 真实数据起点要从本地头解析：本地头的 extra 长度可与中央目录不同
        if local_offset + 30 > len || u32le(bytes, local_offset) != SIG_LOCAL {
            return Err(conv_err!("压缩包本地头损坏（偏移 {local_offset}）"));
        }
        let l_name_len = u16le(bytes, local_offset + 26) as usize;
        let l_extra_len = u16le(bytes, local_offset + 28) as usize;
        let data_offset = local_offset + 30 + l_name_len + l_extra_len;
        out.push(ZipEntry {
            name,
            method,
            crc,
            comp_size,
            uncomp_size,
            data_offset: data_offset as u64,
        });
        p = name_end + extra_len + comment_len;
    }
    Ok(out)
}

/// 读取单个条目的原始字节（STORE 直取，DEFLATE 解压），并校验 CRC
pub fn zip_read(bytes: &[u8], entry: &ZipEntry) -> Result<Vec<u8>, String> {
    let start = entry.data_offset as usize;
    let end = start + entry.comp_size as usize;
    if end > bytes.len() {
        return Err(conv_err!("压缩包数据越界（{}）", entry.name));
    }
    if entry.uncomp_size > MAX_ENTRY_BYTES {
        return Err(conv_err!(
            "压缩包内部件过大（{} bytes，超出 {}MB），拒绝解压",
            entry.uncomp_size,
            MAX_ENTRY_BYTES / 1024 / 1024
        ));
    }
    let raw = &bytes[start..end];
    let data = match entry.method {
        0 => raw.to_vec(),
        8 => {
            let mut dec = DeflateDecoder::new(raw);
            let mut buf = Vec::with_capacity((entry.uncomp_size as usize).min(1 << 20));
            dec.read_to_end(&mut buf)
                .map_err(|e| conv_err!("压缩包解压失败（{}：{e}）", entry.name))?;
            buf
        }
        m => return Err(conv_err!("不支持的压缩算法（method={m}，部件 {}）", entry.name)),
    };
    // 部分打包器把 CRC 留给数据描述符（值为 0），此时跳过校验
    if entry.crc != 0 {
        let got = crc32fast::hash(&data);
        if got != entry.crc {
            return Err(conv_err!("压缩包校验失败（部件 {}：CRC 不匹配）", entry.name));
        }
    }
    Ok(data)
}

/// 按名称（大小写不敏感）查条目
fn zip_find<'a>(entries: &'a [ZipEntry], name: &str) -> Option<&'a ZipEntry> {
    let lower = name.to_ascii_lowercase();
    entries
        .iter()
        .find(|e| e.name.to_ascii_lowercase() == lower)
}

/// 读取部件文本（UTF-8）
fn zip_part(entries: &[ZipEntry], bytes: &[u8], name: &str) -> Result<Option<String>, String> {
    let Some(entry) = zip_find(entries, name) else {
        return Ok(None);
    };
    let data = zip_read(bytes, entry)?;
    Ok(Some(match String::from_utf8(data) {
        Ok(s) => s,
        Err(e) => String::from_utf8_lossy(&e.into_bytes()).to_string(),
    }))
}

// ── XML 标签扫描 ───────────────────────────────────────────

#[derive(Debug, PartialEq)]
enum Ev<'a> {
    Text(&'a str),
    Open(&'a str, &'a str, bool),
    Close(&'a str),
}

/// 把 XML 拆成事件序列；仅用于正文抽取，不做完整 XML 校验（不处理命名空间与属性内的 `>`）
fn events(xml: &str) -> Vec<Ev<'_>> {
    let mut out = Vec::new();
    let b = xml.as_bytes();
    let mut i = 0usize;
    let mut text_start = 0usize;
    while i < b.len() {
        if b[i] != b'<' {
            i += 1;
            continue;
        }
        if i > text_start {
            out.push(Ev::Text(&xml[text_start..i]));
        }
        if xml[i..].starts_with("<!--") {
            match xml[i..].find("-->") {
                Some(rel) => {
                    i += rel + 3;
                    text_start = i;
                }
                None => break,
            }
            continue;
        }
        let mut j = i + 1;
        while j < b.len() && b[j] != b'>' {
            j += 1;
        }
        if j >= b.len() {
            break;
        }
        let inner = xml[i + 1..j].trim_end();
        if let Some(name) = inner.strip_prefix('/') {
            out.push(Ev::Close(name.trim()));
        } else if !inner.starts_with('?') && !inner.starts_with('!') {
            let empty = inner.ends_with('/');
            let (name, attrs) = split_tag_body(inner);
            out.push(Ev::Open(name, attrs, empty));
        }
        i = j + 1;
        text_start = i;
    }
    if text_start < xml.len() {
        out.push(Ev::Text(&xml[text_start..]));
    }
    out
}

fn split_tag_body(body: &str) -> (&str, &str) {
    let trimmed = body.trim_end_matches('/').trim_end();
    let idx = trimmed
        .find(|c: char| c.is_whitespace())
        .unwrap_or(trimmed.len());
    let (name, rest) = trimmed.split_at(idx);
    (name, rest.trim_start())
}

/// 取属性值（仅支持双引号写法，OOXML 一致）；要求 key 前一个是空白，避免 `t=` 命中 `alt=`
fn attr<'a>(attrs: &'a str, key: &str) -> Option<&'a str> {
    let pat = format!("{key}=\"");
    let bytes = attrs.as_bytes();
    let mut from = 0usize;
    loop {
        let rel = attrs[from..].find(&pat)?;
        let at = from + rel;
        from = at + 1;
        if at != 0 && !bytes[at - 1].is_ascii_whitespace() {
            continue;
        }
        let rest = &attrs[at + pat.len()..];
        let end = rest.find('"')?;
        return Some(&rest[..end]);
    }
}

fn xml_decode(raw: &str) -> String {
    if !raw.contains('&') {
        return raw.to_string();
    }
    let mut out = String::with_capacity(raw.len());
    let mut rest = raw;
    while let Some(at) = rest.find('&') {
        out.push_str(&rest[..at]);
        let tail = &rest[at..];
        match tail.find(';') {
            Some(end) if end <= 12 => {
                out.push_str(&decode_entity(&tail[1..end]));
                rest = &tail[end + 1..];
            }
            _ => {
                out.push('&');
                rest = tail.get(1..).unwrap_or("");
            }
        }
    }
    out.push_str(rest);
    out
}

fn decode_entity(body: &str) -> String {
    match body {
        "amp" => "&".into(),
        "lt" => "<".into(),
        "gt" => ">".into(),
        "quot" => "\"".into(),
        "apos" => "'".into(),
        _ => {
            let numeric = body.strip_prefix("#x").or_else(|| body.strip_prefix("#X"));
            if let Some(hex) = numeric {
                return u32::from_str_radix(hex, 16)
                    .ok()
                    .and_then(char::from_u32)
                    .map(|c| c.to_string())
                    .unwrap_or_default();
            }
            if let Some(dec) = body.strip_prefix('#') {
                return dec
                    .parse::<u32>()
                    .ok()
                    .and_then(char::from_u32)
                    .map(|c| c.to_string())
                    .unwrap_or_default();
            }
            format!("&{body};")
        }
    }
}

fn collapse_blank_lines(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut blank = 0usize;
    for line in text.lines() {
        let line = line.trim_end();
        if line.is_empty() {
            blank += 1;
            continue;
        }
        if !out.is_empty() {
            out.push('\n');
            if blank > 1 {
                out.push('\n');
            }
        }
        out.push_str(line);
        blank = 0;
    }
    out
}

fn truncate_preview(text: String) -> (String, Option<String>) {
    let total = text.chars().count();
    if total <= MAX_PREVIEW_CHARS {
        return (text, None);
    }
    let cut = text
        .char_indices()
        .map(|(i, _)| i)
        .nth(MAX_PREVIEW_CHARS)
        .unwrap_or(text.len());
    (
        text[..cut].to_string(),
        Some(format!(
            "预览仅展示前 {MAX_PREVIEW_CHARS} 字（共 {total} 字），完整内容请用本地应用打开"
        )),
    )
}

// ── docx ──────────────────────────────────────────────────

/// docx 正文：只取 `<w:t>` 文本节点，段落结束换行，单元格用制表符分隔
pub fn docx_text(container: &[u8]) -> Result<String, String> {
    let entries = zip_entries(container)?;
    let body = zip_part(&entries, container, "word/document.xml")?
        .ok_or_else(|| conv_err!("缺少 word/document.xml，不是有效的 Word 文档"))?;
    Ok(collapse_blank_lines(&docx_fragment(&body)))
}

fn docx_fragment(xml: &str) -> String {
    let mut out = String::new();
    let mut in_text = false;
    for ev in events(xml) {
        match ev {
            Ev::Open(name, _, empty) => match name {
                "w:t" => in_text = !empty,
                "w:tab" => out.push('\t'),
                "w:br" | "w:cr" => out.push('\n'),
                _ => {}
            },
            Ev::Close(name) => match name {
                "w:t" => in_text = false,
                "w:p" => out.push('\n'),
                "w:tc" => {
                    // 单元格内的段落换行被单元格边界取代，否则表格会裂成多行
                    while out.ends_with('\n') {
                        out.pop();
                    }
                    out.push('\t');
                }
                "w:tr" => {
                    while out.ends_with('\t') {
                        out.pop();
                    }
                    out.push('\n');
                }
                _ => {}
            },
            Ev::Text(txt) => {
                if in_text {
                    out.push_str(&xml_decode(txt));
                }
            }
        }
    }
    out
}

// ── xlsx ──────────────────────────────────────────────────

/// xlsx：工作表名 + 单元格矩阵（共享字符串还原，公式取缓存值）
pub fn xlsx_text(container: &[u8]) -> Result<String, String> {
    let entries = zip_entries(container)?;
    let workbook = zip_part(&entries, container, "xl/workbook.xml")?
        .ok_or_else(|| conv_err!("缺少 xl/workbook.xml，不是有效的 Excel 文档"))?;
    let sheets = sheet_order(&workbook);
    let rels = zip_part(&entries, container, "xl/_rels/workbook.xml.rels")?
        .unwrap_or_default();
    let rel_map = worksheet_targets(&rels);
    let shared = match zip_part(&entries, container, "xl/sharedStrings.xml")? {
        Some(s) => shared_strings(&s),
        None => Vec::new(),
    };
    if sheets.is_empty() {
        return Err(conv_err!("workbook.xml 未声明任何工作表"));
    }

    let mut out = String::new();
    for (idx, (name, rid)) in sheets.iter().enumerate() {
        let part = rel_map
            .get(rid.as_str())
            .cloned()
            .unwrap_or_else(|| format!("xl/worksheets/sheet{}.xml", idx + 1));
        let Some(xml) = zip_part(&entries, container, &part)? else {
            out.push_str(&format!("【{name}】\n（工作表部件缺失：{part}）\n\n"));
            continue;
        };
        let rows = sheet_rows(&xml, &shared);
        out.push_str(&format!("【{name}】\n"));
        let shown_count = rows.len().min(MAX_SHEET_ROWS);
        for row in &rows[..shown_count] {
            out.push_str(&row.join("\t"));
            out.push('\n');
        }
        if rows.len() > shown_count {
            out.push_str(&format!(
                "……（本表共 {} 行，仅展示前 {MAX_SHEET_ROWS} 行）\n",
                rows.len()
            ));
        }
        if rows.is_empty() {
            out.push_str("（空表）\n");
        }
        out.push('\n');
    }
    Ok(out.trim_end().to_string())
}

/// workbook.xml 中 `<sheet>` 的出现顺序 + 名称 + r:id
fn sheet_order(xml: &str) -> Vec<(String, String)> {
    let mut out = Vec::new();
    for ev in events(xml) {
        if let Ev::Open("sheet", attrs, _) = ev {
            let name = attr(attrs, "name").unwrap_or("未命名工作表").to_string();
            let rid = attr(attrs, "r:id")
                .or_else(|| attr(attrs, "id"))
                .unwrap_or_default()
                .to_string();
            out.push((name, rid));
        }
    }
    out
}

/// rels 中 rId → 部件路径（统一成 `xl/...`）
fn worksheet_targets(rels_xml: &str) -> std::collections::HashMap<String, String> {
    let mut map = std::collections::HashMap::new();
    for ev in events(rels_xml) {
        if let Ev::Open("Relationship", attrs, _) = ev {
            let ty = attr(attrs, "Type").unwrap_or_default();
            if !ty.ends_with("/worksheet") {
                continue;
            }
            let (Some(id), Some(target)) = (attr(attrs, "Id"), attr(attrs, "Target")) else {
                continue;
            };
            let normalized = if let Some(abs) = target.strip_prefix('/') {
                abs.to_string()
            } else {
                let mut joined = format!("xl/{target}");
                // Target 可能写成 "../worksheets/sheet1.xml" 之类的相对路径
                while joined.contains("/../") {
                    joined = joined.replace("/../", "/");
                }
                joined
            };
            map.insert(id.to_string(), normalized);
        }
    }
    map
}

fn shared_strings(xml: &str) -> Vec<String> {
    let mut list = Vec::new();
    let mut cur: Option<String> = None;
    let mut in_t = false;
    for ev in events(xml) {
        match ev {
            Ev::Open("si", _, _) => cur = Some(String::new()),
            Ev::Open("t", _, empty) => in_t = !empty,
            Ev::Close("t") => in_t = false,
            Ev::Close("si") => {
                list.push(cur.take().unwrap_or_default());
            }
            Ev::Text(txt) => {
                if in_t {
                    if let Some(c) = cur.as_mut() {
                        c.push_str(&xml_decode(txt));
                    }
                }
            }
            _ => {}
        }
    }
    list
}

fn sheet_rows(xml: &str, shared: &[String]) -> Vec<Vec<String>> {
    let mut rows: Vec<Vec<String>> = Vec::new();
    let mut row: Vec<String> = Vec::new();
    let mut in_row = false;
    let mut in_v = false;
    let mut in_t = false;
    let mut cell_type = String::new();
    let mut cell_ref = String::new();
    let mut cell_raw = String::new();
    for ev in events(xml) {
        match ev {
            Ev::Open(name, attrs, empty) => match name {
                "row" => {
                    in_row = true;
                    row.clear();
                    if empty {
                        rows.push(Vec::new());
                        in_row = false;
                    }
                }
                "c" => {
                    cell_type = attr(attrs, "t").unwrap_or_default().to_string();
                    cell_ref = attr(attrs, "r").unwrap_or_default().to_string();
                    cell_raw.clear();
                    if empty && in_row {
                        push_cell(&mut row, &cell_ref, String::new());
                    }
                }
                "v" => in_v = !empty,
                "t" => in_t = !empty,
                _ => {}
            },
            Ev::Close(name) => match name {
                "v" => in_v = false,
                "t" => in_t = false,
                "c" => {
                    if in_row {
                        let text = resolve_cell(&cell_type, &cell_raw, shared);
                        push_cell(&mut row, &cell_ref, text);
                    }
                    cell_raw.clear();
                }
                "row" => {
                    if in_row {
                        rows.push(std::mem::take(&mut row));
                    }
                    in_row = false;
                }
                _ => {}
            },
            Ev::Text(txt) => {
                if in_v || in_t {
                    cell_raw.push_str(&xml_decode(txt));
                }
            }
        }
    }
    rows
}

/// 按单元格引用（如 "C3"）落位，跳过的列补空保持对齐
fn push_cell(row: &mut Vec<String>, cell_ref: &str, value: String) {
    let Some(col) = column_index(cell_ref) else {
        row.push(value);
        return;
    };
    while row.len() < col {
        row.push(String::new());
    }
    if row.len() == col {
        row.push(value);
    } else {
        row[col] = value;
    }
}

fn column_index(cell_ref: &str) -> Option<usize> {
    let mut idx = 0usize;
    let mut seen = false;
    for ch in cell_ref.chars() {
        if ch.is_ascii_alphabetic() {
            let letter = ch.to_ascii_uppercase() as usize - 'A' as usize + 1;
            idx = idx * 26 + letter;
            seen = true;
        } else {
            break;
        }
    }
    if seen {
        Some(idx - 1)
    } else {
        None
    }
}

fn resolve_cell(cell_type: &str, raw: &str, shared: &[String]) -> String {
    match cell_type {
        "s" => raw
            .trim()
            .parse::<usize>()
            .ok()
            .and_then(|i| shared.get(i))
            .cloned()
            .unwrap_or_else(|| raw.to_string()),
        "b" => {
            if raw.trim() == "1" {
                "TRUE".into()
            } else {
                "FALSE".into()
            }
        }
        _ => raw.to_string(),
    }
}

// ── pptx ──────────────────────────────────────────────────

/// pptx：按幻灯片顺序取 `<a:t>` 文本（备注页不纳入，避免与正文混排）
pub fn pptx_text(container: &[u8]) -> Result<String, String> {
    let entries = zip_entries(container)?;
    let mut slides: Vec<&ZipEntry> = entries
        .iter()
        .filter(|e| {
            e.name.starts_with("ppt/slides/slide")
                && e.name.ends_with(".xml")
        })
        .collect();
    if slides.is_empty() {
        return Err(conv_err!("未找到任何幻灯片部件（ppt/slides/slideN.xml）"));
    }
    slides.sort_by_key(|e| slide_no(&e.name).unwrap_or(usize::MAX));
    let mut out = String::new();
    for (i, entry) in slides.iter().enumerate() {
        let Some(xml) = zip_part(&entries, container, &entry.name)? else {
            continue;
        };
        out.push_str(&format!("【第 {} 页】\n", i + 1));
        out.push_str(&collapse_blank_lines(&pptx_fragment(&xml)));
        out.push('\n');
        out.push('\n');
    }
    Ok(out.trim_end().to_string())
}

fn slide_no(name: &str) -> Option<usize> {
    let tail = name.rsplit('/').next()?;
    let stem = tail.trim_start_matches("slide").trim_end_matches(".xml");
    stem.parse::<usize>().ok()
}

fn pptx_fragment(xml: &str) -> String {
    let mut out = String::new();
    let mut in_t = false;
    for ev in events(xml) {
        match ev {
            Ev::Open("a:t", _, empty) => in_t = !empty,
            Ev::Close("a:t") => in_t = false,
            Ev::Close("a:p") => out.push('\n'),
            Ev::Text(txt) => {
                if in_t {
                    out.push_str(&xml_decode(txt));
                }
            }
            _ => {}
        }
    }
    out
}

// ── PDF ───────────────────────────────────────────────────

/// PDF：给出版本/页数/文档信息，并尽力从内容流提取正文
fn pdf_preview(bytes: &[u8]) -> (String, Option<String>) {
    if !bytes.starts_with(b"%PDF-") {
        return (
            String::new(),
            Some(conv_err!("文件头缺少 %PDF 标识，可能不是有效的 PDF")),
        );
    }
    let version = String::from_utf8_lossy(&bytes[5..std::cmp::min(bytes.len(), 13)])
        .split(|c: char| !c.is_ascii_digit() && c != '.')
        .next()
        .filter(|s| !s.is_empty())
        .unwrap_or("未知")
        .to_string();
    let encrypted = contains_token(bytes, b"/Encrypt");
    let pages = pdf_page_count(bytes);
    let mut head = format!("PDF {version}");
    if let Some(p) = pages {
        head.push_str(&format!(" · 共 {p} 页"));
    }
    if encrypted {
        head.push_str(" · 已加密（正文不可读）");
    }
    for (label, key) in [
        ("标题", b"/Title".as_slice()),
        ("作者", b"/Author".as_slice()),
        ("生成程序", b"/Producer".as_slice()),
        ("创建程序", b"/Creator".as_slice()),
    ] {
        if let Some(v) = pdf_info_value(bytes, key) {
            if !v.trim().is_empty() {
                head.push('\n');
                head.push_str(&format!("{label}：{v}"));
            }
        }
    }
    let body = if encrypted {
        String::new()
    } else {
        pdf_text(bytes)
    };
    if body.trim().is_empty() {
        return (
            collapse_blank_lines(&head),
            Some(if encrypted {
                "PDF 已加密，未提取正文；请解除口令后重试".to_string()
            } else {
                "未能从内容流提取正文（多为嵌入字体子集/CID 编码或纯扫描件），已提供页数与文档信息".to_string()
            }),
        );
    }
    (
        collapse_blank_lines(&format!("{head}\n\n{body}")),
        Some("PDF 正文按内容流顺序提取，不含排版与图片文字".to_string()),
    )
}

fn contains_token(bytes: &[u8], token: &[u8]) -> bool {
    bytes.windows(token.len()).any(|w| w == token)
}

/// 页数：优先数 `/Type /Page`（排除 /Pages），退化到页树 `/Count`
fn pdf_page_count(bytes: &[u8]) -> Option<u32> {
    let mut pages = 0u32;
    let mut i = 0usize;
    while i + 5 < bytes.len() {
        if bytes[i] == b'/' {
            let (name, next) = read_name(bytes, i + 1);
            if name == b"Type" {
                let (value, after) = read_name(bytes, skip_space(bytes, next));
                if value == b"Page" && after < bytes.len() {
                    let c = bytes[after];
                    if !c.is_ascii_alphanumeric() && c != b'_' && c != b'.' {
                        pages += 1;
                    }
                }
                i = after;
                continue;
            }
            i = next;
            continue;
        }
        i += 1;
    }
    if pages > 0 {
        return Some(pages);
    }
    // 页树根 `/Type /Pages ... /Count N`
    let mut best = 0u32;
    let mut i = 0usize;
    while i + 6 < bytes.len() {
        if bytes[i] == b'/' {
            let (name, next) = read_name(bytes, i + 1);
            if name == b"Count" {
                let after = skip_space(bytes, next);
                let mut j = after;
                while j < bytes.len() && bytes[j].is_ascii_digit() {
                    j += 1;
                }
                if j > after {
                    if let Ok(n) = std::str::from_utf8(&bytes[after..j]).map(|s| s.parse::<u32>()) {
                        if let Ok(n) = n {
                            best = best.max(n);
                        }
                    }
                }
            }
            i = next;
            continue;
        }
        i += 1;
    }
    if best > 0 {
        Some(best)
    } else {
        None
    }
}

fn read_name(bytes: &[u8], mut i: usize) -> (Vec<u8>, usize) {
    while i < bytes.len() && bytes[i].is_ascii_whitespace() {
        i += 1;
    }
    if i < bytes.len() && bytes[i] == b'/' {
        i += 1;
    }
    let start = i;
    while i < bytes.len() && !bytes[i].is_ascii_whitespace() && bytes[i] != b'/' && bytes[i] != b'>' {
        i += 1;
    }
    (bytes[start..i].to_vec(), i)
}

fn skip_space(bytes: &[u8], mut i: usize) -> usize {
    while i < bytes.len() && bytes[i].is_ascii_whitespace() {
        i += 1;
    }
    i
}

/// 信息字典里的字符串值（`/Title (…)` 或 `/Title <hex>`）
fn pdf_info_value(bytes: &[u8], key: &[u8]) -> Option<String> {
    let at = find_slice(bytes, key)?;
    let i = skip_space(bytes, at + key.len());
    if i >= bytes.len() {
        return None;
    }
    match bytes[i] {
        b'(' => {
            let (raw, _) = read_literal(bytes, i);
            Some(pdf_string_to_text(&raw))
        }
        b'<' if bytes.get(i + 1).map(|c| *c != b'<').unwrap_or(false) => {
            let end = i + 1;
            let hex_end = bytes[end..]
                .iter()
                .position(|c| *c == b'>')
                .map(|p| end + p)?;
            let hex = String::from_utf8_lossy(&bytes[end..hex_end]);
            let compact: String = hex.chars().filter(|c| !c.is_whitespace()).collect();
            let mut buf = Vec::new();
            let mut it = compact.chars();
            while let (Some(a), Some(b)) = (it.next(), it.next()) {
                if let Ok(v) = u8::from_str_radix(&format!("{a}{b}"), 16) {
                    buf.push(v);
                }
            }
            Some(pdf_string_to_text(&buf))
        }
        _ => None,
    }
}

fn find_slice(hay: &[u8], needle: &[u8]) -> Option<usize> {
    if needle.is_empty() || hay.len() < needle.len() {
        return None;
    }
    hay.windows(needle.len()).position(|w| w == needle)
}

/// 逐个 stream 段尝试解压并抽取文本操作符
fn pdf_text(bytes: &[u8]) -> String {
    let mut out = String::new();
    let mut pos = 0usize;
    while let Some(rel) = find_slice(&bytes[pos..], b"stream") {
        let mut start = pos + rel + 6;
        if bytes.get(start) == Some(&b'\r') {
            start += 1;
        }
        if bytes.get(start) == Some(&b'\n') {
            start += 1;
        }
        let Some(rel_end) = find_slice(&bytes[start..], b"endstream") else {
            break;
        };
        let raw = &bytes[start..start + rel_end];
        let content = match inflate(raw) {
            Some(decoded) => decoded,
            None => raw.to_vec(),
        };
        out.push_str(&pdf_text_ops(&content));
        pos = start + rel_end + 9;
    }
    collapse_blank_lines(&out)
}

fn inflate(raw: &[u8]) -> Option<Vec<u8>> {
    if raw.len() < 8 {
        return None;
    }
    let mut dec = DeflateDecoder::new(raw);
    let mut buf = Vec::new();
    dec.read_to_end(&mut buf).ok()?;
    if buf.is_empty() || buf.len() as u64 > MAX_ENTRY_BYTES {
        return None;
    }
    Some(buf)
}

/// 内容流文本操作符：`(…) Tj`、`[(…) …] TJ`、`(…)'`，以及 BT/ET/Td/TD/T* 换行
fn pdf_text_ops(content: &[u8]) -> String {
    let mut out = String::new();
    let mut i = 0usize;
    while i < content.len() {
        match content[i] {
            b'(' => {
                let (raw, next) = read_literal(content, i);
                if op_after(content, next) == Op::Show {
                    out.push_str(&pdf_string_to_text(&raw));
                }
                i = next;
            }
            b'[' => {
                let (inner, next) = read_array(content, i);
                if op_after(content, next) == Op::BigShow {
                    for raw in inner {
                        out.push_str(&pdf_string_to_text(&raw));
                    }
                }
                i = next;
            }
            b'<' if content.get(i + 1) == Some(&b'<') => {
                let (inner, next) = read_hex_array(content, i);
                if op_after(content, next) == Op::BigShow {
                    out.push_str(&pdf_string_to_text(&inner));
                }
                i = next;
            }
            b'T' if content[i..].starts_with(b"T*") => {
                new_line(&mut out);
                i += 2;
            }
            b'T' if content[i..].starts_with(b"Td") || content[i..].starts_with(b"TD") => {
                new_line(&mut out);
                i += 2;
            }
            b'E' if content[i..].starts_with(b"ET") => {
                new_line(&mut out);
                i += 2;
            }
            _ => i += 1,
        }
    }
    out
}

fn new_line(out: &mut String) {
    if !out.ends_with('\n') && !out.is_empty() {
        out.push('\n');
    }
}

#[derive(PartialEq)]
enum Op {
    None,
    Show,
    BigShow,
}

/// 跳过空白与数字参数后识别紧随其后的操作符
fn op_after(content: &[u8], mut i: usize) -> Op {
    loop {
        i = skip_space(content, i);
        if i >= content.len() {
            return Op::None;
        }
        let c = content[i];
        if c.is_ascii_digit() || c == b'-' || c == b'+' || c == b'.' {
            i += 1;
            continue;
        }
        break;
    }
    let rest = &content[i..];
    if rest.starts_with(b"Tj") {
        Op::Show
    } else if rest.starts_with(b"TJ") {
        Op::BigShow
    } else if rest.starts_with(b"'") {
        Op::Show
    } else {
        Op::None
    }
}

/// 读 `[ … ]` 数组内的字面串（跳过转义与嵌套括号）
fn read_array(content: &[u8], at: usize) -> (Vec<Vec<u8>>, usize) {
    let mut out = Vec::new();
    let mut i = at + 1;
    while i < content.len() {
        match content[i] {
            b']' => return (out, i + 1),
            b'(' => {
                let (raw, next) = read_literal(content, i);
                out.push(raw);
                i = next;
            }
            b'<' if content.get(i + 1) == Some(&b'<') => {
                let (raw, next) = read_hex_array(content, i);
                if !raw.is_empty() {
                    out.push(raw);
                }
                i = next;
            }
            _ => i += 1,
        }
    }
    (out, i)
}

fn read_hex_array(content: &[u8], at: usize) -> (Vec<u8>, usize) {
    let start = at + 2;
    let Some(rel) = content[start..].iter().position(|c| *c == b'>') else {
        return (Vec::new(), content.len());
    };
    let hex = &content[start..start + rel];
    let mut buf = Vec::new();
    let mut digits = hex.iter().filter(|c| c.is_ascii_hexdigit()).map(|c| *c);
    while let (Some(a), Some(b)) = (digits.next(), digits.next()) {
        let hi = (a as char).to_digit(16).unwrap_or(0);
        let lo = (b as char).to_digit(16).unwrap_or(0);
        buf.push((hi * 16 + lo) as u8);
    }
    (buf, start + rel + 1)
}

/// 从 `(` 起读 PDF 字面串，处理反斜杠转义与嵌套括号
fn read_literal(buf: &[u8], at: usize) -> (Vec<u8>, usize) {
    let mut out = Vec::new();
    let mut i = at;
    let mut depth = 0usize;
    if i < buf.len() && buf[i] == b'(' {
        depth = 1;
        i += 1;
    }
    while i < buf.len() {
        let c = buf[i];
        if c == b'\\' {
            i += 1;
            if i >= buf.len() {
                break;
            }
            let e = buf[i];
            match e {
                b'n' => out.push(b'\n'),
                b'r' => out.push(b'\r'),
                b't' => out.push(b'\t'),
                b'b' => out.push(0x08),
                b'f' => out.push(0x0C),
                b'(' | b')' | b'\\' => out.push(e),
                b'0'..=b'7' => {
                    let mut val = e - b'0';
                    let mut j = i + 1;
                    let mut cnt = 1;
                    while j < buf.len() && cnt < 3 && (b'0'..=b'7').contains(&buf[j]) {
                        val = val * 8 + (buf[j] - b'0');
                        j += 1;
                        cnt += 1;
                    }
                    out.push(val);
                    i = j;
                    continue;
                }
                other => out.push(other),
            }
            i += 1;
            continue;
        }
        if c == b'(' {
            depth += 1;
            out.push(c);
            i += 1;
            continue;
        }
        if c == b')' {
            depth = depth.saturating_sub(1);
            i += 1;
            if depth == 0 {
                break;
            }
            out.push(c);
            continue;
        }
        out.push(c);
        i += 1;
    }
    (out, i)
}

/// PDF 串 → 文本：UTF-16BE（BOM）优先，其余按 latin1/UTF-8 宽松解码
fn pdf_string_to_text(raw: &[u8]) -> String {
    if raw.starts_with(&[0xFE, 0xFF]) {
        let mut s = String::new();
        let mut it = raw[2..].chunks(2);
        while let Some(pair) = it.next() {
            if pair.len() == 2 {
                if let Some(c) = char::from_u32(u16::from_be_bytes([pair[0], pair[1]]) as u32) {
                    s.push(c);
                }
            }
        }
        return s;
    }
    if raw.iter().all(|b| *b < 0x80) {
        return String::from_utf8_lossy(raw).to_string();
    }
    let lossy = String::from_utf8_lossy(raw);
    if printable_ratio(&lossy) >= 0.85 {
        return lossy.to_string();
    }
    // 高位字节多为 CID 字体子集编码，输出会是乱码 → 判为不可信正文
    String::new()
}

fn printable_ratio(text: &str) -> f64 {
    let total = text.chars().count();
    if total == 0 {
        return 0.0;
    }
    let ok = text
        .chars()
        .filter(|c| c.is_alphanumeric() || c.is_whitespace() || c.is_ascii_punctuation() || ('\u{4E00}'..='\u{9FFF}').contains(c))
        .count();
    ok as f64 / total as f64
}

// ── 测试辅助：构造 zip 容器（STORE + DEFLATE）─────────────

#[cfg(test)]
pub(crate) fn zip_build(files: &[(&str, &[u8], u16)]) -> Vec<u8> {
    use std::collections::HashMap;
    let mut out: Vec<u8> = Vec::new();
    let mut central: Vec<(String, u16, u32, u32, u32, u32)> = Vec::new(); // name, method, crc, comp, uncomp, offset
    let mut seen: HashMap<String, ()> = HashMap::new();
    for (name, data, method) in files {
        assert!(!seen.contains_key(*name), "重复文件名 {name}");
        seen.insert(name.to_string(), ());
        let crc = crc32fast::hash(data);
        let stored: Vec<u8> = if *method == 8 {
            let mut enc = flate2::write::DeflateEncoder::new(
                Vec::new(),
                flate2::Compression::default(),
            );
            use std::io::Write;
            enc.write_all(data).unwrap();
            enc.finish().unwrap()
        } else {
            data.to_vec()
        };
        let offset = out.len() as u32;
        // 本地头
        out.extend_from_slice(&SIG_LOCAL.to_le_bytes());
        out.extend_from_slice(&20u16.to_le_bytes());
        out.extend_from_slice(&0u16.to_le_bytes());
        out.extend_from_slice(&method.to_le_bytes());
        out.extend_from_slice(&0u16.to_le_bytes());
        out.extend_from_slice(&0u16.to_le_bytes());
        out.extend_from_slice(&crc.to_le_bytes());
        out.extend_from_slice(&(stored.len() as u32).to_le_bytes());
        out.extend_from_slice(&(data.len() as u32).to_le_bytes());
        out.extend_from_slice(&(name.len() as u16).to_le_bytes());
        out.extend_from_slice(&0u16.to_le_bytes());
        out.extend_from_slice(name.as_bytes());
        out.extend_from_slice(&stored);
        central.push((
            name.to_string(),
            *method,
            crc,
            stored.len() as u32,
            data.len() as u32,
            offset,
        ));
    }
    let cd_start = out.len() as u32;
    for (name, method, crc, comp, uncomp, offset) in &central {
        out.extend_from_slice(&SIG_CENTRAL.to_le_bytes());
        out.extend_from_slice(&20u16.to_le_bytes());
        out.extend_from_slice(&20u16.to_le_bytes());
        out.extend_from_slice(&0u16.to_le_bytes());
        out.extend_from_slice(&method.to_le_bytes());
        out.extend_from_slice(&0u16.to_le_bytes());
        out.extend_from_slice(&0u16.to_le_bytes());
        out.extend_from_slice(&crc.to_le_bytes());
        out.extend_from_slice(&comp.to_le_bytes());
        out.extend_from_slice(&uncomp.to_le_bytes());
        out.extend_from_slice(&(name.len() as u16).to_le_bytes());
        out.extend_from_slice(&0u16.to_le_bytes());
        out.extend_from_slice(&0u16.to_le_bytes());   // comment len
        out.extend_from_slice(&0u16.to_le_bytes());   // disk start
        out.extend_from_slice(&0u16.to_le_bytes());   // internal attrs
        out.extend_from_slice(&0u32.to_le_bytes());   // external attrs
        out.extend_from_slice(&offset.to_le_bytes());
        out.extend_from_slice(name.as_bytes());
    }
    let cd_len = (out.len() - cd_start as usize) as u32;
    out.extend_from_slice(&SIG_EOCD.to_le_bytes());
    out.extend_from_slice(&0u16.to_le_bytes());
    out.extend_from_slice(&0u16.to_le_bytes());
    out.extend_from_slice(&(central.len() as u16).to_le_bytes());
    out.extend_from_slice(&(central.len() as u16).to_le_bytes());
    out.extend_from_slice(&cd_len.to_le_bytes());
    out.extend_from_slice(&cd_start.to_le_bytes());
    out.extend_from_slice(&0u16.to_le_bytes());
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn docx_fixture() -> Vec<u8> {
        zip_build(&[
            (
                "[Content_Types].xml",
                b"<?xml version=\"1.0\"?><Types/>",
                0,
            ),
            (
                "word/document.xml",
                "<w:document><w:body>\
                  <w:p><w:r><w:t>通天办公</w:t></w:r></w:p>\
                  <w:p><w:r><w:t xml:space=\"preserve\">Q3 &amp; 展望</w:t><w:tab/><w:t>营收</w:t></w:r></w:p>\
                  <w:p><w:r><w:instrText>HYPERLINK</w:instrText></w:r></w:p>\
                  <w:tbl><w:tr><w:tc><w:p><w:r><w:t>华东</w:t></w:r></w:p></w:tc>\
                  <w:tc><w:p><w:r><w:t>128</w:t></w:r></w:p></w:tc></w:tr></w:tbl>\
                  </w:body></w:document>".as_bytes(),
                8,
            ),
        ])
    }

    #[test]
    fn zip_reads_store_and_deflate() {
        let zip = docx_fixture();
        let entries = zip_entries(&zip).unwrap();
        assert_eq!(entries.len(), 2);
        let plain = zip_read(&zip, &entries[0]).unwrap();
        assert!(String::from_utf8_lossy(&plain).starts_with("<?xml"));
        let body = zip_read(&zip, &entries[1]).unwrap();
        assert!(String::from_utf8_lossy(&body).contains("通天办公"));
    }

    #[test]
    fn zip_rejects_broken_and_truncated() {
        let err = zip_entries(b"PK\x03\x04fake").unwrap_err();
        assert!(err.starts_with("FILE_CONVERSION_FAILED"), "{err}");
        let mut zip = docx_fixture();
        zip.truncate(zip.len() - 30);
        assert!(zip_entries(&zip).unwrap_err().contains("EOCD"));
    }

    #[test]
    fn zip_detects_crc_mismatch() {
        let mut zip = docx_fixture();
        let entries = zip_entries(&zip).unwrap();
        // 破坏 DEFLATE 数据区中部字节
        let victim = &entries[1];
        let at = victim.data_offset as usize + (victim.comp_size as usize / 2);
        zip[at] ^= 0xFF;
        let err = zip_read(&zip, victim).unwrap_err();
        assert!(err.contains("CRC") || err.contains("解压失败"), "{err}");
    }

    #[test]
    fn docx_extracts_paragraphs_and_tables() {
        let zip = docx_fixture();
        let out = docx_text(&zip).unwrap();
        assert!(out.contains("通天办公"), "{out}");
        assert!(out.contains("Q3 & 展望\t营收"), "{out}");
        assert!(out.contains("华东\t128"), "{out}");
        // 域代码（instrText）不得混入正文
        assert!(!out.contains("HYPERLINK"), "{out}");
    }

    #[test]
    fn docx_missing_part_is_explicit() {
        let zip = zip_build(&[("mimetype", b"application/vnd.openxmlformats", 0)]);
        let err = docx_text(&zip).unwrap_err();
        assert!(err.contains("word/document.xml"), "{err}");
    }

    fn xlsx_fixture() -> Vec<u8> {
        zip_build(&[
            (
                "xl/workbook.xml",
                "<workbook><sheets>\
                  <sheet name=\"月度\" sheetId=\"1\" r:id=\"rId1\"/>\
                  <sheet name=\"年度\" sheetId=\"2\" r:id=\"rId2\"/>\
                  </sheets></workbook>".as_bytes(),
                8,
            ),
            (
                "xl/_rels/workbook.xml.rels",
                b"<Relationships>\
                  <Relationship Id=\"rId1\" Type=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet\" Target=\"worksheets/sheet1.xml\"/>\
                  <Relationship Id=\"rId2\" Type=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet\" Target=\"worksheets/sheet2.xml\"/>\
                  <Relationship Id=\"rId9\" Type=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles\" Target=\"styles.xml\"/>\
                  </Relationships>",
                0,
            ),
            (
                "xl/sharedStrings.xml",
                "<sst><si><t>客户</t></si><si><r><t>阿里</t></r><r><t>云</t></r></si><si><t>合同</t></si></sst>".as_bytes(),
                8,
            ),
            (
                "xl/worksheets/sheet1.xml",
                "<worksheet><sheetData>\
                  <row r=\"1\"><c r=\"A1\" t=\"s\"><v>0</v></c><c r=\"B1\" t=\"s\"><v>1</v></c></row>\
                  <row r=\"2\"><c r=\"A2\" t=\"s\"><v>2</v></c><c r=\"C2\"><v>128000.5</v></c><c r=\"D2\" t=\"b\"><v>1</v></c></row>\
                  <row r=\"3\"><c r=\"B3\" t=\"inlineStr\"><is><t>备注 &lt;内部&gt;</t></is></c></row>\
                  </sheetData></worksheet>".as_bytes(),
                8,
            ),
            (
                "xl/worksheets/sheet2.xml",
                b"<worksheet><sheetData><row r=\"1\"><c r=\"A1\"><f>SUM(1,2)</f><v>3</v></c></row></sheetData></worksheet>",
                0,
            ),
        ])
    }

    #[test]
    fn xlsx_renders_sheets_with_shared_strings_and_alignment() {
        let out = xlsx_text(&xlsx_fixture()).unwrap();
        assert!(out.contains("【月度】"), "{out}");
        assert!(out.contains("客户\t阿里云"), "{out}");
        // 稀疏列保持对齐：A2 有值，B2 空，C2 数字，D2 布尔
        assert!(out.contains("合同\t\t128000.5\tTRUE"), "{out}");
        assert!(out.contains("\t备注 <内部>"), "{out}");
        assert!(out.contains("【年度】"), "{out}");
        // 公式取缓存值，不暴露 f 内容
        assert!(out.contains('3') && !out.contains("SUM"), "{out}");
    }

    #[test]
    fn xlsx_without_workbook_is_explicit() {
        let zip = zip_build(&[("xl/worksheets/sheet1.xml", b"<worksheet/>", 0)]);
        let err = xlsx_text(&zip).unwrap_err();
        assert!(err.contains("xl/workbook.xml"), "{err}");
    }

    #[test]
    fn pptx_extracts_slides_in_order() {
        let zip = zip_build(&[
            (
                "ppt/slides/slide2.xml",
                "<p:sld><p:sp><a:t>第二页要点</a:t></p:sp></p:sld>".as_bytes(),
                8,
            ),
            (
                "ppt/slides/slide10.xml",
                "<p:sld><a:p><a:t>第三页</a:t></a:p><a:p><a:t>续行</a:t></a:p></p:sld>".as_bytes(),
                0,
            ),
            (
                "ppt/slides/slide1.xml",
                "<p:sld><a:p><a:t>封面</a:t></a:p></p:sld>".as_bytes(),
                8,
            ),
            ("ppt/notesSlides/notesSlide1.xml", "<a:t>讲者备注</a:t>".as_bytes(), 0),
        ]);
        let out = pptx_text(&zip).unwrap();
        assert!(out.contains("【第 1 页】\n封面"), "{out}");
        assert!(out.contains("【第 2 页】\n第二页要点"), "{out}");
        assert!(out.contains("第三页\n续行"), "{out}");
        assert!(!out.contains("讲者备注"), "{out}");
    }

    #[test]
    fn extract_dispatches_by_mime_and_marks_unsupported() {
        let ex = extract(MIME_DOCX, &docx_fixture()).unwrap();
        assert!(ex.supported);
        assert!(ex.content.contains("通天办公"));
        let err = extract("application/octet-stream", b"MZ").unwrap_err();
        assert!(err.starts_with("FILE_CONVERSION_FAILED"), "{err}");
        assert!(err.contains("docx/xlsx/pptx/pdf"), "{err}");
    }

    #[test]
    fn pdf_extracts_pages_and_flate_text() {
        use flate2::write::DeflateEncoder;
        use std::io::Write;
        let mut enc = DeflateEncoder::new(Vec::new(), flate2::Compression::default());
        enc.write_all(b"BT /F1 12 Tf 72 700 Td (Hello TongTian) Tj 0 -14 Td (\\(esc\\) ok) Tj ET").unwrap();
        let stream = enc.finish().unwrap();
        let mut pdf: Vec<u8> = b"%PDF-1.7\n".to_vec();
        pdf.extend_from_slice(b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n");
        pdf.extend_from_slice(b"2 0 obj<</Type/Pages/Kids[3 0 R 4 0 R]/Count 2>>endobj\n");
        pdf.extend_from_slice(b"3 0 obj<</Type/Page/Parent 2 0 R>>endobj\n");
        pdf.extend_from_slice(b"4 0 obj<</Type/Page/Parent 2 0 R>>endobj\n");
        pdf.extend_from_slice(b"9 0 obj<</Title(Quarterly Review)/Author(Zed)>>endobj\n");
        pdf.extend_from_slice(b"5 0 obj<</Length ");
        pdf.extend_from_slice(stream.len().to_string().as_bytes());
        pdf.extend_from_slice(b" /Filter/FlateDecode>>stream\n");
        pdf.extend_from_slice(&stream);
        pdf.extend_from_slice(b"\nendstream endobj\n");
        let (content, note) = pdf_preview(&pdf);
        assert!(content.contains("PDF 1.7"), "{content}");
        assert!(content.contains("共 2 页"), "{content}");
        assert!(content.contains("标题：Quarterly Review"), "{content}");
        assert!(content.contains("作者：Zed"), "{content}");
        assert!(content.contains("Hello TongTian"), "{content}");
        assert!(content.contains("(esc) ok"), "{content}");
        assert!(note.as_deref().unwrap().contains("不含排版"), "{note:?}");
    }

    #[test]
    fn pdf_without_readable_body_reports_honestly() {
        let mut pdf: Vec<u8> = b"%PDF-1.4\n".to_vec();
        pdf.extend_from_slice(b"1 0 obj<</Type/Page/Parent 2 0 R>>endobj\n");
        pdf.extend_from_slice(b"2 0 obj<</Type/Pages/Count 1/Kids[1 0 R]>>endobj\n");
        pdf.extend_from_slice(b"3 0 obj<</Filter/FlateDecode>>stream\n");
        pdf.extend_from_slice(&[0xFF, 0xFE, 0x01, 0x02]);
        pdf.extend_from_slice(b"\nendstream endobj\n");
        let (content, note) = pdf_preview(&pdf);
        assert!(content.contains("共 1 页"), "{content}");
        assert!(note.as_deref().unwrap().contains("未能从内容流提取正文"), "{note:?}");
    }

    #[test]
    fn pdf_header_missing_is_error_not_silence() {
        let (content, note) = pdf_preview(b"not a pdf at all");
        assert!(content.is_empty());
        assert!(note.as_deref().unwrap().contains("%PDF"), "{note:?}");
    }

    #[test]
    fn extract_marks_unsupported_when_body_empty() {
        // docx 正文为空 → supported=false，note 不为空谎称可读
        let zip = zip_build(&[("word/document.xml", b"<w:document><w:body><w:p/></w:body></w:document>", 0)]);
        let ex = extract(MIME_DOCX, &zip).unwrap();
        assert!(!ex.supported);
        assert!(ex.content.trim().is_empty());
    }

    #[test]
    fn extract_truncates_oversized_body_with_explicit_note() {
        let long = "<w:document><w:body><w:p><w:t>".to_string()
            + &"数".repeat(MAX_PREVIEW_CHARS + 10)
            + "</w:t></w:p></w:body></w:document>";
        let zip = zip_build(&[("word/document.xml", long.as_bytes(), 8)]);
        let ex = extract(MIME_DOCX, &zip).unwrap();
        assert!(ex.content.chars().count() <= MAX_PREVIEW_CHARS);
        assert!(ex.note.clone().unwrap().contains("预览仅展示前"), "{:?}", ex.note);
    }

    #[test]
    fn xml_entity_decoding_covers_forms() {
        assert_eq!(xml_decode("A&amp;B"), "A&B");
        assert_eq!(xml_decode("&lt;x&gt;"), "<x>");
        assert_eq!(xml_decode("&#26159;&quot;"), "是\"");
        assert_eq!(xml_decode("&#x901A;"), "通");
        assert_eq!(xml_decode("a & b"), "a & b");
        assert_eq!(xml_decode("&notanentity;"), "&notanentity;");
        assert_eq!(xml_decode("&toolongentitynamehere;"), "&toolongentitynamehere;");
    }

    #[test]
    fn events_skip_comments_and_declarations() {
        let ev = events("<?xml version=\"1.0\"?><!-- c --><a x=\"1\"><b>t</b></a>");
        assert_eq!(
            ev,
            vec![
                Ev::Open("a", "x=\"1\"", false),
                Ev::Open("b", "", false),
                Ev::Text("t"),
                Ev::Close("b"),
                Ev::Close("a"),
            ]
        );
        assert_eq!(split_tag_body("w:p/"), ("w:p", ""));
        assert_eq!(split_tag_body("w:t xml:space=\"preserve\"/"), ("w:t", "xml:space=\"preserve\""));
    }
}
