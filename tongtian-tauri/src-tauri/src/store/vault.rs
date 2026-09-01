//! 密钥保管箱（keyring 优先，不可用时内存降级）
//!
//! 原则：API Key 永不落库/落盘（密钥永不上云的本地镜像），
//! DB 仅保存 `has_api_key` 引用标记。

use std::collections::HashMap;
use std::sync::Mutex;

pub struct Vault {
    service: String,
    /// keyring 不可用（如 headless CI）时的内存降级存储
    fallback: Mutex<HashMap<String, String>>,
    /// 是否已探测到 keyring 不可用（避免重复告警）
    keyring_broken: Mutex<bool>,
}

impl Vault {
    pub fn new(service: &str) -> Self {
        Self {
            service: service.to_string(),
            fallback: Mutex::new(HashMap::new()),
            keyring_broken: Mutex::new(false),
        }
    }

    fn entry(&self, account: &str) -> Result<keyring::Entry, keyring::Error> {
        keyring::Entry::new(&self.service, account)
    }

    fn mark_broken(&self, err: &keyring::Error) {
        let mut broken = self.keyring_broken.lock().unwrap_or_else(|e| e.into_inner());
        if !*broken {
            eprintln!("[vault] keyring 不可用，降级内存保管（进程退出即清除）: {err}");
            *broken = true;
        }
    }

    /// 存入密钥
    pub fn set_secret(&self, account: &str, secret: &str) -> Result<(), String> {
        match self.entry(account) {
            Ok(entry) => match entry.set_password(secret) {
                Ok(()) => return Ok(()),
                Err(e) => self.mark_broken(&e),
            },
            Err(e) => self.mark_broken(&e),
        }
        // 内存降级
        self.fallback
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .insert(account.to_string(), secret.to_string());
        Ok(())
    }

    /// 读取密钥（keyring 优先，降级查内存）
    pub fn get_secret(&self, account: &str) -> Option<String> {
        if let Ok(entry) = self.entry(account) {
            match entry.get_password() {
                Ok(s) => return Some(s),
                Err(keyring::Error::NoEntry) => {}
                Err(e) => self.mark_broken(&e),
            }
        }
        self.fallback
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .get(account)
            .cloned()
    }

    /// 删除密钥
    pub fn delete_secret(&self, account: &str) {
        if let Ok(entry) = self.entry(account) {
            match entry.delete_password() {
                Ok(()) => {}
                Err(keyring::Error::NoEntry) => {}
                Err(e) => self.mark_broken(&e),
            }
        }
        self.fallback
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .remove(account);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 使用唯一 account 避免与其他测试/真实数据冲突；
    /// keyring 可用时走凭据管理器，不可用时自动内存降级，两路径均覆盖。
    #[test]
    fn test_set_get_delete_roundtrip() {
        let vault = Vault::new("tongtian-tauri-test");
        let account = format!("unit-{}", crate::bridge::event_store::now_ms());
        vault.set_secret(&account, "sk-test-123").unwrap();
        assert_eq!(vault.get_secret(&account).as_deref(), Some("sk-test-123"));
        vault.delete_secret(&account);
        assert_eq!(vault.get_secret(&account), None);
    }

    #[test]
    fn test_missing_secret_returns_none() {
        let vault = Vault::new("tongtian-tauri-test");
        assert_eq!(vault.get_secret("never-set-account"), None);
    }

    #[test]
    fn test_overwrite_secret() {
        let vault = Vault::new("tongtian-tauri-test");
        let account = format!("unit-ow-{}", crate::bridge::event_store::now_ms());
        vault.set_secret(&account, "v1").unwrap();
        vault.set_secret(&account, "v2").unwrap();
        assert_eq!(vault.get_secret(&account).as_deref(), Some("v2"));
        vault.delete_secret(&account);
    }
}
