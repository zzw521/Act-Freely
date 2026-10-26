//! 任务栏未读角标（设置页「任务栏未读状态」的真实后端）
//!
//! 平台事实：
//! - Windows 任务栏不提供数字角标，只支持窗口图标叠加（overlay icon）。
//!   这里在运行时画出 32×32 的红色圆底 + 白色数字（1-9 / 9+），
//!   通过 `Window::set_overlay_icon` 挂到主窗口，count=0 时移除。
//! - macOS 使用系统 Dock 徽标计数（`Window::set_badge_count`）。
//! - 其他平台明确返回不支持，不做一个拨了没反应的开关。
//!
//! 渲染层只在「任务到了需要用户回来的节点且窗口不在前台」时累计未读，
//! 窗口回到前台后清零；开关关闭时即使累计也不下发。

use serde_json::{json, Value};
use tauri::{AppHandle, Manager};

/// 叠加图标上最多显示 9+，与系统角标的常见口径一致
const OVERLAY_MAX_DIGIT: u32 = 9;

/// 当前平台的任务栏未读能力（界面据此决定开关是否可操作）
pub fn support() -> Value {
    #[cfg(target_os = "windows")]
    {
        json!({
            "supported": true,
            "platform": "windows",
            "mode": "overlay",
            "detail": "Windows 通过任务栏图标叠加红色未读标记（1-9 / 9+），回到前台后自动清除",
        })
    }
    #[cfg(target_os = "macos")]
    {
        json!({
            "supported": true,
            "platform": "macos",
            "mode": "count",
            "detail": "macOS 在 Dock 图标上显示未读数量，回到前台后自动清除",
        })
    }
    #[cfg(not(any(target_os = "windows", target_os = "macos")))]
    {
        json!({
            "supported": false,
            "platform": std::env::consts::OS,
            "mode": "none",
            "detail": "当前平台的任务栏不提供未读角标能力",
        })
    }
}

#[tauri::command]
pub fn badge_support() -> Value {
    support()
}

/// 设置/清除主窗口未读角标。count=0 表示清除。
#[tauri::command]
pub fn badge_set(app: AppHandle, count: u32) -> Result<Value, String> {
    let count = count.min(99);
    let window = app
        .get_webview_window("main")
        .ok_or_else(|| "BADGE_WINDOW_MISSING: 主窗口不存在，无法更新任务栏角标".to_string())?;

    #[cfg(target_os = "windows")]
    {
        let icon = if count == 0 {
            None
        } else {
            Some(overlay_icon(count))
        };
        window
            .set_overlay_icon(icon)
            .map_err(|e| format!("BADGE_SET_FAILED: {e}"))?;
    }
    #[cfg(target_os = "macos")]
    {
        window
            .set_badge_count(if count == 0 { None } else { Some(count as i64) })
            .map_err(|e| format!("BADGE_SET_FAILED: {e}"))?;
    }
    #[cfg(not(any(target_os = "windows", target_os = "macos")))]
    {
        let _ = (window, count);
        return Err(format!(
            "BADGE_UNSUPPORTED: 当前平台（{}）不支持任务栏未读角标",
            std::env::consts::OS
        ));
    }

    Ok(json!({ "ok": true, "count": count }))
}

/// 3×5 点阵字模（只覆盖 0-9 与 +，足够画角标）
#[cfg(target_os = "windows")]
fn glyph(ch: char) -> Option<[[u8; 3]; 5]> {
    let g = match ch {
        '0' => [[1, 1, 1], [1, 0, 1], [1, 0, 1], [1, 0, 1], [1, 1, 1]],
        '1' => [[0, 1, 0], [1, 1, 0], [0, 1, 0], [0, 1, 0], [1, 1, 1]],
        '2' => [[1, 1, 1], [0, 0, 1], [1, 1, 1], [1, 0, 0], [1, 1, 1]],
        '3' => [[1, 1, 1], [0, 0, 1], [1, 1, 1], [0, 0, 1], [1, 1, 1]],
        '4' => [[1, 0, 1], [1, 0, 1], [1, 1, 1], [0, 0, 1], [0, 0, 1]],
        '5' => [[1, 1, 1], [1, 0, 0], [1, 1, 1], [0, 0, 1], [1, 1, 1]],
        '6' => [[1, 1, 1], [1, 0, 0], [1, 1, 1], [1, 0, 1], [1, 1, 1]],
        '7' => [[1, 1, 1], [0, 0, 1], [0, 1, 0], [0, 1, 0], [0, 1, 0]],
        '8' => [[1, 1, 1], [1, 0, 1], [1, 1, 1], [1, 0, 1], [1, 1, 1]],
        '9' => [[1, 1, 1], [1, 0, 1], [1, 1, 1], [0, 0, 1], [1, 1, 1]],
        '+' => [[0, 0, 0], [0, 1, 0], [1, 1, 1], [0, 1, 0], [0, 0, 0]],
        _ => return None,
    };
    Some(g)
}

/// 生成任务栏叠加图标：红底白字，边缘按覆盖率做抗锯齿。
/// 32×32 在 100% DPI 下会被系统缩到 16px，数字仍可辨认。
#[cfg(target_os = "windows")]
fn overlay_icon(count: u32) -> tauri::image::Image<'static> {
    const SIZE: u32 = 32;
    const SCALE: i32 = 3;
    const GLYPH_W: i32 = 3 * SCALE; // 9
    const GLYPH_H: i32 = 5 * SCALE; // 15
    const GAP: i32 = 2;

    let text = if count > OVERLAY_MAX_DIGIT {
        "9+".to_string()
    } else {
        count.to_string()
    };
    let chars: Vec<char> = text.chars().collect();
    let text_w = chars.len() as i32 * GLYPH_W + (chars.len() as i32 - 1).max(0) * GAP;
    let start_x = (SIZE as i32 - text_w) / 2;
    let start_y = (SIZE as i32 - GLYPH_H) / 2;

    // 先把字形展开成位图，再与圆底合成，避免斜边处出现半透明白点
    let mut mask = [[false; SIZE as usize]; SIZE as usize];
    for (i, ch) in chars.iter().enumerate() {
        let Some(g) = glyph(*ch) else { continue };
        let ox = start_x + i as i32 * (GLYPH_W + GAP);
        for (gy, row) in g.iter().enumerate() {
            for (gx, on) in row.iter().enumerate() {
                if *on == 0 {
                    continue;
                }
                for sy in 0..SCALE {
                    for sx in 0..SCALE {
                        let x = ox + gx as i32 * SCALE + sx;
                        let y = start_y + gy as i32 * SCALE + sy;
                        if (0..SIZE as i32).contains(&x) && (0..SIZE as i32).contains(&y) {
                            mask[y as usize][x as usize] = true;
                        }
                    }
                }
            }
        }
    }

    let center = SIZE as f32 / 2.0;
    let radius = SIZE as f32 / 2.0 - 1.0;
    let mut rgba = vec![0u8; (SIZE * SIZE * 4) as usize];
    for y in 0..SIZE {
        for x in 0..SIZE {
            let dx = x as f32 + 0.5 - center;
            let dy = y as f32 + 0.5 - center;
            let dist = (dx * dx + dy * dy).sqrt();
            // 圆外透明；边缘一个像素内做覆盖率过渡，缩小后不出现锯齿
            let coverage = (radius - dist + 0.5).clamp(0.0, 1.0);
            if coverage <= 0.0 {
                continue;
            }
            let (r, g, b) = if mask[y as usize][x as usize] {
                (255u8, 255u8, 255u8)
            } else {
                (225u8, 72u8, 77u8)
            };
            let idx = ((y * SIZE + x) * 4) as usize;
            rgba[idx] = r;
            rgba[idx + 1] = g;
            rgba[idx + 2] = b;
            rgba[idx + 3] = (coverage * 255.0).round() as u8;
        }
    }
    tauri::image::Image::new_owned(rgba, SIZE, SIZE)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn support_is_platform_truthful() {
        let v = support();
        assert!(v["supported"].is_boolean(), "{v}");
        assert!(v["platform"].is_string(), "{v}");
        assert!(v["mode"].is_string(), "{v}");
        let detail = v["detail"].as_str().unwrap_or_default();
        assert!(!detail.is_empty(), "能力说明不能为空：{v}");
        #[cfg(target_os = "windows")]
        assert_eq!(v["mode"], json!("overlay"));
        #[cfg(target_os = "macos")]
        assert_eq!(v["mode"], json!("count"));
    }

    #[cfg(target_os = "windows")]
    #[test]
    fn overlay_icon_has_alpha_and_center_is_opaque() {
        let img = overlay_icon(3);
        assert_eq!(img.width(), 32);
        assert_eq!(img.height(), 32);
        let rgba = img.rgba();
        assert_eq!(rgba.len(), 32 * 32 * 4);
        let center = ((16 * 32 + 16) * 4) as usize;
        assert_eq!(rgba[center + 3], 255, "圆心必须不透明");
        for (x, y) in [(0, 0), (31, 0), (0, 31), (31, 31)] {
            let a = rgba[((y * 32 + x) * 4 + 3) as usize];
            assert_eq!(a, 0, "四角应透明：({x},{y})");
        }
    }

    #[cfg(target_os = "windows")]
    #[test]
    fn overlay_digit_and_9plus_differ_but_stay_inside_circle() {
        let one = overlay_icon(1);
        let many = overlay_icon(42);
        assert_ne!(one.rgba(), many.rgba(), "不同数量应画出不同字形");
        // 角上像素透明，说明没有画出圆外
        for (x, y) in [(0, 0), (31, 0), (0, 31), (31, 31)] {
            let a = one.rgba()[((y * 32 + x) * 4 + 3) as usize];
            assert_eq!(a, 0, "圆外像素应为透明：({x},{y})");
        }
    }
}
