//! 内置工具声明清单
//!
//! 真实注册表在随包引擎里（`engine/server.py` 的 `build_toolkit` → `Toolkit.tools`），
//! 引擎直连通道通过 `GET /api/tools` 读到的是它。桌面通道在引擎未启动时也要能回答
//! 「这个应用到底给了模型哪些工具」，因此这里声明同一份清单。
//!
//! 两边漂移由 `tests/unit/extensions/builtin-tools-contract.test.ts` 拦住：
//! 它会解析引擎源码里的 `register_tool_function(...)` 与本清单逐条比对。
//!
//! 注意：这里只列**确实注册了**的能力。此前界面上出现的「命令执行」「系统查询」
//! 「数据分析」三张卡片在引擎里没有任何对应工具，属于假宣传，已删除。

use serde_json::{json, Value};

/// (工具名, 展示名, 说明) —— 名称必须与 `register_tool_function` 的函数名一致
pub const BUILTIN_TOOLS: &[(&str, &str, &str)] = &[
    ("view_text_file", "读取文件", "读取工作目录内的文本文件（按行返回内容）"),
    ("write_text_file", "写入文件", "在工作目录内创建或覆盖文本文件"),
    ("insert_text_file", "修改文件", "在工作目录内按行插入或替换文本内容"),
];

/// 声明清单视图（source 标明来源，界面据此说明它不是实测值）
pub fn view() -> Value {
    json!({
        "source": "declared",
        "count": BUILTIN_TOOLS.len(),
        "tools": BUILTIN_TOOLS
            .iter()
            .map(|(id, name, desc)| json!({ "id": id, "name": name, "desc": desc, "source": "builtin" }))
            .collect::<Vec<Value>>(),
        "note": "清单与随包引擎的工具注册表同源维护（构建期契约测试锁定），未启动引擎时按声明显示",
    })
}

/// 引擎在跑时读它的实测注册表，并把函数名换成中文展示名；读不到就回落到声明清单。
fn merge_engine_view(raw: &Value) -> Option<Value> {
    if raw.get("ok").and_then(Value::as_bool) != Some(true) {
        return None;
    }
    let tools = raw.get("tools")?.as_array()?;
    let merged: Vec<Value> = tools
        .iter()
        .filter_map(|t| t.get("id").and_then(Value::as_str).map(|id| (t, id)))
        .map(|(t, id)| {
            let declared = BUILTIN_TOOLS.iter().find(|(name, _, _)| *name == id);
            let desc = match declared {
                Some((_, _, d)) => (*d).to_string(),
                None => t
                    .get("desc")
                    .and_then(Value::as_str)
                    .unwrap_or_default()
                    .to_string(),
            };
            json!({
                "id": id,
                "name": declared.map(|(_, n, _)| *n).unwrap_or(id),
                "desc": desc,
                "source": t.get("source").and_then(Value::as_str).unwrap_or("builtin"),
            })
        })
        .collect();
    let count = merged.len();
    Some(json!({
        "source": "engine",
        "count": count,
        "tools": merged,
        "note": "清单读自运行中引擎的工具注册表（实测值）",
    }))
}

/// 内置工具清单（桌面通道）。
/// 有引擎实例在跑就向它要实测注册表，否则回落到与引擎同源的声明清单。
#[tauri::command]
pub async fn agent_builtin_tools(
    bridge: tauri::State<'_, super::agent::BridgeState>,
) -> Result<Value, String> {
    if let Some(port) = super::agent::any_running_port(bridge.inner()).await {
        let url = format!("http://127.0.0.1:{port}/api/tools");
        if let Ok(resp) = reqwest::Client::new().get(url).send().await {
            if let Ok(raw) = resp.json::<Value>().await {
                if let Some(v) = merge_engine_view(&raw) {
                    return Ok(v);
                }
            }
        }
    }
    Ok(view())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn engine_view_merges_declared_names() {
        let raw = json!({
            "ok": true,
            "tools": [
                { "id": "view_text_file", "name": "view_text_file", "desc": "read", "source": "builtin" },
                { "id": "custom_mcp_tool", "name": "custom_mcp_tool", "desc": "外部工具", "source": "mcp" },
            ]
        });
        let v = merge_engine_view(&raw).expect("应解析成功");
        assert_eq!(v["source"], "engine");
        assert_eq!(v["count"], 2);
        assert_eq!(v["tools"][0]["name"], "读取文件");
        // 引擎里多出来的工具（如 MCP）不得被丢掉，展示名回落到原名
        assert_eq!(v["tools"][1]["name"], "custom_mcp_tool");
        assert_eq!(v["tools"][1]["desc"], "外部工具");
        assert_eq!(v["tools"][1]["source"], "mcp");
    }

    #[test]
    fn engine_view_rejects_failed_payload() {
        assert!(merge_engine_view(&json!({ "ok": false })).is_none());
        assert!(merge_engine_view(&json!({ "ok": true })).is_none());
    }

    #[test]
    fn declared_ids_are_unique_and_complete() {
        let mut ids: Vec<&str> = BUILTIN_TOOLS.iter().map(|t| t.0).collect();
        ids.sort_unstable();
        let before = ids.len();
        ids.dedup();
        assert_eq!(ids.len(), before, "工具名重复");
        // 三个文件工具是引擎当前唯一注册的能力
        assert_eq!(
            ids,
            vec!["insert_text_file", "view_text_file", "write_text_file"]
        );
    }

    #[test]
    fn view_shape_matches_contract() {
        let v = view();
        assert_eq!(v["source"], "declared");
        assert_eq!(v["count"], BUILTIN_TOOLS.len());
        assert_eq!(v["tools"].as_array().unwrap().len(), BUILTIN_TOOLS.len());
        let first = &v["tools"][0];
        for key in ["id", "name", "desc", "source"] {
            assert!(first.get(key).is_some(), "缺少字段 {key}：{first}");
        }
        assert!(v["note"].as_str().unwrap().contains("契约测试"));
    }
}
