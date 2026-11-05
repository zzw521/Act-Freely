//! 桌面用量上报（缺陷 D-43 的客户端部分）
//!
//! 服务端 `POST /api/v1/usage/report` 接收「设备 × 来源 × 用户 × 模型 × 日」的累计值，
//! 按主键幂等覆盖。桌面只负责把自己 `model_usage` 台账按行推上去，不解析、不合并：
//! 合并视图与去重规则都留在服务端（`origin=desktop` 的引擎行会被排除）。
//!
//! 边界：
//! - 只读本地 SQLite，由 Rust 主进程执行；不把库文件或路径交给服务端；
//! - 只上报当前登录账号（JWT `sub`）的用量，未登录直接拒绝；
//! - 设备 id 首次生成后落在 `settings_kv`，避免多台设备互相覆盖；
//! - 单次最多 500 行，超出分批；服务端上限 2000 行。

use std::collections::{BTreeMap, HashMap};

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

use crate::auth;
use crate::store::{settings, Store};

/// settings_kv 中的设备 id（首次上报时生成，跨重启稳定）
pub const DEVICE_KEY: &str = "usage.deviceId";
/// 服务端单次上报上限 2000；这里留余量分批，失败时最多影响一批
pub const BATCH_SIZE: usize = 500;
pub const SCHEMA_VERSION: i64 = 1;

/// 上报行：字段名与服务端 `UsageRowIn`、引擎导出行同名同义
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct UsageUploadRow {
    pub sub: String,
    pub model_name: String,
    pub day: String,
    #[serde(default)]
    pub origin: String,
    #[serde(default)]
    pub org: String,
    pub prompt_tokens: i64,
    pub completion_tokens: i64,
    pub tokens_used: i64,
    pub calls: i64,
    pub priced_calls: i64,
    pub unpriced_calls: i64,
    pub latency_ms_total: i64,
    pub latency_samples: i64,
    pub costs: BTreeMap<String, i64>,
    pub last_used_at_ms: i64,
}

/// 上报结果（命令返回体，前端如实展示）
#[derive(Debug, Clone, PartialEq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct UsageUploadResult {
    pub uploaded: bool,
    pub rows: usize,
    pub accepted: usize,
    pub device_id: String,
    pub message: String,
}

/// 设备 id：优先读设置，缺省生成 UUID 并落库（生成失败不阻断，回退 local-device）
pub fn device_id(store: &Store) -> String {
    if let Ok(Some(id)) = settings::get(store, DEVICE_KEY) {
        let trimmed = id.trim().to_string();
        if !trimmed.is_empty() {
            return trimmed;
        }
    }
    let id = uuid::Uuid::new_v4().simple().to_string();
    if let Err(e) = settings::set(store, DEVICE_KEY, &id) {
        eprintln!("[usage] 设备 id 落库失败（本次回退 local-device）: {e}");
        return "local-device".to_string();
    }
    id
}

/// 收集本地台账：按「模型 × 日」一行，成本按币种合并。
pub fn collect_rows(store: &Store, sub: &str, org: &str) -> Result<Vec<UsageUploadRow>, String> {
    let sub = sub.trim();
    if sub.is_empty() {
        return Err("USAGE_UPLOAD_LOGIN_REQUIRED: 当前账号缺少 uid，无法上报".into());
    }
    store.with_conn(|c| {
        let mut costs: HashMap<(String, String), (BTreeMap<String, i64>, i64)> = HashMap::new();
        {
            let mut stmt = c
                .prepare(
                    "SELECT model_name, day, currency, SUM(cost_micros), SUM(priced_calls) \
                     FROM model_usage_cost GROUP BY model_name, day, currency",
                )
                .map_err(|e| format!("USAGE_UPLOAD_FAILED: {e}"))?;
            let rows = stmt
                .query_map([], |r| {
                    Ok((
                        r.get::<_, String>(0)?,
                        r.get::<_, String>(1)?,
                        r.get::<_, String>(2)?,
                        r.get::<_, i64>(3)?,
                        r.get::<_, i64>(4)?,
                    ))
                })
                .map_err(|e| format!("USAGE_UPLOAD_FAILED: {e}"))?;
            for row in rows {
                let (model, day, currency, micros, priced) =
                    row.map_err(|e| format!("USAGE_UPLOAD_FAILED: {e}"))?;
                let entry = costs.entry((model, day)).or_default();
                entry.0.insert(currency.to_uppercase(), micros.max(0));
                entry.1 += priced.max(0);
            }
        }

        let mut stmt = c
            .prepare(
                "SELECT model_name, day, prompt_tokens, completion_tokens, calls, \
                        last_used_at_ms, latency_ms_total, latency_samples \
                 FROM model_usage ORDER BY day ASC, model_name ASC",
            )
            .map_err(|e| format!("USAGE_UPLOAD_FAILED: {e}"))?;
        let rows = stmt
            .query_map([], |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, String>(1)?,
                    r.get::<_, i64>(2)?,
                    r.get::<_, i64>(3)?,
                    r.get::<_, i64>(4)?,
                    r.get::<_, i64>(5)?,
                    r.get::<_, i64>(6)?,
                    r.get::<_, i64>(7)?,
                ))
            })
            .map_err(|e| format!("USAGE_UPLOAD_FAILED: {e}"))?
            .collect::<Result<Vec<_>, _>>()
            .map_err(|e| format!("USAGE_UPLOAD_FAILED: {e}"))?;

        let mut out = Vec::with_capacity(rows.len());
        for (model, day, prompt, completion, calls, last_used, latency, samples) in rows {
            let (cost_map, priced) = costs
                .remove(&(model.clone(), day.clone()))
                .unwrap_or_default();
            let priced = priced.clamp(0, calls.max(0));
            out.push(UsageUploadRow {
                sub: sub.to_string(),
                model_name: model,
                day,
                origin: "desktop".to_string(),
                org: org.trim().to_string(),
                prompt_tokens: prompt,
                completion_tokens: completion,
                tokens_used: prompt + completion,
                calls,
                priced_calls: priced,
                unpriced_calls: (calls - priced).max(0),
                latency_ms_total: latency,
                latency_samples: samples,
                costs: cost_map,
                last_used_at_ms: last_used,
            });
        }
        Ok(out)
    })
}

async fn post_batch(
    base: &str,
    token: &str,
    device: &str,
    rows: &[UsageUploadRow],
) -> Result<usize, String> {
    let payload = json!({
        "source": "desktop",
        "deviceId": device,
        "schemaVersion": SCHEMA_VERSION,
        "rows": rows,
    });
    let client = auth::service::client_for_base(base);
    let resp = client
        .post(format!("{base}/api/v1/usage/report"))
        .bearer_auth(token)
        .json(&payload)
        .timeout(std::time::Duration::from_secs(20))
        .send()
        .await
        .map_err(|e| {
            format!(
                "USAGE_UPLOAD_FAILED: 无法连接账号服务（{base}）：{}",
                auth::service::network_reason(&e)
            )
        })?;
    let status = resp.status();
    let body: Value = resp
        .json()
        .await
        .map_err(|e| format!("USAGE_UPLOAD_FAILED: 服务端响应无法解析（{e}）"))?;
    if status == reqwest::StatusCode::UNAUTHORIZED {
        return Err("USAGE_UPLOAD_AUTH_EXPIRED: 登录态已失效，请重新登录后再上报".into());
    }
    if !status.is_success() || body.get("ok").and_then(|v| v.as_bool()) != Some(true) {
        let code = body
            .get("code")
            .and_then(|v| v.as_str())
            .unwrap_or("USAGE_UPLOAD_FAILED");
        let message = body
            .get("message")
            .and_then(|v| v.as_str())
            .unwrap_or("服务端拒绝了用量上报");
        return Err(format!("{code}: {message}"));
    }
    Ok(body
        .get("accepted")
        .and_then(|v| v.as_u64())
        .unwrap_or(rows.len() as u64) as usize)
}

/// 分批上报（供登录态解析后的调用；401 时强制刷新 token 再试一次）。
pub async fn upload_rows(
    store: &Store,
    base: &str,
    token: &str,
    device: &str,
    rows: &[UsageUploadRow],
) -> Result<(usize, usize), String> {
    if rows.is_empty() {
        return Ok((0, 0));
    }
    auth::service::validate_server_url(base)?;
    let mut token = token.to_string();
    let mut accepted = 0;
    for chunk in rows.chunks(BATCH_SIZE) {
        match post_batch(base, &token, device, chunk).await {
            Ok(n) => accepted += n,
            Err(e) if e.starts_with("USAGE_UPLOAD_AUTH_EXPIRED") => {
                token = auth::service::force_access_token(store).await?;
                accepted += post_batch(base, &token, device, chunk).await?;
            }
            Err(e) => return Err(e),
        }
    }
    Ok((rows.len(), accepted))
}

/// 一次完整上报：登录态 → 收集台账 → 分批 POST。
pub async fn upload_session(store: &Store) -> Result<UsageUploadResult, String> {
    let profile = auth::service::load_profile(store).ok_or_else(|| {
        "USAGE_UPLOAD_LOGIN_REQUIRED: 请先登录账号，再把本地用量上报到合并视图".to_string()
    })?;
    let sub = profile
        .get("uid")
        .and_then(|v| v.as_str())
        .map(str::trim)
        .filter(|v| !v.is_empty())
        .ok_or_else(|| "USAGE_UPLOAD_LOGIN_REQUIRED: 账号资料缺少 uid".to_string())?
        .to_string();
    let org = profile
        .get("org")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    let device = device_id(store);
    let rows = collect_rows(store, &sub, &org)?;
    if rows.is_empty() {
        return Ok(UsageUploadResult {
            uploaded: false,
            rows: 0,
            accepted: 0,
            device_id: device,
            message: "本地没有可上报的用量".into(),
        });
    }
    let base = auth::service::auth_server_url(store);
    let token = auth::service::access_token(store).await?;
    let (total, accepted) = upload_rows(store, &base, &token, &device, &rows).await?;
    Ok(UsageUploadResult {
        uploaded: true,
        rows: total,
        accepted,
        device_id: device,
        message: format!("已上报 {accepted} / {total} 行"),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{Read, Write};
    use std::path::PathBuf;

    fn temp_store(name: &str) -> (Store, PathBuf) {
        let dir = std::env::temp_dir().join(format!(
            "tt-usage-upload-{name}-{}",
            crate::bridge::event_store::now_ms()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let store = Store::open(dir.join("t.db")).unwrap();
        (store, dir)
    }

    #[test]
    fn test_collect_rows_shape_and_cost() {
        let (store, dir) = temp_store("rows");
        crate::model::usage::record_sample(
            &store,
            "qwen-plus",
            120,
            80,
            Some(300),
            Some(1500),
            Some("cny"),
            true,
        )
        .unwrap();
        crate::model::usage::record(&store, "qwen-plus", 30, 20).unwrap();
        let rows = collect_rows(&store, "u-1", "org-1").unwrap();
        assert_eq!(rows.len(), 1);
        let row = &rows[0];
        assert_eq!(row.sub, "u-1");
        assert_eq!(row.org, "org-1");
        assert_eq!(row.origin, "desktop");
        assert_eq!(row.model_name, "qwen-plus");
        assert_eq!(row.prompt_tokens, 150);
        assert_eq!(row.completion_tokens, 100);
        assert_eq!(row.tokens_used, 250);
        assert_eq!(row.calls, 2);
        assert_eq!(row.priced_calls, 1);
        assert_eq!(row.unpriced_calls, 1);
        assert_eq!(row.costs.get("CNY"), Some(&1500));
        assert_eq!(row.latency_samples, 1);
        assert_eq!(row.latency_ms_total, 300);
        // 序列化字段名与服务端一致（camelCase/tokensUsed）
        let json = serde_json::to_value(row).unwrap();
        assert_eq!(json["modelName"], "qwen-plus");
        assert_eq!(json["tokensUsed"], 250);
        assert_eq!(json["origin"], "desktop");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_device_id_is_stable() {
        let (store, dir) = temp_store("device");
        let first = device_id(&store);
        assert!(!first.is_empty());
        assert_eq!(device_id(&store), first);
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// 假服务端：收一个请求，回固定 JSON，并把请求原文交给测试断言
    fn fake_server(response: &'static str) -> (String, std::thread::JoinHandle<String>) {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        let handle = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut raw = Vec::new();
            let mut buf = [0u8; 4096];
            let mut expected = None;
            loop {
                let n = stream.read(&mut buf).unwrap();
                if n == 0 {
                    break;
                }
                raw.extend_from_slice(&buf[..n]);
                if expected.is_none() {
                    if let Some(pos) = raw.windows(4).position(|w| w == b"\r\n\r\n") {
                        let head = String::from_utf8_lossy(&raw[..pos]).to_ascii_lowercase();
                        let len = head
                            .lines()
                            .find_map(|l| l.strip_prefix("content-length:"))
                            .and_then(|v| v.trim().parse::<usize>().ok())
                            .unwrap_or(0);
                        expected = Some(pos + 4 + len);
                    }
                }
                if expected.is_some_and(|e| raw.len() >= e) {
                    break;
                }
            }
            let body = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                response.len(),
                response
            );
            stream.write_all(body.as_bytes()).unwrap();
            String::from_utf8_lossy(&raw).to_string()
        });
        (format!("http://{addr}"), handle)
    }

    #[tokio::test]
    async fn test_upload_posts_expected_payload() {
        let (store, dir) = temp_store("upload");
        crate::model::usage::record(&store, "qwen-plus", 10, 5).unwrap();
        let rows = collect_rows(&store, "u-1", "org-1").unwrap();
        let (base, server) = fake_server(r#"{"ok":true,"accepted":1}"#);
        let (total, accepted) = upload_rows(&store, &base, "token-1", "dev-1", &rows)
            .await
            .unwrap();
        assert_eq!((total, accepted), (1, 1));
        let request = server.join().unwrap();
        assert!(request.starts_with("POST /api/v1/usage/report HTTP/1.1"), "{request}");
        assert!(request.to_ascii_lowercase().contains("authorization: bearer token-1"));
        assert!(request.contains("\"source\":\"desktop\""));
        assert!(request.contains("\"deviceId\":\"dev-1\""));
        assert!(request.contains("\"schemaVersion\":1"));
        assert!(request.contains("\"origin\":\"desktop\""));
        assert!(request.contains("\"modelName\":\"qwen-plus\""));
        assert!(request.contains("\"tokensUsed\":15"));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[tokio::test]
    async fn test_upload_without_rows_does_not_call_network() {
        let (store, dir) = temp_store("empty");
        let rows = collect_rows(&store, "u-1", "").unwrap();
        assert!(rows.is_empty());
        let (total, accepted) = upload_rows(&store, "http://127.0.0.1:9", "t", "d", &rows)
            .await
            .unwrap();
        assert_eq!((total, accepted), (0, 0));
        let _ = std::fs::remove_dir_all(&dir);
    }
}
