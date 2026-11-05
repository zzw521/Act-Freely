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
    /// 首包耗时：从发出请求到解析出第一个「有内容」的增量（正文或工具调用）。
    /// 整包耗时看 `latency_ms`；这两个分开记，长任务基准才看得出「模型排队」与「出字慢」是两件事。
    pub first_token_ms: u64,
    pub cost_micros: Option<i64>,
    pub currency: Option<String>,
    /// 本轮发生过的模型切换（from/to/reason），调用方摊成可见步骤与审计
    pub switches: Vec<Value>,
    /// 端点拒收工具后退回纯文本的事实（model/reason）。
    /// 有值就等于「这一轮没有任何工具真正执行过」，调用方必须把它写在脸上。
    pub tools_degraded: Option<Value>,
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

fn build_body(model: &str, messages: &[Value], tools: &[Value], want_usage: bool) -> Value {
    let mut body = json!({
        "model": model,
        "messages": messages,
        "stream": true,
    });
    if let Some(map) = body.as_object_mut() {
        if want_usage {
            // OpenAI 兼容协议的流式**默认不回 usage**：不显式要，用量永远是 0 ——
            // 日额度、单任务预算（D-14.1）、用量台账、成本全跟着变瞎子。
            // 真机验收第一次连百炼就撞上：不带这个字段一个用量包都没有。
            // 严格校验的自建服务会把它当非法字段 400 掉，所以 `call_once` 里
            // 认出这类拒绝会去掉它重发一次（宁可少一个用量，也不能少一次回答）。
            map.insert(
                "stream_options".into(),
                json!({ "include_usage": true }),
            );
        }
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
    /// 第一个「有内容」增量到达时距离请求起点的毫秒数（首包）。
    /// 只有 usage、没有正文也没有工具调用的包不算首包。
    first_ms: Option<u64>,
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
        let latency_ms = started.elapsed().as_millis() as u64;
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
            latency_ms,
            // 一个有内容的包都没收到（例如整包一次到达且只被末尾兜底解析）时，
            // 首包就等于整包：不编一个更小的数出来
            first_token_ms: self.first_ms.unwrap_or(latency_ms),
            cost_micros: cost,
            currency,
            switches: Vec::new(),
            tools_degraded: None,
        }
    }
}

/// 记下首包：只有真正带正文或工具调用的包才算「第一个字」。
/// 只记第一次，之后的包不再改写。
fn mark_first_packet(delta: &mut Delta, started: Instant) {
    if delta.first_ms.is_some() {
        return;
    }
    if delta.text.is_empty() && delta.calls.is_empty() {
        return;
    }
    delta.first_ms = Some(started.elapsed().as_millis() as u64);
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

/// 发出请求并确认状态码：非 2xx 一律带上端点自己回的那段正文（截 400 字）。
async fn open_stream(ep: &Endpoint, body: &Value) -> Result<reqwest::Response, ModelError> {
    let resp = request(ep, body)
        .map_err(ModelError::Failed)?
        .send()
        .await
        .map_err(|e| ModelError::Failed(format!("MODEL_CONNECTION_FAILED: 模型请求失败：{e}")))?;
    let status = resp.status();
    if !status.is_success() {
        let text = resp.text().await.unwrap_or_default();
        let brief: String = text.chars().take(400).collect();
        return Err(ModelError::Failed(format!(
            "MODEL_HTTP_{}: 模型返回错误：{}",
            status.as_u16(),
            brief.trim()
        )));
    }
    Ok(resp)
}

/// 端点是否把 `stream_options` 本身当成非法字段（严格 schema 校验的自建服务会 400 它）。
///
/// 只认 400/415/422 这三类「请求本身被拒」，且错误正文点出这个字段名；
/// 认证、限流、超时一类不算——去掉这个字段也不会变好，重试只是白烧一次请求。
fn stream_options_rejection(err: &ModelError) -> bool {
    let ModelError::Failed(msg) = err else {
        return false;
    };
    let rejected = ["MODEL_HTTP_400", "MODEL_HTTP_415", "MODEL_HTTP_422"]
        .iter()
        .any(|code| msg.starts_with(code));
    rejected && msg.to_ascii_lowercase().contains("stream_options")
}

async fn call_once(
    ep: &Endpoint,
    messages: &[Value],
    tools: &[Value],
    cancel: &(dyn Fn() -> bool + Send + Sync),
) -> Result<Outcome, ModelError> {
    if cancel() {
        return Err(ModelError::Cancelled);
    }
    let started = Instant::now();
    // 先按「要用量」发；只有端点明确说这个字段非法，才退成「不要用量」再发一次。
    let resp = match open_stream(ep, &build_body(&ep.model, messages, tools, true)).await {
        Ok(resp) => resp,
        Err(err) if stream_options_rejection(&err) => {
            open_stream(ep, &build_body(&ep.model, messages, tools, false)).await?
        }
        Err(err) => return Err(err),
    };

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
                        mark_first_packet(&mut delta, started);
                    }
                }
            }
        }
    }
    if let Some(payload) = parse_sse_line(&buf) {
        delta.merge(&payload);
        mark_first_packet(&mut delta, started);
    }
    let outcome = delta.finish(ep, started);
    if outcome.text.trim().is_empty() && outcome.tool_calls.is_empty() {
        return Err(ModelError::Failed(
            "MODEL_EMPTY_RESPONSE: 模型没有返回任何内容".to_string(),
        ));
    }
    Ok(outcome)
}

fn err_text(err: &ModelError) -> String {
    match err {
        ModelError::Cancelled => "任务已取消".to_string(),
        ModelError::Failed(msg) => msg.clone(),
    }
}

/// 端点是否把失败写在「工具」上：只看它自己回的话，不猜某个模型的能力。
///
/// 认 400/415/422 这三类请求本身被拒的状态码，且错误正文点出 tool/function；
/// 认证、限流、超时一类不算（去掉工具也不会变好，重试只是白烧一次请求）。
fn tool_schema_rejection(err: &ModelError) -> Option<String> {
    let ModelError::Failed(msg) = err else {
        return None;
    };
    let rest = msg.strip_prefix("MODEL_HTTP_")?;
    let code: u16 = rest.get(..3)?.parse().ok()?;
    if !matches!(code, 400 | 415 | 422) {
        return None;
    }
    let lower = msg.to_lowercase();
    if !(lower.contains("tool") || lower.contains("function")) {
        return None;
    }
    Some(msg.chars().take(240).collect::<String>())
}

/// 一次调配：带工具失败且端点明确说工具不行时，同一端点退一次纯文本。
///
/// 换模型最容易撞上的不是「连不上」，而是新端点不接这份工具清单。
/// 退一步让本轮仍给出回答，但 `tools_degraded` 必须留下事实：文件没有真的被写过。
async fn call_degraded(
    ep: &Endpoint,
    messages: &[Value],
    reason: &str,
    cancel: &(dyn Fn() -> bool + Send + Sync),
) -> Result<Outcome, ModelError> {
    let mut out = call_once(ep, messages, &[], cancel).await?;
    out.tools_degraded = Some(json!({ "model": ep.display, "reason": reason }));
    Ok(out)
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
///
/// 换模型最容易撞上的不是连不上，而是新端点不接这份工具清单。顺序是
/// 「先换下一个带得动工具的端点」，全链都拒收才退纯文本：宁可少一次工具，
/// 也不要一上来就把本轮降级成只能聊天。退纯文本成功时 `tools_degraded` 有值，
/// 调用方必须把「本轮没有任何工具真正执行」写在过程里。
pub async fn stream(
    endpoints: &[Endpoint],
    messages: &[Value],
    tools: &[Value],
    cancel: &(dyn Fn() -> bool + Send + Sync),
) -> Result<Outcome, ModelError> {
    let mut switches: Vec<Value> = Vec::new();
    let mut last_err = String::new();
    // 第一个「明确说工具不行」的端点：退纯文本时落回它，而不是落回链尾
    let mut tool_rejected: Option<(usize, String)> = None;
    for (i, ep) in endpoints.iter().enumerate() {
        if cancel() {
            return Err(ModelError::Cancelled);
        }
        match call_once(ep, messages, tools, cancel).await {
            Ok(mut out) => {
                out.switches = switches;
                return Ok(out);
            }
            Err(ModelError::Cancelled) => return Err(ModelError::Cancelled),
            Err(ModelError::Failed(err)) => {
                // 没带工具时不谈「退纯文本」：那是模型侧的答复，不是本轮能改的请求
                if !tools.is_empty() && tool_rejected.is_none() {
                    if let Some(reason) = tool_schema_rejection(&ModelError::Failed(err.clone())) {
                        tool_rejected = Some((i, reason));
                    }
                }
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
    if let Some((idx, reason)) = tool_rejected {
        if cancel() {
            return Err(ModelError::Cancelled);
        }
        match call_degraded(&endpoints[idx], messages, &reason, cancel).await {
            Ok(mut out) => {
                out.switches = switches;
                return Ok(out);
            }
            Err(retry) => {
                // 退到纯文本仍跑不起来：原始原因更有信息量，重试结果补在后面
                last_err = format!("{last_err}（去掉工具重试一次仍失败：{}）", err_text(&retry));
            }
        }
    }
    Err(chain_error(endpoints, &last_err))
}

/// 一次性调用（不带工具）：上下文压缩生成摘要用。
/// 与 `stream` 同样的容灾链语义——主模型失败按链切换，取消优先返回 Cancelled。
/// 摘要不该抢工具：请求体里不带 tools，模型也就不会在压缩这一步发起调用。
pub async fn oneshot(
    endpoints: &[Endpoint],
    messages: &[Value],
    cancel: &(dyn Fn() -> bool + Send + Sync),
) -> Result<Outcome, ModelError> {
    let mut switches: Vec<Value> = Vec::new();
    let mut last_err = String::new();
    for (i, ep) in endpoints.iter().enumerate() {
        if cancel() {
            return Err(ModelError::Cancelled);
        }
        match call_once(ep, messages, &[], cancel).await {
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

    /// 流式请求必须显式要用量：OpenAI 兼容协议默认不出 usage 包，
    /// 而日额度、单任务预算（D-14.1）、用量台账、成本全靠这一个数。
    #[test]
    fn test_body_asks_for_stream_usage_by_default() {
        let with = build_body("qwen-plus", &[], &[], true);
        assert_eq!(with["stream_options"]["include_usage"], json!(true), "{with}");
        assert_eq!(with["stream"], json!(true), "用量包只在流式里有意义");
        let without = build_body("qwen-plus", &[], &[], false);
        assert!(
            without.get("stream_options").is_none(),
            "端点不认这个字段时要能去掉：{without}"
        );
        // 工具清单与用量请求互不影响
        let tools = json!([{"type": "function", "function": {"name": "write_text_file"}}]);
        let both = build_body("qwen-plus", &[], tools.as_array().unwrap(), true);
        assert_eq!(both["stream_options"]["include_usage"], json!(true));
        assert_eq!(both["tools"].as_array().unwrap().len(), 1);
        assert_eq!(both["tool_choice"], json!("auto"));
    }

    #[test]
    fn only_stream_options_shaped_rejection_is_worth_retrying() {
        let hit = |m: &str| stream_options_rejection(&ModelError::Failed(m.to_string()));
        assert!(hit("MODEL_HTTP_400: 模型返回错误：body.stream_options: extra inputs are not permitted"));
        assert!(hit("MODEL_HTTP_422: 模型返回错误：unknown field STREAM_OPTIONS"));
        // 工具类拒绝另有处理；鉴权/限流/连接类去掉这个字段也不会变好
        assert!(!hit("MODEL_HTTP_400: 模型返回错误：this model does not support tools"));
        assert!(!hit("MODEL_HTTP_401: 模型返回错误：stream_options is not authorized"));
        assert!(!hit("MODEL_HTTP_429: 模型返回错误：rate limited on stream_options"));
        assert!(!hit("MODEL_CONNECTION_FAILED: stream_options"));
    }

    /// 逐字节把一条 HTTP 请求的正文读出来（头里带 Content-Length）。
    fn read_http_body(sock: &mut std::net::TcpStream) -> String {
        use std::io::Read;
        let mut head: Vec<u8> = Vec::new();
        let mut byte = [0u8; 1];
        while sock.read(&mut byte).unwrap_or(0) == 1 {
            head.push(byte[0]);
            if head.ends_with(b"\r\n\r\n") {
                break;
            }
        }
        let headers = String::from_utf8_lossy(&head).to_lowercase();
        let len = headers
            .split("content-length:")
            .nth(1)
            .and_then(|rest| rest.trim().split(|c: char| !c.is_ascii_digit()).next().map(str::to_owned))
            .and_then(|digits| digits.parse::<usize>().ok())
            .unwrap_or(0);
        let mut body = vec![0u8; len];
        if len > 0 {
            sock.read_exact(&mut body).ok();
        }
        String::from_utf8_lossy(&body).to_string()
    }

    /// 严格 schema 的自建服务会把 `stream_options` 当非法字段 400 掉：
    /// 这时要去掉它原地重发一次——宁可这一路没有用量，也不能没有回答。
    #[tokio::test]
    async fn test_stream_retries_without_stream_options_when_rejected() {
        use std::io::Write;

        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        let server = std::thread::spawn(move || {
            let mut bodies: Vec<String> = Vec::new();
            let (mut sock, _) = listener.accept().unwrap();
            bodies.push(read_http_body(&mut sock));
            let msg = r#"{"error":{"message":"body.stream_options: extra inputs are not permitted"}}"#;
            let resp = format!(
                "HTTP/1.1 400 Bad Request\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\
                 Connection: close\r\n\r\n{}",
                msg.len(),
                msg
            );
            let _ = sock.write_all(resp.as_bytes());

            let (mut sock, _) = listener.accept().unwrap();
            bodies.push(read_http_body(&mut sock));
            let body = concat!(
                "data: {\"choices\":[{\"delta\":{\"content\":\"退一步也能答\"}}]}\n\n",
                "data: [DONE]\n\n",
            );
            let resp = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: {}\r\n\
                 Connection: close\r\n\r\n{}",
                body.len(),
                body
            );
            let _ = sock.write_all(resp.as_bytes());
            bodies
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
        .expect("端点不认 stream_options 不该让本轮没有回答");
        assert_eq!(out.text, "退一步也能答");
        assert_eq!(out.prompt_tokens, 0, "退掉 stream_options 就没有用量可报");
        let bodies = server.join().unwrap();
        assert_eq!(bodies.len(), 2, "被拒后应当只重发一次");
        assert!(bodies[0].contains("stream_options"), "{}", bodies[0]);
        assert!(
            !bodies[1].contains("stream_options"),
            "重发要真的把字段去掉：{}",
            bodies[1]
        );
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

    #[test]
    fn only_tool_shaped_rejection_is_worth_retrying() {
        let hit = |m: &str| tool_schema_rejection(&ModelError::Failed(m.to_string())).is_some();
        assert!(hit("MODEL_HTTP_400: 模型返回错误：This model does not support tools"));
        assert!(hit("MODEL_HTTP_422: 模型返回错误：invalid function definition"));
        assert!(hit("MODEL_HTTP_415: 模型返回错误：tool_choice unsupported"));
        // 鉴权/限流/连接类失败：去掉工具也不会变好，重试只是白烧一次请求
        assert!(!hit("MODEL_HTTP_401: 模型返回错误：tools are not authorized"));
        assert!(!hit("MODEL_HTTP_429: 模型返回错误：rate limited"));
        assert!(!hit("MODEL_HTTP_500: 模型返回错误：internal error for tool"));
        assert!(!hit("MODEL_CONNECTION_FAILED: 无法连接"));
        assert!(!hit("MODEL_HTTP_4"));
        assert!(matches!(
            tool_schema_rejection(&ModelError::Cancelled),
            None
        ));
    }

    /// 每次连接按脚本返回一个响应；返回 base_url 与「收到的请求体」句柄
    fn scripted(responses: Vec<(u16, String)>) -> (String, std::thread::JoinHandle<Vec<String>>) {
        use std::io::{Read, Write};

        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        let handle = std::thread::spawn(move || {
            let mut bodies = Vec::new();
            for (status, body) in responses {
                let (mut sock, _) = listener.accept().unwrap();
                let mut buf = [0u8; 8192];
                let n = sock.read(&mut buf).unwrap_or(0);
                bodies.push(String::from_utf8_lossy(&buf[..n]).to_string());
                let resp = format!(
                    "HTTP/1.1 {status} X\r\nContent-Type: text/event-stream\r\n\
                     Content-Length: {}\r\nConnection: close\r\n\r\n{body}",
                    body.len()
                );
                let _ = sock.write_all(resp.as_bytes());
            }
            bodies
        });
        (format!("http://{addr}/v1"), handle)
    }

    #[tokio::test]
    async fn test_stream_degrades_to_text_when_the_chain_rejects_tools() {
        let ok = concat!(
            "data: {\"choices\":[{\"delta\":{\"content\":\"纯文本回答\"}}]}\n\n",
            "data: [DONE]\n\n",
        );
        let (base, server) = scripted(vec![
            (
                400,
                "{\"error\":{\"message\":\"This model does not support tools\"}}".to_string(),
            ),
            (200, ok.to_string()),
        ]);
        let endpoint = Endpoint {
            base_url: base,
            ..ep()
        };
        let tools = vec![json!({"type": "function", "function": {
            "name": "write_text_file", "description": "写入", "parameters": {"type": "object"}
        }})];
        let out = stream(
            &[endpoint],
            &[json!({ "role": "user", "content": "hi" })],
            &tools,
            &|| false,
        )
        .await
        .unwrap();
        assert_eq!(out.text, "纯文本回答");
        let degraded = out.tools_degraded.expect("端点拒收工具必须留下事实");
        assert!(
            degraded["reason"].as_str().unwrap().contains("does not support"),
            "{degraded}"
        );
        assert_eq!(degraded["model"].as_str(), Some("主模型"));
        let bodies = server.join().unwrap();
        assert_eq!(bodies.len(), 2);
        assert!(bodies[0].contains("\"tools\""), "第一次请求带着工具清单");
        assert!(
            !bodies[1].contains("\"tools\""),
            "退纯文本时不能继续带着被拒的字段"
        );
    }

    #[tokio::test]
    async fn test_stream_prefers_tool_capable_fallback_over_degrading() {
        let ok = concat!(
            "data: {\"choices\":[{\"delta\":{\"content\":\"备用模型的回答\"}}]}\n\n",
            "data: [DONE]\n\n",
        );
        let (base1, first) = scripted(vec![(
            400,
            "{\"error\":{\"message\":\"tools are not supported\"}}".to_string(),
        )]);
        let (base2, second) = scripted(vec![(200, ok.to_string())]);
        let a = Endpoint {
            base_url: base1,
            display: "主模型".into(),
            ..ep()
        };
        let b = Endpoint {
            base_url: base2,
            display: "备用模型".into(),
            ..ep()
        };
        let tools = vec![json!({"type": "function", "function": {
            "name": "write_text_file", "description": "写入", "parameters": {"type": "object"}
        }})];
        let out = stream(&[a, b], &[json!({ "role": "user", "content": "hi" })], &tools, &|| false)
            .await
            .unwrap();
        assert!(
            out.tools_degraded.is_none(),
            "链上还有带得动工具的端点，不该先把本轮降级"
        );
        assert_eq!(out.model, "备用模型");
        assert_eq!(out.switches.len(), 1);
        assert_eq!(out.switches[0]["from"], "主模型");
        let bodies = second.join().unwrap();
        assert!(bodies[0].contains("\"tools\""), "备用端点仍要收到工具清单");
        first.join().unwrap();
    }
}
