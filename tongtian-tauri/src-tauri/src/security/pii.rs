//! 出站脱敏档位（设置项 `piiOutboundMode`）
//!
//! 真正的脱敏发生在引擎（`engine/privacy_guard.py`，规则与 `security/classify.rs` 同源）：
//! Rust 只负责读取档位、非法值 fail-closed，并随 runtime 下发给本机引擎。
//! 档位语义：
//! - `off`：原样发送（默认，保持既有行为）；
//! - `mask`：身份证号/手机号/银行卡号/邮箱/明文密钥替换为掩码后发送；
//! - `block`：命中即中止本次模型调用（任务以 PRIVACY_OUTBOUND_BLOCKED 收尾）。

use crate::store::{settings, Store};

pub const POLICY_KEY: &str = "piiOutboundMode";
pub const MODES: [&str; 3] = ["off", "mask", "block"];
pub const DEFAULT_MODE: &str = "off";

/// 读取档位：未保存过用默认档；已保存但值非法时 fail-closed 返回错误，
/// 由引擎按关闭处理并如实播报，而不是猜一个档位。
pub fn read_mode(store: &Store) -> Result<(String, bool), String> {
    let stored = settings::get(store, POLICY_KEY)?;
    match stored {
        None => Ok((DEFAULT_MODE.to_string(), false)),
        Some(raw) => normalize(&raw).map(|v| (v, true)),
    }
}

pub fn normalize(raw: &str) -> Result<String, String> {
    let v = raw.trim().trim_matches('"');
    if MODES.contains(&v) {
        Ok(v.to_string())
    } else {
        Err(format!(
            "PII_MODE_INVALID: 出站脱敏档位非法「{v}」，仅支持 {}",
            MODES.join(" / ")
        ))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_store(name: &str) -> (Store, std::path::PathBuf) {
        let dir = std::env::temp_dir().join(format!(
            "tt-pii-{name}-{}",
            crate::bridge::event_store::now_ms()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let store = Store::open(dir.join("t.db")).unwrap();
        (store, dir)
    }

    #[test]
    fn defaults_to_off_and_reports_not_persisted() {
        let (store, dir) = temp_store("default");
        assert_eq!(read_mode(&store).unwrap(), ("off".to_string(), false));
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn accepts_known_modes_and_normalizes_quotes() {
        let (store, dir) = temp_store("modes");
        settings::set(&store, POLICY_KEY, "mask").unwrap();
        assert_eq!(read_mode(&store).unwrap(), ("mask".to_string(), true));
        settings::set(&store, POLICY_KEY, "\"block\"").unwrap();
        assert_eq!(read_mode(&store).unwrap(), ("block".to_string(), true));
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn invalid_mode_fails_closed_with_readable_code() {
        let (store, dir) = temp_store("bad");
        settings::set(&store, POLICY_KEY, "sometimes").unwrap();
        let err = read_mode(&store).unwrap_err();
        assert!(err.contains("PII_MODE_INVALID"), "{err}");
        std::fs::remove_dir_all(&dir).ok();
    }
}
