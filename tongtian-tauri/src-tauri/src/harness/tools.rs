//! harness 的本地工具面：文件读写 + 技能正文。
//!
//! 与引擎注册的工具同名同参，界面上的执行步骤口径一致。边界只有一条：
//! 所有路径都必须落在本轮工作目录内；相对路径解析到工作目录，绝对路径
//! 越界一律拒绝（符号链接按真实路径判定）。

use std::path::{Component, Path, PathBuf};

use serde_json::{json, Value};

/// 单个文本文件的读取上限，避免误读大文件把进程内存打满
const MAX_READ_BYTES: u64 = 2 * 1024 * 1024;

#[derive(Debug, Clone)]
pub struct Skill {
    pub id: String,
    pub name: String,
    pub description: String,
    pub content: String,
}

#[derive(Debug, Clone)]
pub struct ToolBox {
    base: PathBuf,
    skills: Vec<Skill>,
}

#[derive(Debug, Clone)]
pub struct ToolRun {
    pub content: String,
    /// 是否改变了工作目录或外部状态（检查点去重只对副作用工具生效）
    pub side_effect: bool,
}

impl ToolBox {
    pub fn new(base: &Path, skills: Vec<Skill>) -> Result<Self, String> {
        let base = base
            .canonicalize()
            .map_err(|e| format!("HARNESS_WORKDIR_INVALID: 工作目录不可访问：{e}"))?;
        if !base.is_dir() {
            return Err("HARNESS_WORKDIR_INVALID: 工作目录不是一个目录".to_string());
        }
        Ok(Self { base, skills })
    }

    fn resolve(&self, raw: &str) -> Result<PathBuf, String> {
        let text = raw.trim();
        if text.is_empty() {
            return Err("错误：文件路径不能为空".to_string());
        }
        let given = Path::new(text);
        if given.components().any(|c| matches!(c, Component::ParentDir)) {
            return Err(format!("错误：路径不能包含上级目录（..）：{text}"));
        }
        let joined = if given.is_absolute() {
            given.to_path_buf()
        } else {
            self.base.join(given)
        };
        if joined.exists() {
            let canon = joined
                .canonicalize()
                .map_err(|e| format!("错误：路径不可访问 {text}：{e}"))?;
            if !canon.starts_with(&self.base) {
                return Err(format!("错误：路径超出工作目录范围: {}", canon.display()));
            }
            return Ok(canon);
        }
        // 新文件：从最深一层已存在的祖先判定真实落点是否仍在工作目录内，
        // 再保留原路径（含尚未创建的中间目录）供写入使用。
        let mut cur = joined.clone();
        while !cur.exists() {
            if !cur.pop() {
                break;
            }
        }
        let real = cur
            .canonicalize()
            .map_err(|e| format!("错误：路径不可访问：{e}"))?;
        if !real.starts_with(&self.base) {
            return Err(format!("错误：路径超出工作目录范围: {}", joined.display()));
        }
        Ok(joined)
    }

    /// OpenAI function-calling 的工具声明。技能清单为空时不注册 use_skill，
    /// 避免模型看到「有用法说明却取不到正文」的空工具。
    pub fn specs(&self) -> Vec<Value> {
        let mut out = vec![
            json!({
                "type": "function",
                "function": {
                    "name": "view_text_file",
                    "description": "查看工作目录内的文本文件内容。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "file_path": {"type": "string", "description": "文件路径（相对路径解析到工作目录）"},
                            "start_line": {"type": "integer", "description": "起始行号（从 1 开始），缺省为文件开头"},
                            "end_line": {"type": "integer", "description": "结束行号（含），缺省为文件末尾"}
                        },
                        "required": ["file_path"]
                    }
                }
            }),
            json!({
                "type": "function",
                "function": {
                    "name": "write_text_file",
                    "description": "创建或覆盖工作目录内的文本文件（写入前自动创建父目录）。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "file_path": {"type": "string", "description": "文件路径（相对路径解析到工作目录）"},
                            "content": {"type": "string", "description": "要写入的完整文件内容"}
                        },
                        "required": ["file_path", "content"]
                    }
                }
            }),
            json!({
                "type": "function",
                "function": {
                    "name": "insert_text_file",
                    "description": "向工作目录内的文本文件插入内容（不指定行号则追加到末尾）。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "file_path": {"type": "string", "description": "文件路径（相对路径解析到工作目录）"},
                            "content": {"type": "string", "description": "要插入的内容"},
                            "line": {"type": "integer", "description": "插入到的行号（从 1 开始），缺省追加到末尾"}
                        },
                        "required": ["file_path", "content"]
                    }
                }
            }),
        ];
        if !self.skills.is_empty() {
            out.push(json!({
                "type": "function",
                "function": {
                    "name": "use_skill",
                    "description": "获取某个已启用技能/套件指令的正文，按正文里的流程执行。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "skill_id": {"type": "string", "description": "技能 id"}
                        },
                        "required": ["skill_id"]
                    }
                }
            }));
        }
        out
    }

    pub fn run(&self, name: &str, args: &Value) -> ToolRun {
        match name {
            "view_text_file" => ToolRun {
                content: self.view(args),
                side_effect: false,
            },
            "write_text_file" => ToolRun {
                content: self.write(args),
                side_effect: true,
            },
            "insert_text_file" => ToolRun {
                content: self.insert(args),
                side_effect: true,
            },
            "use_skill" => ToolRun {
                content: self.use_skill(args),
                side_effect: false,
            },
            other => ToolRun {
                content: format!("错误：未注册的工具 {other}"),
                side_effect: false,
            },
        }
    }

    fn view(&self, args: &Value) -> String {
        let raw = args
            .get("file_path")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        let path = match self.resolve(raw) {
            Ok(p) => p,
            Err(e) => return e,
        };
        if !path.is_file() {
            return format!("错误：文件不存在: {raw}");
        }
        if let Ok(meta) = std::fs::metadata(&path) {
            if meta.len() > MAX_READ_BYTES {
                return format!(
                    "错误：文件超过 {} MB 读取上限: {raw}",
                    MAX_READ_BYTES / (1024 * 1024)
                );
            }
        }
        let text = match std::fs::read_to_string(&path) {
            Ok(t) => t,
            Err(e) => return format!("错误：文件读取失败：{e}"),
        };
        let lines: Vec<&str> = text.lines().collect();
        let start = args
            .get("start_line")
            .and_then(|v| v.as_i64())
            .unwrap_or(1)
            .max(1) as usize;
        let end = args
            .get("end_line")
            .and_then(|v| v.as_i64())
            .map(|n| n.max(0) as usize)
            .unwrap_or(lines.len());
        lines[start.saturating_sub(1).min(lines.len())..end.min(lines.len())].join("\n")
    }

    fn write(&self, args: &Value) -> String {
        let raw = args
            .get("file_path")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        let content = args.get("content").and_then(|v| v.as_str()).unwrap_or("");
        let path = match self.resolve(raw) {
            Ok(p) => p,
            Err(e) => return e,
        };
        if let Some(parent) = path.parent() {
            if let Err(e) = std::fs::create_dir_all(parent) {
                return format!("错误：父目录创建失败：{e}");
            }
        }
        match std::fs::write(&path, content) {
            Ok(()) => format!("已成功写入文件: {}", path.display()),
            Err(e) => format!("错误：文件写入失败：{e}"),
        }
    }

    fn insert(&self, args: &Value) -> String {
        let raw = args
            .get("file_path")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        let content = args.get("content").and_then(|v| v.as_str()).unwrap_or("");
        let path = match self.resolve(raw) {
            Ok(p) => p,
            Err(e) => return e,
        };
        if !path.is_file() {
            return format!("错误：文件不存在: {raw}");
        }
        let text = match std::fs::read_to_string(&path) {
            Ok(t) => t,
            Err(e) => return format!("错误：文件读取失败：{e}"),
        };
        let mut lines: Vec<String> = text.lines().map(str::to_string).collect();
        let new_lines: Vec<String> = content.lines().map(str::to_string).collect();
        match args.get("line").and_then(|v| v.as_i64()) {
            Some(n) if n >= 1 => {
                let idx = (n as usize - 1).min(lines.len());
                for (offset, line) in new_lines.into_iter().enumerate() {
                    lines.insert(idx + offset, line);
                }
            }
            _ => lines.extend(new_lines),
        }
        match std::fs::write(&path, lines.join("\n")) {
            Ok(()) => format!("已成功更新文件: {}", path.display()),
            Err(e) => format!("错误：文件写入失败：{e}"),
        }
    }

    fn use_skill(&self, args: &Value) -> String {
        let id = args
            .get("skill_id")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .trim();
        if id.is_empty() {
            return "错误：skill_id 不能为空".to_string();
        }
        match self.skills.iter().find(|s| s.id.eq_ignore_ascii_case(id)) {
            Some(skill) if !skill.content.trim().is_empty() => skill.content.clone(),
            Some(skill) => format!(
                "错误：技能「{}」没有可用的正文（{}）",
                skill.name,
                if skill.content.trim().is_empty() {
                    "SKILL.md 内容为空"
                } else {
                    "正文缺失"
                }
            ),
            None => {
                let ids: Vec<&str> = self.skills.iter().map(|s| s.id.as_str()).collect();
                format!(
                    "错误：没有技能 {id}（当前启用：{}）",
                    if ids.is_empty() {
                        "无".to_string()
                    } else {
                        ids.join("、")
                    }
                )
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tmp_box() -> (PathBuf, ToolBox) {
        let base = std::env::temp_dir().join(format!(
            "tt-harness-tools-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_nanos())
                .unwrap_or(0)
        ));
        std::fs::create_dir_all(&base).unwrap();
        let tb = ToolBox::new(&base, vec![Skill {
            id: "weekly".into(),
            name: "周报".into(),
            description: "写周报".into(),
            content: "先列三项进展".into(),
        }])
        .unwrap();
        (base, tb)
    }

    #[test]
    fn test_write_read_roundtrip() {
        let (base, tb) = tmp_box();
        let out = tb.run("write_text_file", &json!({"file_path": "notes/a.md", "content": "第一行\n第二行"}));
        assert!(out.side_effect);
        assert!(out.content.contains("已成功写入"), "{}", out.content);
        assert!(base.join("notes/a.md").exists());
        let read = tb.run("view_text_file", &json!({"file_path": "notes/a.md"}));
        assert_eq!(read.content, "第一行\n第二行");
        let part = tb.run(
            "view_text_file",
            &json!({"file_path": "notes/a.md", "start_line": 2, "end_line": 2}),
        );
        assert_eq!(part.content, "第二行");
        let _ = std::fs::remove_dir_all(base);
    }

    #[test]
    fn test_insert_appends_and_inserts_at_line() {
        let (base, tb) = tmp_box();
        tb.run("write_text_file", &json!({"file_path": "a.md", "content": "a\nc"}));
        tb.run("insert_text_file", &json!({"file_path": "a.md", "content": "b", "line": 2}));
        assert_eq!(std::fs::read_to_string(base.join("a.md")).unwrap(), "a\nb\nc");
        tb.run("insert_text_file", &json!({"file_path": "a.md", "content": "d"}));
        assert_eq!(std::fs::read_to_string(base.join("a.md")).unwrap(), "a\nb\nc\nd");
        let _ = std::fs::remove_dir_all(base);
    }

    #[test]
    fn test_rejects_escape_and_missing_file() {
        let (base, tb) = tmp_box();
        let out = tb.run("write_text_file", &json!({"file_path": "../escape.md", "content": "x"}));
        assert!(out.content.contains("上级目录"), "{}", out.content);
        let abs = if cfg!(windows) { "C:\\Windows\\win.ini" } else { "/etc/hosts" };
        let out = tb.run("view_text_file", &json!({"file_path": abs}));
        assert!(out.content.contains("超出工作目录范围"), "{}", out.content);
        let out = tb.run("view_text_file", &json!({"file_path": "missing.md"}));
        assert!(out.content.contains("不存在"), "{}", out.content);
        let _ = std::fs::remove_dir_all(base);
    }

    #[test]
    fn test_use_skill_returns_body_and_explains_unknown() {
        let (base, tb) = tmp_box();
        let ok = tb.run("use_skill", &json!({"skill_id": "WEEKLY"}));
        assert_eq!(ok.content, "先列三项进展");
        assert!(!ok.side_effect);
        let miss = tb.run("use_skill", &json!({"skill_id": "nope"}));
        assert!(miss.content.contains("没有技能 nope"), "{}", miss.content);
        let _ = std::fs::remove_dir_all(base);
    }

    #[test]
    fn test_specs_include_use_skill_only_with_skills() {
        let (base, tb) = tmp_box();
        let names: Vec<String> = tb
            .specs()
            .iter()
            .map(|v| v["function"]["name"].as_str().unwrap_or("").to_string())
            .collect();
        assert!(names.contains(&"use_skill".to_string()));
        let other = ToolBox::new(&base, vec![]).unwrap();
        let names: Vec<String> = other
            .specs()
            .iter()
            .map(|v| v["function"]["name"].as_str().unwrap_or("").to_string())
            .collect();
        assert!(!names.contains(&"use_skill".to_string()));
        let _ = std::fs::remove_dir_all(base);
    }

    #[test]
    fn test_unknown_tool_is_reported_not_panicked() {
        let (base, tb) = tmp_box();
        let out = tb.run("run_command", &json!({}));
        assert!(out.content.contains("未注册的工具"));
        let _ = std::fs::remove_dir_all(base);
    }
}
