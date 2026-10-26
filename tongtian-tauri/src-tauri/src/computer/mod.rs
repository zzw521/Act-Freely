//! 电脑操控执行器（Windows）
//!
//! 与 #63 的「受控命令执行」同一条原则：配置 ≠ 授权，授权之外还要逐次可控。
//! - 策略三档：每次询问（默认，系统确认框）/ 自动执行 / 禁用；
//! - 能力只在 Windows 桌面壳内可用：GDI 截屏 + SendInput 键鼠注入，不依赖外部程序；
//! - 截屏与操作都由本机回环网关执行并落审计，引擎拿不到直接调用系统 API 的能力；
//! - 输入前统一校验坐标与按键组合，Win 键组合与任务管理器快捷键直接拒绝。
//!
//! 隐私边界：截屏只在用户确认后发生（「自动执行」档位是用户显式选择的授权）；
//! 审计只记动作、坐标、按键组合与文本长度，不记输入的正文与截图内容。

use serde_json::{json, Value};

use crate::store::{settings, Store};

pub const POLICY_KEY: &str = "computerUsePolicy";
pub const POLICIES: [&str; 3] = ["ask_every_time", "auto", "disabled"];
pub const DEFAULT_POLICY: &str = "ask_every_time";

/// 截图最长边上限：超过就等比降采样，避免一张 4K 截图撑爆模型上下文
pub const MAX_SHOT_WIDTH: u32 = 1600;
/// 单次输入文本上限（字符数）
pub const MAX_TYPE_CHARS: usize = 4000;
/// 按键组合最多 4 段（3 个修饰键 + 1 个主键）
pub const MAX_KEY_PARTS: usize = 4;

pub fn platform() -> &'static str {
    std::env::consts::OS
}

pub fn supported() -> bool {
    cfg!(target_os = "windows")
}

pub fn features() -> Value {
    json!({ "screenshot": supported(), "input": supported() })
}

/// 策略归一：未保存过用默认档；已保存但值非法时 fail-closed 返回错误，
/// 由调用方如实说明「本轮电脑操控不可用」，而不是猜一个档位。
pub fn read_policy(store: &Store) -> Result<(String, bool), String> {
    let stored = settings::get(store, POLICY_KEY)?;
    match stored {
        None => Ok((DEFAULT_POLICY.to_string(), false)),
        Some(raw) => {
            let v = raw.trim().trim_matches('"');
            if POLICIES.contains(&v) {
                Ok((v.to_string(), true))
            } else {
                Err(format!(
                    "COMPUTER_USE_INVALID_POLICY: 电脑操控档位非法「{v}」，仅支持 {}",
                    POLICIES.join(" / ")
                ))
            }
        }
    }
}

pub fn normalize_policy(raw: &str) -> Result<String, String> {
    let v = raw.trim().trim_matches('"');
    if POLICIES.contains(&v) {
        Ok(v.to_string())
    } else {
        Err(format!(
            "COMPUTER_USE_INVALID_POLICY: 电脑操控档位非法「{v}」，仅支持 {}",
            POLICIES.join(" / ")
        ))
    }
}

// ── 动作解析与描述 ───────────────────────────────────────────

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Button {
    Left,
    Right,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Action {
    Click { x: i32, y: i32, button: Button, count: u8 },
    Move { x: i32, y: i32 },
    Scroll { x: Option<i32>, y: Option<i32>, amount: i32 },
    Type { text: String },
    Key { combo: KeyCombo },
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct KeyCombo {
    pub modifiers: Vec<u16>,
    pub key: u16,
}

fn as_i32(v: &Value, key: &str) -> Result<i32, String> {
    let n = v
        .get(key)
        .and_then(Value::as_i64)
        .ok_or_else(|| format!("COMPUTER_USE_INVALID_ACTION: 缺少整数参数 {key}"))?;
    if n < i32::MIN as i64 || n > i32::MAX as i64 {
        return Err(format!("COMPUTER_USE_INVALID_ACTION: {key} 超出范围"));
    }
    Ok(n as i32)
}

fn opt_i32(v: &Value, key: &str) -> Result<Option<i32>, String> {
    match v.get(key) {
        None | Some(Value::Null) => Ok(None),
        Some(_) => as_i32(v, key).map(Some),
    }
}

/// 解析引擎传来的动作参数（不执行系统调用，便于离线单测）
pub fn parse_action(v: &Value) -> Result<Action, String> {
    let action = v
        .get("action")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .to_ascii_lowercase();
    match action.as_str() {
        "click" | "double_click" | "right_click" => Ok(Action::Click {
            x: as_i32(v, "x")?,
            y: as_i32(v, "y")?,
            button: if action == "right_click" { Button::Right } else { Button::Left },
            count: if action == "double_click" { 2 } else { 1 },
        }),
        "move" => Ok(Action::Move {
            x: as_i32(v, "x")?,
            y: as_i32(v, "y")?,
        }),
        "scroll" => {
            let amount = as_i32(v, "amount")?;
            if amount == 0 || amount.abs() > 20 {
                return Err("COMPUTER_USE_INVALID_ACTION: 滚动格数必须在 -20 到 20 之间且不为 0".into());
            }
            Ok(Action::Scroll {
                x: opt_i32(v, "x")?,
                y: opt_i32(v, "y")?,
                amount,
            })
        }
        "type" => {
            let text = v
                .get("text")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string();
            if text.is_empty() {
                return Err("COMPUTER_USE_INVALID_ACTION: type 动作缺少 text".into());
            }
            let chars = text.chars().count();
            if chars > MAX_TYPE_CHARS {
                return Err(format!(
                    "COMPUTER_USE_INVALID_ACTION: 单次输入最多 {MAX_TYPE_CHARS} 个字符（收到 {chars}）"
                ));
            }
            Ok(Action::Type { text })
        }
        "key" => {
            let keys = v.get("keys").and_then(Value::as_str).unwrap_or("");
            Ok(Action::Key { combo: parse_key_combo(keys)? })
        }
        "" => Err("COMPUTER_USE_INVALID_ACTION: 缺少 action".into()),
        other => Err(format!(
            "COMPUTER_USE_INVALID_ACTION: 不支持的动作「{other}」，可用 click / double_click / right_click / move / scroll / type / key"
        )),
    }
}

/// 给用户看/写审计的动作描述。`type` 只报字符数：正文可能含密码等敏感内容。
pub fn describe_action(a: &Action) -> String {
    match a {
        Action::Click { x, y, button, count } => {
            let what = match (button, count) {
                (Button::Right, _) => "右键点击",
                (_, 2) => "双击",
                _ => "点击",
            };
            format!("{what}屏幕坐标 ({x}, {y})")
        }
        Action::Move { x, y } => format!("移动鼠标到 ({x}, {y})"),
        Action::Scroll { x, y, amount } => {
            let at = match (x, y) {
                (Some(x), Some(y)) => format!("在 ({x}, {y}) "),
                _ => String::new(),
            };
            format!("{at}滚动 {} 格", amount.abs())
        }
        Action::Type { text } => format!("输入文本（{} 个字符）", text.chars().count()),
        Action::Key { combo } => format!("按键 {}", combo.display()),
    }
}

impl KeyCombo {
    pub fn display(&self) -> String {
        let mut parts: Vec<String> = self.modifiers.iter().map(|v| vk_name(*v)).collect();
        parts.push(vk_name(self.key));
        parts.join("+")
    }
}

fn vk_name(vk: u16) -> String {
    match vk {
        0x11 => "ctrl".into(),
        0x10 => "shift".into(),
        0x12 => "alt".into(),
        0x0D => "enter".into(),
        0x1B => "esc".into(),
        0x09 => "tab".into(),
        0x20 => "space".into(),
        0x08 => "backspace".into(),
        0x2E => "delete".into(),
        0x2D => "insert".into(),
        0x24 => "home".into(),
        0x23 => "end".into(),
        0x21 => "pageup".into(),
        0x22 => "pagedown".into(),
        0x25 => "left".into(),
        0x26 => "up".into(),
        0x27 => "right".into(),
        0x28 => "down".into(),
        v @ 0x70..=0x7B => format!("f{}", v - 0x6F),
        v @ 0x41..=0x5A => ((v as u8 as char).to_ascii_lowercase()).to_string(),
        v @ 0x30..=0x39 => ((v as u8 as char)).to_string(),
        _ => format!("vk-{vk:02x}"),
    }
}

fn vk_for(name: &str) -> Option<u16> {
    let simple = match name {
        "enter" | "return" => 0x0D,
        "esc" | "escape" => 0x1B,
        "tab" => 0x09,
        "space" => 0x20,
        "backspace" => 0x08,
        "delete" | "del" => 0x2E,
        "insert" | "ins" => 0x2D,
        "home" => 0x24,
        "end" => 0x23,
        "pageup" | "pgup" => 0x21,
        "pagedown" | "pgdn" => 0x22,
        "left" => 0x25,
        "up" => 0x26,
        "right" => 0x27,
        "down" => 0x28,
        "-" => 0xBD,
        "=" => 0xBB,
        "[" => 0xDB,
        "]" => 0xDD,
        "\\" => 0xDC,
        ";" => 0xBA,
        "'" => 0xDE,
        "," => 0xBC,
        "." => 0xBE,
        "/" => 0xBF,
        "`" => 0xC0,
        _ => 0,
    };
    if simple != 0 {
        return Some(simple);
    }
    if let Some(rest) = name.strip_prefix('f') {
        if let Ok(n) = rest.parse::<u16>() {
            if (1..=12).contains(&n) {
                return Some(0x70 + n - 1);
            }
        }
    }
    let mut chars = name.chars();
    if let (Some(c), None) = (chars.next(), chars.next()) {
        if c.is_ascii_alphabetic() {
            return Some(0x41 + (c.to_ascii_uppercase() as u16 - b'A' as u16));
        }
        if c.is_ascii_digit() {
            return Some(0x30 + (c as u16 - b'0' as u16));
        }
    }
    None
}

/// 解析 `ctrl+shift+t` 形式的组合键：修饰键在前，只允许一个主键。
/// Win 键组合与任务管理器快捷键直接拒绝——它们能绕过受控命令执行的边界。
pub fn parse_key_combo(raw: &str) -> Result<KeyCombo, String> {
    let parts: Vec<String> = raw
        .split('+')
        .map(|p| p.trim().to_ascii_lowercase())
        .filter(|p| !p.is_empty())
        .collect();
    if parts.is_empty() {
        return Err("COMPUTER_USE_INVALID_ACTION: key 动作缺少按键组合".into());
    }
    if parts.len() > MAX_KEY_PARTS {
        return Err(format!(
            "COMPUTER_USE_INVALID_ACTION: 按键组合最多 {MAX_KEY_PARTS} 段（收到 {}）",
            parts.len()
        ));
    }
    let mut modifiers: Vec<u16> = Vec::new();
    let mut key: Option<u16> = None;
    for p in &parts {
        match p.as_str() {
            "ctrl" | "control" => modifiers.push(0x11),
            "shift" => modifiers.push(0x10),
            "alt" | "option" => modifiers.push(0x12),
            "win" | "meta" | "cmd" | "super" | "windows" => {
                return Err(
                    "COMPUTER_USE_BLOCKED_COMBO: 出于安全考虑，电脑操控不允许发送 Win 键组合".into(),
                )
            }
            _ => {
                let vk = vk_for(p).ok_or_else(|| {
                    format!("COMPUTER_USE_INVALID_ACTION: 无法识别的按键「{p}」")
                })?;
                if key.is_some() {
                    return Err(
                        "COMPUTER_USE_INVALID_ACTION: 组合键只能包含一个非修饰键".into(),
                    );
                }
                key = Some(vk);
            }
        }
    }
    let key = key.ok_or_else(|| "COMPUTER_USE_INVALID_ACTION: 组合键缺少主键".to_string())?;
    if modifiers.contains(&0x11) && modifiers.contains(&0x10) && key == 0x1B {
        return Err(
            "COMPUTER_USE_BLOCKED_COMBO: 出于安全考虑，电脑操控不允许发送任务管理器快捷键".into(),
        );
    }
    Ok(KeyCombo { modifiers, key })
}

// ── 策略门禁与确认框 ─────────────────────────────────────────

/// 按当前档位决定是否放行；`ask_every_time` 弹系统确认框，用户取消即拒绝。
pub fn gate(policy: &str, description: &str) -> Result<(), String> {
    match policy {
        "auto" => Ok(()),
        "ask_every_time" => confirm_dialog(description),
        _ => Err("COMPUTER_USE_DISABLED: 电脑操控已禁用".into()),
    }
}

#[cfg(windows)]
fn confirm_dialog(description: &str) -> Result<(), String> {
    use windows_sys::Win32::UI::WindowsAndMessaging::{
        MessageBoxW, MB_ICONWARNING, MB_OKCANCEL, MB_SETFOREGROUND, MB_TOPMOST,
    };

    let text = format!(
        "通天办公请求操控你的电脑：\n\n{description}\n\n允许这一次操作吗？\n（可在「电脑操控」页改为自动执行或禁用）"
    );
    let text = wide(&text);
    let caption = wide("通天办公 · 电脑操控");
    let ret = unsafe {
        MessageBoxW(
            std::ptr::null_mut(),
            text.as_ptr(),
            caption.as_ptr(),
            MB_OKCANCEL | MB_ICONWARNING | MB_TOPMOST | MB_SETFOREGROUND,
        )
    };
    if ret == 1 {
        Ok(())
    } else {
        Err("COMPUTER_USE_DENIED_BY_USER: 用户拒绝了本次电脑操控".into())
    }
}

#[cfg(not(windows))]
fn confirm_dialog(_description: &str) -> Result<(), String> {
    Err("COMPUTER_USE_UNSUPPORTED: 当前平台没有电脑操控执行器（仅支持 Windows）".into())
}

#[cfg(windows)]
fn wide(s: &str) -> Vec<u16> {
    use std::os::windows::ffi::OsStrExt;
    std::ffi::OsStr::new(s)
        .encode_wide()
        .chain(std::iter::once(0))
        .collect()
}

// ── 截图与 PNG 编码 ──────────────────────────────────────────

#[derive(Debug, Clone)]
pub struct Capture {
    pub png: Vec<u8>,
    pub width: u32,
    pub height: u32,
    pub original_width: u32,
    pub original_height: u32,
    /// 虚拟桌面左上角在系统坐标里的位置（多显示器时可能为负）
    pub origin_x: i32,
    pub origin_y: i32,
    pub scaled: bool,
}

/// 最近邻降采样：截图只用于模型观察界面，不需要插值画质
pub fn downscale_rgb(src: &[u8], w: u32, h: u32, max_w: u32) -> (Vec<u8>, u32, u32, bool) {
    if max_w == 0 || w <= max_w || w == 0 || h == 0 {
        return (src.to_vec(), w, h, false);
    }
    let factor = (w + max_w - 1) / max_w;
    let nw = (w / factor).max(1);
    let nh = (h / factor).max(1);
    let mut out = vec![0u8; nw as usize * nh as usize * 3];
    for y in 0..nh {
        let sy = (y * factor + factor / 2).min(h - 1);
        for x in 0..nw {
            let sx = (x * factor + factor / 2).min(w - 1);
            let si = ((sy * w + sx) * 3) as usize;
            let di = ((y * nw + x) * 3) as usize;
            out[di..di + 3].copy_from_slice(&src[si..si + 3]);
        }
    }
    (out, nw, nh, true)
}

fn push_chunk(out: &mut Vec<u8>, kind: &[u8; 4], data: &[u8]) {
    out.extend_from_slice(&(data.len() as u32).to_be_bytes());
    out.extend_from_slice(kind);
    out.extend_from_slice(data);
    let mut hasher = crc32fast::Hasher::new();
    hasher.update(kind);
    hasher.update(data);
    out.extend_from_slice(&hasher.finalize().to_be_bytes());
}

/// 最小 PNG 编码（RGB 8bit、无滤波）：避免为一张截图引入图像编解码依赖
pub fn encode_png_rgb(width: u32, height: u32, rgb: &[u8]) -> Vec<u8> {
    use flate2::{write::ZlibEncoder, Compression};
    use std::io::Write;

    let mut raw = Vec::with_capacity((height as usize) * (1 + width as usize * 3));
    for row in 0..height as usize {
        raw.push(0);
        let start = row * width as usize * 3;
        raw.extend_from_slice(&rgb[start..start + width as usize * 3]);
    }
    let mut enc = ZlibEncoder::new(Vec::new(), Compression::fast());
    let _ = enc.write_all(&raw);
    let compressed = enc.finish().unwrap_or_default();

    let mut ihdr = Vec::with_capacity(13);
    ihdr.extend_from_slice(&width.to_be_bytes());
    ihdr.extend_from_slice(&height.to_be_bytes());
    ihdr.extend_from_slice(&[8, 2, 0, 0, 0]);

    let mut out = Vec::with_capacity(compressed.len() + 64);
    out.extend_from_slice(b"\x89PNG\r\n\x1a\n");
    push_chunk(&mut out, b"IHDR", &ihdr);
    push_chunk(&mut out, b"IDAT", &compressed);
    push_chunk(&mut out, b"IEND", &[]);
    out
}

#[cfg(windows)]
pub fn capture() -> Result<Capture, String> {
    use windows_sys::Win32::Foundation::HWND;
    use windows_sys::Win32::Graphics::Gdi::{
        BitBlt, CreateCompatibleBitmap, CreateCompatibleDC, DeleteDC, DeleteObject, GetDC,
        GetDIBits, ReleaseDC, SelectObject, BITMAPINFO, BITMAPINFOHEADER, BI_RGB, CAPTUREBLT,
        DIB_RGB_COLORS, HGDIOBJ, SRCCOPY,
    };
    use windows_sys::Win32::UI::WindowsAndMessaging::{
        GetSystemMetrics, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN, SM_XVIRTUALSCREEN,
        SM_YVIRTUALSCREEN,
    };

    let (vx, vy, vw, vh) = unsafe {
        (
            GetSystemMetrics(SM_XVIRTUALSCREEN),
            GetSystemMetrics(SM_YVIRTUALSCREEN),
            GetSystemMetrics(SM_CXVIRTUALSCREEN),
            GetSystemMetrics(SM_CYVIRTUALSCREEN),
        )
    };
    if vw <= 0 || vh <= 0 {
        return Err("COMPUTER_USE_CAPTURE_FAILED: 读不到屏幕尺寸（可能处于锁屏或安全桌面）".into());
    }

    unsafe {
        let screen = GetDC(std::ptr::null_mut::<std::ffi::c_void>() as HWND);
        if screen.is_null() {
            return Err("COMPUTER_USE_CAPTURE_FAILED: 无法获取屏幕设备上下文".into());
        }
        let mem = CreateCompatibleDC(screen);
        if mem.is_null() {
            ReleaseDC(std::ptr::null_mut(), screen);
            return Err("COMPUTER_USE_CAPTURE_FAILED: 无法创建截屏缓冲区".into());
        }
        let bmp = CreateCompatibleBitmap(screen, vw, vh);
        if bmp.is_null() {
            DeleteDC(mem);
            ReleaseDC(std::ptr::null_mut(), screen);
            return Err("COMPUTER_USE_CAPTURE_FAILED: 无法创建截屏位图".into());
        }
        let old = SelectObject(mem, bmp as HGDIOBJ);

        let ok = BitBlt(mem, 0, 0, vw, vh, screen, vx, vy, SRCCOPY | CAPTUREBLT);
        if ok == 0 {
            SelectObject(mem, old);
            DeleteObject(bmp as HGDIOBJ);
            DeleteDC(mem);
            ReleaseDC(std::ptr::null_mut(), screen);
            return Err("COMPUTER_USE_CAPTURE_FAILED: 屏幕内容复制失败（锁屏或安全桌面下不可截屏）".into());
        }

        let mut bmi: BITMAPINFO = std::mem::zeroed();
        bmi.bmiHeader.biSize = std::mem::size_of::<BITMAPINFOHEADER>() as u32;
        bmi.bmiHeader.biWidth = vw;
        // 负高度：请求 top-down 扫描线，省一次翻转
        bmi.bmiHeader.biHeight = -vh;
        bmi.bmiHeader.biPlanes = 1;
        bmi.bmiHeader.biBitCount = 32;
        bmi.bmiHeader.biCompression = BI_RGB;

        let mut bgra = vec![0u8; vw as usize * vh as usize * 4];
        let lines = GetDIBits(
            mem,
            bmp,
            0,
            vh as u32,
            bgra.as_mut_ptr() as *mut std::ffi::c_void,
            &mut bmi,
            DIB_RGB_COLORS,
        );

        SelectObject(mem, old);
        DeleteObject(bmp as HGDIOBJ);
        DeleteDC(mem);
        ReleaseDC(std::ptr::null_mut(), screen);

        if lines != vh {
            return Err(format!(
                "COMPUTER_USE_CAPTURE_FAILED: 读取屏幕像素失败（返回 {lines}/{vh} 行）"
            ));
        }

        let mut rgb = vec![0u8; vw as usize * vh as usize * 3];
        for i in 0..(vw as usize * vh as usize) {
            rgb[i * 3] = bgra[i * 4 + 2];
            rgb[i * 3 + 1] = bgra[i * 4 + 1];
            rgb[i * 3 + 2] = bgra[i * 4];
        }
        let (rgb, nw, nh, scaled) = downscale_rgb(&rgb, vw as u32, vh as u32, MAX_SHOT_WIDTH);
        Ok(Capture {
            png: encode_png_rgb(nw, nh, &rgb),
            width: nw,
            height: nh,
            original_width: vw as u32,
            original_height: vh as u32,
            origin_x: vx,
            origin_y: vy,
            scaled,
        })
    }
}

#[cfg(not(windows))]
pub fn capture() -> Result<Capture, String> {
    Err("COMPUTER_USE_UNSUPPORTED: 当前平台没有电脑操控执行器（仅支持 Windows）".into())
}

// ── 键鼠注入 ────────────────────────────────────────────────

#[cfg(windows)]
mod wininput {
    use windows_sys::Win32::UI::Input::KeyboardAndMouse::{
        SendInput, INPUT, INPUT_0, INPUT_KEYBOARD, INPUT_MOUSE, KEYBDINPUT, KEYEVENTF_KEYUP,
        KEYEVENTF_UNICODE, MOUSEINPUT, MOUSEEVENTF_ABSOLUTE, MOUSEEVENTF_LEFTDOWN,
        MOUSEEVENTF_LEFTUP, MOUSEEVENTF_MOVE, MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP,
        MOUSEEVENTF_VIRTUALDESK, MOUSEEVENTF_WHEEL, VIRTUAL_KEY,
    };
    use windows_sys::Win32::UI::WindowsAndMessaging::{
        GetSystemMetrics, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN, SM_XVIRTUALSCREEN,
        SM_YVIRTUALSCREEN, WHEEL_DELTA,
    };

    use super::{Action, Button, KeyCombo};

    fn screen() -> (i32, i32, i32, i32) {
        unsafe {
            (
                GetSystemMetrics(SM_XVIRTUALSCREEN),
                GetSystemMetrics(SM_YVIRTUALSCREEN),
                GetSystemMetrics(SM_CXVIRTUALSCREEN),
                GetSystemMetrics(SM_CYVIRTUALSCREEN),
            )
        }
    }

    fn check_point(x: i32, y: i32) -> Result<(), String> {
        let (vx, vy, vw, vh) = screen();
        if vw <= 0 || vh <= 0 {
            return Err("COMPUTER_USE_ACTION_FAILED: 读不到屏幕尺寸".into());
        }
        if x < vx || y < vy || x >= vx + vw || y >= vy + vh {
            return Err(format!(
                "COMPUTER_USE_OUT_OF_BOUNDS: 坐标 ({x}, {y}) 不在可视屏幕范围 ({vx}, {vy}) - ({}, {}) 内",
                vx + vw - 1,
                vy + vh - 1
            ));
        }
        Ok(())
    }

    fn abs_coord(value: i32, origin: i32, size: i32) -> i32 {
        if size <= 1 {
            0
        } else {
            (((value - origin) as i64 * 65535) / (size as i64 - 1)) as i32
        }
    }

    fn mouse_input(dx: i32, dy: i32, flags: u32, data: u32) -> INPUT {
        INPUT {
            r#type: INPUT_MOUSE,
            Anonymous: INPUT_0 {
                mi: MOUSEINPUT {
                    dx,
                    dy,
                    mouseData: data,
                    dwFlags: flags,
                    time: 0,
                    dwExtraInfo: 0,
                },
            },
        }
    }

    fn key_input(vk: u16, flags: u32) -> INPUT {
        INPUT {
            r#type: INPUT_KEYBOARD,
            Anonymous: INPUT_0 {
                ki: KEYBDINPUT {
                    wVk: vk as VIRTUAL_KEY,
                    wScan: 0,
                    dwFlags: flags,
                    time: 0,
                    dwExtraInfo: 0,
                },
            },
        }
    }

    fn unicode_input(code_unit: u16, flags: u32) -> INPUT {
        INPUT {
            r#type: INPUT_KEYBOARD,
            Anonymous: INPUT_0 {
                ki: KEYBDINPUT {
                    wVk: 0,
                    wScan: code_unit,
                    dwFlags: KEYEVENTF_UNICODE | flags,
                    time: 0,
                    dwExtraInfo: 0,
                },
            },
        }
    }

    fn send(inputs: &[INPUT]) -> Result<(), String> {
        let sent = unsafe { SendInput(inputs.len() as u32, inputs.as_ptr(), std::mem::size_of::<INPUT>() as i32) };
        if sent != inputs.len() as u32 {
            return Err(
                "COMPUTER_USE_ACTION_FAILED: 系统拒绝模拟输入（目标窗口可能以管理员权限运行，或当前处于安全桌面）".into(),
            );
        }
        Ok(())
    }

    fn move_mouse(x: i32, y: i32) -> Result<(), String> {
        check_point(x, y)?;
        let (vx, vy, vw, vh) = screen();
        let dx = abs_coord(x, vx, vw);
        let dy = abs_coord(y, vy, vh);
        send(&[mouse_input(
            dx,
            dy,
            MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK,
            0,
        )])?;
        std::thread::sleep(std::time::Duration::from_millis(20));
        Ok(())
    }

    fn button_inputs(button: &Button, down: bool) -> INPUT {
        let flags = match (button, down) {
            (Button::Left, true) => MOUSEEVENTF_LEFTDOWN,
            (Button::Left, false) => MOUSEEVENTF_LEFTUP,
            (Button::Right, true) => MOUSEEVENTF_RIGHTDOWN,
            (Button::Right, false) => MOUSEEVENTF_RIGHTUP,
        };
        mouse_input(0, 0, flags, 0)
    }

    fn click(x: i32, y: i32, button: Button, count: u8) -> Result<(), String> {
        move_mouse(x, y)?;
        for i in 0..count {
            send(&[button_inputs(&button, true), button_inputs(&button, false)])?;
            if i + 1 < count {
                std::thread::sleep(std::time::Duration::from_millis(60));
            }
        }
        Ok(())
    }

    fn scroll(x: Option<i32>, y: Option<i32>, amount: i32) -> Result<(), String> {
        if let (Some(x), Some(y)) = (x, y) {
            move_mouse(x, y)?;
        }
        send(&[mouse_input(
            0,
            0,
            MOUSEEVENTF_WHEEL,
            (amount * WHEEL_DELTA as i32) as u32,
        )])?;
        Ok(())
    }

    fn type_text(text: &str) -> Result<(), String> {
        let units: Vec<u16> = text.encode_utf16().collect();
        for chunk in units.chunks(200) {
            let mut inputs = Vec::with_capacity(chunk.len() * 2);
            for u in chunk {
                inputs.push(unicode_input(*u, 0));
                inputs.push(unicode_input(*u, KEYEVENTF_KEYUP));
            }
            send(&inputs)?;
            std::thread::sleep(std::time::Duration::from_millis(2));
        }
        Ok(())
    }

    fn press(combo: &KeyCombo) -> Result<(), String> {
        let mut down: Vec<INPUT> = combo
            .modifiers
            .iter()
            .map(|vk| key_input(*vk, 0))
            .collect();
        down.push(key_input(combo.key, 0));
        send(&down)?;
        std::thread::sleep(std::time::Duration::from_millis(20));
        let mut up: Vec<INPUT> = vec![key_input(combo.key, KEYEVENTF_KEYUP)];
        up.extend(
            combo
                .modifiers
                .iter()
                .rev()
                .map(|vk| key_input(*vk, KEYEVENTF_KEYUP)),
        );
        send(&up)?;
        Ok(())
    }

    pub fn perform(action: &Action) -> Result<(), String> {
        match action {
            Action::Click { x, y, button, count } => click(*x, *y, button.clone(), *count),
            Action::Move { x, y } => move_mouse(*x, *y),
            Action::Scroll { x, y, amount } => scroll(*x, *y, *amount),
            Action::Type { text } => type_text(text),
            Action::Key { combo } => press(combo),
        }
    }
}

#[cfg(windows)]
pub fn perform_action(action: &Action) -> Result<(), String> {
    wininput::perform(action)
}

#[cfg(not(windows))]
pub fn perform_action(_action: &Action) -> Result<(), String> {
    Err("COMPUTER_USE_UNSUPPORTED: 当前平台没有电脑操控执行器（仅支持 Windows）".into())
}

// ── Tauri command ───────────────────────────────────────────

/// 设置页/电脑操控页的真实能力视图：平台、档位、网关状态一次说清
#[tauri::command]
pub async fn computer_use_capabilities(
    p0: tauri::State<'_, std::sync::Arc<crate::P0Services>>,
) -> Result<Value, String> {
    let supported = supported();
    let (policy, persisted, policy_error) = match read_policy(&p0.store) {
        Ok((p, saved)) => (p, saved, None),
        // fail-closed：坏档位按禁用处理，但把原因原样带回界面
        Err(e) => ("disabled".to_string(), true, Some(e)),
    };
    let gate = crate::bridge::computer_gate::endpoint().is_some();
    let mut reason = String::new();
    if let Some(e) = &policy_error {
        reason = format!("策略读取失败，电脑操控按禁用处理：{e}");
    } else if !supported {
        reason = "当前仅支持 Windows 桌面执行器".into();
    } else if policy == "disabled" {
        reason = "电脑操控已禁用".into();
    } else if !gate {
        reason = "本机电脑操控网关未启动，执行器不可用".into();
    }
    Ok(json!({
        "supported": supported,
        "platform": platform(),
        "features": features(),
        "policy": policy,
        "persisted": persisted,
        "gate": gate,
        "reason": reason,
        "policyError": policy_error,
    }))
}

/// 设置页的「截屏自检」：用户主动触发也按当前档位确认（自动执行档位直接截），
/// 截完只回传给界面展示，不落盘；审计只记尺寸，不记画面。
#[tauri::command]
pub async fn computer_use_capture(
    p0: tauri::State<'_, std::sync::Arc<crate::P0Services>>,
) -> Result<Value, String> {
    if !supported() {
        return Err("COMPUTER_USE_UNSUPPORTED: 当前平台没有电脑操控执行器（仅支持 Windows）".into());
    }
    let store = std::sync::Arc::clone(&p0.store);
    let (policy, _) = read_policy(&store)?;
    let description = "截取整个屏幕用于执行器自检";
    let desc = description.to_string();
    let p = policy.clone();
    tauri::async_runtime::spawn_blocking(move || gate(&p, &desc))
        .await
        .map_err(|e| format!("COMPUTER_USE_ACTION_FAILED: 确认框线程异常：{e}"))??;

    let cap = tauri::async_runtime::spawn_blocking(capture)
        .await
        .map_err(|e| format!("COMPUTER_USE_CAPTURE_FAILED: 截屏线程异常：{e}"))??;
    let kb = cap.png.len() as f64 / 1024.0;
    crate::security::audit::record_quiet(
        &store,
        crate::security::audit::ACTION_COMPUTER_CAPTURE,
        crate::security::audit::SEV_INFO,
        "用户自检",
        &format!(
            "截屏 {}x{}{}（{kb:.0} KB，画面内容不入审计）",
            cap.width,
            cap.height,
            if cap.scaled {
                format!("，原始 {}x{} 已降采样", cap.original_width, cap.original_height)
            } else {
                String::new()
            }
        ),
    );
    use base64::Engine as _;
    Ok(json!({
        "ok": true,
        "mime": "image/png",
        "imageBase64": base64::engine::general_purpose::STANDARD.encode(&cap.png),
        "width": cap.width,
        "height": cap.height,
        "originalWidth": cap.original_width,
        "originalHeight": cap.original_height,
        "originX": cap.origin_x,
        "originY": cap.origin_y,
        "scaled": cap.scaled,
    }))
}

// ── 测试 ────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn policy_is_enumerated_and_trimmed() {
        assert_eq!(normalize_policy(" auto ").unwrap(), "auto");
        assert_eq!(normalize_policy("\"disabled\"").unwrap(), "disabled");
        assert!(normalize_policy("always").unwrap_err().contains("COMPUTER_USE_INVALID_POLICY"));
    }

    #[test]
    fn action_parser_rejects_missing_or_out_of_range_fields() {
        assert!(parse_action(&json!({})).unwrap_err().contains("缺少 action"));
        assert!(parse_action(&json!({"action":"click"})).unwrap_err().contains("缺少整数参数 x"));
        assert!(parse_action(&json!({"action":"scroll","amount":0})).unwrap_err().contains("滚动"));
        assert!(parse_action(&json!({"action":"scroll","amount":21})).unwrap_err().contains("滚动"));
        assert!(parse_action(&json!({"action":"type","text":""})).unwrap_err().contains("缺少 text"));
        assert!(parse_action(&json!({"action":"nope"})).unwrap_err().contains("不支持的动作"));
    }

    #[test]
    fn type_description_never_contains_the_text() {
        let secret = "my-secret-password";
        let a = parse_action(&json!({"action":"type","text":secret})).unwrap();
        let d = describe_action(&a);
        assert!(d.contains(&format!("{} 个字符", secret.chars().count())));
        assert!(!d.contains("secret"));
    }

    #[test]
    fn key_combo_orders_modifiers_and_blocks_dangerous_ones() {
        let c = parse_key_combo("T+Shift+CTRL").unwrap();
        assert_eq!(c.modifiers, vec![0x10, 0x11]);
        assert_eq!(c.key, 0x54);
        assert!(c.display().contains("shift+ctrl+t"));
        assert!(parse_key_combo("win+r").unwrap_err().contains("COMPUTER_USE_BLOCKED_COMBO"));
        assert!(parse_key_combo("ctrl+shift+esc").unwrap_err().contains("任务管理器"));
        assert!(parse_key_combo("ctrl+a+b").unwrap_err().contains("一个非修饰键"));
        assert!(parse_key_combo("ctrl+").unwrap_err().contains("缺少主键"));
    }

    #[test]
    fn png_encoder_writes_valid_container() {
        let rgb = vec![255u8, 0, 0, 0, 255, 0, 0, 0, 255, 255, 255, 255];
        let png = encode_png_rgb(2, 2, &rgb);
        assert_eq!(&png[..8], b"\x89PNG\r\n\x1a\n");
        assert_eq!(&png[12..16], b"IHDR");
        assert_eq!(u32::from_be_bytes([png[16], png[17], png[18], png[19]]), 2);
        assert_eq!(u32::from_be_bytes([png[20], png[21], png[22], png[23]]), 2);
        // IDAT 解压后 = 每行 1 字节滤波 + 2*3 字节 RGB
        let idat_len = u32::from_be_bytes([png[33], png[34], png[35], png[36]]) as usize;
        let idat = &png[41..41 + idat_len];
        use flate2::read::ZlibDecoder;
        use std::io::Read;
        let mut raw = Vec::new();
        ZlibDecoder::new(idat).read_to_end(&mut raw).unwrap();
        assert_eq!(raw.len(), 2 * (1 + 2 * 3));
        assert_eq!(&raw[1..4], &[255, 0, 0]);
    }

    #[test]
    fn downscale_keeps_aspect_and_max_width() {
        let src = vec![7u8; 4 * 2 * 3];
        let (out, w, h, scaled) = downscale_rgb(&src, 4, 2, 2);
        assert!(scaled);
        assert_eq!((w, h), (2, 1));
        assert_eq!(out.len(), 2 * 1 * 3);
        let (same, w2, h2, scaled2) = downscale_rgb(&src, 4, 2, 1600);
        assert!(!scaled2);
        assert_eq!((w2, h2), (4, 2));
        assert_eq!(same.len(), src.len());
    }

    #[test]
    fn capabilities_only_claim_what_the_platform_has() {
        let f = features();
        assert_eq!(f["screenshot"], json!(supported()));
        assert_eq!(f["input"], json!(supported()));
    }
}
