//! What the operating system knows: machine name and today's local date.

use crate::timeutil::Date;

/// Machine name, as `socket.gethostname()` returns it.
#[cfg(unix)]
pub fn hostname() -> String {
    let mut buffer = [0u8; 256];
    // SAFETY: the buffer is valid for its whole length and gethostname writes at most
    // that many bytes.
    let status = unsafe { libc::gethostname(buffer.as_mut_ptr().cast(), buffer.len()) };
    if status != 0 {
        return "?".into();
    }
    let end = buffer.iter().position(|&b| b == 0).unwrap_or(buffer.len());
    String::from_utf8_lossy(&buffer[..end]).into_owned()
}

/// Machine name, as `socket.gethostname()` returns it.
#[cfg(windows)]
pub fn hostname() -> String {
    use windows_sys::Win32::System::SystemInformation::{
        ComputerNamePhysicalDnsHostname, GetComputerNameExW,
    };
    let mut size: u32 = 0;
    // SAFETY: a null buffer with size 0 only asks for the required size.
    unsafe { GetComputerNameExW(ComputerNamePhysicalDnsHostname, std::ptr::null_mut(), &mut size) };
    let mut buffer = vec![0u16; size.max(1) as usize];
    // SAFETY: the buffer holds `size` UTF-16 units, as the first call asked.
    let ok = unsafe {
        GetComputerNameExW(ComputerNamePhysicalDnsHostname, buffer.as_mut_ptr(), &mut size)
    };
    if ok == 0 {
        return std::env::var("COMPUTERNAME").unwrap_or_else(|_| "?".into());
    }
    String::from_utf16_lossy(&buffer[..size as usize])
}

/// Today's date in the local time zone (`date.today()`).
#[cfg(unix)]
pub fn local_today() -> Date {
    // SAFETY: `time` accepts a null pointer; `localtime_r` writes into our own `tm`.
    let tm = unsafe {
        let now = libc::time(std::ptr::null_mut());
        let mut tm: libc::tm = std::mem::zeroed();
        if libc::localtime_r(&now, &mut tm).is_null() {
            return utc_today();
        }
        tm
    };
    Date::new(tm.tm_year + 1900, (tm.tm_mon + 1) as u32, tm.tm_mday as u32).unwrap_or_else(utc_today)
}

/// Today's date in the local time zone (`date.today()`).
#[cfg(windows)]
pub fn local_today() -> Date {
    use windows_sys::Win32::Foundation::SYSTEMTIME;
    use windows_sys::Win32::System::SystemInformation::GetLocalTime;
    // SAFETY: GetLocalTime fills the structure it is given.
    let now = unsafe {
        let mut now: SYSTEMTIME = std::mem::zeroed();
        GetLocalTime(&mut now);
        now
    };
    Date::new(i32::from(now.wYear), u32::from(now.wMonth), u32::from(now.wDay))
        .unwrap_or_else(utc_today)
}

/// Today's date in UTC, the fallback when the local date is unavailable.
fn utc_today() -> Date {
    let (year, month, day) = crate::timeutil::civil_from_days(crate::timeutil::now_seconds() / 86_400);
    Date::new(year, month, day).unwrap_or(Date::MIN)
}
