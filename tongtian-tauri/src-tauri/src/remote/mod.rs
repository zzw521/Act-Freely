//! 远程桌面（本机实时画面）
//!
//! 边界先说清楚：当前版本只做「本机桌面 → 当前窗口」的受控实时画面，
//! 不做跨主机转发、不做远程键鼠注入——那需要中继服务与主机配对，尚未提供。
//!
//! 安全约束：
//! - 开始会话必须经过 `computer::gate`：每次询问档位弹系统确认框，禁用档位直接拒绝；
//! - 会话最长 10 分钟，到期自动结束；帧间隔不小于 700ms，避免把截屏变成录屏；
//! - 只看不控：本模块不提供任何输入通道；
//! - 审计只记开始/结束/到期与帧数，画面内容与尺寸细节不入审计、不落盘。

use std::sync::{Mutex, MutexGuard};

use serde_json::{json, Value};

use crate::bridge::event_store::now_ms;
use crate::computer;

/// 单次会话上限：10 分钟。到点即失效，续期必须重新确认。
pub const SESSION_MAX_MS: u64 = 10 * 60 * 1000;
/// 帧间隔下限：约 1.4 fps，足够观察界面变化又不至于变成持续录屏。
pub const FRAME_MIN_INTERVAL_MS: u64 = 700;

pub const REMOTE_UNSUPPORTED: &str =
    "REMOTE_UNSUPPORTED: 当前平台没有桌面截屏执行器（仅支持 Windows）";
pub const REMOTE_NO_SESSION: &str = "REMOTE_NO_SESSION: 远程桌面会话未开启或已结束";
pub const REMOTE_SESSION_EXPIRED: &str =
    "REMOTE_SESSION_EXPIRED: 远程桌面会话已到期（单次最长 10 分钟），请重新开启";
pub const REMOTE_DISABLED: &str =
    "REMOTE_DISABLED: 电脑操控档位为禁用，远程桌面无法开启；请先在「电脑操控」页调整档位";

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Session {
    /// 会话标识：截屏耗时期间若会话被结束/替换，用它对账，避免把帧写进新会话
    pub id: u64,
    pub started_at_ms: u64,
    pub expires_at_ms: u64,
    /// 0 表示尚未取过帧（首帧不受节流限制）
    pub last_frame_ms: u64,
    pub frames: u64,
}

#[derive(Debug, PartialEq, Eq)]
pub enum FrameDecision {
    Capture,
    Throttled { wait_ms: u64 },
    Expired,
}

#[derive(Default)]
pub struct RemoteDesktopState {
    session: Mutex<Option<Session>>,
}

impl RemoteDesktopState {
    fn lock(&self) -> MutexGuard<'_, Option<Session>> {
        self.session.lock().unwrap_or_else(|e| e.into_inner())
    }
}

pub fn new_state() -> RemoteDesktopState {
    RemoteDesktopState::default()
}

fn next_id() -> u64 {
    use std::sync::atomic::{AtomicU64, Ordering};
    static NEXT: AtomicU64 = AtomicU64::new(1);
    NEXT.fetch_add(1, Ordering::Relaxed)
}

pub fn new_session(now: u64, max_ms: u64) -> Session {
    Session {
        id: next_id(),
        started_at_ms: now,
        expires_at_ms: now.saturating_add(max_ms),
        last_frame_ms: 0,
        frames: 0,
    }
}

/// 纯函数：给定会话与当前时刻，判断该截屏、节流还是已到期（便于离线单测）
pub fn frame_decision(session: &Session, now: u64, interval_ms: u64) -> FrameDecision {
    if now >= session.expires_at_ms {
        return FrameDecision::Expired;
    }
    if session.last_frame_ms > 0 {
        let next = session.last_frame_ms.saturating_add(interval_ms);
        if now < next {
            return FrameDecision::Throttled { wait_ms: next - now };
        }
    }
    FrameDecision::Capture
}

fn is_active(session: &Session, now: u64) -> bool {
    now < session.expires_at_ms
}

fn session_view(session: Option<&Session>, now: u64) -> Value {
    match session {
        Some(s) if is_active(s, now) => json!({
            "active": true,
            "startedAtMs": s.started_at_ms,
            "expiresAtMs": s.expires_at_ms,
            "remainingMs": s.expires_at_ms - now,
            "frames": s.frames,
            "frameIntervalMs": FRAME_MIN_INTERVAL_MS,
            "sessionMaxMs": SESSION_MAX_MS,
        }),
        Some(s) => json!({
            "active": false,
            "startedAtMs": s.started_at_ms,
            "expiresAtMs": s.expires_at_ms,
            "remainingMs": 0,
            "frames": s.frames,
            "frameIntervalMs": FRAME_MIN_INTERVAL_MS,
            "sessionMaxMs": SESSION_MAX_MS,
        }),
        None => json!({
            "active": false,
            "startedAtMs": 0,
            "expiresAtMs": 0,
            "remainingMs": 0,
            "frames": 0,
            "frameIntervalMs": FRAME_MIN_INTERVAL_MS,
            "sessionMaxMs": SESSION_MAX_MS,
        }),
    }
}

fn cross_host_boundary() -> Value {
    json!({
        "supported": false,
        "reason": "当前版本只提供本机桌面实时画面；跨主机转发需要中继服务与主机配对，尚未提供",
    })
}

fn audit_session(p0: &crate::P0Services, severity: &str, detail: &str) {
    crate::security::audit::record_quiet(
        &p0.store,
        crate::security::audit::ACTION_REMOTE_SESSION,
        severity,
        "本机桌面",
        detail,
    );
}

// ── Tauri commands ──────────────────────────────────────────

#[tauri::command]
pub async fn remote_desktop_capabilities() -> Result<Value, String> {
    let supported = computer::supported();
    Ok(json!({
        "supported": supported,
        "platform": computer::platform(),
        "localView": supported,
        "frameIntervalMs": FRAME_MIN_INTERVAL_MS,
        "sessionMaxMs": SESSION_MAX_MS,
        "reason": if supported {
            ""
        } else {
            "当前平台没有桌面截屏执行器，远程桌面仅在 Windows 桌面版可用"
        },
        "crossHost": cross_host_boundary(),
    }))
}

#[tauri::command]
pub async fn remote_desktop_status(
    state: tauri::State<'_, RemoteDesktopState>,
) -> Result<Value, String> {
    let now = now_ms();
    Ok(session_view(state.lock().as_ref(), now))
}

/// 开启会话：已有活跃会话时幂等返回，不重复弹确认框；
/// 否则按电脑操控档位确认后签发 10 分钟会话。
#[tauri::command]
pub async fn remote_desktop_start(
    p0: tauri::State<'_, std::sync::Arc<crate::P0Services>>,
    state: tauri::State<'_, RemoteDesktopState>,
) -> Result<Value, String> {
    if !computer::supported() {
        return Err(REMOTE_UNSUPPORTED.into());
    }
    let now = now_ms();
    {
        let mut guard = state.lock();
        if let Some(existing) = guard.as_ref() {
            if is_active(existing, now) {
                return Ok(json!({
                    "ok": true,
                    "reused": true,
                    "session": session_view(Some(existing), now),
                }));
            }
            // 到期残留：先清掉再签发新会话，避免 status 显示一个已失效的旧会话
            let _ = guard.take();
        }
    }

    let (policy, _) = computer::read_policy(&p0.store)
        .map_err(|e| format!("REMOTE_DISABLED: 电脑操控档位不可用，远程桌面无法开启：{e}"))?;
    if policy == "disabled" {
        return Err(REMOTE_DISABLED.into());
    }
    let description =
        "开启本机桌面实时画面（最长 10 分钟；画面只回传当前窗口，不写入磁盘）".to_string();
    let policy_for_gate = policy.clone();
    tauri::async_runtime::spawn_blocking(move || computer::gate(&policy_for_gate, &description))
        .await
        .map_err(|e| format!("COMPUTER_USE_ACTION_FAILED: 确认框线程异常：{e}"))??;

    let session = new_session(now, SESSION_MAX_MS);
    {
        let mut guard = state.lock();
        *guard = Some(session.clone());
    }
    audit_session(
        &p0,
        crate::security::audit::SEV_INFO,
        "开始本机桌面实时画面（上限 10 分钟，只看不控，画面不落盘）",
    );
    Ok(json!({
        "ok": true,
        "reused": false,
        "session": session_view(Some(&session), now),
    }))
}

/// 取一帧：到期即清理并报错；间隔不足返回 throttled（不截屏）；正常截屏返回 PNG。
#[tauri::command]
pub async fn remote_desktop_frame(
    p0: tauri::State<'_, std::sync::Arc<crate::P0Services>>,
    state: tauri::State<'_, RemoteDesktopState>,
) -> Result<Value, String> {
    if !computer::supported() {
        return Err(REMOTE_UNSUPPORTED.into());
    }
    let now = now_ms();
    let mut expired: Option<Session> = None;
    let (session_id, decision) = {
        let mut guard = state.lock();
        let Some(session) = guard.as_ref() else {
            return Err(REMOTE_NO_SESSION.into());
        };
        let decision = frame_decision(session, now, FRAME_MIN_INTERVAL_MS);
        if decision == FrameDecision::Expired {
            expired = guard.take();
            (0, FrameDecision::Expired)
        } else {
            (session.id, decision)
        }
    };
    if let Some(s) = expired {
        audit_session(
            &p0,
            crate::security::audit::SEV_WARNING,
            &format!("远程桌面会话到期自动结束（共 {} 帧）", s.frames),
        );
        return Err(REMOTE_SESSION_EXPIRED.into());
    }

    if let FrameDecision::Throttled { wait_ms } = decision {
        let guard = state.lock();
        let view = session_view(guard.as_ref(), now);
        return Ok(json!({
            "ok": true,
            "throttled": true,
            "waitMs": wait_ms,
            "capturedAtMs": guard.as_ref().map(|s| s.last_frame_ms).unwrap_or(0),
            "session": view,
        }));
    }

    let cap = tauri::async_runtime::spawn_blocking(computer::capture)
        .await
        .map_err(|e| format!("COMPUTER_USE_CAPTURE_FAILED: 截屏线程异常：{e}"))??;
    let now2 = now_ms();
    let mut expired: Option<Session> = None;
    let stopped = {
        let mut guard = state.lock();
        let (same_session, active) = guard
            .as_ref()
            .map(|s| (s.id == session_id, is_active(s, now2)))
            .unwrap_or((false, false));
        if same_session && active {
            if let Some(s) = guard.as_mut() {
                s.last_frame_ms = now2;
                s.frames += 1;
            }
            false
        } else if same_session {
            expired = guard.take();
            false
        } else {
            true
        }
    };
    if let Some(s) = expired {
        audit_session(
            &p0,
            crate::security::audit::SEV_WARNING,
            &format!("远程桌面会话到期自动结束（共 {} 帧）", s.frames),
        );
        return Err(REMOTE_SESSION_EXPIRED.into());
    }
    if stopped {
        // 截屏期间用户点了结束：不再把这一帧交出去，也不复活会话
        return Err(REMOTE_NO_SESSION.into());
    }

    let view = {
        let guard = state.lock();
        let session = guard.as_ref().expect("session updated above");
        use base64::Engine as _;
        json!({
            "ok": true,
            "throttled": false,
            "mime": "image/png",
            "imageBase64": base64::engine::general_purpose::STANDARD.encode(&cap.png),
            "width": cap.width,
            "height": cap.height,
            "originalWidth": cap.original_width,
            "originalHeight": cap.original_height,
            "originX": cap.origin_x,
            "originY": cap.origin_y,
            "scaled": cap.scaled,
            "capturedAtMs": now2,
            "frames": session.frames,
            "expiresAtMs": session.expires_at_ms,
            "remainingMs": session.expires_at_ms.saturating_sub(now2),
        })
    };
    Ok(view)
}

/// 结束会话（幂等）：返回本次会话的帧数与持续时长，供页面与审计对账。
#[tauri::command]
pub async fn remote_desktop_stop(
    p0: tauri::State<'_, std::sync::Arc<crate::P0Services>>,
    state: tauri::State<'_, RemoteDesktopState>,
) -> Result<Value, String> {
    let removed = state.lock().take();
    let now = now_ms();
    match removed {
        Some(s) => {
            audit_session(
                &p0,
                crate::security::audit::SEV_INFO,
                &format!(
                    "结束本机桌面实时画面（共 {} 帧，持续 {} 秒）",
                    s.frames,
                    now.saturating_sub(s.started_at_ms) / 1000
                ),
            );
            Ok(json!({
                "ok": true,
                "active": false,
                "frames": s.frames,
                "durationMs": now.saturating_sub(s.started_at_ms),
                "session": session_view(None, now),
            }))
        }
        None => Ok(json!({
            "ok": true,
            "active": false,
            "frames": 0,
            "durationMs": 0,
            "session": session_view(None, now),
        })),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn first_frame_captures_without_waiting() {
        let s = new_session(1_000, SESSION_MAX_MS);
        assert_eq!(frame_decision(&s, 1_000, FRAME_MIN_INTERVAL_MS), FrameDecision::Capture);
    }

    #[test]
    fn frames_are_throttled_between_captures() {
        let mut s = new_session(1_000, SESSION_MAX_MS);
        s.last_frame_ms = 1_000;
        s.frames = 1;
        assert_eq!(
            frame_decision(&s, 1_200, FRAME_MIN_INTERVAL_MS),
            FrameDecision::Throttled { wait_ms: 500 }
        );
        assert_eq!(
            frame_decision(&s, 1_700, FRAME_MIN_INTERVAL_MS),
            FrameDecision::Capture
        );
    }

    #[test]
    fn session_expires_after_max_duration() {
        let s = new_session(1_000, SESSION_MAX_MS);
        let expires = 1_000 + SESSION_MAX_MS;
        assert_eq!(
            frame_decision(&s, expires - 1, FRAME_MIN_INTERVAL_MS),
            FrameDecision::Capture
        );
        assert_eq!(
            frame_decision(&s, expires, FRAME_MIN_INTERVAL_MS),
            FrameDecision::Expired
        );
    }

    #[test]
    fn status_view_reports_remaining_and_never_lies_about_expiry() {
        let s = new_session(5_000, SESSION_MAX_MS);
        let active = session_view(Some(&s), 5_000);
        assert_eq!(active["active"], json!(true));
        assert_eq!(active["remainingMs"].as_u64(), Some(SESSION_MAX_MS));
        let expired = session_view(Some(&s), 5_000 + SESSION_MAX_MS);
        assert_eq!(expired["active"], json!(false));
        assert_eq!(expired["remainingMs"].as_u64(), Some(0));
        assert_eq!(session_view(None, 0)["active"], json!(false));
    }

    #[test]
    fn cross_host_is_explicitly_unsupported() {
        let c = cross_host_boundary();
        assert_eq!(c["supported"], json!(false));
        assert!(c["reason"].as_str().unwrap().contains("尚未提供"));
    }

    #[test]
    fn capabilities_do_not_claim_local_view_on_unsupported_platform() {
        // 测试环境可能不是 Windows；断言的是「承诺与平台能力一致」
        assert_eq!(computer::supported(), cfg!(target_os = "windows"));
    }
}
