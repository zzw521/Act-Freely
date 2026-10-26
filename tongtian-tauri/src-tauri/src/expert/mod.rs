//! 专家团队注册表（多专家运行时的事实来源）
//!
//! 每个专家是一条配置：名称、可被主 Agent 理解的职责描述、角色提示词、可选模型绑定、
//! 可选技能白名单。注册表落在 `settings_kv.experts.registry`（JSON 数组），不新增表，
//! 历史数据无需迁移；删除专家只删除配置，不动任何任务/会话数据。
//!
//! 模型密钥只在 Rust 侧解析：渲染层拿到的是 `modelId`/`modelName`，运行时 bundle 才带
//! `modelConfig`（含 apiKey）下发给本机引擎。检查点落盘时引擎会再剥掉 apiKey。
//!
//! 失败口径：
//! - 注册表 JSON 损坏 → `EXPERTS_CORRUPT`，不返回空列表假装「没有专家」；
//! - 绑定的模型不存在/缺密钥 → 保存时 `EXPERT_MODEL_NOT_FOUND` / 运行时条目带 `modelError`；
//! - 负责人专家在提交时校验，模型不可用直接拒绝提交，不留下必然失败的任务。

use std::sync::Arc;

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use tauri::State;

use crate::model;
use crate::store::settings;
use crate::store::Store;

/// 注册表在 settings_kv 中的键（值为 JSON 数组）
pub const REGISTRY_KEY: &str = "experts.registry";

/// 专家数量上限：超过后主 Agent 的派发提示词会失控，也会拖慢每次提交的模型解析
const MAX_EXPERTS: usize = 20;
const MAX_ID_LEN: usize = 64;
const MAX_NAME_CHARS: usize = 40;
const MAX_DESCRIPTION_CHARS: usize = 300;
const MAX_PROMPT_CHARS: usize = 8000;
const MAX_SKILLS: usize = 20;
const MAX_SKILL_ID_LEN: usize = 64;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct Expert {
    pub id: String,
    pub name: String,
    #[serde(default)]
    pub description: String,
    #[serde(default)]
    pub prompt: String,
    #[serde(default)]
    pub model_id: Option<String>,
    #[serde(default)]
    pub skills: Vec<String>,
    #[serde(default)]
    pub updated_at: i64,
}

/// 新建/更新入参（与前端 ExpertSaveReq 同口径，camelCase）
#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ExpertInput {
    pub id: Option<String>,
    pub name: String,
    #[serde(default)]
    pub description: String,
    pub prompt: String,
    #[serde(default)]
    pub model_id: Option<String>,
    #[serde(default)]
    pub skills: Vec<String>,
}

fn char_len(raw: &str) -> usize {
    raw.chars().count()
}

fn normalize_id(raw: &str) -> Result<String, String> {
    let id = raw.trim().to_lowercase();
    if id.is_empty() {
        return Err("EXPERT_INVALID: 专家标识不能为空".into());
    }
    if id.len() > MAX_ID_LEN {
        return Err(format!(
            "EXPERT_INVALID: 专家标识过长（{} 字符，上限 {MAX_ID_LEN}）",
            id.len()
        ));
    }
    if !id
        .chars()
        .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_' || c == '-')
    {
        return Err("EXPERT_INVALID: 专家标识仅支持小写字母、数字、下划线与连字符".into());
    }
    Ok(id)
}

fn generate_id() -> String {
    format!("expert-{}", crate::bridge::event_store::now_ms())
}

/// 校验并归一化用户输入；`id_exists` 为 true 表示这是更新（不允许静默新建）
fn normalize_input(
    store: &Store,
    req: ExpertInput,
    id_exists: bool,
) -> Result<Expert, String> {
    let name = req.name.trim().to_string();
    if name.is_empty() {
        return Err("EXPERT_INVALID: 专家名称不能为空".into());
    }
    if char_len(&name) > MAX_NAME_CHARS {
        return Err(format!(
            "EXPERT_INVALID: 专家名称过长（上限 {MAX_NAME_CHARS} 字）"
        ));
    }
    let description = req.description.trim().to_string();
    if description.is_empty() {
        return Err("EXPERT_INVALID: 职责描述不能为空（主 Agent 靠它决定把什么子任务交给该专家）".into());
    }
    if char_len(&description) > MAX_DESCRIPTION_CHARS {
        return Err(format!(
            "EXPERT_INVALID: 职责描述过长（上限 {MAX_DESCRIPTION_CHARS} 字）"
        ));
    }
    let prompt = req.prompt.trim().to_string();
    if prompt.is_empty() {
        return Err("EXPERT_INVALID: 角色提示词不能为空".into());
    }
    if char_len(&prompt) > MAX_PROMPT_CHARS {
        return Err(format!(
            "EXPERT_INVALID: 角色提示词过长（上限 {MAX_PROMPT_CHARS} 字）"
        ));
    }

    let id = match req.id.as_deref().map(str::trim).filter(|s| !s.is_empty()) {
        Some(raw) => {
            let id = normalize_id(raw)?;
            if !id_exists {
                return Err(format!("EXPERT_NOT_FOUND: 要更新的专家 {id} 不存在"));
            }
            id
        }
        None => generate_id(),
    };

    let model_id = match req.model_id.as_deref().map(str::trim).filter(|s| !s.is_empty()) {
        Some(mid) => {
            if char_len(mid) > MAX_ID_LEN {
                return Err("EXPERT_INVALID: 模型标识过长".into());
            }
            match model::service::get(store, mid) {
                Ok(Some(_)) => Some(mid.to_string()),
                Ok(None) => {
                    return Err(format!(
                        "EXPERT_MODEL_NOT_FOUND: 绑定的模型 {mid} 不存在，请先在模型中心添加"
                    ))
                }
                Err(e) => return Err(format!("EXPERT_MODEL_UNAVAILABLE: {e}")),
            }
        }
        None => None,
    };

    let mut skills: Vec<String> = Vec::new();
    for raw in req.skills {
        let skill = raw.trim().to_string();
        if skill.is_empty() {
            continue;
        }
        if char_len(&skill) > MAX_SKILL_ID_LEN {
            return Err("EXPERT_INVALID: 技能标识过长".into());
        }
        if !skills.iter().any(|s| s.eq_ignore_ascii_case(&skill)) {
            skills.push(skill);
        }
    }
    if skills.len() > MAX_SKILLS {
        return Err(format!("EXPERT_INVALID: 单个专家最多选择 {MAX_SKILLS} 个技能"));
    }

    Ok(Expert {
        id,
        name,
        description,
        prompt,
        model_id,
        skills,
        updated_at: crate::bridge::event_store::now_ms() as i64,
    })
}

/// 读取注册表。键不存在 → 空列表；JSON 损坏/条目非法 → 显式错误（不返回空列表）。
pub fn list(store: &Store) -> Result<Vec<Expert>, String> {
    let Some(raw) = settings::get(store, REGISTRY_KEY)? else {
        return Ok(Vec::new());
    };
    let raw = raw.trim();
    if raw.is_empty() {
        return Ok(Vec::new());
    }
    let items: Vec<Expert> = serde_json::from_str(raw)
        .map_err(|e| format!("EXPERTS_CORRUPT: 专家注册表不是合法 JSON：{e}"))?;
    for item in &items {
        if item.id.trim().is_empty() || item.name.trim().is_empty() {
            return Err("EXPERTS_CORRUPT: 专家注册表存在缺少 id/name 的条目".into());
        }
    }
    Ok(items)
}

pub fn get(store: &Store, id: &str) -> Result<Option<Expert>, String> {
    let key = id.trim().to_lowercase();
    Ok(list(store)?.into_iter().find(|e| e.id == key))
}

fn save_items(store: &Store, items: &[Expert]) -> Result<(), String> {
    let raw = serde_json::to_string(items)
        .map_err(|e| format!("EXPERTS_SAVE_FAILED: 序列化失败：{e}"))?;
    settings::set(store, REGISTRY_KEY, &raw)
}

pub fn save(store: &Store, req: ExpertInput) -> Result<Expert, String> {
    let mut items = list(store)?;
    let wanted_id = req
        .id
        .as_deref()
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(str::to_lowercase);
    let index = wanted_id
        .as_ref()
        .and_then(|id| items.iter().position(|e| &e.id == id));
    let expert = normalize_input(store, req, index.is_some())?;

    if items
        .iter()
        .enumerate()
        .any(|(i, e)| Some(i) != index && e.name.eq_ignore_ascii_case(&expert.name))
    {
        return Err(format!(
            "EXPERT_NAME_DUPLICATED: 已存在同名专家「{}」，请换一个名称（派发时按名称/index 解析会歧义）",
            expert.name
        ));
    }

    match index {
        Some(i) => items[i] = expert.clone(),
        None => {
            if items.len() >= MAX_EXPERTS {
                return Err(format!("EXPERT_LIMIT: 专家数量已达上限 {MAX_EXPERTS}"));
            }
            items.push(expert.clone());
        }
    }
    save_items(store, &items)?;
    Ok(expert)
}

pub fn delete(store: &Store, id: &str) -> Result<Expert, String> {
    let key = id.trim().to_lowercase();
    let mut items = list(store)?;
    let Some(index) = items.iter().position(|e| e.id == key) else {
        return Err(format!("EXPERT_NOT_FOUND: 专家 {id} 不存在"));
    };
    let removed = items.remove(index);
    save_items(store, &items)?;
    Ok(removed)
}

/// 负责人在提交阶段的模型校验：绑定了模型就要求它现在可用，避免任务跑起来才 401。
pub fn lead_model_config(store: &Store, expert_id: &str) -> Result<Option<Value>, String> {
    let Some(expert) = get(store, expert_id)? else {
        return Err(format!("EXPERT_NOT_FOUND: 专家 {expert_id} 不存在"));
    };
    let Some(model_id) = expert
        .model_id
        .as_deref()
        .map(str::trim)
        .filter(|s| !s.is_empty())
    else {
        return Ok(None);
    };
    model::service::runtime_config(store, Some(model_id)).map_err(|err| {
        format!(
            "EXPERT_MODEL_UNAVAILABLE: 专家「{}」绑定的模型不可用：{err}",
            expert.name
        )
    })
}

/// 模型解析结果（运行时与页面共用同一口径）
fn model_fields(store: &Store, expert: &Expert) -> (Option<Value>, Option<String>, Option<String>) {
    let Some(model_id) = expert
        .model_id
        .as_deref()
        .map(str::trim)
        .filter(|s| !s.is_empty())
    else {
        return (None, None, None);
    };
    match model::service::runtime_config(store, Some(model_id)) {
        Ok(Some(cfg)) => {
            let name = cfg
                .get("modelName")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_string();
            (Some(cfg), Some(name), None)
        }
        Ok(None) => (
            None,
            None,
            Some("绑定的模型已不存在，请重新选择".to_string()),
        ),
        Err(e) => (None, None, Some(e)),
    }
}

fn public_view(store: &Store, expert: &Expert) -> Value {
    let (_, model_name, model_error) = model_fields(store, expert);
    json!({
        "id": expert.id,
        "name": expert.name,
        "description": expert.description,
        "prompt": expert.prompt,
        "modelId": expert.model_id,
        "modelName": model_name,
        "modelError": model_error,
        "skills": expert.skills,
        "updatedAt": expert.updated_at,
    })
}

/// 运行时 bundle 的 `experts` 段：带解析后的模型配置（含密钥），只发给本机引擎。
pub fn runtime_block(store: Option<&Store>) -> Value {
    let Some(store) = store else {
        return json!({ "items": [], "persisted": false });
    };
    match list(store) {
        Ok(items) => {
            let views: Vec<Value> = items
                .iter()
                .map(|expert| {
                    let (config, model_name, model_error) = model_fields(store, expert);
                    let mut item = json!({
                        "id": expert.id,
                        "name": expert.name,
                        "description": expert.description,
                        "prompt": expert.prompt,
                        "modelId": expert.model_id,
                        "modelName": model_name,
                        "skills": expert.skills,
                    });
                    if let Some(cfg) = config {
                        item["modelConfig"] = cfg;
                    }
                    if let Some(err) = model_error {
                        item["modelError"] = json!(err);
                    }
                    item
                })
                .collect();
            json!({ "items": views, "persisted": true })
        }
        Err(e) => json!({ "items": [], "persisted": true, "error": e }),
    }
}

// ── Tauri commands ──────────────────────────────────────────

#[tauri::command]
pub async fn experts_list(
    p0: State<'_, Arc<crate::P0Services>>,
) -> Result<Value, String> {
    let items = list(&p0.store)?;
    Ok(Value::Array(
        items.iter().map(|e| public_view(&p0.store, e)).collect(),
    ))
}

#[tauri::command]
pub async fn experts_save(
    p0: State<'_, Arc<crate::P0Services>>,
    req: ExpertInput,
) -> Result<Value, String> {
    let expert = save(&p0.store, req)?;
    Ok(public_view(&p0.store, &expert))
}

#[tauri::command]
pub async fn experts_delete(
    p0: State<'_, Arc<crate::P0Services>>,
    expert_id: String,
) -> Result<Value, String> {
    let removed = delete(&p0.store, &expert_id)?;
    Ok(json!({ "ok": true, "id": removed.id, "name": removed.name }))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::service::{add, ModelAddReq};

    fn temp_store(name: &str) -> (Store, std::path::PathBuf) {
        let dir = std::env::temp_dir().join(format!(
            "tt-experts-{name}-{}",
            crate::bridge::event_store::now_ms()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let store = Store::open(dir.join("t.db")).unwrap();
        (store, dir)
    }

    fn input(name: &str) -> ExpertInput {
        ExpertInput {
            id: None,
            name: name.to_string(),
            description: format!("{name}的职责"),
            prompt: format!("你是{name}，按专业标准完成子任务。"),
            model_id: None,
            skills: vec![],
        }
    }

    fn local_model(store: &Store, name: &str) -> String {
        add(
            store,
            ModelAddReq {
                name: name.to_string(),
                source: "custom".into(),
                base_url: "http://127.0.0.1:11434/v1".into(),
                api_key: None,
                model_name: format!("{name}-model"),
                context_window: 4096,
                is_default: false,
                agent_binding: String::new(),
                force_local: true,
                ..Default::default()
            },
        )
        .unwrap()
        .id
    }

    #[test]
    fn save_roundtrip_and_duplicate_name_rejected() {
        let (store, dir) = temp_store("roundtrip");
        let saved = save(&store, input("财务分析师")).unwrap();
        assert_eq!(saved.name, "财务分析师");
        assert!(saved.id.starts_with("expert-"));

        let mut dup = input("财务分析师");
        dup.name = " 财务分析师 ".into();
        let err = save(&store, dup).unwrap_err();
        assert!(err.contains("EXPERT_NAME_DUPLICATED"), "{err}");

        let items = list(&store).unwrap();
        assert_eq!(items.len(), 1);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn missing_fields_are_rejected() {
        let (store, dir) = temp_store("invalid");
        let mut req = input("甲");
        req.name = "  ".into();
        assert!(save(&store, req).unwrap_err().contains("名称不能为空"));

        let mut req = input("乙");
        req.description = "".into();
        assert!(save(&store, req).unwrap_err().contains("职责描述不能为空"));

        let mut req = input("丙");
        req.prompt = "".into();
        assert!(save(&store, req).unwrap_err().contains("角色提示词不能为空"));

        assert!(list(&store).unwrap().is_empty());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn update_unknown_id_does_not_create() {
        let (store, dir) = temp_store("unknown-update");
        let mut req = input("甲");
        req.id = Some("expert-missing".into());
        let err = save(&store, req).unwrap_err();
        assert!(err.contains("EXPERT_NOT_FOUND"), "{err}");
        assert!(list(&store).unwrap().is_empty());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn model_binding_is_resolved_and_public_view_has_no_secret() {
        let (store, dir) = temp_store("model");
        let model_id = local_model(&store, "本地模型");
        let mut req = input("代码专家");
        req.model_id = Some(model_id.clone());
        let saved = save(&store, req).unwrap();
        assert_eq!(saved.model_id.as_deref(), Some(model_id.as_str()));

        let view = public_view(&store, &saved);
        assert_eq!(view["modelName"], json!("本地模型-model"));
        assert!(view.get("modelConfig").is_none(), "{view}");
        assert!(view.get("apiKey").is_none(), "{view}");

        let runtime = runtime_block(Some(&store));
        let item = &runtime["items"][0];
        assert_eq!(item["modelConfig"]["modelName"], json!("本地模型-model"));
        assert_eq!(item["modelConfig"]["apiKey"], json!(""));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn unknown_model_binding_is_rejected_at_save() {
        let (store, dir) = temp_store("bad-model");
        let mut req = input("甲");
        req.model_id = Some("model-missing".into());
        let err = save(&store, req).unwrap_err();
        assert!(err.contains("EXPERT_MODEL_NOT_FOUND"), "{err}");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn corrupted_registry_is_an_error_not_an_empty_list() {
        let (store, dir) = temp_store("corrupt");
        settings::set(&store, REGISTRY_KEY, "{not-json").unwrap();
        let err = list(&store).unwrap_err();
        assert!(err.contains("EXPERTS_CORRUPT"), "{err}");
        let block = runtime_block(Some(&store));
        assert_eq!(block["items"], json!([]));
        assert!(block["error"]
            .as_str()
            .unwrap_or_default()
            .contains("EXPERTS_CORRUPT"));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn delete_only_removes_registry_entry() {
        let (store, dir) = temp_store("delete");
        let a = save(&store, input("甲")).unwrap();
        let b = save(&store, input("乙")).unwrap();
        let removed = delete(&store, &a.id).unwrap();
        assert_eq!(removed.id, a.id);
        let items = list(&store).unwrap();
        assert_eq!(items.len(), 1);
        assert_eq!(items[0].id, b.id);
        assert!(delete(&store, "expert-missing")
            .unwrap_err()
            .contains("EXPERT_NOT_FOUND"));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn lead_model_config_requires_existing_expert_and_model() {
        let (store, dir) = temp_store("lead");
        let model_id = local_model(&store, "负责人模型");
        let mut req = input("负责人");
        req.model_id = Some(model_id);
        let saved = save(&store, req).unwrap();
        let cfg = lead_model_config(&store, &saved.id).unwrap().unwrap();
        assert_eq!(cfg["modelName"], json!("负责人模型-model"));
        let err = lead_model_config(&store, "expert-missing").unwrap_err();
        assert!(err.contains("EXPERT_NOT_FOUND"), "{err}");
        let _ = std::fs::remove_dir_all(&dir);
    }
}
