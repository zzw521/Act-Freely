//! harness 的本地工具面：目录浏览、文件读写、文档正文抽取、删除与技能正文。
//!
//! 与引擎注册的工具**同名**，参数与分页预算**同源**，不再各写各的（见下面的分页预算段）：
//! worker 执行体在 `engine/ticket_worker_adapter.py`，引擎主会话在 `engine/server.py` 的
//! `view_text_file` / `read_document` / `list_directory`（D-48.4b 补齐；本文件此处曾写
//! 「引擎主会话仍是无预算那一套、差异是刻意的」，那句话随 D-48.4b 作废）。
//! 三面同源的数字由 `tests/integration/engine_document_tools_selftest.py` 机器核对：
//! 它正则抓本文件的 `const …: usize = …` 与 Python 侧常量比对，改一边不改另一边就红。
//! 落盘前过两道判定：
//!
//! 1. 文件访问档位（`security::file_perm`，与会话里选的那一档同源）：
//!    - 每次询问：目录内的创建/更新/删除要调用方先拿到用户放行（见 `ToolBox::gate`），
//!      未经确认的调用一律不执行；
//!    - 自动审批：只在绑定工作目录内放行，越界拒绝，删除仍需完全访问；
//!    - 完全访问：可以越出工作目录，家目录/系统关键目录/应用数据目录仍硬拒，且只有它允许删除。
//! 2. 路径本身的合法性：相对路径解析到工作目录，含 `..` 一律拒绝（要出目录请显式给绝对路径），
//!    符号链接按真实路径判定。
//!
//! 删除不是真删：先复制进 `{数据根}/file-trash` 并留下来源与原因的旁证文件，
//! 复制失败就不删——这与引擎侧 `delete_text_file` 同一套行为，换执行面不换语义。
//!
//! 读取的三个工具（`view_text_file` / `read_document` / `list_directory`）都带**分页预算**：
//! 正文默认 4000、上限 8000 单元，列表默认 20、上限 50 项（单元=字符，不是 token），
//! 超量时如实报「还剩多少 + 带哪个参数接着读」。数字与引擎
//! `ticket_worker_adapter.py` 同源；加这一层的理由见下面 `TEXT_UNITS_DEFAULT` 处的注释
//! ——一条超限的工具结果落在压缩保不住的「最近轮次」里，长任务必撞窗口。

use std::path::{Component, Path, PathBuf};

use serde_json::{json, Value};

use crate::security::file_perm;

/// 单个文本文件的读取上限，避免误读大文件把进程内存打满
const MAX_READ_BYTES: u64 = 2 * 1024 * 1024;

/// 文档（docx/xlsx/pptx/pdf）读取上限，与附件预览同一口径
const MAX_DOC_BYTES: u64 = 50 * 1024 * 1024;

/// 读取分页预算（D-48.4）：与引擎 `ticket_worker_adapter.py` 的
/// `TEXT_READ_DEFAULT_UNITS` / `TEXT_READ_MAX_UNITS` / `LIST_DEFAULT_ITEMS` /
/// `LIST_MAX_ITEMS` **同一组数字**，对齐的理由是窗口而不是习惯——
/// 一条工具结果如果自身就超过压缩预留（`compress.rs` 的 `RESERVE_MIN_TOKENS` = 4096），
/// 它落在「最近保留的三组轮次」里，压缩永远拿它没办法，100 轮长任务必撞窗口。
/// 60000 字符的文档正文按中文口径就是 6 万 tokens，比大多数窗口还大。
///
/// **单元=字符，不是 token**：harness 没有分词器，这里不假装算得准，
/// 提示词与返回值都按字符说话（与引擎同一句口径）。
const TEXT_UNITS_DEFAULT: usize = 4_000;
const TEXT_UNITS_MAX: usize = 8_000;
const LIST_ITEMS_DEFAULT: usize = 20;
const LIST_ITEMS_MAX: usize = 50;

/// 可直接按文本读取的扩展名（Office/PDF 之外的纯文本类）
const TEXT_EXTS: [&str; 8] = ["txt", "md", "markdown", "csv", "log", "json", "yaml", "yml"];

/// 走 office::extract 的扩展名；doc/xls/ppt 会如实报「旧版格式暂不支持」
const DOC_EXTS: [&str; 7] = ["docx", "xlsx", "pptx", "pdf", "doc", "xls", "ppt"];

/// 把「我要多少」夹进 `1..=max`，缺省取 `default`，与引擎 `clamp_units` 同一套说法。
///
/// 越界**如实带话**而不是默默按默认：模型要看得见自己的申请被改成了多少，
/// 下一轮才可能改成「翻页读」，而不是照着原额再申请一次超量。
/// 认不出来（非数字 / 非整数 / 负数）也带话，不猜模型的意图。
fn clamp_units(raw: Option<&Value>, default: usize, max: usize, what: &str) -> (usize, String) {
    let Some(v) = raw.filter(|v| !v.is_null()) else {
        return (default, String::new());
    };
    let parsed = v
        .as_i64()
        .or_else(|| v.as_str().and_then(|s| s.trim().parse::<i64>().ok()));
    let Some(value) = parsed else {
        return (
            default,
            format!("{what}「{}」不是整数，已按默认 {default} 执行。", v),
        );
    };
    if value <= 0 {
        return (
            default,
            format!("{what}「{value}」不是正数，已按默认 {default} 执行。"),
        );
    }
    let value = value as usize;
    if value > max {
        return (
            max,
            format!("{what}「{value}」超出上限 {max}，已按上限截断——要读更多请分页。"),
        );
    }
    if value < default {
        return (
            value,
            format!("{what}按你的请求取 {value}（低于默认 {default}，本次只给这些）。"),
        );
    }
    (value, String::new())
}

/// 分页起点：非负整数，认不出来就按 0 并**如实带话**（引擎 `clamp_start` 同口径）。
/// `what` 是给模型看的参数名（`view_text_file` 叫 `start`，`read_document` 叫 `offset`）。
fn clamp_start(raw: Option<&Value>, what: &str) -> (usize, String) {
    let Some(v) = raw.filter(|v| !v.is_null()) else {
        return (0, String::new());
    };
    let parsed = v
        .as_i64()
        .or_else(|| v.as_str().and_then(|s| s.trim().parse::<i64>().ok()));
    match parsed {
        Some(value) if value >= 0 => (value as usize, String::new()),
        Some(value) => (0, format!("{what}「{value}」是负数，本次从头开始读。")),
        None => (0, format!("{what}「{v}」不是整数，本次从头开始读。")),
    }
}

/// 把两条中文说明并成一条（都空就返回空串，不留下多余空格）。
fn join_note(a: &str, b: &str) -> String {
    match (a.is_empty(), b.is_empty()) {
        (true, true) => String::new(),
        (true, false) => b.to_string(),
        (false, true) => a.to_string(),
        (false, false) => format!("{a} {b}"),
    }
}

/// 人类可读的文件大小（与引擎 list_directory 同一口径）
fn size_label(size: u64) -> String {
    let mut value = size as f64;
    for unit in ["B", "KB", "MB", "GB"] {
        if value < 1024.0 || unit == "GB" {
            return if unit == "B" {
                format!("{size} B")
            } else {
                format!("{value:.1} {unit}")
            };
        }
        value /= 1024.0;
    }
    format!("{size} B")
}

/// 目录优先、各自按名称排序，逐层收集；达到 limit 即停止并标记截断。
/// 不跟随符号链接（`DirEntry::file_type` 本身就按链接本身判定）。
fn walk_dir(
    root: &Path,
    cur: &Path,
    depth: usize,
    limit: usize,
    rows: &mut Vec<String>,
    truncated: &mut bool,
) {
    if *truncated {
        return;
    }
    let entries = match std::fs::read_dir(cur) {
        Ok(iter) => iter,
        Err(_) => return,
    };
    let mut dirs = Vec::new();
    let mut files = Vec::new();
    for entry in entries.flatten() {
        if entry.file_type().map(|t| t.is_dir()).unwrap_or(false) {
            dirs.push(entry);
        } else {
            files.push(entry);
        }
    }
    dirs.sort_by_key(|e| e.file_name());
    files.sort_by_key(|e| e.file_name());
    for entry in dirs.iter().chain(files.iter()) {
        if rows.len() >= limit {
            *truncated = true;
            return;
        }
        let path = entry.path();
        let rel = path
            .strip_prefix(root)
            .unwrap_or(&path)
            .to_string_lossy()
            .replace('\\', "/");
        if entry.file_type().map(|t| t.is_dir()).unwrap_or(false) {
            rows.push(format!("[目录] {rel}/"));
        } else {
            let size = entry
                .metadata()
                .map(|m| size_label(m.len()))
                .unwrap_or_else(|_| "?".to_string());
            rows.push(format!("[文件] {rel}  {size}"));
        }
    }
    if depth <= 1 {
        return;
    }
    for entry in dirs {
        walk_dir(root, &entry.path(), depth - 1, limit, rows, truncated);
        if *truncated {
            return;
        }
    }
}

/// 会改动磁盘的工具：档位只管这些，读取与技能正文不受限
const MUTATING_TOOLS: [&str; 3] = ["write_text_file", "insert_text_file", "delete_text_file"];

/// 一次工具调用在执行前的判定结果
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Gate {
    /// 档位允许直接执行
    Pass,
    /// 档位要求先问用户（附带确认框里显示的描述）
    Ask(String),
    /// 档位或硬拒清单不允许：本次调用不得执行
    Refused(String),
}

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
    /// 本轮会话的文件访问档位（已归一）
    permission: String,
    /// 删除前的落备份处（缺省 `{数据根}/file-trash`；单测显式传临时目录）
    trash: PathBuf,
}

#[derive(Debug, Clone)]
pub struct ToolRun {
    pub content: String,
    /// 是否改变了工作目录或外部状态（检查点去重只对副作用工具生效）
    pub side_effect: bool,
}

impl ToolBox {
    /// 缺省构造：最保守档「每次询问」。调用方若已有档位，请用 `with_permission`，
    /// 别让默认值替用户表态。
    pub fn new(base: &Path, skills: Vec<Skill>) -> Result<Self, String> {
        Self::with_permission(base, skills, file_perm::DEFAULT_MODE)
    }

    /// 带档位构造：工作目录校验一次，档位非法时归一到最保守档（不是拒绝构造）
    pub fn with_permission(base: &Path, skills: Vec<Skill>, permission: &str) -> Result<Self, String> {
        let base = base
            .canonicalize()
            .map_err(|e| format!("HARNESS_WORKDIR_INVALID: 工作目录不可访问：{e}"))?;
        if !base.is_dir() {
            return Err("HARNESS_WORKDIR_INVALID: 工作目录不是一个目录".to_string());
        }
        Ok(Self {
            base,
            skills,
            permission: file_perm::normalize_or_default(Some(permission)),
            trash: file_perm::trash_dir(),
        })
    }

    /// 覆盖回收目录落点。生产路径用缺省的 `{数据根}/file-trash`；
    /// 单测必须显式指到临时目录，否则「删除先备份」这条会写进真实数据根。
    #[allow(dead_code)]
    pub fn with_trash(mut self, trash: &Path) -> Self {
        self.trash = trash.to_path_buf();
        self
    }

    pub fn permission(&self) -> &str {
        &self.permission
    }

    /// 执行前判定：路径合法 + 档位边界 + 是否需要问用户。
    /// `run` 会在未确认的情况下拒绝执行 Ask 档调用，所以漏判不会变成静默写盘。
    pub fn gate(&self, name: &str, args: &Value) -> Gate {
        if !MUTATING_TOOLS.contains(&name) {
            return Gate::Pass;
        }
        let raw = args.get("file_path").and_then(|v| v.as_str()).unwrap_or("");
        let deleting = name == "delete_text_file";
        // 边界与硬拒由 check 判定；能走到这里说明这一笔操作档位允许
        let target = match self.check(raw, true, deleting) {
            Ok(p) => p,
            Err(e) => return Gate::Refused(e),
        };
        if file_perm::asks_in_area(&self.permission) {
            let action = if deleting { "删除" } else { "写入" };
            return Gate::Ask(format!(
                "工具：{name}\n{}：{}（档位「{}」）",
                action,
                target.display(),
                file_perm::label(&self.permission)
            ));
        }
        Gate::Pass
    }

    /// 只做路径合法性：解析到工作目录、拒绝 `..`、按真实路径判定符号链接。
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
            return joined
                .canonicalize()
                .map_err(|e| format!("错误：路径不可访问 {text}：{e}"));
        }
        // 新文件：从最深一层已存在的祖先取真实落点做判定，再保留原路径
        // （含尚未创建的中间目录）供写入使用。
        let mut cur = joined.clone();
        while !cur.exists() {
            if !cur.pop() {
                break;
            }
        }
        let real_parent = cur
            .canonicalize()
            .map_err(|e| format!("错误：路径不可访问：{e}"))?;
        if !real_parent.starts_with(&self.base) {
            // 名义上在目录内、真实祖先却指向外面（符号链接）：按真实落点判定，
            // 免得「目录里建个链接再写外面」绕开档位边界
            let tail = joined
                .strip_prefix(&cur)
                .map(|p| p.to_path_buf())
                .unwrap_or_default();
            return Ok(real_parent.join(tail));
        }
        Ok(joined)
    }

    /// 合法性 + 档位：所有工具（含只读的 view）都先过这一关。
    fn check(&self, raw: &str, write: bool, delete: bool) -> Result<PathBuf, String> {
        let target = self.resolve(raw)?;
        match file_perm::access_denial(&self.permission, &target, Some(&self.base), write, delete) {
            Some(msg) => Err(format!("错误：文件访问权限拒绝：{msg}")),
            None => Ok(target),
        }
    }

    /// OpenAI function-calling 的工具声明。技能清单为空时不注册 use_skill，
    /// 避免模型看到「有用法说明却取不到正文」的空工具。
    ///
    /// 描述里的可写范围跟着档位走：只有本轮确实越得出目录时才告诉模型「目录外也可以」，
    /// 免得模型按一个档位允许的说法去写、结果被另一个档位挡下来。
    pub fn specs(&self) -> Vec<Value> {
        let scope = if file_perm::allows_outside(&self.permission) {
            "工作目录内，或目录外的普通本机路径（家目录、系统关键目录与应用数据目录除外）"
        } else {
            "本轮绑定的工作目录内"
        };
        let mut out = vec![
            json!({
                "type": "function",
                "function": {
                    "name": "list_directory",
                    "description": format!("列出目录里的文件与子目录（名称、类型、大小），用于先看清目录再决定读哪些文件（可读范围：{scope}）。单页有预算：默认 {LIST_ITEMS_DEFAULT} 条、上限 {LIST_ITEMS_MAX} 条，没列完会在末尾点名，用 offset 接着列。"),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "目录路径（相对路径解析到工作目录），缺省为工作目录本身"},
                            "depth": {"type": "integer", "description": "递归深度：1 只看当前目录，最大 3"},
                            "limit": {"type": "integer", "description": format!("这一页最多返回多少条，默认 {LIST_ITEMS_DEFAULT}、上限 {LIST_ITEMS_MAX}；超出预算会如实带话")},
                            "offset": {"type": "integer", "description": "从第几条开始列（从 0 开始），用于翻下一页"}
                        },
                        "required": []
                    }
                }
            }),
            json!({
                "type": "function",
                "function": {
                    "name": "view_text_file",
                    "description": format!("查看文本文件内容（可读范围：{scope}）。单次有预算：默认 {TEXT_UNITS_DEFAULT}、上限 {TEXT_UNITS_MAX} 单元（单元=字符，不是 token），超出会按预算给一页并点名还剩多少。"),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "file_path": {"type": "string", "description": "文件路径（相对路径解析到工作目录）"},
                            "start_line": {"type": "integer", "description": "起始行号（从 1 开始），缺省为文件开头"},
                            "end_line": {"type": "integer", "description": "结束行号（含），缺省为文件末尾"},
                            "units": {"type": "integer", "description": format!("这一页最多返回多少字符，默认 {TEXT_UNITS_DEFAULT}、上限 {TEXT_UNITS_MAX}")},
                            "start": {"type": "integer", "description": "从选定范围的第几个字符开始读（从 0 开始），配合返回值里的下一页参数使用"}
                        },
                        "required": ["file_path"]
                    }
                }
            }),
            json!({
                "type": "function",
                "function": {
                    "name": "read_document",
                    "description": format!("按路径读取 Word（.docx）、Excel（.xlsx）、PowerPoint（.pptx）或 PDF 的正文并返回文本（可读范围：{scope}）。文本类文件请用 view_text_file。单次有预算：默认 {TEXT_UNITS_DEFAULT}、上限 {TEXT_UNITS_MAX} 字符，没读完的带 offset 接着读。"),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "file_path": {"type": "string", "description": "文件路径（相对路径解析到工作目录）"},
                            "max_chars": {"type": "integer", "description": format!("这一页返回的最大字符数，默认 {TEXT_UNITS_DEFAULT}、上限 {TEXT_UNITS_MAX}；超出预算会如实带话")},
                            "offset": {"type": "integer", "description": "从正文第几个字符开始返回（从 0 开始），用于翻下一页"}
                        },
                        "required": ["file_path"]
                    }
                }
            }),
            json!({
                "type": "function",
                "function": {
                    "name": "write_text_file",
                    "description": format!("创建或覆盖文本文件（写入前自动创建父目录；可写范围：{scope}）。当前档位「{}」。", file_perm::label(&self.permission)),
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
                    "description": format!("向文本文件插入内容（不指定行号则追加到末尾；可写范围：{scope}）。"),
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
            json!({
                "type": "function",
                "function": {
                    "name": "delete_text_file",
                    "description": "删除单个文本文件。只有档位「完全访问」会放行，其余档位一律不删；\
                                  放行时先把文件复制进应用回收目录（含原路径与原因），再删原件。"
                        .to_string(),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "file_path": {"type": "string", "description": "要删除的文件路径（相对路径解析到工作目录）"},
                            "reason": {"type": "string", "description": "为什么要删（写进回收目录的旁证，便于找回）"}
                        },
                        "required": ["file_path"]
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

    /// 直接执行：档位要求逐次确认时不会动手（见 `gate`），
    /// 调用方必须先取得放行，或用 `run_after_approval`。
    pub fn run(&self, name: &str, args: &Value) -> ToolRun {
        match self.gate(name, args) {
            Gate::Refused(msg) => ToolRun {
                content: msg,
                side_effect: false,
            },
            Gate::Ask(desc) => ToolRun {
                content: format!(
                    "错误：文件访问档位为「每次询问」，本次操作未经用户确认，未执行。待确认操作：{desc}"
                ),
                side_effect: false,
            },
            Gate::Pass => self.execute(name, args),
        }
    }

    /// 用户已经放行（或档位本就允许直接执行）之后执行这一次调用。
    /// 拒绝类判定仍然生效：确认框只解决「问不问」，越界与硬拒不会因为放行而放开。
    pub fn run_after_approval(&self, name: &str, args: &Value) -> ToolRun {
        match self.gate(name, args) {
            Gate::Refused(msg) => ToolRun {
                content: msg,
                side_effect: false,
            },
            _ => self.execute(name, args),
        }
    }

    fn execute(&self, name: &str, args: &Value) -> ToolRun {
        match name {
            "list_directory" => ToolRun {
                content: self.list_directory(args),
                side_effect: false,
            },
            "read_document" => ToolRun {
                content: self.read_document(args),
                side_effect: false,
            },
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
            "delete_text_file" => ToolRun {
                content: self.delete(args),
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

    /// 读取文本文件的选定行，并按分页预算给正文（默认 4000、上限 8000 单元，单元=字符）。
    ///
    /// 行范围（`start_line` / `end_line`）决定「看哪一段」，`start` / `units` 决定
    /// 「这一段这次给多少字符」——两轴都留着，是因为模型既会说「给我第 200 行起」，
    /// 也会说「这文件太大，先头 4000 字」。截断时把**下一页的参数**原样写在返回值里。
    fn view(&self, args: &Value) -> String {
        let raw = args
            .get("file_path")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        let path = match self.check(raw, false, false) {
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
        let from = args
            .get("start_line")
            .and_then(|v| v.as_i64())
            .unwrap_or(1)
            .max(1) as usize;
        let to = args
            .get("end_line")
            .and_then(|v| v.as_i64())
            .map(|n| n.max(0) as usize)
            .unwrap_or(lines.len());
        let first_line = from.saturating_sub(1).min(lines.len());
        let body = lines[first_line..to.min(lines.len())].join("\n");

        let (units, note) = clamp_units(
            args.get("units"),
            TEXT_UNITS_DEFAULT,
            TEXT_UNITS_MAX,
            "读取单元",
        );
        let (offset, offset_note) = clamp_start(args.get("start"), "start");
        let mut note = join_note(&note, &offset_note);
        let total = body.chars().count();
        if offset > total {
            note = join_note(
                &note,
                &format!("start={offset} 已超过选定范围的长度 {total}，本次没有内容可给。"),
            );
        }
        let chunk: String = body.chars().skip(offset).take(units).collect();
        let given = chunk.chars().count();
        let out_of_range = offset >= total;
        let truncated = offset + given < total;
        if truncated || out_of_range {
            let line_at = first_line
                + 1
                + body
                    .chars()
                    .take(offset.min(total))
                    .filter(|c| *c == '\n')
                    .count();
            let remaining = total.saturating_sub(offset + given);
            note = join_note(
                &note,
                &format!(
                    "选定范围共 {total} 个单元（单元=字符，非 token），本次给第 {offset} 起的 {given} 个（从第 {line_at} 行开始）；\
                     后面还有 {remaining} 个单元没给——接着读请带 start={}，别整篇塞进上下文。",
                    offset + given
                ),
            );
        }
        if note.is_empty() {
            return chunk;
        }
        format!("{note}\n{chunk}")
    }

    /// 列出目录（深度 1–3、分页预算默认 20 条、上限 50 条，`offset` 往后翻）。
    /// 只读，不写盘；越界仍按档位判定。
    fn list_directory(&self, args: &Value) -> String {
        let raw = args
            .get("path")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .trim();
        let raw = if raw.is_empty() { "." } else { raw };
        let dir = match self.check(raw, false, false) {
            Ok(p) => p,
            Err(e) => return e,
        };
        if !dir.exists() {
            return format!("错误：目录不存在: {raw}");
        }
        if !dir.is_dir() {
            return format!("错误：不是目录: {raw}");
        }
        let depth = args
            .get("depth")
            .and_then(|v| v.as_i64())
            .unwrap_or(1)
            .clamp(1, 3) as usize;
        let (cap, note) = clamp_units(
            args.get("limit"),
            LIST_ITEMS_DEFAULT,
            LIST_ITEMS_MAX,
            "列出条数",
        );
        let (offset, offset_note) = clamp_start(args.get("offset"), "offset");
        let note = join_note(&note, &offset_note);

        // 多收一条：只有真的越过 offset+cap 才敢说「后面还有」，不然就是「已经列完」
        let want = offset + cap + 1;
        let mut collected = Vec::new();
        let mut hit_cap = false;
        walk_dir(&dir, &dir, depth, want, &mut collected, &mut hit_cap);
        let got = collected.len();
        let page: Vec<String> = collected
            .into_iter()
            .skip(offset)
            .take(cap)
            .collect();
        // 两种「后面还有」：收集时撞到上限，或这一页之后本地已经收到的条目还没给完
        let has_more = hit_cap || got > offset + page.len();

        let mut out = String::new();
        if !note.is_empty() {
            out.push_str(&note);
            out.push('\n');
        }
        if page.is_empty() {
            out.push_str(&format!(
                "目录：{}（深度 {depth}，单页上限 {cap} 条）",
                dir.display()
            ));
            out.push_str(if offset > 0 {
                "\n（这个偏移之后没有条目了；要回头看请带 offset=0）"
            } else {
                "\n（目录为空）"
            });
            return out;
        }
        out.push_str(&format!(
            "目录：{}（深度 {depth}，本次第 {}–{} 条，单页上限 {cap} 条）",
            dir.display(),
            offset + 1,
            offset + page.len(),
        ));
        out.push('\n');
        out.push_str(&page.join("\n"));
        if has_more {
            out.push_str(&format!(
                "\n…（单页 {cap} 条已到，后面还有更多没列——接着列请带 offset={}，别指望一次看全）",
                offset + cap
            ));
        }
        out
    }

    /// 从 Word/Excel/PowerPoint/PDF 抽取正文（复用附件预览的解析器，不另写一套）。
    ///
    /// 正文按分页预算返回：`max_chars` 默认 4000、上限 8000 字符，`offset` 往后翻；
    /// 一次给不完的字符数会如实报在末尾（旧版是默认 60000 且只能截前缀、读不到后面）。
    fn read_document(&self, args: &Value) -> String {
        let raw = args
            .get("file_path")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        let path = match self.check(raw, false, false) {
            Ok(p) => p,
            Err(e) => return e,
        };
        if !path.is_file() {
            return format!("错误：文件不存在: {raw}");
        }
        let (cap, cap_note) = clamp_units(
            args.get("max_chars"),
            TEXT_UNITS_DEFAULT,
            TEXT_UNITS_MAX,
            "读取字符数",
        );
        let (offset, offset_note) = clamp_start(args.get("offset"), "offset");
        let budget_note = join_note(&cap_note, &offset_note);
        if let Ok(meta) = std::fs::metadata(&path) {
            if meta.len() > MAX_DOC_BYTES {
                return format!(
                    "错误：文件超过 {} MB 读取上限: {raw}",
                    MAX_DOC_BYTES / (1024 * 1024)
                );
            }
        }
        let name = path
            .file_name()
            .and_then(|n| n.to_str())
            .unwrap_or(raw)
            .to_string();
        let ext = path
            .extension()
            .and_then(|e| e.to_str())
            .map(|e| e.to_ascii_lowercase())
            .unwrap_or_default();
        let bytes = match std::fs::read(&path) {
            Ok(b) => b,
            Err(e) => return format!("错误：文件读取失败：{e}"),
        };

        let text;
        let note;
        if TEXT_EXTS.contains(&ext.as_str()) {
            text = String::from_utf8_lossy(&bytes).to_string();
            note = None;
        } else if DOC_EXTS.contains(&ext.as_str()) {
            // 加密 PDF 的内容流解不开，先明确报「已加密」，别让用户以为文件是空的
            if ext == "pdf" && bytes.windows(8).any(|w| w == b"/Encrypt") {
                return "错误：PDF 已加密，请先解除密码或提供未加密副本".to_string();
            }
            let mime = crate::file::service::mime_of(&path);
            match crate::file::office::extract(mime, &bytes) {
                Ok(ex) => {
                    if !ex.supported || ex.content.trim().is_empty() {
                        let why = ex.note.unwrap_or_else(|| {
                            "若为扫描件/图片型 PDF，请先用 OCR 转成可复制文本".to_string()
                        });
                        return format!("错误：未能从 {name} 抽取到正文（{why}）");
                    }
                    text = ex.content;
                    note = ex.note;
                }
                Err(e) => {
                    let msg = e.strip_prefix("FILE_CONVERSION_FAILED: ").unwrap_or(&e);
                    return format!("错误：读取文档失败：{msg}");
                }
            }
        } else {
            let label = if ext.is_empty() {
                "（无扩展名）".to_string()
            } else {
                format!(".{ext}")
            };
            return format!(
                "错误：不支持的文档格式 {label}；支持 .docx/.xlsx/.pptx/.pdf 与文本类文件"
            );
        }

        if text.trim().is_empty() {
            return format!(
                "错误：未能从 {name} 抽取到正文。若为扫描件/图片型 PDF，请先用 OCR 转成可复制文本，或在输入框以附件方式发送"
            );
        }
        let total = text.chars().count();
        let body: String = text.chars().skip(offset).take(cap).collect();
        let given = body.chars().count();
        let remaining = total.saturating_sub(offset + given);
        let mut clipped = String::new();
        if remaining > 0 || offset >= total {
            clipped = format!(
                "\n…（正文共 {total} 字符，本次按预算给第 {offset} 起的 {given} 个（单元=字符，非 token）；\
                 后面还有 {remaining} 字符没给——接着读请带 offset={}，别整篇塞进上下文。）",
                offset + given
            );
        }
        let mut header = format!("已读取：{}（{ext}）", path.display());
        if let Some(n) = note {
            header.push_str(&format!("（{n}）"));
        }
        if !budget_note.is_empty() {
            header.push('\n');
            header.push_str(&budget_note);
        }
        format!("{header}\n\n{body}{clipped}")
    }

    fn write(&self, args: &Value) -> String {
        let raw = args
            .get("file_path")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        let content = args.get("content").and_then(|v| v.as_str()).unwrap_or("");
        let path = match self.check(raw, true, false) {
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
        let path = match self.check(raw, true, false) {
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

    /// 删除：先复制进回收目录并留下来源旁证，备份失败就一个都不动。
    /// 目录不删（只删单个文件），与引擎侧 `delete_text_file` 同口径。
    fn delete(&self, args: &Value) -> String {
        let raw = args
            .get("file_path")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        let reason = args.get("reason").and_then(|v| v.as_str()).unwrap_or("");
        let path = match self.check(raw, false, true) {
            Ok(p) => p,
            Err(e) => return e,
        };
        if path.is_dir() {
            return format!("错误：只允许删除单个文件，不删目录：{}", path.display());
        }
        if !path.is_file() {
            return format!("错误：文件不存在: {raw}");
        }
        let trashed = match file_perm::copy_into_trash_dir(&self.trash, &path, reason) {
            Ok(name) => name,
            Err(e) => return format!("错误：{e}"),
        };
        match std::fs::remove_file(&path) {
            Ok(()) => format!(
                "已删除文件: {}（原件已复制进回收目录：{trashed}）",
                path.display()
            ),
            Err(e) => format!(
                "错误：文件删除失败：{e}；原件仍在 {}，回收目录里的副本为 {trashed}",
                path.display()
            ),
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

    /// 默认构造的用例箱：档位取「自动审批」，回收目录指到临时区内。
    /// 为什么不是最保守档：那会让每个写入用例都变成「等用户点确认」，
    /// 确认框在本机是系统弹窗，单测里既不能弹也不该弹；「每次询问」另有专门用例覆盖。
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
        let tb = ToolBox::with_permission(
            &base,
            vec![Skill {
                id: "weekly".into(),
                name: "周报".into(),
                description: "写周报".into(),
                content: "先列三项进展".into(),
            }],
            "auto_approve",
        )
        .unwrap()
        .with_trash(&base.join("_trash"));
        (base, tb)
    }

    fn box_of(tier: &str) -> (PathBuf, ToolBox) {
        let (base, _) = tmp_box();
        let tb = ToolBox::with_permission(&base, Vec::new(), tier)
            .unwrap()
            .with_trash(&base.join("_trash"));
        (base, tb)
    }

    /// 一个确实落在工作目录外的路径（各档位的边界用例都用它）。
    /// 注意它通常仍在家目录里，而家目录是硬拒根，所以「越界放行」要用 `outside_allowed_dir`。
    fn outside_of(base: &Path, name: &str) -> PathBuf {
        let sibling = base
            .parent()
            .unwrap_or_else(|| Path::new("."))
            .join(format!("tt-harness-outside-{}-{name}", std::process::id()));
        sibling
    }

    /// 找一个「既不在家目录也不在硬拒清单里」的真实可写目录，
    /// 用来证明完全访问确实越得出绑定目录。系统临时目录通常就在家目录里，
    /// 拿它当越界样本只会测出硬拒，测不出放行。找不到就返回 None，
    /// 用例跳过这一条，硬拒与删除备份两条照常断言。
    fn outside_allowed_dir() -> Option<PathBuf> {
        let roots: Vec<PathBuf> = if cfg!(windows) {
            vec![PathBuf::from(r"C:\Users\Public")]
        } else {
            vec![PathBuf::from("/tmp")]
        };
        for root in roots {
            let dir = root.join(format!("tt-harness-cross-{}", std::process::id()));
            if std::fs::create_dir_all(&dir).is_ok() && file_perm::hard_deny_hit(&dir).is_none() {
                return Some(dir);
            }
            let _ = std::fs::remove_dir_all(&dir);
        }
        None
    }

    #[test]
    fn test_asks_before_any_mutation_and_writes_nothing() {
        let (base, tb) = box_of("ask_every_time");
        let args = json!({"file_path": "a.md", "content": "x"});
        // 判定层：要求确认，且能看清要动哪个文件
        match tb.gate("write_text_file", &args) {
            Gate::Ask(desc) => assert!(desc.contains("a.md") && desc.contains("每次询问"), "{desc}"),
            other => panic!("每次询问应要求确认：{other:?}"),
        }
        // 执行层：没确认就一个字节都不写
        let out = tb.run("write_text_file", &args);
        assert!(!out.side_effect, "未确认的调用不该算作已执行");
        assert!(out.content.contains("未经用户确认"), "{}", out.content);
        assert!(!base.join("a.md").exists());
        // 读取不受「每次询问」影响：问的是写删，不是看
        tb.run("write_text_file", &args);
        let read = tb.run("view_text_file", &json!({"file_path": "a.md"}));
        assert!(read.content.contains("不存在"), "{}", read.content);
        // 放行之后才真的写
        let done = tb.run_after_approval("write_text_file", &args);
        assert!(done.side_effect);
        assert!(base.join("a.md").exists());
        // 确认框只解决「问不问」：越界并不会因为点了一次允许而放开。
        // 措辞按落点分两种（硬拒 / 需要完全访问），都必须是拒绝而不是询问。
        let far = outside_of(&base, "esc.md");
        match tb.gate("write_text_file", &json!({"file_path": far.to_string_lossy()})) {
            Gate::Refused(msg) => assert!(
                msg.contains("完全访问") || msg.contains("硬拒"),
                "{msg}"
            ),
            other => panic!("目录外写入应拒绝：{other:?}"),
        }
        let _ = std::fs::remove_dir_all(base);
    }

    #[test]
    fn test_auto_approve_stays_inside_and_cannot_delete() {
        let (base, tb) = box_of("auto_approve");
        assert_eq!(tb.gate("write_text_file", &json!({"file_path": "in.md", "content": "x"})), Gate::Pass);
        let far = outside_of(&base, "out.md");
        match tb.gate("write_text_file", &json!({"file_path": far.to_string_lossy()})) {
            Gate::Refused(msg) => assert!(msg.contains("文件访问权限拒绝"), "{msg}"),
            other => panic!("自动审批越界应拒绝：{other:?}"),
        }
        // 删除：任何非完全访问档位都问都不问，直接拒
        tb.run("write_text_file", &json!({"file_path": "gone.md", "content": "x"}));
        let out = tb.run("delete_text_file", &json!({"file_path": "gone.md"}));
        assert!(!out.side_effect);
        assert!(out.content.contains("完全访问"), "{}", out.content);
        assert!(base.join("gone.md").exists(), "被拒的删除不能真的动文件");
        let _ = std::fs::remove_dir_all(base);
    }

    #[test]
    fn test_full_access_crosses_but_hard_deny_and_trash_still_apply() {
        let (base, tb) = box_of("full_access");
        // 目录外的普通路径：允许，且不需要确认。
        // 样本必须落在硬拒清单外——本机临时目录就在家目录里，拿它测越界只会测出硬拒。
        let cross = outside_allowed_dir();
        if let Some(dir) = &cross {
            assert_eq!(
                tb.gate("write_text_file", &json!({"file_path": dir.join("cross.md").to_string_lossy()})),
                Gate::Pass,
                "完全访问应允许写目录外的普通本机路径"
            );
        } else {
            eprintln!("跳过越界放行用例：本机没有硬拒清单之外的可写样本目录");
        }
        // 同一条越界路径如果落在家目录里，仍然是硬拒（与档位无关）
        let in_denied = outside_of(&base, "home.md");
        if file_perm::hard_deny_hit(&in_denied).is_some() {
            match tb.gate("write_text_file", &json!({"file_path": in_denied.to_string_lossy()})) {
                Gate::Refused(msg) => assert!(msg.contains("硬拒"), "{msg}"),
                other => panic!("家目录内的越界写入应硬拒：{other:?}"),
            }
        }
        // 系统关键目录：完全访问也写不进去
        let protected = if cfg!(windows) { "C:\\Windows\\System32\\tt.md" } else { "/etc/tt.md" };
        match tb.gate("write_text_file", &json!({"file_path": protected})) {
            Gate::Refused(msg) => assert!(msg.contains("硬拒"), "{msg}"),
            other => panic!("硬拒清单应压过完全访问：{other:?}"),
        }
        // 删除：先备份再删，备份与原件同处临时区（with_trash 指的落点）
        let victim = base.join("gone.md");
        std::fs::write(&victim, "要删的内容").unwrap();
        let out = tb.run("delete_text_file", &json!({"file_path": "gone.md", "reason": "清理"}));
        assert!(out.side_effect);
        assert!(out.content.contains("已删除文件"), "{}", out.content);
        assert!(!victim.exists());
        assert!(base.join("_trash").exists(), "删除必须先在回收目录留一份");
        // 目录不存在时如实报错，不谎报已删除
        let miss = tb.run("delete_text_file", &json!({"file_path": "never.md"}));
        assert!(miss.content.contains("不存在"), "{}", miss.content);
        // 目录不删
        let dir_out = tb.run("delete_text_file", &json!({"file_path": "."}));
        assert!(dir_out.content.contains("不删目录") || dir_out.content.contains("不存在"), "{}", dir_out.content);
        let _ = std::fs::remove_dir_all(&base);
        if let Some(dir) = cross {
            let _ = std::fs::remove_dir_all(&dir);
        }
    }

    #[test]
    fn test_specs_advertise_the_tier_and_the_delete_tool() {
        let (base, tb) = box_of("auto_approve");
        let names: Vec<String> = tb
            .specs()
            .iter()
            .map(|v| v["function"]["name"].as_str().unwrap_or("").to_string())
            .collect();
        assert!(names.contains(&"delete_text_file".to_string()), "{names:?}");
        // 模型看到的可写范围必须与真实档位一致
        let text = serde_json::to_string(&tb.specs()).unwrap();
        assert!(text.contains("本轮绑定的工作目录内"), "{text}");
        assert!(text.contains("自动审批"), "{text}");
        let (base2, tb2) = box_of("full_access");
        assert!(
            serde_json::to_string(&tb2.specs())
                .unwrap()
                .contains("或目录外的普通本机路径")
        );
        let _ = std::fs::remove_dir_all(base);
        let _ = std::fs::remove_dir_all(base2);
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

    /// 截断页的形状是「第一行预算说明 + 正文」；没话可说的一页原样返回正文。
    /// 用例按这个约定取正文，不能拿正文里的第一个换行当分界（那样会把正文截断）。
    fn page_body(out: &str) -> &str {
        match out.split_once('\n') {
            Some((head, rest)) if head.contains("单元") || head.contains("字符") => rest,
            _ => out,
        }
    }

    #[test]
    fn test_view_text_file_paging_budget_is_honest_and_continuable() {
        let (base, tb) = tmp_box();
        // 400 行 × 每行 16 字符，用 \n 接起来共 400*16+399 = 6799 字符，超过默认一页的 4000
        let body = (0..400)
            .map(|i| format!("第{i:03}行：{}", "汉".repeat(10)))
            .collect::<Vec<_>>()
            .join("\n");
        assert_eq!(body.chars().count(), 6_799);
        tb.run("write_text_file", &json!({"file_path": "big.md", "content": body.clone()}));

        // 缺省一页：给满 4000 字符就收口，并写明还剩多少、下一页带什么参数
        let first = tb.run("view_text_file", &json!({"file_path": "big.md"}));
        let (first_note, first_chunk) = first.content.split_once('\n').unwrap();
        assert!(first_note.contains("选定范围共 6799 个单元"), "{first_note}");
        assert!(first_note.contains("单元=字符，非 token"), "{first_note}");
        assert!(first_note.contains("后面还有 2799 个单元没给"), "{first_note}");
        assert!(first_note.contains("start=4000"), "{first_note}");
        assert_eq!(first_chunk.chars().count(), 4_000, "返回值本身不许超预算");

        // 第二页：接着第一页的偏移读，拿到的是剩下的 2799 个，不再喊「后面还有」
        let second = tb.run("view_text_file", &json!({"file_path": "big.md", "start": 4000}));
        assert!(!second.content.contains("后面还有"), "{}", &second.content[..80]);
        assert!(second.content.contains("第399行："));
        let second_chunk = page_body(&second.content);
        assert_eq!(second_chunk.chars().count(), 2_799, "两页拼起来要正好等于原文");

        // 两页拼回去 = 原文：分页不许丢内容，也不许重给
        let joined = format!("{first_chunk}{second_chunk}");
        assert_eq!(joined, body);

        // 申请超上限：夹到 8000 并如实带话，而不是默默按默认 4000
        let greedy = tb.run("view_text_file", &json!({"file_path": "big.md", "units": 99999}));
        assert!(greedy.content.contains("「99999」超出上限 8000"), "{}", greedy.content);
        assert!(!greedy.content.contains("后面还有"), "{}", greedy.content);

        // 认不出来的参数要带话，不许猜模型的意图
        let bad_units = tb.run("view_text_file", &json!({"file_path": "big.md", "units": "很多"}));
        assert!(
            bad_units.content.contains("不是整数，已按默认 4000 执行"),
            "{}",
            bad_units.content
        );
        let bad_start = tb.run("view_text_file", &json!({"file_path": "big.md", "start": -5}));
        assert!(
            bad_start.content.contains("start「-5」是负数，本次从头开始读"),
            "{}",
            bad_start.content
        );
        let over_start = tb.run("view_text_file", &json!({"file_path": "big.md", "start": 90000}));
        assert!(
            over_start.content.contains("start=90000 已超过选定范围的长度 6799，本次没有内容可给"),
            "{}",
            over_start.content
        );

        // 行轴不受预算影响：短范围原样返回，一个字都不多给
        let one_line = tb.run(
            "view_text_file",
            &json!({"file_path": "big.md", "start_line": 12, "end_line": 12}),
        );
        assert_eq!(one_line.content, format!("第011行：{}", "汉".repeat(10)));
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
        // 自动审批连读都不越界：目录外的文件要看得先切「完全访问」
        assert!(out.content.contains("文件访问权限拒绝"), "{}", out.content);
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
    fn test_list_directory_respects_depth_limit_and_boundary() {
        let (base, tb) = tmp_box();
        std::fs::write(base.join("a.txt"), "hello").unwrap();
        std::fs::create_dir_all(base.join("sub/deep")).unwrap();
        std::fs::write(base.join("sub/b.txt"), "b").unwrap();
        std::fs::write(base.join("sub/deep/c.txt"), "c").unwrap();

        let top = tb.run("list_directory", &json!({}));
        assert!(!top.side_effect, "列目录是只读操作");
        assert!(top.content.contains("[文件] a.txt  5 B"), "{}", top.content);
        assert!(top.content.contains("[目录] sub/"), "{}", top.content);
        assert!(!top.content.contains("sub/b.txt"), "{}", top.content);
        assert!(
            top.content.contains("深度 1") && top.content.contains("单页上限 20 条"),
            "{}",
            top.content
        );
        // 一页列完就不许再喊「后面还有」，否则模型会白翻一页
        assert!(!top.content.contains("后面还有更多没列"), "{}", top.content);

        let two = tb.run("list_directory", &json!({"depth": 2}));
        assert!(two.content.contains("[文件] sub/b.txt"), "{}", two.content);
        assert!(!two.content.contains("sub/deep/c.txt"), "{}", two.content);

        // 深度上限夹到 3：模型给 9 也要落回文档承诺的范围
        let three = tb.run("list_directory", &json!({"depth": 9}));
        assert!(
            three.content.contains("深度 3") && three.content.contains("sub/deep/c.txt"),
            "{}",
            three.content
        );

        // 显式小请求：按请求给一条，并如实说「低于默认 20」+ 下一页的 offset
        let limited = tb.run("list_directory", &json!({"limit": 1}));
        assert!(
            limited.content.contains("列出条数按你的请求取 1")
                && limited.content.contains("本次第 1–1 条")
                && limited.content.contains("offset=1"),
            "{}",
            limited.content
        );
        // 翻一页：offset=1 拿到的是剩下的那条，不是重列第一页
        let next_page = tb.run("list_directory", &json!({"limit": 1, "offset": 1}));
        assert!(
            next_page.content.contains("本次第 2–2 条")
                && !next_page.content.contains("后面还有更多没列"),
            "{}",
            next_page.content
        );
        // 申请超上限：夹到 50 并点名「要读更多请分页」
        let greedy = tb.run("list_directory", &json!({"limit": 500}));
        assert!(greedy.content.contains("「500」超出上限 50"), "{}", greedy.content);
        // 越界偏移要如实说没有，不许假装目录空了
        let beyond = tb.run("list_directory", &json!({"offset": 99}));
        assert!(beyond.content.contains("这个偏移之后没有条目了"), "{}", beyond.content);

        let as_file = tb.run("list_directory", &json!({"path": "a.txt"}));
        assert!(as_file.content.contains("不是目录"), "{}", as_file.content);
        let missing = tb.run("list_directory", &json!({"path": "nope"}));
        assert!(missing.content.contains("目录不存在"), "{}", missing.content);

        // 自动审批档：读取同样不越界
        let far = outside_of(&base, "dir");
        let denied = tb.run("list_directory", &json!({"path": far.to_string_lossy()}));
        assert!(denied.content.contains("文件访问权限拒绝"), "{}", denied.content);
        let _ = std::fs::remove_dir_all(base);
    }

    #[test]
    fn test_read_document_extracts_office_text_and_reports_failures() {
        let (base, tb) = tmp_box();
        let docx = crate::file::office::zip_build(&[
            ("[Content_Types].xml", b"<?xml version=\"1.0\"?><Types/>", 0),
            (
                "word/document.xml",
                "<w:document><w:body><w:p><w:r><w:t>季度总结</w:t></w:r></w:p></w:body></w:document>"
                    .as_bytes(),
                0,
            ),
        ]);
        std::fs::write(base.join("report.docx"), docx).unwrap();
        let out = tb.run("read_document", &json!({"file_path": "report.docx"}));
        assert!(!out.side_effect, "读文档是只读操作");
        assert!(out.content.contains("已读取：") && out.content.contains("（docx）"), "{}", out.content);
        assert!(out.content.contains("季度总结"), "{}", out.content);

        // 文本类走原文，不经过 Office 解析器
        std::fs::write(base.join("notes.md"), "# 标题\n正文").unwrap();
        let md = tb.run("read_document", &json!({"file_path": "notes.md"}));
        assert!(md.content.contains("# 标题") && md.content.contains("正文"), "{}", md.content);

        // 正文为空时要如实说抽不到，而不是返回空白假装读到了
        let blank = crate::file::office::zip_build(&[(
            "word/document.xml",
            b"<w:document><w:body><w:p/></w:body></w:document>",
            0,
        )]);
        std::fs::write(base.join("blank.docx"), blank).unwrap();
        let blank_out = tb.run("read_document", &json!({"file_path": "blank.docx"}));
        assert!(blank_out.content.contains("未能从") && blank_out.content.contains("抽取到正文"), "{}", blank_out.content);

        // 旧版二进制 Office：报「另存为」而不是假装支持
        std::fs::write(base.join("legacy.doc"), b"\xd0\xcf\x11\xe0").unwrap();
        let legacy = tb.run("read_document", &json!({"file_path": "legacy.doc"}));
        assert!(legacy.content.contains("旧版二进制"), "{}", legacy.content);

        std::fs::write(base.join("blob.bin"), [0u8, 1, 2]).unwrap();
        let unsupported = tb.run("read_document", &json!({"file_path": "blob.bin"}));
        assert!(unsupported.content.contains("不支持的文档格式"), "{}", unsupported.content);

        let missing = tb.run("read_document", &json!({"file_path": "nope.docx"}));
        assert!(missing.content.contains("文件不存在"), "{}", missing.content);

        // 超预算的请求：如实带话「按你的请求取 1」，并给出下一页的 offset（旧版只能截前缀、读不到后面）
        std::fs::write(base.join("long.md"), "数".repeat(3000)).unwrap();
        let clipped = tb.run("read_document", &json!({"file_path": "long.md", "max_chars": 1}));
        assert!(
            clipped.content.contains("读取字符数按你的请求取 1")
                && clipped.content.contains("还有 2999 字符没给")
                && clipped.content.contains("offset=1"),
            "{}",
            clipped.content
        );
        // 第二页真的能接着读：offset=1 起再取 1 个字符，不是又把开头给了一遍
        let second = tb.run(
            "read_document",
            &json!({"file_path": "long.md", "max_chars": 1, "offset": 1}),
        );
        assert!(second.content.contains("本次按预算给第 1 起的 1 个"), "{}", second.content);
        // 申请超上限：夹到 8000 并说明「要读更多请分页」，不是默默按默认；剩余量与下一页参数都要在
        std::fs::write(base.join("huge.md"), "数".repeat(9_000)).unwrap();
        let over = tb.run("read_document", &json!({"file_path": "huge.md", "max_chars": 99999}));
        assert!(
            over.content.contains("「99999」超出上限 8000")
                && over.content.contains("本次按预算给第 0 起的 8000 个")
                && over.content.contains("还有 1000 字符没给")
                && over.content.contains("offset=8000"),
            "{}",
            over.content
        );

        // 自动审批档：目录外文档不读
        let abs = if cfg!(windows) { "C:\\Windows\\win.ini" } else { "/etc/hosts" };
        let denied = tb.run("read_document", &json!({"file_path": abs}));
        assert!(denied.content.contains("文件访问权限拒绝"), "{}", denied.content);
        let _ = std::fs::remove_dir_all(base);
    }

    #[test]
    fn test_specs_include_directory_and_document_tools() {
        let (base, tb) = box_of("auto_approve");
        let names: Vec<String> = tb
            .specs()
            .iter()
            .map(|v| v["function"]["name"].as_str().unwrap_or("").to_string())
            .collect();
        for name in ["list_directory", "read_document", "view_text_file", "write_text_file"] {
            assert!(names.contains(&name.to_string()), "缺少工具声明 {name}：{names:?}");
        }
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
