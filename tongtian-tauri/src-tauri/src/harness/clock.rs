//! 本地时钟：不引入 chrono，用系统调用 + 纯函数换算。

/// 当前 Unix 毫秒
pub fn now_ms() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as i64)
        .unwrap_or(0)
}

/// 当前本地时间（北京时间等系统时区），格式 `YYYY-MM-DD HH:MM:SS`，附星期（0=周日）
pub fn local_now() -> (String, usize) {
    let secs = now_ms() / 1000 + local_offset_seconds();
    let days = secs.div_euclid(86_400);
    let rem = secs.rem_euclid(86_400);
    let (y, m, d) = civil_from_days(days);
    let weekday = ((days + 4).rem_euclid(7)) as usize;
    (
        format!(
            "{y:04}-{m:02}-{d:02} {:02}:{:02}:{:02}",
            rem / 3600,
            (rem % 3600) / 60,
            rem % 60
        ),
        weekday,
    )
}

/// Unix 毫秒 → 本地时间字符串（检查点 savedAt 展示用）
pub fn format_local_ms(ms: i64) -> String {
    if ms <= 0 {
        return String::new();
    }
    let secs = ms / 1000 + local_offset_seconds();
    let days = secs.div_euclid(86_400);
    let rem = secs.rem_euclid(86_400);
    let (y, m, d) = civil_from_days(days);
    format!(
        "{y:04}-{m:02}-{d:02} {:02}:{:02}:{:02}",
        rem / 3600,
        (rem % 3600) / 60,
        rem % 60
    )
}

pub fn civil_from_days(z: i64) -> (i64, i64, i64) {
    let z = z + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    (if m <= 2 { y + 1 } else { y }, m, d)
}

pub fn days_from_civil(y: i64, m: i64, d: i64) -> i64 {
    let y = if m <= 2 { y - 1 } else { y };
    let era = if y >= 0 { y } else { y - 399 } / 400;
    let yoe = y - era * 400;
    let mp = if m > 2 { m - 3 } else { m + 9 };
    let doy = (153 * mp + 2) / 5 + d - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146_097 + doe - 719_468
}

/// 本机时区相对 UTC 的偏移秒数（含当前夏令时状态）
#[cfg(unix)]
fn local_offset_seconds() -> i64 {
    let now = now_ms() / 1000;
    let local = unsafe { localtime_seconds(now) };
    local - now
}

#[cfg(unix)]
#[repr(C)]
struct LibcTm {
    tm_sec: i32,
    tm_min: i32,
    tm_hour: i32,
    tm_mday: i32,
    tm_mon: i32,
    tm_year: i32,
    tm_wday: i32,
    tm_yday: i32,
    tm_isdst: i32,
}

#[cfg(unix)]
extern "C" {
    fn localtime_r(timep: *const i64, result: *mut LibcTm) -> *mut LibcTm;
}

#[cfg(unix)]
unsafe fn localtime_seconds(epoch: i64) -> i64 {
    let mut tm: LibcTm = std::mem::zeroed();
    if localtime_r(&epoch, &mut tm).is_null() {
        return epoch;
    }
    days_from_civil(tm.tm_year as i64 + 1900, tm.tm_mon as i64 + 1, tm.tm_mday as i64) * 86_400
        + tm.tm_hour as i64 * 3600
        + tm.tm_min as i64 * 60
        + tm.tm_sec as i64
}

#[cfg(windows)]
#[repr(C)]
struct WinSystemTime {
    year: u16,
    month: u16,
    day_of_week: u16,
    day: u16,
    hour: u16,
    minute: u16,
    second: u16,
    milliseconds: u16,
}

#[cfg(windows)]
#[link(name = "kernel32")]
extern "system" {
    fn GetLocalTime(out: *mut WinSystemTime);
    fn GetSystemTime(out: *mut WinSystemTime);
}

#[cfg(windows)]
fn local_offset_seconds() -> i64 {
    // 同时取本地与 UTC 墙上时间，差值即当前偏移（自动覆盖夏令时）
    let wall = |st: WinSystemTime| -> i64 {
        days_from_civil(st.year as i64, st.month as i64, st.day as i64) * 86_400
            + st.hour as i64 * 3600
            + st.minute as i64 * 60
            + st.second as i64
    };
    unsafe {
        let mut local: WinSystemTime = std::mem::zeroed();
        let mut utc: WinSystemTime = std::mem::zeroed();
        GetLocalTime(&mut local);
        GetSystemTime(&mut utc);
        wall(local) - wall(utc)
    }
}

#[cfg(not(any(unix, windows)))]
fn local_offset_seconds() -> i64 {
    0
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn civil_roundtrip() {
        for (y, m, d) in [(1970, 1, 1), (2026, 10, 28), (2000, 2, 29)] {
            let days = days_from_civil(y, m, d);
            assert_eq!(civil_from_days(days), (y, m, d));
        }
    }

    #[test]
    fn formats_a_fixed_epoch() {
        let text = format_local_ms(1_700_000_000_000);
        assert_eq!(text.len(), 19, "{text}");
        assert!(text.starts_with("2023-11-"), "{text}");
        assert_eq!(format_local_ms(0), "");
    }

    #[test]
    fn offset_is_within_a_day() {
        let off = local_offset_seconds();
        assert!(off.abs() < 86_400, "{off}");
    }
}
