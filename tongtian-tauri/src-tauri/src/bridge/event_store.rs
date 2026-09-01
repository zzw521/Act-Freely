//! 事件持久化与重放窗口（流 D3，三层架构演进 P0）
//!
//! 依据需求分析报告：AgentEvent 事件持久化 24h（断线补发）+ SSE 重放窗口 ≥10min。
//! 设计要点（性能视角）：
//! - append-only JSONL 持久化，**异步批量刷盘**（64 条或 100ms flush，失败只记日志不阻塞事件流）
//! - 内存 `VecDeque` 重放窗口双上限（2048 条 + 10min，drop-oldest 防内存膨胀）
//! - 24h TTL 清理（惰性 + 周期扫描）
//! - 单任务文件容量上限 10MB，超限滚动重开

use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::{HashMap, VecDeque};
use std::path::PathBuf;
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};

/// 重放窗口条数上限（drop-oldest）
pub const REPLAY_WINDOW_MAX_EVENTS: usize = 2048;
/// 重放窗口时间上限（≥10min，报告附录 A 冻结值）
pub const REPLAY_WINDOW_MS: u64 = 10 * 60 * 1000;
/// 事件持久化保留时长（24h，报告 ADJ-R2-2）
pub const EVENT_TTL_MS: u64 = 24 * 60 * 60 * 1000;
/// 单任务 JSONL 文件容量上限（10MB）
pub const MAX_TASK_FILE_BYTES: u64 = 10 * 1024 * 1024;
/// 批量刷盘条数阈值
pub const FLUSH_BATCH: usize = 64;
/// 批量刷盘时间阈值（ms）
pub const FLUSH_INTERVAL_MS: u64 = 100;

pub fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

/// 持久化事件条目（JSONL 一行一条）
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct StoredEvent {
    pub seq: Option<u64>,
    /// 写入时间（epoch ms）
    pub ts: u64,
    /// 事件类型（plan/step_*/deliver/done/error/status_change/quota_warning）
    pub kind: String,
    pub data: Value,
}

/// 内存重放窗口：双上限 VecDeque（条数 + 时间），drop-oldest
#[derive(Debug, Default)]
pub struct ReplayWindow {
    entries: VecDeque<StoredEvent>,
}

impl ReplayWindow {
    pub fn new() -> Self {
        Self {
            entries: VecDeque::with_capacity(256),
        }
    }

    pub fn push(&mut self, ev: StoredEvent) {
        if self.entries.len() >= REPLAY_WINDOW_MAX_EVENTS {
            self.entries.pop_front();
        }
        self.entries.push_back(ev);
    }

    /// 返回 seq > from_seq 且未过期的事件（惰性清理过期头部）
    pub fn replay_after(&mut self, from_seq: u64) -> Vec<StoredEvent> {
        let now = now_ms();
        while let Some(front) = self.entries.front() {
            if now.saturating_sub(front.ts) > REPLAY_WINDOW_MS {
                self.entries.pop_front();
            } else {
                break;
            }
        }
        self
            .entries
            .iter()
            .filter(|e| e.seq.map(|s| s > from_seq).unwrap_or(true))
            .cloned()
            .collect()
    }

    pub fn len(&self) -> usize {
        self.entries.len()
    }

    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }
}

type FlushItem = (String, StoredEvent);

/// 事件存储：内存重放窗口 + append-only JSONL 异步批量刷盘
pub struct EventStore {
    dir: PathBuf,
    windows: Mutex<HashMap<String, ReplayWindow>>,
    tx: tokio::sync::mpsc::UnboundedSender<FlushItem>,
    rx: Mutex<Option<tokio::sync::mpsc::UnboundedReceiver<FlushItem>>>,
}

impl EventStore {
    pub fn new(dir: PathBuf) -> Self {
        let (tx, rx) = tokio::sync::mpsc::unbounded_channel();
        Self {
            dir,
            windows: Mutex::new(HashMap::new()),
            tx,
            rx: Mutex::new(Some(rx)),
        }
    }

    /// 追加事件：写内存窗口 + 投递刷盘通道（通道满/关闭不阻塞事件流）
    pub fn append(&self, task_id: &str, kind: &str, seq: Option<u64>, data: Value) -> StoredEvent {
        let ev = StoredEvent {
            seq,
            ts: now_ms(),
            kind: kind.to_string(),
            data,
        };
        if let Ok(mut map) = self.windows.lock() {
            map.entry(task_id.to_string())
                .or_insert_with(ReplayWindow::new)
                .push(ev.clone());
        }
        let _ = self.tx.send((task_id.to_string(), ev.clone()));
        ev
    }

    /// 重放：优先内存窗口；窗口过期时从 JSONL 文件兜底补齐（24h 内）
    pub fn replay(&self, task_id: &str, from_seq: u64) -> Vec<StoredEvent> {
        if let Ok(mut map) = self.windows.lock() {
            if let Some(w) = map.get_mut(task_id) {
                let in_window = w.replay_after(from_seq);
                if !in_window.is_empty() || !w.is_empty() {
                    return in_window;
                }
            }
        }
        // 内存窗口为空（如应用重启）：从磁盘读取 24h 内事件
        self.read_from_disk(task_id, from_seq)
    }

    fn read_from_disk(&self, task_id: &str, from_seq: u64) -> Vec<StoredEvent> {
        let path = self.task_file(task_id);
        let content = match std::fs::read_to_string(&path) {
            Ok(c) => c,
            Err(_) => return Vec::new(),
        };
        let now = now_ms();
        content
            .lines()
            .filter_map(|line| serde_json::from_str::<StoredEvent>(line).ok())
            .filter(|e| now.saturating_sub(e.ts) <= EVENT_TTL_MS)
            .filter(|e| e.seq.map(|s| s > from_seq).unwrap_or(true))
            .collect()
    }

    /// 启动后台刷盘任务（批量：≥64 条或 100ms 超时即 flush；失败仅记日志）
    pub fn start_flusher(self: &std::sync::Arc<Self>) -> tokio::task::JoinHandle<()> {
        let rx = self.rx.lock().ok().and_then(|mut g| g.take());
        let store = std::sync::Arc::clone(self);
        tokio::spawn(async move {
            let mut rx = match rx {
                Some(rx) => rx,
                None => return, // 已启动过
            };
            let mut batch: Vec<FlushItem> = Vec::with_capacity(FLUSH_BATCH);
            loop {
                // 首条阻塞等待，后续在 100ms 窗口内尽量攒批
                let first = rx.recv().await;
                match first {
                    Some(item) => batch.push(item),
                    None => break, // 通道关闭（应用退出）
                }
                let deadline = tokio::time::Instant::now()
                    + std::time::Duration::from_millis(FLUSH_INTERVAL_MS);
                while batch.len() < FLUSH_BATCH {
                    tokio::select! {
                        item = rx.recv() => match item {
                            Some(i) => batch.push(i),
                            None => break,
                        },
                        _ = tokio::time::sleep_until(deadline) => break,
                    }
                }
                store.flush_batch(&batch);
                batch.clear();
            }
            // 退出前刷掉残留
            if !batch.is_empty() {
                store.flush_batch(&batch);
            }
        })
    }

    /// 同步刷盘（供 flusher 与测试共用）：按任务分组追加写 JSONL
    pub fn flush_batch(&self, batch: &[FlushItem]) {
        let mut by_task: HashMap<&str, Vec<&StoredEvent>> = HashMap::new();
        for (task_id, ev) in batch {
            by_task.entry(task_id.as_str()).or_default().push(ev);
        }
        if let Err(err) = std::fs::create_dir_all(&self.dir) {
            eprintln!("[event_store] mkdir failed: {err}");
            return;
        }
        for (task_id, events) in by_task {
            let path = self.task_file(task_id);
            // 容量上限：超限滚动重开（旧事件仍在 24h TTL 内可被新文件覆盖期外的重放窗口兜底）
            if let Ok(meta) = std::fs::metadata(&path) {
                if meta.len() > MAX_TASK_FILE_BYTES {
                    let _ = std::fs::remove_file(&path);
                }
            }
            let mut payload = String::new();
            for ev in events {
                if let Ok(line) = serde_json::to_string(ev) {
                    payload.push_str(&line);
                    payload.push('\n');
                }
            }
            if let Err(err) = std::fs::OpenOptions::new()
                .create(true)
                .append(true)
                .open(&path)
                .and_then(|mut f| std::io::Write::write_all(&mut f, payload.as_bytes()))
            {
                // 持久化失败只记日志，不阻塞事件转发路径
                eprintln!("[event_store] flush failed for {task_id}: {err}");
            }
        }
    }

    /// 清理超过 24h TTL 的任务事件文件与内存窗口，返回清理数量
    pub fn cleanup_expired(&self) -> usize {
        let mut removed = 0usize;
        if let Ok(entries) = std::fs::read_dir(&self.dir) {
            let now = now_ms();
            for entry in entries.flatten() {
                let path = entry.path();
                if path.extension().and_then(|e| e.to_str()) != Some("jsonl") {
                    continue;
                }
                let expired = std::fs::metadata(&path)
                    .and_then(|m| m.modified())
                    .ok()
                    .and_then(|t| t.duration_since(UNIX_EPOCH).ok())
                    .map(|age| now.saturating_sub(age.as_millis() as u64) > EVENT_TTL_MS)
                    .unwrap_or(false);
                if expired {
                    if std::fs::remove_file(&path).is_ok() {
                        removed += 1;
                    }
                }
            }
        }
        removed
    }

    fn task_file(&self, task_id: &str) -> PathBuf {
        // 文件名消毒：防止路径穿越
        let safe: String = task_id
            .chars()
            .map(|c| if c.is_ascii_alphanumeric() || c == '-' || c == '_' { c } else { '_' })
            .collect();
        self.dir.join(format!("{safe}.jsonl"))
    }
}

/// 周期 TTL 清理循环（应用启动时 spawn 一次，每 10min 扫描）
pub fn spawn_ttl_cleanup_loop(
    store: std::sync::Arc<EventStore>,
) -> tokio::task::JoinHandle<()> {
    tokio::spawn(async move {
        let mut interval = tokio::time::interval(std::time::Duration::from_secs(600));
        interval.tick().await; // 首个 tick 立即返回，跳过
        loop {
            interval.tick().await;
            let n = store.cleanup_expired();
            if n > 0 {
                eprintln!("[event_store] ttl cleanup removed {n} files");
            }
        }
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn temp_store_dir(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "tt-event-store-test-{}-{}",
            name,
            now_ms()
        ));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn test_replay_window_seq_filter() {
        let mut w = ReplayWindow::new();
        for i in 1..=5 {
            w.push(StoredEvent {
                seq: Some(i),
                ts: now_ms(),
                kind: "step_done".into(),
                data: json!({}),
            });
        }
        let replay = w.replay_after(3);
        assert_eq!(replay.len(), 2);
        assert_eq!(replay[0].seq, Some(4));
        assert_eq!(replay[1].seq, Some(5));
    }

    #[test]
    fn test_replay_window_count_cap_drop_oldest() {
        let mut w = ReplayWindow::new();
        for i in 0..(REPLAY_WINDOW_MAX_EVENTS as u64 + 10) {
            w.push(StoredEvent {
                seq: Some(i),
                ts: now_ms(),
                kind: "step_progress".into(),
                data: json!({}),
            });
        }
        assert_eq!(w.len(), REPLAY_WINDOW_MAX_EVENTS);
        // 最旧的 10 条已被淘汰
        let replay = w.replay_after(0);
        assert_eq!(replay[0].seq, Some(10));
    }

    #[test]
    fn test_append_and_replay_in_memory() {
        let dir = temp_store_dir("append");
        let store = EventStore::new(dir);
        store.append("t1", "step_start", Some(1), json!({"stepId":"s1"}));
        store.append("t1", "done", Some(2), json!({}));
        let replay = store.replay("t1", 0);
        assert_eq!(replay.len(), 2);
        assert_eq!(replay[1].kind, "done");
        let replay_from_1 = store.replay("t1", 1);
        assert_eq!(replay_from_1.len(), 1);
        assert_eq!(replay_from_1[0].seq, Some(2));
    }

    #[test]
    fn test_flush_batch_writes_jsonl_and_disk_replay() {
        let dir = temp_store_dir("flush");
        let store = EventStore::new(dir.clone());
        let batch: Vec<FlushItem> = (1..=3)
            .map(|i| {
                (
                    "t2".to_string(),
                    StoredEvent {
                        seq: Some(i),
                        ts: now_ms(),
                        kind: "step_done".into(),
                        data: json!({"i": i}),
                    },
                )
            })
            .collect();
        store.flush_batch(&batch);

        // 新 store（模拟应用重启，内存窗口为空）从磁盘重放
        let store2 = EventStore::new(dir.clone());
        let replay = store2.replay("t2", 1);
        assert_eq!(replay.len(), 2);
        assert_eq!(replay[0].seq, Some(2));

        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_task_file_sanitization() {
        let dir = temp_store_dir("sanitize");
        let store = EventStore::new(dir.clone());
        let path = store.task_file("../../etc/passwd");
        assert!(path.starts_with(&dir));
        assert!(!path.to_string_lossy().contains(".."));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[tokio::test]
    async fn test_flusher_batches_and_writes() {
        let dir = temp_store_dir("flusher");
        let store = std::sync::Arc::new(EventStore::new(dir.clone()));
        let handle = store.start_flusher();
        store.append("t3", "done", Some(1), json!({}));
        // 等待 100ms 批量窗口触发刷盘
        tokio::time::sleep(std::time::Duration::from_millis(300)).await;
        drop(store.tx.clone()); // 保持通道存活；直接检查文件
        let file = dir.join("t3.jsonl");
        assert!(file.exists(), "flusher 应在 100ms 窗口内刷盘");
        let content = std::fs::read_to_string(&file).unwrap();
        assert!(content.contains("\"kind\":\"done\""));
        handle.abort();
        let _ = std::fs::remove_dir_all(&dir);
    }
}
