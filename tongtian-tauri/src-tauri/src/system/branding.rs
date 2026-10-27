//! 品牌外观：应用显示名与图标的单次自定义
//!
//! 需求边界（界面必须如实说明，不做假承诺）：
//! - 能改：窗口标题、任务栏/Alt+Tab 窗口图标、托盘图标、界面内名称与图标；
//! - 不能改：已安装的桌面快捷方式名称与应用可执行文件名（由安装包写入，运行期不改系统文件）；
//! - 只给一次机会：首次成功保存后写入 `appBrandCustomizedAt`，应用内不再提供重置入口。
//!
//! 图标只接受 PNG：解码走已在依赖树内的 `png` 0.17，避免为一张图标引入 `image` 大依赖。
//! 落盘保存的是用户上传的原始 PNG（先解码校验），重启时再解码一次恢复窗口/托盘图标。

use std::io::Cursor;
use std::path::PathBuf;
use std::sync::Arc;

use base64::Engine as _;
use serde::Deserialize;
use serde_json::{json, Value};
use tauri::{AppHandle, Manager, State};

use crate::store::{db, settings, Store};
use crate::P0Services;

pub const DEFAULT_NAME: &str = "任我行";
pub const ATTRIBUTION: &str = "北京通天云计算有限公司";

/// settings_kv 键名（与其余设置共用同一张表）
pub const KEY_NAME: &str = "appBrandName";
pub const KEY_ICON: &str = "appBrandIconPath";
pub const KEY_CUSTOMIZED_AT: &str = "appBrandCustomizedAt";

const ICON_DIR: &str = "branding";
const ICON_FILE: &str = "app-icon.png";
const MAX_ICON_BYTES: usize = 4 * 1024 * 1024;
const MAX_EDGE: u32 = 1024;
const MIN_EDGE: u32 = 16;
const MAX_NAME_CHARS: usize = 24;

const PNG_MAGIC: [u8; 8] = [0x89, b'P', b'N', b'G', 0x0D, 0x0A, 0x1A, 0x0A];

/// 已落库的品牌状态
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Branding {
    pub name: String,
    pub icon_path: Option<PathBuf>,
    pub customized_at: Option<u64>,
}

impl Branding {
    pub fn customized(&self) -> bool {
        self.customized_at.is_some()
    }
}

/// 解码后的 RGBA 图标（original 为通过校验的原始 PNG 字节，用于落盘）
#[derive(Debug)]
pub struct DecodedIcon {
    pub rgba: Vec<u8>,
    pub width: u32,
    pub height: u32,
    pub original: Vec<u8>,
}

/// 图标文件固定落在运行目录下，与 SQLite 同根（`TONGTIAN_HOME` 可覆盖，便于隔离验证）
pub fn icon_path() -> PathBuf {
    db::runtime_home().join(ICON_DIR).join(ICON_FILE)
}

/// 显示名校验：去首尾空白后 1–24 个字符，不允许控制字符（含换行）
pub fn validate_name(raw: &str) -> Result<String, String> {
    let name = raw.trim();
    if name.is_empty() {
        return Err("BRAND_NAME_EMPTY: 名称不能为空".into());
    }
    if name.chars().count() > MAX_NAME_CHARS {
        return Err(format!("BRAND_NAME_TOO_LONG: 名称最多 {MAX_NAME_CHARS} 个字符"));
    }
    if name.chars().any(|c| c.is_control()) {
        return Err("BRAND_NAME_INVALID: 名称不能包含换行或控制字符".into());
    }
    Ok(name.to_string())
}

/// 单次机会校验：`appBrandCustomizedAt` 存在即已使用
pub fn ensure_customizable(store: &Store) -> Result<(), String> {
    match settings::get(store, KEY_CUSTOMIZED_AT)? {
        Some(_) => Err("BRAND_ALREADY_CUSTOMIZED: 品牌修改机会已使用，应用内不再提供第二次修改".into()),
        None => Ok(()),
    }
}

/// 读取当前品牌状态；老版本没有这些键时回退默认名称
pub fn read(store: &Store) -> Result<Branding, String> {
    let name = settings::get(store, KEY_NAME)?
        .filter(|s| !s.trim().is_empty())
        .unwrap_or_else(|| DEFAULT_NAME.to_string());
    let icon_path = settings::get(store, KEY_ICON)?
        .filter(|s| !s.trim().is_empty())
        .map(PathBuf::from)
        .filter(|p| p.is_file());
    let customized_at = settings::get(store, KEY_CUSTOMIZED_AT)?
        .and_then(|s| s.trim().parse::<u64>().ok());
    Ok(Branding {
        name,
        icon_path,
        customized_at,
    })
}

fn strip_data_uri(raw: &str) -> &str {
    match raw.find("base64,") {
        Some(idx) => &raw[idx + "base64,".len()..],
        None => raw,
    }
}

/// 解码 PNG → RGBA8。只接受 PNG（签名校验在前，避免把任意字节交给解码器）
pub fn decode_png(bytes: Vec<u8>) -> Result<DecodedIcon, String> {
    if bytes.len() < PNG_MAGIC.len() || bytes[..PNG_MAGIC.len()] != PNG_MAGIC {
        return Err("BRAND_ICON_NOT_PNG: 只支持 PNG 图片".into());
    }
    if bytes.len() > MAX_ICON_BYTES {
        return Err(format!(
            "BRAND_ICON_TOO_LARGE: 图片不能超过 {} MB",
            MAX_ICON_BYTES / 1024 / 1024
        ));
    }
    let mut decoder = png::Decoder::new(Cursor::new(&bytes));
    // 调色板展开为 RGB(A)、16 位降为 8 位，后面只需处理灰度/RGB/RGBA 三种排布
    decoder.set_transformations(png::Transformations::EXPAND | png::Transformations::STRIP_16);
    let mut reader = decoder
        .read_info()
        .map_err(|e| format!("BRAND_ICON_INVALID: 图片无法解析（{e}）"))?;
    let mut buf = vec![0; reader.output_buffer_size()];
    let info = reader
        .next_frame(&mut buf)
        .map_err(|e| format!("BRAND_ICON_INVALID: 图片无法解析（{e}）"))?;
    if info.width < MIN_EDGE || info.height < MIN_EDGE {
        return Err(format!(
            "BRAND_ICON_TOO_SMALL: 图片至少 {MIN_EDGE}×{MIN_EDGE} 像素"
        ));
    }
    if info.width > MAX_EDGE || info.height > MAX_EDGE {
        return Err(format!(
            "BRAND_ICON_TOO_LARGE_DIM: 图片不能超过 {MAX_EDGE}×{MAX_EDGE} 像素"
        ));
    }
    let data = &buf[..info.buffer_size()];
    let rgba = match info.color_type {
        png::ColorType::Rgba => data.to_vec(),
        png::ColorType::Rgb => data
            .chunks_exact(3)
            .flat_map(|p| [p[0], p[1], p[2], 255])
            .collect(),
        png::ColorType::GrayscaleAlpha => data
            .chunks_exact(2)
            .flat_map(|p| [p[0], p[0], p[0], p[1]])
            .collect(),
        png::ColorType::Grayscale => data.iter().flat_map(|&g| [g, g, g, 255]).collect(),
        // EXPAND 已展开调色板，正常不会走到这里
        png::ColorType::Indexed => {
            return Err("BRAND_ICON_INVALID: 不支持的 PNG 调色板格式".into())
        }
    };
    let expected = info.width as usize * info.height as usize * 4;
    if rgba.len() != expected {
        return Err("BRAND_ICON_INVALID: 像素数据不完整".into());
    }
    Ok(DecodedIcon {
        rgba,
        width: info.width,
        height: info.height,
        original: bytes,
    })
}

/// 渲染层传来的 base64 → PNG 解码（容忍 `data:image/png;base64,` 前缀）
pub fn decode_icon_base64(raw: &str) -> Result<DecodedIcon, String> {
    let bytes = base64::engine::general_purpose::STANDARD
        .decode(strip_data_uri(raw).trim())
        .map_err(|e| format!("BRAND_ICON_INVALID: 图片编码无效（{e}）"))?;
    decode_png(bytes)
}

/// 图标转 data URL（设置页回显上次上传的图标；不落第二份状态）
pub fn icon_data_url(path: &PathBuf) -> Option<String> {
    let bytes = std::fs::read(path).ok()?;
    if bytes.len() > MAX_ICON_BYTES || bytes.len() < PNG_MAGIC.len() || bytes[..8] != PNG_MAGIC {
        return None;
    }
    let b64 = base64::engine::general_purpose::STANDARD.encode(bytes);
    Some(format!("data:image/png;base64,{b64}"))
}

fn save_icon(dec: &DecodedIcon) -> Result<PathBuf, String> {
    let path = icon_path();
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir)
            .map_err(|e| format!("BRAND_ICON_SAVE_FAILED: 无法创建图标目录（{e}）"))?;
    }
    let tmp = path.with_extension("png.tmp");
    std::fs::write(&tmp, &dec.original)
        .map_err(|e| format!("BRAND_ICON_SAVE_FAILED: 图标写入失败（{e}）"))?;
    std::fs::rename(&tmp, &path)
        .map_err(|e| format!("BRAND_ICON_SAVE_FAILED: 图标落盘失败（{e}）"))?;
    Ok(path)
}

/// 本次修改真实作用到的位置（供界面如实展示，不推断）
#[derive(Debug, Default, Clone, Copy, PartialEq, Eq)]
pub struct Applied {
    pub window_title: bool,
    pub window_icon: bool,
    pub tray_icon: bool,
}

fn apply(app: &AppHandle, name: &str, icon: Option<&DecodedIcon>) -> (Applied, Vec<String>) {
    let mut applied = Applied::default();
    let mut warnings = Vec::new();

    match app.get_webview_window("main") {
        Some(window) => {
            if let Err(e) = window.set_title(name) {
                warnings.push(format!("窗口标题更新失败：{e}"));
            } else {
                applied.window_title = true;
            }
            if let Some(dec) = icon {
                let image = tauri::image::Image::new_owned(dec.rgba.clone(), dec.width, dec.height);
                if let Err(e) = window.set_icon(image) {
                    warnings.push(format!("任务栏图标更新失败：{e}"));
                } else {
                    applied.window_icon = true;
                }
            }
        }
        None => warnings.push("主窗口尚未创建，标题与图标将在下次启动生效".to_string()),
    }

    if let Some(dec) = icon {
        match app.tray_by_id("tongtian-main") {
            Some(tray) => {
                let image =
                    tauri::image::Image::new_owned(dec.rgba.clone(), dec.width, dec.height);
                if let Err(e) = tray.set_icon(Some(image)) {
                    warnings.push(format!("托盘图标更新失败：{e}"));
                } else {
                    applied.tray_icon = true;
                }
            }
            None => warnings.push("托盘未创建（后台常驻不可用），未更新托盘图标".to_string()),
        }
    }

    (applied, warnings)
}

fn now_ms() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

/// 设置页读取：默认名、署名、当前名、当前图标、是否已用掉修改机会
pub fn view(store: &Store) -> Result<Value, String> {
    let branding = read(store)?;
    Ok(json!({
        "defaultName": DEFAULT_NAME,
        "attribution": ATTRIBUTION,
        "name": branding.name,
        "iconDataUrl": branding.icon_path.as_ref().and_then(icon_data_url),
        "customized": branding.customized(),
        "customizedAt": branding.customized_at,
        "platform": "desktop",
    }))
}

/// 启动时恢复自定义品牌（未自定义则保持安装包默认，不做多余设置）
pub fn apply_startup(app: &AppHandle, store: &Store) {
    let branding = match read(store) {
        Ok(b) => b,
        Err(e) => {
            eprintln!("[branding] 读取失败，保持默认品牌: {e}");
            return;
        }
    };
    if !branding.customized() {
        return;
    }
    let icon = branding
        .icon_path
        .as_ref()
        .and_then(|p| std::fs::read(p).ok())
        .and_then(|bytes| decode_png(bytes).ok());
    let (applied, warnings) = apply(app, &branding.name, icon.as_ref());
    for w in warnings {
        eprintln!("[branding] {w}");
    }
    eprintln!(
        "[branding] 启动恢复：标题={} 窗口图标={} 托盘图标={}",
        applied.window_title, applied.window_icon, applied.tray_icon
    );
}

#[tauri::command]
pub fn branding_get(p0: State<'_, Arc<P0Services>>) -> Result<Value, String> {
    view(&p0.store)
}

#[derive(Debug, Deserialize)]
pub struct CustomizeReq {
    pub name: Option<String>,
    pub icon_base64: Option<String>,
}

/// 单次自定义：名称与图标可在同一次提交里一起改；成功即写入 `appBrandCustomizedAt`
#[tauri::command]
pub fn branding_customize(
    app: AppHandle,
    p0: State<'_, Arc<P0Services>>,
    req: CustomizeReq,
) -> Result<Value, String> {
    ensure_customizable(&p0.store)?;

    let name = match req.name.as_deref() {
        Some(raw) => Some(validate_name(raw)?),
        None => None,
    };
    let icon = match req.icon_base64.as_deref().filter(|s| !s.trim().is_empty()) {
        Some(raw) => Some(decode_icon_base64(raw)?),
        None => None,
    };
    if name.is_none() && icon.is_none() {
        return Err("BRAND_NO_CHANGE: 请至少修改名称或选择一张图片".into());
    }

    let icon_path = match icon.as_ref() {
        Some(dec) => Some(save_icon(dec)?),
        None => None,
    };

    let final_name = name.clone().unwrap_or_else(|| {
        read(&p0.store)
            .map(|b| b.name)
            .unwrap_or_else(|_| DEFAULT_NAME.to_string())
    });
    let (applied, warnings) = apply(&app, &final_name, icon.as_ref());

    let persist = (|| -> Result<u64, String> {
        if let Some(n) = &name {
            settings::set(&p0.store, KEY_NAME, n)?;
        }
        if let Some(p) = &icon_path {
            settings::set(&p0.store, KEY_ICON, &p.display().to_string())?;
        }
        let at = now_ms();
        settings::set(&p0.store, KEY_CUSTOMIZED_AT, &at.to_string())?;
        Ok(at)
    })();
    let customized_at = match persist {
        Ok(at) => at,
        Err(e) => {
            if let Some(p) = &icon_path {
                let _ = std::fs::remove_file(p);
            }
            return Err(e);
        }
    };

    let mut out = view(&p0.store)?;
    if let Some(map) = out.as_object_mut() {
        map.insert("customizedAt".into(), json!(customized_at));
        map.insert(
            "applied".into(),
            json!({
                "windowTitle": applied.window_title,
                "windowIcon": applied.window_icon,
                "trayIcon": applied.tray_icon,
            }),
        );
        map.insert("warnings".into(), json!(warnings));
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_store(name: &str) -> (Store, PathBuf) {
        let dir = std::env::temp_dir().join(format!(
            "tt-branding-{name}-{}",
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_nanos())
                .unwrap_or(0)
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let store = Store::open(dir.join("t.db")).unwrap();
        (store, dir)
    }

    /// 1×1 透明 PNG：用同一套 png 编码器现生成，避免手写字面量字节导致假阳性
    fn tiny_png() -> Vec<u8> {
        let mut out = Vec::new();
        {
            let mut enc = png::Encoder::new(&mut out, 1, 1);
            enc.set_color(png::ColorType::Rgba);
            enc.set_depth(png::BitDepth::Eight);
            let mut writer = enc.write_header().unwrap();
            writer.write_image_data(&[0, 0, 0, 0]).unwrap();
        }
        out
    }

    #[test]
    fn name_validation_trims_and_rejects_invalid() {
        assert_eq!(validate_name("  任我行  ").unwrap(), "任我行");
        assert!(validate_name("").is_err());
        assert!(validate_name("   ").is_err());
        assert!(validate_name(&"名".repeat(24)).is_ok());
        assert!(validate_name(&"名".repeat(25)).is_err());
        assert!(validate_name("任我行\n第二行").is_err());
        assert!(validate_name("任我行\u{0007}").is_err());
    }

    #[test]
    fn decode_rejects_non_png_and_tiny_images() {
        assert!(decode_png(b"not a png".to_vec()).is_err());
        // 1×1 图片低于 16×16 下限
        let tiny = tiny_png();
        let err = decode_png(tiny.clone()).unwrap_err();
        assert!(err.contains("BRAND_ICON_TOO_SMALL"), "{err}");
        // data URI 前缀应被容忍
        let b64 = base64::engine::general_purpose::STANDARD.encode(&tiny);
        let err = decode_icon_base64(&format!("data:image/png;base64,{b64}")).unwrap_err();
        assert!(err.contains("BRAND_ICON_TOO_SMALL"), "{err}");
    }

    #[test]
    fn customization_is_single_use() {
        let (store, dir) = temp_store("single");
        assert!(!read(&store).unwrap().customized());
        assert_eq!(read(&store).unwrap().name, DEFAULT_NAME);
        assert!(ensure_customizable(&store).is_ok());

        settings::set(&store, KEY_NAME, "我的工作台").unwrap();
        settings::set(&store, KEY_CUSTOMIZED_AT, "1700000000000").unwrap();

        let b = read(&store).unwrap();
        assert_eq!(b.name, "我的工作台");
        assert_eq!(b.customized_at, Some(1_700_000_000_000));
        let err = ensure_customizable(&store).unwrap_err();
        assert!(err.contains("BRAND_ALREADY_CUSTOMIZED"), "{err}");
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn missing_icon_file_is_not_reported() {
        let (store, dir) = temp_store("icon");
        let ghost = dir.join("ghost.png");
        settings::set(&store, KEY_ICON, &ghost.display().to_string()).unwrap();
        assert_eq!(read(&store).unwrap().icon_path, None);
        assert_eq!(view(&store).unwrap()["iconDataUrl"], serde_json::Value::Null);
        let _ = std::fs::remove_dir_all(dir);
    }
}
