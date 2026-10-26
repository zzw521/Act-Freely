//! 零配置本地模型：探测本机 Ollama（原生 /api/tags，不走 OpenAI 兼容层）
//!
//! 检测只接受本机回环地址（默认 127.0.0.1:11434，可带自定义端口），
//! 不把任意 URL 变成渲染层可触发的探测通道。探测失败是一种可展示的事实，
//! 返回 `available=false` 与具体原因；只有地址非法才返回 Err。

use std::time::Duration;

use serde::{Deserialize, Serialize};

pub const DEFAULT_OLLAMA_BASE: &str = "http://127.0.0.1:11434";
const PROBE_TIMEOUT: Duration = Duration::from_secs(3);

#[derive(Debug, Clone, PartialEq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct LocalModelInfo {
    pub name: String,
    pub size_bytes: u64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub modified_at: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct LocalDetectResp {
    /// 当前通道是否具备本机探测能力（桌面端恒为 true；浏览器/Mock 由前端如实置 false）
    pub supported: bool,
    /// 是否真的拿到了 Ollama 的有效响应
    pub available: bool,
    /// Ollama 原生 API 根（不带 /v1）
    pub endpoint: String,
    /// 可直接保存为模型配置的 OpenAI 兼容 baseUrl
    pub openai_base_url: String,
    pub latency_ms: u64,
    pub models: Vec<LocalModelInfo>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

#[derive(Debug, Deserialize)]
struct OllamaTags {
    /// 必须是数组（含空数组）；缺失说明对面不是 Ollama，不能当成「已运行但无模型」
    models: Option<Vec<OllamaTag>>,
}

#[derive(Debug, Deserialize)]
struct OllamaTag {
    #[serde(default)]
    name: String,
    #[serde(default)]
    model: String,
    #[serde(default)]
    size: u64,
    #[serde(default)]
    modified_at: Option<String>,
}

/// 归一化探测根地址：只允许本机回环 + 根路径或 /v1。
fn normalize_root(input: &str) -> Result<String, String> {
    let raw = input.trim();
    let candidate = if raw.is_empty() { DEFAULT_OLLAMA_BASE } else { raw };
    let parsed = reqwest::Url::parse(candidate)
        .map_err(|e| format!("LOCAL_MODEL_INVALID: 地址无法解析（{e}）"))?;
    if !matches!(parsed.scheme(), "http" | "https") {
        return Err("LOCAL_MODEL_INVALID: 地址必须以 http:// 或 https:// 开头".into());
    }
    if !parsed.username().is_empty() || parsed.password().is_some() {
        return Err("LOCAL_MODEL_INVALID: 地址不能包含用户信息".into());
    }
    if parsed.query().is_some() || parsed.fragment().is_some() {
        return Err("LOCAL_MODEL_INVALID: 地址不能包含查询串或片段".into());
    }
    // url crate 对 IPv6 主机会带方括号（[::1]），比较前先去掉
    let host = parsed
        .host_str()
        .unwrap_or_default()
        .trim_start_matches('[')
        .trim_end_matches(']')
        .to_ascii_lowercase();
    if !matches!(host.as_str(), "localhost" | "127.0.0.1" | "::1") {
        return Err(format!(
            "LOCAL_MODEL_INVALID: 自动检测只支持本机回环地址（当前主机 {host}）"
        ));
    }
    let path = parsed.path().trim_end_matches('/');
    if !(path.is_empty() || path.eq_ignore_ascii_case("/v1")) {
        return Err("LOCAL_MODEL_INVALID: 地址只接受服务根或 /v1 结尾".into());
    }
    let mut root = parsed;
    root.set_path("");
    root.set_query(None);
    root.set_fragment(None);
    Ok(root.as_str().trim_end_matches('/').to_string())
}

fn parse_tags(body: &str) -> Result<Vec<LocalModelInfo>, String> {
    let parsed: OllamaTags =
        serde_json::from_str(body).map_err(|_| "返回内容不是合法 JSON".to_string())?;
    let tags = parsed
        .models
        .ok_or_else(|| "返回内容没有 models 数组".to_string())?;
    let mut out: Vec<LocalModelInfo> = Vec::new();
    for tag in tags {
        let name = if tag.name.trim().is_empty() {
            tag.model.trim().to_string()
        } else {
            tag.name.trim().to_string()
        };
        if name.is_empty() || out.iter().any(|m| m.name == name) {
            continue;
        }
        out.push(LocalModelInfo {
            name,
            size_bytes: tag.size,
            modified_at: tag
                .modified_at
                .map(|s| s.trim().to_string())
                .filter(|s| !s.is_empty()),
        });
    }
    Ok(out)
}

/// 真实探测本机 Ollama。不可达/非 Ollama 响应返回 available=false，不伪报成功。
pub async fn detect(base_url: Option<&str>) -> Result<LocalDetectResp, String> {
    let root = normalize_root(base_url.unwrap_or(""))?;
    let started = std::time::Instant::now();
    // 本机探测永远直连：系统代理常不 bypass 回环
    let client = crate::proxy::loopback_client();
    let result = tokio::time::timeout(
        PROBE_TIMEOUT,
        client.get(format!("{root}/api/tags")).send(),
    )
    .await;
    let latency_ms = started.elapsed().as_millis() as u64;
    let mut resp = LocalDetectResp {
        supported: true,
        available: false,
        endpoint: root.clone(),
        openai_base_url: format!("{root}/v1"),
        latency_ms,
        models: Vec::new(),
        error: None,
    };
    let body = match result {
        Ok(Ok(res)) if res.status().is_success() => {
            match tokio::time::timeout(PROBE_TIMEOUT, res.text()).await {
                Ok(Ok(text)) => text,
                _ => {
                    resp.error = Some("LOCAL_MODEL_NOT_RUNNING: 读取 Ollama 响应超时".into());
                    return Ok(resp);
                }
            }
        }
        Ok(Ok(res)) => {
            resp.error = Some(format!(
                "LOCAL_MODEL_NOT_RUNNING: Ollama 返回 HTTP {}",
                res.status().as_u16()
            ));
            return Ok(resp);
        }
        Ok(Err(e)) => {
            resp.error = Some(format!("LOCAL_MODEL_NOT_RUNNING: 无法连接 {root}（{e}）"));
            return Ok(resp);
        }
        Err(_) => {
            resp.error = Some(format!(
                "LOCAL_MODEL_NOT_RUNNING: 检测超时（{}s）",
                PROBE_TIMEOUT.as_secs()
            ));
            return Ok(resp);
        }
    };
    match parse_tags(&body) {
        Ok(models) => {
            resp.available = true;
            resp.models = models;
        }
        Err(reason) => {
            resp.error = Some(format!("LOCAL_MODEL_NOT_RUNNING: {reason}"));
        }
    }
    Ok(resp)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{Read, Write};

    #[test]
    fn normalize_defaults_and_strips_v1() {
        assert_eq!(normalize_root("").unwrap(), DEFAULT_OLLAMA_BASE);
        assert_eq!(
            normalize_root("http://127.0.0.1:11434/v1/").unwrap(),
            "http://127.0.0.1:11434"
        );
        assert_eq!(
            normalize_root("http://localhost:12345").unwrap(),
            "http://localhost:12345"
        );
        assert_eq!(
            normalize_root("http://[::1]:11434/v1").unwrap(),
            "http://[::1]:11434"
        );
    }

    #[test]
    fn normalize_rejects_remote_and_unsafe_paths() {
        for bad in [
            "https://api.example.com/v1",
            "http://192.168.1.8:11434",
            "ftp://127.0.0.1:11434",
            "http://user:pw@127.0.0.1:11434",
            "http://127.0.0.1:11434/api/tags",
            "http://127.0.0.1:11434/v1?x=1",
        ] {
            let err = normalize_root(bad).unwrap_err();
            assert!(
                err.starts_with("LOCAL_MODEL_INVALID"),
                "{bad} → {err}"
            );
        }
    }

    #[test]
    fn parse_tags_prefers_name_and_dedupes() {
        let body = r#"{"models":[
            {"name":"qwen2.5:14b","size":9000000000,"modified_at":"2026-10-01T00:00:00Z"},
            {"model":"llama3.2:latest","size":2000000000},
            {"name":"qwen2.5:14b","size":1},
            {"name":"  ","model":""}
        ]}"#;
        let models = parse_tags(body).unwrap();
        assert_eq!(models.len(), 2);
        assert_eq!(models[0].name, "qwen2.5:14b");
        assert_eq!(models[0].size_bytes, 9_000_000_000);
        assert!(models[0].modified_at.is_some());
        assert_eq!(models[1].name, "llama3.2:latest");
        assert!(models[1].modified_at.is_none());
    }

    #[test]
    fn parse_tags_requires_models_field() {
        assert!(parse_tags("{}").is_err());
        assert!(parse_tags(r#"{"models":[]}"#).unwrap().is_empty());
        assert!(parse_tags("not-json").is_err());
    }

    /// 起一个只回一次 HTTP 响应的小服务，真实走完 reqwest 解析链路
    fn spawn_tags_server(body: &'static str) -> u16 {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        std::thread::spawn(move || {
            if let Ok((mut stream, _)) = listener.accept() {
                let mut buf = [0u8; 4096];
                let _ = stream.read(&mut buf);
                let resp = format!(
                    "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                    body.len(),
                    body
                );
                let _ = stream.write_all(resp.as_bytes());
            }
        });
        port
    }

    #[tokio::test]
    async fn detect_real_tags_response() {
        let port = spawn_tags_server(
            r#"{"models":[{"name":"qwen2.5:14b","size":123,"modified_at":"2026-10-01T00:00:00Z"}]}"#,
        );
        let resp = detect(Some(&format!("http://127.0.0.1:{port}/v1")))
            .await
            .unwrap();
        assert!(resp.supported);
        assert!(resp.available, "{:?}", resp.error);
        assert_eq!(resp.endpoint, format!("http://127.0.0.1:{port}"));
        assert_eq!(
            resp.openai_base_url,
            format!("http://127.0.0.1:{port}/v1")
        );
        assert_eq!(resp.models.len(), 1);
        assert_eq!(resp.models[0].name, "qwen2.5:14b");
    }

    #[tokio::test]
    async fn detect_unreachable_reports_not_running() {
        let resp = detect(Some("http://127.0.0.1:1")).await.unwrap();
        assert!(resp.supported);
        assert!(!resp.available);
        assert!(resp.models.is_empty());
        assert!(resp
            .error
            .as_deref()
            .unwrap_or_default()
            .starts_with("LOCAL_MODEL_NOT_RUNNING"));
    }
}
