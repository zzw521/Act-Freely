//! Model Hub 服务：CRUD + 连通性自测
//!
//! 契约对齐 shared/types.ts ModelConfig / contracts.ts ModelTestReq/Resp。
//! API Key 仅存 keyring（vault），DB 保存 hasApiKey 引用标记。

use serde::{Deserialize, Serialize};

use crate::store::Store;

/// 模型配置（camelCase 对齐前端；apiKeyEncrypted 恒为空串——密钥在 keyring）
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ModelConfig {
    pub id: String,
    pub name: String,
    pub source: String,
    pub base_url: String,
    /// 密钥不落库：恒为空串，真实密钥经 vault 按 id 存取
    #[serde(default)]
    pub api_key_encrypted: String,
    pub model_name: String,
    pub context_window: i64,
    pub is_default: bool,
    pub agent_binding: String,
    pub force_local: bool,
    /// keyring 中是否存有 API Key（前端展示「已配置密钥」）
    #[serde(default)]
    pub has_api_key: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub last_used_at: Option<String>,
    pub created_at: String,
    pub updated_at: String,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ModelAddReq {
    pub name: String,
    pub source: String,
    #[serde(default)]
    pub base_url: String,
    #[serde(default)]
    pub api_key: Option<String>,
    pub model_name: String,
    #[serde(default = "default_context_window")]
    pub context_window: i64,
    #[serde(default)]
    pub is_default: bool,
    #[serde(default)]
    pub agent_binding: String,
    #[serde(default)]
    pub force_local: bool,
}

fn default_context_window() -> i64 {
    4096
}

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ModelUpdateReq {
    pub id: String,
    pub name: Option<String>,
    pub source: Option<String>,
    pub base_url: Option<String>,
    /// 传入时更新密钥（空串表示清除）；None 表示不动
    pub api_key: Option<String>,
    pub model_name: Option<String>,
    pub context_window: Option<i64>,
    pub agent_binding: Option<String>,
    pub force_local: Option<bool>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ModelTestReq {
    #[serde(default)]
    pub base_url: String,
    #[serde(default)]
    pub api_key: String,
    #[serde(default)]
    pub model_name: String,
    #[serde(default)]
    pub context_window: i64,
    /// 可选：传已存模型 id 时，api_key 缺省将从 keyring 解析（列表卡片测试场景）
    #[serde(default)]
    pub id: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ModelTestResp {
    pub ok: bool,
    pub latency_ms: u64,
    pub model: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub available_models: Option<Vec<String>>,
}

fn now_iso() -> String {
    let ms = crate::bridge::event_store::now_ms();
    let secs = ms / 1000;
    // 简化 ISO 输出（UTC）
    let days = secs / 86_400;
    let time = secs % 86_400;
    let (h, m, s) = (time / 3600, (time % 3600) / 60, time % 60);
    let z = days as i64 + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let mth = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = if mth <= 2 { y + 1 } else { y };
    format!("{:04}-{:02}-{:02}T{:02}:{:02}:{:02}Z", y, mth, d, h, m, s)
}

const SELECT_COLS: &str = "SELECT id, name, source, base_url, model_name, context_window,
        is_default, agent_binding, force_local, created_at, updated_at FROM model_configs";

/// 列出全部模型配置
pub fn list(store: &Store) -> Result<Vec<ModelConfig>, String> {
    store.with_conn(|c| {
        let mut stmt = c.prepare(SELECT_COLS).map_err(|e| e.to_string())?;
        let rows = stmt
            .query_map([], |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                    row.get::<_, String>(3)?,
                    row.get::<_, String>(4)?,
                    row.get::<_, i64>(5)?,
                    row.get::<_, i64>(6)?,
                    row.get::<_, String>(7)?,
                    row.get::<_, i64>(8)?,
                    row.get::<_, String>(9)?,
                    row.get::<_, String>(10)?,
                ))
            })
            .map_err(|e| e.to_string())?;
        let mut out = Vec::new();
        for row in rows {
            let (id, name, source, base_url, model_name, cw, isd, ab, fl, ca, ua) =
                row.map_err(|e| e.to_string())?;
            out.push(ModelConfig {
                has_api_key: store.vault().get_secret(&id).is_some(),
                id,
                name,
                source,
                base_url,
                api_key_encrypted: String::new(),
                model_name,
                context_window: cw,
                is_default: isd != 0,
                agent_binding: ab,
                force_local: fl != 0,
                last_used_at: None,
                created_at: ca,
                updated_at: ua,
            });
        }
        Ok(out)
    })
}

fn clear_default(c: &rusqlite::Connection) -> Result<(), String> {
    c.execute("UPDATE model_configs SET is_default = 0", [])
        .map(|_| ())
        .map_err(|e| e.to_string())
}

/// 添加模型（密钥存 vault；is_default 时清除其他默认）
pub fn add(store: &Store, req: ModelAddReq) -> Result<ModelConfig, String> {
    if req.name.trim().is_empty() || req.model_name.trim().is_empty() {
        return Err("VALIDATION_ERROR: name/modelName 不能为空".into());
    }
    let id = uuid::Uuid::new_v4().to_string();
    let now = now_iso();
    if let Some(key) = &req.api_key {
        if !key.trim().is_empty() {
            store.vault().set_secret(&id, key.trim())?;
        }
    }
    store.with_conn(|c| {
        if req.is_default {
            clear_default(c)?;
        }
        c.execute(
            "INSERT INTO model_configs (id, name, source, base_url, model_name, context_window,
                is_default, agent_binding, force_local, created_at, updated_at)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11)",
            rusqlite::params![
                id,
                req.name.trim(),
                req.source,
                req.base_url,
                req.model_name.trim(),
                req.context_window,
                req.is_default as i64,
                req.agent_binding,
                req.force_local as i64,
                now,
                now,
            ],
        )
        .map(|_| ())
        .map_err(|e| format!("MODEL_WRITE_FAILED: {e}"))
    })?;
    get(store, &id)?.ok_or_else(|| "MODEL_NOT_FOUND: 写入后读取失败".into())
}

/// 按 id 读取
pub fn get(store: &Store, id: &str) -> Result<Option<ModelConfig>, String> {
    store.with_conn(|c| {
        let mut stmt = c
            .prepare(&format!("{SELECT_COLS} WHERE id = ?1"))
            .map_err(|e| e.to_string())?;
        let mut rows = stmt.query([id]).map_err(|e| e.to_string())?;
        match rows.next().map_err(|e| e.to_string())? {
            Some(row) => {
                let tup = (
                    row.get::<_, String>(0).map_err(|e| e.to_string())?,
                    row.get::<_, String>(1).map_err(|e| e.to_string())?,
                    row.get::<_, String>(2).map_err(|e| e.to_string())?,
                    row.get::<_, String>(3).map_err(|e| e.to_string())?,
                    row.get::<_, String>(4).map_err(|e| e.to_string())?,
                    row.get::<_, i64>(5).map_err(|e| e.to_string())?,
                    row.get::<_, i64>(6).map_err(|e| e.to_string())?,
                    row.get::<_, String>(7).map_err(|e| e.to_string())?,
                    row.get::<_, i64>(8).map_err(|e| e.to_string())?,
                    row.get::<_, String>(9).map_err(|e| e.to_string())?,
                    row.get::<_, String>(10).map_err(|e| e.to_string())?,
                );
                Ok(Some(ModelConfig {
                    has_api_key: store.vault().get_secret(&tup.0).is_some(),
                    id: tup.0,
                    name: tup.1,
                    source: tup.2,
                    base_url: tup.3,
                    api_key_encrypted: String::new(),
                    model_name: tup.4,
                    context_window: tup.5,
                    is_default: tup.6 != 0,
                    agent_binding: tup.7,
                    force_local: tup.8 != 0,
                    last_used_at: None,
                    created_at: tup.9,
                    updated_at: tup.10,
                }))
            }
            None => Ok(None),
        }
    })
}

/// 更新模型（部分字段；api_key 空串=清除密钥）
pub fn update(store: &Store, req: ModelUpdateReq) -> Result<ModelConfig, String> {
    let existing = get(store, &req.id)?
        .ok_or_else(|| format!("MODEL_NOT_FOUND: {}", req.id))?;
    if let Some(key) = &req.api_key {
        if key.trim().is_empty() {
            store.vault().delete_secret(&req.id);
        } else {
            store.vault().set_secret(&req.id, key.trim())?;
        }
    }
    let mut next = existing.clone();
    if let Some(v) = req.name {
        next.name = v;
    }
    if let Some(v) = req.source {
        next.source = v;
    }
    if let Some(v) = req.base_url {
        next.base_url = v;
    }
    if let Some(v) = req.model_name {
        next.model_name = v;
    }
    if let Some(v) = req.context_window {
        next.context_window = v;
    }
    if let Some(v) = req.agent_binding {
        next.agent_binding = v;
    }
    if let Some(v) = req.force_local {
        next.force_local = v;
    }
    let now = now_iso();
    store.with_conn(|c| {
        c.execute(
            "UPDATE model_configs SET name=?2, source=?3, base_url=?4, model_name=?5,
                context_window=?6, agent_binding=?7, force_local=?8, updated_at=?9
             WHERE id=?1",
            rusqlite::params![
                req.id,
                next.name,
                next.source,
                next.base_url,
                next.model_name,
                next.context_window,
                next.agent_binding,
                next.force_local as i64,
                now,
            ],
        )
        .map(|_| ())
        .map_err(|e| format!("MODEL_WRITE_FAILED: {e}"))
    })?;
    get(store, &req.id)?.ok_or_else(|| "MODEL_NOT_FOUND: 更新后读取失败".into())
}

/// 删除模型（连同 vault 密钥）
pub fn delete(store: &Store, id: &str) -> Result<(), String> {
    let removed = store.with_conn(|c| {
        c.execute("DELETE FROM model_configs WHERE id = ?1", [id])
            .map_err(|e| format!("MODEL_WRITE_FAILED: {e}"))
    })?;
    if removed == 0 {
        return Err(format!("MODEL_NOT_FOUND: {id}"));
    }
    store.vault().delete_secret(id);
    Ok(())
}

/// 设为默认（清除其他默认位）
pub fn set_default(store: &Store, id: &str) -> Result<(), String> {
    let updated = store.with_conn(|c| {
        clear_default(c)?;
        c.execute(
            "UPDATE model_configs SET is_default = 1, updated_at = ?2 WHERE id = ?1",
            rusqlite::params![id, now_iso()],
        )
        .map_err(|e| format!("MODEL_WRITE_FAILED: {e}"))
    })?;
    if updated == 0 {
        return Err(format!("MODEL_NOT_FOUND: {id}"));
    }
    Ok(())
}

/// 连通性自测：OpenAI 兼容 /chat/completions 最小探测（超时 10s）
pub async fn test(req: ModelTestReq) -> ModelTestResp {
    let started = std::time::Instant::now();
    let base = req.base_url.trim_end_matches('/');
    if base.is_empty() {
        return ModelTestResp {
            ok: false,
            latency_ms: 0,
            model: req.model_name.clone(),
            error: Some("MODEL_CONNECTION_FAILED: baseUrl 为空".into()),
            available_models: None,
        };
    }
    // 走「网络代理」设置：本地模型（localhost/127.0.0.1）由 ALWAYS_BYPASS 保证直连
    let client = crate::proxy::client();
    let body = serde_json::json!({
        "model": req.model_name,
        "messages": [{ "role": "user", "content": "ping" }],
        "max_tokens": 1,
        "stream": false,
    });
    let result = tokio::time::timeout(
        std::time::Duration::from_secs(10),
        client
            .post(format!("{base}/chat/completions"))
            .bearer_auth(&req.api_key)
            .json(&body)
            .send(),
    )
    .await;

    let latency = started.elapsed().as_millis() as u64;
    match result {
        Ok(Ok(resp)) if resp.status().is_success() => {
            // 顺带拉取可用模型列表（失败不影响主结果）
            let available = fetch_models(&client, base, &req.api_key).await;
            ModelTestResp {
                ok: true,
                latency_ms: latency,
                model: req.model_name,
                error: None,
                available_models: available,
            }
        }
        Ok(Ok(resp)) => {
            let status = resp.status().as_u16();
            let text = resp.text().await.unwrap_or_default();
            let code = if status == 401 || status == 403 {
                "MODEL_AUTH_FAILED"
            } else {
                "MODEL_CONNECTION_FAILED"
            };
            ModelTestResp {
                ok: false,
                latency_ms: latency,
                model: req.model_name,
                error: Some(format!("{code}: HTTP {status} {}", truncate(&text, 200))),
                available_models: None,
            }
        }
        Ok(Err(e)) => ModelTestResp {
            ok: false,
            latency_ms: latency,
            model: req.model_name,
            error: Some(format!("MODEL_CONNECTION_FAILED: {e}")),
            available_models: None,
        },
        Err(_) => ModelTestResp {
            ok: false,
            latency_ms: latency,
            model: req.model_name,
            error: Some("MODEL_CONNECTION_FAILED: 请求超时（10s）".into()),
            available_models: None,
        },
    }
}

async fn fetch_models(
    client: &reqwest::Client,
    base: &str,
    api_key: &str,
) -> Option<Vec<String>> {
    let resp = tokio::time::timeout(
        std::time::Duration::from_secs(5),
        client.get(format!("{base}/models")).bearer_auth(api_key).send(),
    )
    .await
    .ok()?
    .ok()?;
    if !resp.status().is_success() {
        return None;
    }
    let body: serde_json::Value = resp.json().await.ok()?;
    body.get("data")
        .and_then(|d| d.as_array())
        .map(|arr| {
            arr.iter()
                .filter_map(|m| {
                    m.get("id")
                        .or_else(|| m.get("name"))
                        .and_then(|v| v.as_str())
                        .map(|s| s.to_string())
                })
                .collect()
        })
}

fn truncate(s: &str, n: usize) -> String {
    if s.chars().count() <= n {
        s.to_string()
    } else {
        s.chars().take(n).collect::<String>() + "…"
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    fn temp_store(name: &str) -> (Store, PathBuf) {
        let dir = std::env::temp_dir().join(format!(
            "tt-model-{name}-{}",
            crate::bridge::event_store::now_ms()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let store = Store::open(dir.join("t.db")).unwrap();
        (store, dir)
    }

    fn add_req(name: &str, is_default: bool) -> ModelAddReq {
        ModelAddReq {
            name: name.into(),
            source: "openai_compat".into(),
            base_url: "http://127.0.0.1:9999/v1".into(),
            api_key: Some("sk-test".into()),
            model_name: "qwen-plus".into(),
            context_window: 32768,
            is_default,
            agent_binding: String::new(),
            force_local: false,
        }
    }

    #[test]
    fn test_add_and_get_roundtrip() {
        let (store, dir) = temp_store("add");
        let cfg = add(&store, add_req("通义千问", false)).unwrap();
        assert!(!cfg.id.is_empty());
        assert_eq!(cfg.name, "通义千问");
        assert!(cfg.has_api_key); // keyring/降级内存已存
        assert_eq!(cfg.api_key_encrypted, ""); // 密钥不出库
        let fetched = get(&store, &cfg.id).unwrap().unwrap();
        assert_eq!(fetched.model_name, "qwen-plus");
        delete(&store, &cfg.id).unwrap();
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_add_validation_empty_name() {
        let (store, dir) = temp_store("valid");
        let mut req = add_req("x", false);
        req.name = "  ".into();
        assert!(add(&store, req).is_err());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_list_and_update() {
        let (store, dir) = temp_store("list");
        let cfg = add(&store, add_req("m1", false)).unwrap();
        assert_eq!(list(&store).unwrap().len(), 1);
        let updated = update(
            &store,
            ModelUpdateReq {
                id: cfg.id.clone(),
                name: Some("m1-renamed".into()),
                source: None,
                base_url: None,
                api_key: None, // 不动密钥
                model_name: Some("qwen-max".into()),
                context_window: Some(65536),
                agent_binding: None,
                force_local: None,
            },
        )
        .unwrap();
        assert_eq!(updated.name, "m1-renamed");
        assert_eq!(updated.model_name, "qwen-max");
        assert!(updated.has_api_key); // 未传 api_key，密钥保留
        delete(&store, &cfg.id).unwrap();
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_update_clears_key_with_empty_string() {
        let (store, dir) = temp_store("clearkey");
        let cfg = add(&store, add_req("m1", false)).unwrap();
        update(
            &store,
            ModelUpdateReq {
                id: cfg.id.clone(),
                name: None,
                source: None,
                base_url: None,
                api_key: Some("".into()), // 空串=清除
                model_name: None,
                context_window: None,
                agent_binding: None,
                force_local: None,
            },
        )
        .unwrap();
        assert!(!get(&store, &cfg.id).unwrap().unwrap().has_api_key);
        delete(&store, &cfg.id).unwrap();
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_set_default_exclusive() {
        let (store, dir) = temp_store("default");
        let a = add(&store, add_req("a", true)).unwrap();
        let b = add(&store, add_req("b", false)).unwrap();
        assert!(get(&store, &a.id).unwrap().unwrap().is_default);
        set_default(&store, &b.id).unwrap();
        assert!(!get(&store, &a.id).unwrap().unwrap().is_default);
        assert!(get(&store, &b.id).unwrap().unwrap().is_default);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_delete_missing_errors() {
        let (store, dir) = temp_store("delmiss");
        assert!(delete(&store, "no-such").is_err());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[tokio::test]
    async fn test_test_model_empty_base_url() {
        let resp = test(ModelTestReq {
            base_url: "".into(),
            api_key: "k".into(),
            model_name: "m".into(),
            context_window: 4096,
            id: None,
        })
        .await;
        assert!(!resp.ok);
        assert!(resp.error.unwrap().contains("MODEL_CONNECTION_FAILED"));
    }

    #[tokio::test]
    async fn test_test_model_unreachable() {
        // 不存在的端口：连接失败路径
        let resp = test(ModelTestReq {
            base_url: "http://127.0.0.1:1/v1".into(),
            api_key: "k".into(),
            model_name: "m".into(),
            context_window: 4096,
            id: None,
        })
        .await;
        assert!(!resp.ok);
        assert!(resp.latency_ms < 10_000);
    }
}
