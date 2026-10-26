//! Model Hub 服务：CRUD + 连通性自测
//!
//! 契约对齐 shared/types.ts ModelConfig / contracts.ts ModelTestReq/Resp。
//! API Key 仅存 keyring（vault），DB 保存 hasApiKey 引用标记。

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

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
    /// 每 100 万输入 token 单价；None=未配置（不按 0 元计价）
    pub price_input_per_m: Option<f64>,
    /// 每 100 万输出 token 单价；None=未配置
    pub price_output_per_m: Option<f64>,
    pub price_currency: String,
    /// 有序故障转移链（模型 id，最多 3 个）
    pub fallback_ids: Vec<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub last_used_at: Option<String>,
    pub created_at: String,
    pub updated_at: String,
}

/// 价格配置：输入/输出单价必须同时给出或同时省略（省略=未配置，不是免费）
#[derive(Debug, Clone, Default, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ModelPricesReq {
    #[serde(default)]
    pub input_per_m: Option<f64>,
    #[serde(default)]
    pub output_per_m: Option<f64>,
    #[serde(default = "default_currency")]
    pub currency: String,
}

#[derive(Debug, Clone, Default, Deserialize)]
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
    #[serde(default)]
    pub prices: Option<ModelPricesReq>,
    #[serde(default)]
    pub fallback_ids: Vec<String>,
}

fn default_context_window() -> i64 {
    4096
}

fn default_currency() -> String {
    "CNY".into()
}

#[derive(Debug, Clone, Default, Deserialize)]
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
    /// None=不动；Some(输入输出都为 None)=清除单价回到「未配置」
    pub prices: Option<ModelPricesReq>,
    /// None=不动；Some(空数组)=清空故障转移链
    pub fallback_ids: Option<Vec<String>>,
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
        is_default, agent_binding, force_local, created_at, updated_at,
        price_input_per_m, price_output_per_m, price_currency, fallback_ids FROM model_configs";

/// 行 → ModelConfig（fallback_ids 损坏时显式报错，不静默丢掉转移链）
fn config_from_row(store: &Store, row: &rusqlite::Row<'_>) -> Result<ModelConfig, String> {
    let get_str = |i: usize| row.get::<_, String>(i).map_err(|e| e.to_string());
    let get_i64 = |i: usize| row.get::<_, i64>(i).map_err(|e| e.to_string());
    let get_f64 = |i: usize| row.get::<_, Option<f64>>(i).map_err(|e| e.to_string());
    let id = get_str(0)?;
    let fallback_ids: Vec<String> = serde_json::from_str(&get_str(14)?).map_err(|e| {
        format!("MODEL_CONFIG_INVALID: 模型 {id} 的 fallback_ids 不是合法 JSON 数组（{e}）")
    })?;
    Ok(ModelConfig {
        has_api_key: store.vault().get_secret(&id).is_some(),
        id,
        name: get_str(1)?,
        source: get_str(2)?,
        base_url: get_str(3)?,
        api_key_encrypted: String::new(),
        model_name: get_str(4)?,
        context_window: get_i64(5)?,
        is_default: get_i64(6)? != 0,
        agent_binding: get_str(7)?,
        force_local: get_i64(8)? != 0,
        price_input_per_m: get_f64(11)?,
        price_output_per_m: get_f64(12)?,
        price_currency: get_str(13)?,
        fallback_ids,
        last_used_at: None,
        created_at: get_str(9)?,
        updated_at: get_str(10)?,
    })
}

/// 列出全部模型配置
pub fn list(store: &Store) -> Result<Vec<ModelConfig>, String> {
    store.with_conn(|c| {
        let mut stmt = c.prepare(SELECT_COLS).map_err(|e| e.to_string())?;
        let mut rows = stmt.query([]).map_err(|e| e.to_string())?;
        let mut out = Vec::new();
        while let Some(row) = rows.next().map_err(|e| e.to_string())? {
            out.push(config_from_row(store, row)?);
        }
        Ok(out)
    })
}

fn clear_default(c: &rusqlite::Connection) -> Result<(), String> {
    c.execute("UPDATE model_configs SET is_default = 0", [])
        .map(|_| ())
        .map_err(|e| e.to_string())
}

/// 单价规范化：两项同时配置或同时省略；省略=未配置，绝不按 0 元计价
fn normalize_prices(prices: Option<&ModelPricesReq>) -> Result<Option<(f64, f64, String)>, String> {
    let Some(p) = prices else { return Ok(None) };
    match (p.input_per_m, p.output_per_m) {
        (None, None) => Ok(None),
        (Some(i), Some(o)) => {
            if !i.is_finite() || !o.is_finite() || i < 0.0 || o < 0.0 {
                return Err("MODEL_PRICE_INVALID: 单价必须是非负有限数字".into());
            }
            let currency = p.currency.trim().to_ascii_uppercase();
            if currency.len() != 3 || !currency.chars().all(|c| c.is_ascii_uppercase()) {
                return Err(
                    "MODEL_PRICE_CURRENCY_INVALID: 币种必须是 3 位字母代码（如 CNY / USD）".into(),
                );
            }
            Ok(Some((i, o, currency)))
        }
        _ => Err("MODEL_PRICE_INCOMPLETE: 输入与输出单价必须同时配置或同时留空".into()),
    }
}

/// 故障转移链校验：最多 3 个、去首尾空白、不重复、不指向自身、目标必须存在
fn normalize_fallback_ids(
    store: &Store,
    self_id: Option<&str>,
    ids: &[String],
) -> Result<Vec<String>, String> {
    if ids.len() > 3 {
        return Err("MODEL_FALLBACK_LIMIT: 故障转移链最多 3 个模型".into());
    }
    let mut out: Vec<String> = Vec::new();
    for raw in ids {
        let id = raw.trim();
        if id.is_empty() {
            return Err("MODEL_FALLBACK_INVALID: 故障转移链包含空 id".into());
        }
        if Some(id) == self_id {
            return Err("MODEL_FALLBACK_SELF: 故障转移链不能指向自身".into());
        }
        if out.iter().any(|v| v == id) {
            return Err(format!("MODEL_FALLBACK_DUPLICATE: 故障转移链重复 {id}"));
        }
        if get(store, id)?.is_none() {
            return Err(format!("MODEL_FALLBACK_NOT_FOUND: 未找到转移目标 {id}"));
        }
        out.push(id.to_string());
    }
    Ok(out)
}

fn prices_columns(prices: &Option<(f64, f64, String)>) -> (Option<f64>, Option<f64>, String) {
    match prices {
        Some((i, o, c)) => (Some(*i), Some(*o), c.clone()),
        None => (None, None, default_currency()),
    }
}

fn ids_json(ids: &[String]) -> String {
    serde_json::to_string(ids).unwrap_or_else(|_| "[]".into())
}

/// 添加模型（密钥存 vault；is_default 时清除其他默认）
pub fn add(store: &Store, req: ModelAddReq) -> Result<ModelConfig, String> {
    if req.name.trim().is_empty() || req.model_name.trim().is_empty() {
        return Err("VALIDATION_ERROR: name/modelName 不能为空".into());
    }
    let prices = normalize_prices(req.prices.as_ref())?;
    let fallback_ids = normalize_fallback_ids(store, None, &req.fallback_ids)?;
    let (price_in, price_out, currency) = prices_columns(&prices);
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
                is_default, agent_binding, force_local, price_input_per_m, price_output_per_m,
                price_currency, fallback_ids, created_at, updated_at)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15)",
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
                price_in,
                price_out,
                currency,
                ids_json(&fallback_ids),
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
            Some(row) => Ok(Some(config_from_row(store, row)?)),
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
    if let Some(p) = req.prices.as_ref() {
        match normalize_prices(Some(p))? {
            Some((i, o, c)) => {
                next.price_input_per_m = Some(i);
                next.price_output_per_m = Some(o);
                next.price_currency = c;
            }
            None => {
                next.price_input_per_m = None;
                next.price_output_per_m = None;
            }
        }
    }
    if let Some(ids) = req.fallback_ids.as_ref() {
        next.fallback_ids = normalize_fallback_ids(store, Some(&req.id), ids)?;
    }
    let now = now_iso();
    store.with_conn(|c| {
        c.execute(
            "UPDATE model_configs SET name=?2, source=?3, base_url=?4, model_name=?5,
                context_window=?6, agent_binding=?7, force_local=?8, price_input_per_m=?9,
                price_output_per_m=?10, price_currency=?11, fallback_ids=?12, updated_at=?13
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
                next.price_input_per_m,
                next.price_output_per_m,
                next.price_currency,
                ids_json(&next.fallback_ids),
                now,
            ],
        )
        .map(|_| ())
        .map_err(|e| format!("MODEL_WRITE_FAILED: {e}"))
    })?;
    get(store, &req.id)?.ok_or_else(|| "MODEL_NOT_FOUND: 更新后读取失败".into())
}

/// 从其他模型的故障转移链里摘掉已删除的 id（只改引用，不动其他字段）
fn strip_fallback_refs(c: &rusqlite::Connection, removed_id: &str) -> Result<(), String> {
    let updates: Vec<(String, Vec<String>)> = {
        let mut stmt = c
            .prepare("SELECT id, fallback_ids FROM model_configs")
            .map_err(|e| e.to_string())?;
        let rows = stmt
            .query_map([], |r| {
                Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?))
            })
            .map_err(|e| e.to_string())?;
        let mut updates = Vec::new();
        for row in rows {
            let (id, raw) = row.map_err(|e| e.to_string())?;
            // 损坏的行不擅自改写（读取时会显式报 MODEL_CONFIG_INVALID）
            let Ok(mut ids) = serde_json::from_str::<Vec<String>>(&raw) else {
                continue;
            };
            let before = ids.len();
            ids.retain(|v| v != removed_id);
            if ids.len() != before {
                updates.push((id, ids));
            }
        }
        updates
    };
    for (id, ids) in updates {
        c.execute(
            "UPDATE model_configs SET fallback_ids = ?2 WHERE id = ?1",
            rusqlite::params![id, ids_json(&ids)],
        )
        .map_err(|e| format!("MODEL_WRITE_FAILED: {e}"))?;
    }
    Ok(())
}

/// 删除模型（连同 vault 密钥；其他模型转移链里的引用一并摘除）
pub fn delete(store: &Store, id: &str) -> Result<(), String> {
    let removed = store.with_conn(|c| {
        let removed = c
            .execute("DELETE FROM model_configs WHERE id = ?1", [id])
            .map_err(|e| format!("MODEL_WRITE_FAILED: {e}"))?;
        if removed > 0 {
            strip_fallback_refs(c, id)?;
        }
        Ok(removed)
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

/// 单个可调用模型的运行时配置（主模型与每个转移目标共用同一形状，不含嵌套转移链）
fn candidate_json(store: &Store, m: &ModelConfig) -> Result<Value, String> {
    let model_name = m.model_name.trim();
    if model_name.is_empty() {
        return Err(format!(
            "MODEL_INCOMPLETE: 模型「{}」缺少服务端模型名",
            m.name
        ));
    }
    let base_url = m.base_url.trim().to_string();
    let api_key = store.vault().get_secret(&m.id).unwrap_or_default();
    let host = crate::security::policy::host_of(&base_url);
    if !base_url.is_empty()
        && api_key.trim().is_empty()
        && !crate::security::policy::is_local_host(&host)
    {
        return Err(format!(
            "MODEL_KEY_MISSING: 模型「{}」未配置 API Key",
            m.name
        ));
    }
    // 单价必须成对出现；只有一边有效时按「未配置」下发，不拿半套价格计价
    let prices = match (m.price_input_per_m, m.price_output_per_m) {
        (Some(i), Some(o)) => {
            json!({ "inputPerM": i, "outputPerM": o, "currency": m.price_currency })
        }
        _ => Value::Null,
    };
    Ok(json!({
        "modelId": m.id,
        "displayName": m.name,
        "baseUrl": base_url,
        "apiKey": api_key,
        "modelName": model_name,
        "source": m.source,
        "contextWindow": if m.context_window > 0 { json!(m.context_window) } else { Value::Null },
        "prices": prices,
    }))
}

/// 解析本轮要下发给引擎的模型调用配置（显式 id 优先，否则默认模型）。
///
/// 渲染层只传 `modelId`，端点与密钥由 Rust 从 SQLite + keyring 解析，密钥不再经过
/// 渲染层。三条失败语义都显式报错，不静默改用默认模型或环境变量：
/// - 选了但不存在 → MODEL_NOT_FOUND
/// - 缺服务端模型名 → MODEL_INCOMPLETE
/// - 外部端点缺密钥 → MODEL_KEY_MISSING（本地回环允许无密钥）
/// 未选且没有默认模型时返回 None，由引擎按环境变量兜底。
///
/// 故障转移目标逐条解析；不可用（已删除/缺密钥/接入方式不同）的跳过并写进
/// `fallbackNotes`，不阻断主模型，也不静默假装链路完整。
pub fn runtime_config(store: &Store, id: Option<&str>) -> Result<Option<Value>, String> {
    let picked = match id.map(str::trim).filter(|s| !s.is_empty()) {
        Some(model_id) => Some(
            get(store, model_id)?
                .ok_or_else(|| format!("MODEL_NOT_FOUND: 未找到模型 {model_id}"))?,
        ),
        None => list(store)?.into_iter().find(|m| m.is_default),
    };
    let Some(m) = picked else { return Ok(None) };
    let mut primary = candidate_json(store, &m)?;
    let primary_custom = !m.base_url.trim().is_empty();

    let mut fallbacks = Vec::new();
    let mut notes: Vec<String> = Vec::new();
    for fb_id in m.fallback_ids.iter().take(3) {
        if fb_id == &m.id {
            notes.push(format!("转移目标与主模型重复（{}），已跳过", m.name));
            continue;
        }
        let Some(fb) = get(store, fb_id)? else {
            notes.push(format!("转移目标 {fb_id} 不存在（可能已被删除），已跳过"));
            continue;
        };
        if primary_custom != !fb.base_url.trim().is_empty() {
            notes.push(format!(
                "转移目标「{}」的接入方式（自定义端点/环境变量）与主模型不同，无法共用同一格式化器，已跳过",
                fb.name
            ));
            continue;
        }
        match candidate_json(store, &fb) {
            Ok(v) => fallbacks.push(v),
            Err(e) => notes.push(format!("转移目标「{}」不可用：{e}，已跳过", fb.name)),
        }
    }
    if m.fallback_ids.len() > 3 {
        notes.push(format!(
            "故障转移链共 {} 个目标，仅前 3 个生效",
            m.fallback_ids.len()
        ));
    }

    if let Some(obj) = primary.as_object_mut() {
        obj.insert("fallbacks".into(), json!(fallbacks));
        obj.insert("fallbackNotes".into(), json!(notes));
    }
    Ok(Some(primary))
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
            ..Default::default()
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
                ..Default::default()
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
                ..Default::default()
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

    #[test]
    fn test_runtime_config_resolves_selected_and_default() {
        let (store, dir) = temp_store("runtime");
        let a = add(&store, add_req("a", false)).unwrap();
        let b = add(&store, add_req("b", true)).unwrap();
        update(
            &store,
            ModelUpdateReq {
                id: b.id.clone(),
                name: None,
                source: None,
                base_url: None,
                api_key: None,
                model_name: Some("qwen-max".into()),
                context_window: None,
                agent_binding: None,
                force_local: None,
                ..Default::default()
            },
        )
        .unwrap();

        // 显式选择：端点与密钥来自该条目（密钥不出参数字面量，从这里取回原值）
        let picked = runtime_config(&store, Some(&a.id)).unwrap().unwrap();
        assert_eq!(picked["baseUrl"], "http://127.0.0.1:9999/v1");
        assert_eq!(picked["modelName"], "qwen-plus");
        assert_eq!(picked["apiKey"], "sk-test");
        assert_eq!(picked["source"], "openai_compat");
        // 上下文窗口必须下发：桌面压缩此前只能退回 8192 默认值
        assert_eq!(picked["contextWindow"], 32768);
        assert_eq!(picked["modelId"], a.id);
        assert_eq!(picked["displayName"], "a");
        assert_eq!(picked["prices"], Value::Null);
        assert!(picked["fallbacks"].as_array().unwrap().is_empty());
        assert!(picked["fallbackNotes"].as_array().unwrap().is_empty());

        // Auto：解析 is_default 条目
        let auto = runtime_config(&store, None).unwrap().unwrap();
        assert_eq!(auto["modelName"], "qwen-max");

        delete(&store, &a.id).unwrap();
        delete(&store, &b.id).unwrap();
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_runtime_config_without_default_returns_none() {
        let (store, dir) = temp_store("runtime-empty");
        assert!(runtime_config(&store, None).unwrap().is_none());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_runtime_config_honest_errors() {
        let (store, dir) = temp_store("runtime-errors");
        // 选了不存在的模型：不得静默改用默认模型
        let err = runtime_config(&store, Some("no-such")).unwrap_err();
        assert!(err.starts_with("MODEL_NOT_FOUND"), "{err}");

        // 外部端点缺密钥：拒绝下发，而不是带空密钥去撞 401
        let mut remote = add_req("remote", false);
        remote.base_url = "https://api.example.com/v1".into();
        remote.api_key = None;
        let remote = add(&store, remote).unwrap();
        let err = runtime_config(&store, Some(&remote.id)).unwrap_err();
        assert!(err.starts_with("MODEL_KEY_MISSING"), "{err}");

        // 本地回环允许无密钥（Ollama 等本地服务）
        let mut local = add_req("local", false);
        local.base_url = "http://127.0.0.1:11434/v1".into();
        local.api_key = None;
        let local = add(&store, local).unwrap();
        let cfg = runtime_config(&store, Some(&local.id)).unwrap().unwrap();
        assert_eq!(cfg["apiKey"], "");
        assert_eq!(cfg["baseUrl"], "http://127.0.0.1:11434/v1");

        // 缺服务端模型名：显式报 MODEL_INCOMPLETE
        update(
            &store,
            ModelUpdateReq {
                id: local.id.clone(),
                name: None,
                source: None,
                base_url: None,
                api_key: None,
                model_name: Some("  ".into()),
                context_window: None,
                agent_binding: None,
                force_local: None,
                ..Default::default()
            },
        )
        .unwrap();
        let err = runtime_config(&store, Some(&local.id)).unwrap_err();
        assert!(err.starts_with("MODEL_INCOMPLETE"), "{err}");

        delete(&store, &remote.id).unwrap();
        delete(&store, &local.id).unwrap();
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_prices_and_fallback_chain_roundtrip() {
        let (store, dir) = temp_store("pricing");
        let a = add(&store, add_req("主模型", false)).unwrap();
        let b = add(&store, add_req("备用1", false)).unwrap();
        let c = add(&store, add_req("备用2", false)).unwrap();

        let updated = update(
            &store,
            ModelUpdateReq {
                id: a.id.clone(),
                prices: Some(ModelPricesReq {
                    input_per_m: Some(0.8),
                    output_per_m: Some(2.0),
                    currency: "usd".into(), // 小写自动归一成 USD
                }),
                fallback_ids: Some(vec![b.id.clone(), c.id.clone()]),
                ..Default::default()
            },
        )
        .unwrap();
        assert_eq!(updated.price_input_per_m, Some(0.8));
        assert_eq!(updated.price_output_per_m, Some(2.0));
        assert_eq!(updated.price_currency, "USD");
        assert_eq!(updated.fallback_ids, vec![b.id.clone(), c.id.clone()]);

        // 读取（列表/单条）口径一致
        let fetched = get(&store, &a.id).unwrap().unwrap();
        assert_eq!(fetched.fallback_ids, vec![b.id.clone(), c.id.clone()]);
        assert_eq!(fetched.price_currency, "USD");
        assert_eq!(list(&store).unwrap().iter().find(|m| m.id == a.id).unwrap().price_input_per_m, Some(0.8));

        // 未传 prices/fallbackIds = 保持原值
        let untouched = update(
            &store,
            ModelUpdateReq {
                id: a.id.clone(),
                name: Some("改名".into()),
                ..Default::default()
            },
        )
        .unwrap();
        assert_eq!(untouched.price_input_per_m, Some(0.8));
        assert_eq!(untouched.fallback_ids.len(), 2);

        // 两项单价都留空 = 回到未配置；空数组 = 清空转移链
        let cleared = update(
            &store,
            ModelUpdateReq {
                id: a.id.clone(),
                prices: Some(ModelPricesReq::default()),
                fallback_ids: Some(vec![]),
                ..Default::default()
            },
        )
        .unwrap();
        assert!(cleared.price_input_per_m.is_none() && cleared.price_output_per_m.is_none());
        assert!(cleared.fallback_ids.is_empty());

        delete(&store, &a.id).unwrap();
        delete(&store, &b.id).unwrap();
        delete(&store, &c.id).unwrap();
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_prices_validation_rejects_half_or_negative() {
        let (store, dir) = temp_store("pricing-bad");
        let cfg = add(&store, add_req("m", false)).unwrap();

        let err = update(
            &store,
            ModelUpdateReq {
                id: cfg.id.clone(),
                prices: Some(ModelPricesReq {
                    input_per_m: Some(1.0),
                    output_per_m: None,
                    currency: "CNY".into(),
                }),
                ..Default::default()
            },
        )
        .unwrap_err();
        assert!(err.starts_with("MODEL_PRICE_INCOMPLETE"), "{err}");

        let err = update(
            &store,
            ModelUpdateReq {
                id: cfg.id.clone(),
                prices: Some(ModelPricesReq {
                    input_per_m: Some(-1.0),
                    output_per_m: Some(2.0),
                    currency: "CNY".into(),
                }),
                ..Default::default()
            },
        )
        .unwrap_err();
        assert!(err.starts_with("MODEL_PRICE_INVALID"), "{err}");

        let err = update(
            &store,
            ModelUpdateReq {
                id: cfg.id.clone(),
                prices: Some(ModelPricesReq {
                    input_per_m: Some(1.0),
                    output_per_m: Some(2.0),
                    currency: "C1Y".into(),
                }),
                ..Default::default()
            },
        )
        .unwrap_err();
        assert!(err.starts_with("MODEL_PRICE_CURRENCY_INVALID"), "{err}");

        // 0 元是合法的「免费」，不能和「未配置」混为一谈
        let free = update(
            &store,
            ModelUpdateReq {
                id: cfg.id.clone(),
                prices: Some(ModelPricesReq {
                    input_per_m: Some(0.0),
                    output_per_m: Some(0.0),
                    currency: "CNY".into(),
                }),
                ..Default::default()
            },
        )
        .unwrap();
        assert_eq!(free.price_input_per_m, Some(0.0));

        delete(&store, &cfg.id).unwrap();
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_fallback_validation_and_delete_cleanup() {
        let (store, dir) = temp_store("fallback");
        let a = add(&store, add_req("a", false)).unwrap();
        let b = add(&store, add_req("b", false)).unwrap();

        let err = update(
            &store,
            ModelUpdateReq {
                id: a.id.clone(),
                fallback_ids: Some(vec![a.id.clone()]),
                ..Default::default()
            },
        )
        .unwrap_err();
        assert!(err.starts_with("MODEL_FALLBACK_SELF"), "{err}");

        let err = update(
            &store,
            ModelUpdateReq {
                id: a.id.clone(),
                fallback_ids: Some(vec![b.id.clone(), b.id.clone()]),
                ..Default::default()
            },
        )
        .unwrap_err();
        assert!(err.starts_with("MODEL_FALLBACK_DUPLICATE"), "{err}");

        let err = update(
            &store,
            ModelUpdateReq {
                id: a.id.clone(),
                fallback_ids: Some(vec!["no-such".into()]),
                ..Default::default()
            },
        )
        .unwrap_err();
        assert!(err.starts_with("MODEL_FALLBACK_NOT_FOUND"), "{err}");

        let err = update(
            &store,
            ModelUpdateReq {
                id: a.id.clone(),
                fallback_ids: Some(vec!["x1".into(), "x2".into(), "x3".into(), b.id.clone()]),
                ..Default::default()
            },
        )
        .unwrap_err();
        assert!(err.starts_with("MODEL_FALLBACK_LIMIT"), "{err}");

        update(
            &store,
            ModelUpdateReq {
                id: a.id.clone(),
                fallback_ids: Some(vec![b.id.clone()]),
                ..Default::default()
            },
        )
        .unwrap();

        // 删除转移目标：引用自动摘除，主模型本身不受影响
        delete(&store, &b.id).unwrap();
        let cfg = get(&store, &a.id).unwrap().unwrap();
        assert!(cfg.fallback_ids.is_empty(), "被删除目标的引用必须摘除");

        delete(&store, &a.id).unwrap();
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_runtime_config_includes_usable_fallback_and_notes() {
        let (store, dir) = temp_store("runtime-fallback");
        let primary = add(&store, add_req("主模型", false)).unwrap();
        let backup = add(&store, add_req("备份", false)).unwrap();
        let broken = {
            let mut req = add_req("缺密钥", false);
            req.base_url = "https://api.example.com/v1".into();
            add(&store, req).unwrap()
        };
        // 清掉密钥：外部端点缺密钥时不能作为转移目标
        update(
            &store,
            ModelUpdateReq {
                id: broken.id.clone(),
                api_key: Some("".into()),
                ..Default::default()
            },
        )
        .unwrap();
        let env_model = {
            let mut req = add_req("环境变量模型", false);
            req.base_url = "".into();
            req.api_key = None;
            add(&store, req).unwrap()
        };
        update(
            &store,
            ModelUpdateReq {
                id: primary.id.clone(),
                prices: Some(ModelPricesReq {
                    input_per_m: Some(1.0),
                    output_per_m: Some(2.0),
                    currency: "CNY".into(),
                }),
                fallback_ids: Some(vec![
                    backup.id.clone(),
                    broken.id.clone(),
                    env_model.id.clone(),
                ]),
                ..Default::default()
            },
        )
        .unwrap();

        let cfg = runtime_config(&store, Some(&primary.id)).unwrap().unwrap();
        assert_eq!(cfg["contextWindow"], 32768);
        assert_eq!(cfg["prices"]["currency"], "CNY");
        assert_eq!(cfg["prices"]["inputPerM"], 1.0);
        let fallbacks = cfg["fallbacks"].as_array().unwrap();
        assert_eq!(fallbacks.len(), 1, "只有接入方式一致且可用的目标进入转移链");
        assert_eq!(fallbacks[0]["modelId"], backup.id);
        assert_eq!(fallbacks[0]["modelName"], "qwen-plus");
        assert_eq!(fallbacks[0]["contextWindow"], 32768);
        assert!(fallbacks[0].get("fallbacks").is_none(), "转移目标不再嵌套转移链");
        let notes = cfg["fallbackNotes"].as_array().unwrap();
        assert_eq!(notes.len(), 2, "{notes:?}");
        assert!(notes.iter().any(|n| n.as_str().unwrap().contains("API Key")));
        assert!(notes.iter().any(|n| n.as_str().unwrap().contains("接入方式")));

        delete(&store, &primary.id).unwrap();
        delete(&store, &backup.id).unwrap();
        delete(&store, &broken.id).unwrap();
        delete(&store, &env_model.id).unwrap();
        let _ = std::fs::remove_dir_all(&dir);
    }
}
