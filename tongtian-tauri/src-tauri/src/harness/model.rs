//! 客户端 harness 的模型适配器（OpenAI 兼容 /chat/completions）。
//!
//! 只做三件事：把本轮模型配置解析成候选端点、流式或一次性调用、主模型失败时
//! 按配置的容灾链切换。密钥只在进程内存里，既不落盘也不进事件流。
//! 输出口径与引擎一致：切换目标必须与主模型同一种接入方式，避免用错协议。

use std::collections::BTreeMap;
use std::time::{Duration, Instant};

use futures_util::StreamExt;
use serde_json::{json, Value};

const CONNECT_TIMEOUT: Duration = Duration::from_secs(15);
const REQUEST_TIMEOUT: Duration = Duration::from_secs(300);

#[derive(Debug, Clone)]
pub struct Price {
    pub input_per_m: f64,
    pub output_per_m: f64,
    pub currency: String,
}

#[derive(Debug, Clone)]
pub struct Endpoint {
    pub display: String,
    pub model: String,
    pub base_url: String,
    pub api_key: String,
    pub price: Option<Price>,
}

#[derive(Debug, Clone, Default)]
pub struct ToolCall {
    pub id: String,
    pub name: String,
    pub arguments: String,
}

#[derive(Debug, Clone, Default)]
pub struct Outcome {
    pub text: String,
    pub tool_calls: Vec<ToolCall>,
    pub prompt_tokens: u64,
    pub completion_tokens: u64,
    pub model: String,
    pub latency_ms: u64,
    pub cost_micros: Option<i64>,
    pub currency: Option<String>,
    /// 本轮发生过的模型切换（from/to/reason），调用方摊成可见步骤与审计
    pub switches: Vec<Value>,
}

#[derive(Debug)]
pub enum ModelError {
    Cancelled,
    Failed(String),
}

fn text_of(obj: &Value, keys: &[&str]) -> String {
    keys.iter()
        .find_map(|k| obj.get(*k).and_then(|v| v.as_str()))
        .unwrap_or("")
        .trim()
        .to_string()
}

fn default_base_for(source: &str) -> &'static str {
    match source {
        "preset_qwen" => "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "preset_deepseek" => "https://api.deepseek.com/v1",
        "preset_openai" => "https://api.openai.com/v1",
        "ollama" => "http://127.0.0.1:11434/v1",
        _ => "",
    }
}

fn price_of(v: &Value) -> Option<Price> {
    let p = v.get("prices")?;
    if !p.is_object() {
        return None;
    }
    let input = p.get("inputPerM").and_then(|x| x.as_f64())?;
    let output = p.get("outputPerM").and_then(|x| x.as_f64())?;
    Some(Price {
        input_per_m: input,
        output_per_m: output,
        currency: text_of(p, &["currency"]),
    })
}

fn endpoint_from(v: &Value) -> Result<Endpoint, String> {
    let model = text_of(v, &["modelName", "model_name", "model"]);
    if model.is_empty() {
        return Err("MODEL_INCOMPLETE: 模型缺少服务端模型名".to_string());
    }
    let source = text_of(v, &["source"]);
    let mut base = text_of(v, &["baseUrl", "base_url"]);
    if base.is_empty() {
        base = default_base_for(&source).to_string();
    }
    if base.is_empty() {
        base = std::env::var("TT_BASE_URL").unwrap_or_default();
    }
    if base.is_empty() {
        return Err(format!(
            "MODEL_INCOMPLETE: 模型「{model}」缺少接入地址（baseUrl）"
        ));
    }
    let key = text_of(v, &["apiKey", "api_key"]);
    let host = crate::security::policy::host_of(&base);
    if key.is_empty() && !crate::security::policy::is_local_host(&host) {
        return Err(format!("MODEL_KEY_MISSING: 模型「{model}」未配置 API Key"));
    }
    Ok(Endpoint {
        display: text_of(v, &["displayName", "display_name"])
            .is_empty()
            .then(|| model.clone())
            .unwrap_or_else(|| text_of(v, &["displayName", "display_name"])),
        model,
        base_url: base.trim_end_matches('/').to_string(),
        api_key: key,
        price: price_of(v),
    })
}

/// 接入方式判定：显式配置 baseUrl 视为自定义端点，其余走内置预设地址。
/// 与 model/service.rs 里下发给引擎的过滤口径一致（转移链不跨接入方式）。
fn custom_endpoint(v: &Value) -> bool {
    !text_of(v, &["baseUrl", "base_url"]).is_empty()
}

fn env_endpoints() -> Result<Vec<Endpoint>, String> {
    let key = std::env::var("TT_API_KEY").unwrap_or_default();
    if key.trim().is_empty() {
        return Err(
            "MODEL_NOT_CONFIGURED: 未配置可用模型：请在「设置 → 模型与用量」中添加模型".to_string(),
        );
    }
    let provider = std::env::var("TT_PROVIDER")
        .unwrap_or_else(|_| "dashscope".to_string())
        .to_lowercase();
    let base = std::env::var("TT_BASE_URL").unwrap_or_default();
    let base = if !base.trim().is_empty() {
        base
    } else if provider == "dashscope" {
        "https://dashscope.aliyuncs.com/compatible-mode/v1".to_string()
    } else {
        "https://api.openai.com/v1".to_string()
    };
    let model = std::env::var("TT_MODEL").unwrap_or_else(|_| {
        if provider == "dashscope" {
            "qwen-plus".to_string()
        } else {
            String::new()
        }
    });
    if model.trim().is_empty() {
        return Err(
            "MODEL_NOT_CONFIGURED: 未设置 TT_MODEL，无法确定要调用的模型".to_string(),
        );
    }
    Ok(vec![Endpoint {
        display: model.clone(),
        model,
        base_url: base.trim_end_matches('/').to_string(),
        api_key: key,
        price: None,
    }])
}

/// 本轮候选端点：主模型 + 与之同接入方式的容灾目标（最多 3 个）。
pub fn endpoints(cfg: Option<&Value>) -> Result<Vec<Endpoint>, String> {
    let Some(cfg) = cfg.filter(|v| v.is_object()) else {
        return env_endpoints();
    };
    let primary = endpoint_from(cfg)?;
    let primary_custom = custom_endpoint(cfg);
    let mut out = vec![primary];
    if let Some(arr) = cfg.get("fallbacks").and_then(|v| v.as_array()) {
        for item in arr.iter().take(3) {
            let Ok(ep) = endpoint_from(item) else {
                continue;
            };
            if custom_endpoint(item) == primary_custom {
                out.push(ep);
            }
        }
    }
    Ok(out)
}

fn build_body(model: &str, messages: &[Value], tools: &[Value]) -> Value {
    let mut body = json!({
        "model": model,
        "messages": messages,
        "stream": true,
    });
    if let Some(map) = body.as_object_mut() {
        if !tools.is_empty() {
            map.insert("tools".into(), Value::Array(tools.to_vec()));
            map.insert("tool_choice".into(), json!("auto"));
        }
    }
    body
}

fn client() -> Result<reqwest::Client, String> {
    reqwest::Client::builder()
        .connect_timeout(CONNECT_TIMEOUT)
        .timeout(REQUEST_TIMEOUT)
        .build()
        .map_err(|e| format!("MODEL_CLIENT_FAILED: 无法创建 HTTP 客户端：{e}"))
}

fn request(ep: &Endpoint, body: &Value) -> Result<reqwest::RequestBuilder, String> {
    let mut req = client()?
        .post(format!("{}/chat/completions", ep.base_url))
        .header("Content-Type", "application/json")
        .json(body);
    if !ep.api_key.trim().is_empty() {
        req = req.header("Authorization", format!("Bearer {}", ep.api_key.trim()));
    }
    Ok(req)
}

/// 单次流式调配的增量解析结果
#[derive(Debug, Default)]
struct Delta {
    text: String,
    calls: BTreeMap<usize, ToolCall>,
    prompt_tokens: Option<u64>,
    completion_tokens: Option<u64>,
}

impl Delta {
    fn merge(&mut self, payload: &Value) {
        if let Some(usage) = payload.get("usage") {
            if let Some(p) = usage.get("prompt_tokens").and_then(|v| v.as_u64()) {
                self.prompt_tokens = Some(p);
            }
            if let Some(c) = usage.get("completion_tokens").and_then(|v| v.as_u64()) {
                self.completion_tokens = Some(c);
            }
        }
        let Some(choice) = payload
            .get("choices")
            .and_then(|v| v.as_array())
            .and_then(|arr| arr.first())
        else {
            return;
        };
        let Some(delta) = choice.get("delta") else {
            return;
        };
        if let Some(piece) = delta.get("content").and_then(|v| v.as_str()) {
            self.text.push_str(piece);
        }
        let Some(calls) = delta.get("tool_calls").and_then(|v| v.as_array()) else {
            return;
        };
        for item in calls {
            let idx = item.get("index").and_then(|v| v.as_u64()).unwrap_or(0) as usize;
            let slot = self.calls.entry(idx).or_default();
            if let Some(id) = item.get("id").and_then(|v| v.as_str()) {
                if !id.is_empty() {
                    slot.id = id.to_string();
                }
            }
            let Some(func) = item.get("function") else {
                continue;
            };
            if let Some(name) = func.get("name").and_then(|v| v.as_str()) {
                if !name.is_empty() && slot.name.is_empty() {
                    slot.name = name.to_string();
                }
            }
            if let Some(args) = func.get("arguments").and_then(|v| v.as_str()) {
                slot.arguments.push_str(args);
            }
        }
    }

    fn finish(self, ep: &Endpoint, started: Instant) -> Outcome {
        let mut calls: Vec<ToolCall> = self
            .calls
            .into_values()
            .filter(|c| !c.name.trim().is_empty())
            .collect();
        for (i, call) in calls.iter_mut().enumerate() {
            if call.id.trim().is_empty() {
                call.id = format!("call-{i}");
            }
        }
        let (cost, currency) = cost_of(ep, self.prompt_tokens.unwrap_or(0), self.completion_tokens.unwrap_or(0));
        Outcome {
            text: self.text,
            tool_calls: calls,
            prompt_tokens: self.prompt_tokens.unwrap_or(0),
            completion_tokens: self.completion_tokens.unwrap_or(0),
            model: ep.display.clone(),
            latency_ms: started.elapsed().as_millis() as u64,
            cost_micros: cost,
            currency,
            switches: Vec::new(),
        }
    }
}

fn cost_of(ep: &Endpoint, prompt: u64, completion: u64) -> (Option<i64>, Option<String>) {
    let Some(price) = &ep.price else {
        return (None, None);
    };
    let micros = prompt as f64 * price.input_per_m + completion as f64 * price.output_per_m;
    (
        Some(micros.round() as i64),
        Some(price.currency.clone()).filter(|c| !c.trim().is_empty()),
    )
}

fn parse_sse_line(line: &str) -> Option<Value> {
    let payload = line.trim_end_matches('\r').strip_prefix("data:")?;
    let payload = payload.trim();
    if payload.is_empty() || payload == "[DONE]" {
        return None;
    }
    serde_json::from_str::<Value>(payload).ok()
}

async fn call_streaming(
    ep: &Endpoint,
    messages: &[Value],
    tools: &[Value],
    cancel: &(dyn Fn() -> bool + Send + Sync),
) -> Result<Outcome, ModelError> {
    if cancel() {
        return Err(ModelError::Cancelled);
    }
    let started = Instant::now();
    let body = build_body(&ep.model, messages, tools);
    let resp = request(ep, &body)
        .map_err(ModelError::Failed)?
        .send()
        .await
        .map_err(|e| ModelError::Failed(format!("MODEL_CONNECTION_FAILED: 模型请求失败：{e}")))?;
    let status = resp.status();
    if !status.is_success() {
        let body = resp.text().await.unwrap_or_default();
        let brief: String = body.chars().take(400).collect();
        return Err(ModelError::Failed(format!(
            "MODEL_HTTP_{}: 模型返回错误：{}",
            status.as_u16(),
            brief.trim()
        )));
    }

    let mut delta = Delta::default();
    let mut buf = String::new();
    let mut stream = resp.bytes_stream();
    // 取消轮询与数据流并行：模型长时间不出字时，停止键也要能立刻生效
    let mut ticker = tokio::time::interval(std::time::Duration::from_millis(200));
    loop {
        tokio::select! {
            _ = ticker.tick() => {
                if cancel() {
                    return Err(ModelError::Cancelled);
                }
            }
            item = stream.next() => {
                let Some(chunk) = item else { break };
                let bytes = chunk.map_err(|e| {
                    ModelError::Failed(format!("MODEL_STREAM_FAILED: 模型流中断：{e}"))
                })?;
                buf.push_str(&String::from_utf8_lossy(&bytes));
                while let Some(pos) = buf.find('\n') {
                    let line: String = buf.drain(..=pos).collect();
                    if let Some(payload) = parse_sse_line(&line) {
                        delta.merge(&payload);
                    }
                }
            }
        }
    }
    if let Some(payload) = parse_sse_line(&buf) {
        delta.merge(&payload);
    }
    let outcome = delta.finish(ep, started);
    if outcome.text.trim().is_empty() && outcome.tool_calls.is_empty() {
        return Err(ModelError::Failed(
            "MODEL_EMPTY_RESPONSE: 模型没有返回任何内容".to_string(),
        ));
    }
    Ok(outcome)
}

fn chain_error(endpoints: &[Endpoint], last: &str) -> ModelError {
    if endpoints.len() <= 1 {
        return ModelError::Failed(last.to_string());
    }
    let names: Vec<&str> = endpoints.iter().map(|e| e.display.as_str()).collect();
    ModelError::Failed(format!(
        "模型容灾链全部失败（{}）：{last}",
        names.join(" → ")
    ))
}

/// 流式调用：主模型在前，按容灾链依次尝试；只在「尚未输出任何内容」时切换。
pub async fn stream(
    endpoints: &[Endpoint],
    messages: &[Value],
    tools: &[Value],
    cancel: &(dyn Fn() -> bool + Send + Sync),
) -> Result<Outcome, ModelError> {
    let mut switches: Vec<Value> = Vec::new();
    let mut last_err = String::new();
    for (i, ep) in endpoints.iter().enumerate() {
        if cancel() {
            return Err(ModelError::Cancelled);
        }
        match call_streaming(ep, messages, tools, cancel).await {
            Ok(mut out) => {
                out.switches = switches;
                return Ok(out);
            }
            Err(ModelError::Cancelled) => return Err(ModelError::Cancelled),
            Err(ModelError::Failed(err)) => {
                last_err = err;
                if let Some(next) = endpoints.get(i + 1) {
                    switches.push(json!({
                        "from": ep.display,
                        "to": next.display,
                        "reason": last_err,
                    }));
                }
            }
        }
    }
    Err(chain_error(endpoints, &last_err))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ep() -> Endpoint {
        Endpoint {
            display: "主模型".into(),
            model: "qwen-plus".into(),
            base_url: "http://127.0.0.1:9/v1".into(),
            api_key: "k".into(),
            price: Some(Price {
                input_per_m: 0.4,
                output_per_m: 1.2,
                currency: "CNY".into(),
            }),
        }
    }

    #[test]
    fn test_parse_sse_line_ignores_done_and_comments() {
        assert!(parse_sse_line("data: [DONE]").is_none());
        assert!(parse_sse_line(": keepalive").is_none());
        assert!(parse_sse_line("event: message").is_none());
        let v = parse_sse_line(r#"data: {"choices":[{"delta":{"content":"a"}}]}"#).unwrap();
        assert_eq!(v["choices"][0]["delta"]["content"], "a");
    }

    #[test]
    fn test_delta_merges_text_and_tool_calls() {
        let mut d = Delta::default();
        d.merge(&json!({"choices":[{"delta":{"content":"你好"}}]}));
        d.merge(&json!({"choices":[{"delta":{"content":"，世界"}}]}));
        d.merge(&json!({"choices":[{"delta":{"tool_calls":[
            {"index":0,"id":"call_1","function":{"name":"view_text_file","arguments":"{\"file_"}}
        ]}}]}));
        d.merge(&json!({"choices":[{"delta":{"tool_calls":[
            {"index":0,"function":{"arguments":"path\":\"a.md\"}"}}
        ]}}]}));
        d.merge(&json!({"usage":{"prompt_tokens":10,"completion_tokens":5}}));
        let out = d.finish(&ep(), Instant::now());
        assert_eq!(out.text, "你好，世界");
        assert_eq!(out.tool_calls.len(), 1);
        assert_eq!(out.tool_calls[0].name, "view_text_file");
        assert_eq!(out.tool_calls[0].arguments, r#"{"file_path":"a.md"}"#);
        assert_eq!(out.prompt_tokens, 10);
        assert_eq!(out.completion_tokens, 5);
        // 10 * 0.4 + 5 * 1.2 = 10 微元（价格按每百万 token 计）
        assert_eq!(out.cost_micros, Some(10));
        assert_eq!(out.currency.as_deref(), Some("CNY"));
    }

    #[test]
    fn test_delta_drops_nameless_calls_and_fills_id() {
        let mut d = Delta::default();
        d.merge(&json!({"choices":[{"delta":{"tool_calls":[
            {"index":0,"function":{"arguments":"{}"}},
            {"index":1,"function":{"name":"use_skill","arguments":"{}"}}
        ]}}]}));
        let out = d.finish(&ep(), Instant::now());
        assert_eq!(out.tool_calls.len(), 1);
        assert_eq!(out.tool_calls[0].id, "call-0");
    }

    #[test]
    fn test_endpoint_resolution_uses_source_defaults() {
        let cfg = json!({
            "modelId": "m1",
            "displayName": "Qwen",
            "modelName": "qwen-plus",
            "baseUrl": "",
            "apiKey": "k",
            "source": "preset_qwen"
        });
        let eps = endpoints(Some(&cfg)).unwrap();
        assert_eq!(eps.len(), 1);
        assert!(eps[0].base_url.contains("dashscope.aliyuncs.com"));
        assert_eq!(eps[0].display, "Qwen");
    }

    #[test]
    fn test_endpoint_requires_key_for_remote_host() {
        let cfg = json!({
            "modelId": "m1",
            "modelName": "gpt-4o",
            "baseUrl": "https://api.openai.com/v1",
            "apiKey": "",
            "source": "preset_openai"
        });
        let err = endpoints(Some(&cfg)).unwrap_err();
        assert!(err.contains("MODEL_KEY_MISSING"), "{err}");
    }

    #[test]
    fn test_fallbacks_require_same_access_mode() {
        let cfg = json!({
            "modelId": "m1",
            "modelName": "qwen-plus",
            "baseUrl": "https://example.com/v1",
            "apiKey": "k",
            "fallbacks": [
                {"modelName": "qwen-max", "baseUrl": "https://example.com/v1", "apiKey": "k"},
                {"modelName": "gpt-4o", "baseUrl": "", "apiKey": "k", "source": "preset_openai"}
            ]
        });
        let eps = endpoints(Some(&cfg)).unwrap();
        assert_eq!(eps.len(), 2);
        assert_eq!(eps[1].model, "qwen-max");
    }

    #[test]
    fn test_cost_without_price_is_unknown() {
        let mut e = ep();
        e.price = None;
        let (cost, currency) = cost_of(&e, 100, 50);
        assert!(cost.is_none());
        assert!(currency.is_none());
    }

    #[tokio::test]
    async fn test_stream_parses_sse_text_tools_and_usage() {
        use std::io::{Read, Write};

        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        let server = std::thread::spawn(move || {
            let (mut sock, _) = listener.accept().unwrap();
            let mut buf = [0u8; 4096];
            let _ = sock.read(&mut buf);
            let body = concat!(
                "data: {\"choices\":[{\"delta\":{\"content\":\"你好\"}}]}\n\n",
                "data: {\"choices\":[{\"delta\":{\"tool_calls\":[{\"index\":0,\"id\":\"c1\",\
                 \"function\":{\"name\":\"view_text_file\",\"arguments\":\"{}\"}}]}}]}\n\n",
                "data: {\"usage\":{\"prompt_tokens\":3,\"completion_tokens\":2}}\n\n",
                "data: [DONE]\n\n",
            );
            let resp = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: {}\r\n\
                 Connection: close\r\n\r\n{}",
                body.len(),
                body
            );
            let _ = sock.write_all(resp.as_bytes());
        });

        let endpoint = Endpoint {
            base_url: format!("http://{addr}/v1"),
            ..ep()
        };
        let out = stream(
            &[endpoint],
            &[json!({ "role": "user", "content": "hi" })],
            &[],
            &|| false,
        )
        .await
        .unwrap();
        assert_eq!(out.text, "你好");
        assert_eq!(out.tool_calls.len(), 1);
        assert_eq!(out.tool_calls[0].name, "view_text_file");
        assert_eq!(out.prompt_tokens, 3);
        assert_eq!(out.completion_tokens, 2);
        server.join().unwrap();
    }

    #[tokio::test]
    async fn test_stream_honours_cancel_before_request() {
        let endpoint = Endpoint {
            // 端口不可达也无所谓：取消判断在建立连接之前
            base_url: "http://127.0.0.1:9/v1".into(),
            ..ep()
        };
        let out = stream(&[endpoint], &[], &[], &|| true).await;
        assert!(matches!(out, Err(ModelError::Cancelled)));
    }
}
