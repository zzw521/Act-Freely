//! 密钥保管箱（keyring 优先，不可用时内存降级）
//!
//! 原则：API Key 永不落库/落盘（密钥永不上云的本地镜像），
//! DB 仅保存 `has_api_key` 引用标记。

use std::collections::HashMap;
use std::sync::{Mutex, MutexGuard};

/// 进程内串行化凭据管理器调用。
/// keyring 2.3.3 的 Windows 后端文档明确写明：多线程并发读写不保证顺序，
/// crate 自身测试在 Win10/11 上观测到偶发失败。全量测试并发跑模型/账号两组
/// keyring 用例时，出现过「写入返回 Ok、紧接着读到 NoEntry」的假失败。
/// 凭据读写只在启动、保存配置、刷新会话时发生，串行化的代价可以忽略。
static KEYRING_LOCK: Mutex<()> = Mutex::new(());

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

    fn keyring_guard() -> MutexGuard<'static, ()> {
        KEYRING_LOCK.lock().unwrap_or_else(|e| e.into_inner())
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
        let _guard = Self::keyring_guard();
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
        let _guard = Self::keyring_guard();
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
        let _guard = Self::keyring_guard();
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

    /// 多实例并发读写不同账号：每次写后立刻读回自己的值。
    /// 没有进程内串行化时，Windows 凭据管理器偶发「写成功后读不到」，
    /// 会让模型/账号两组测试在全量并行下互相干扰。
    #[test]
    fn test_concurrent_roundtrip_is_serialized() {
        let stamp = crate::bridge::event_store::now_ms();
        let handles: Vec<_> = (0..3)
            .map(|t| {
                let stamp = stamp;
                std::thread::spawn(move || {
                    let vault = Vault::new("tongtian-tauri-test");
                    for i in 0..3 {
                        let account = format!("unit-conc-{stamp}-{t}-{i}");
                        let secret = format!("sk-{t}-{i}");
                        vault.set_secret(&account, &secret).unwrap();
                        assert_eq!(
                            vault.get_secret(&account).as_deref(),
                            Some(secret.as_str()),
                            "并发写入后读不到刚写入的密钥：{account}"
                        );
                        vault.delete_secret(&account);
                    }
                })
            })
            .collect();
        for h in handles {
            h.join().unwrap();
        }
    }
}
