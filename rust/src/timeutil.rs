//! Dates and instants without a date-time library: civil-calendar arithmetic, ISO 8601
//! output, and ISO 8601 input with the rules of Python's `fromisoformat`.

use std::time::{SystemTime, UNIX_EPOCH};

/// A calendar date (proleptic Gregorian, years 1 to 9999).
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct Date {
    pub year: i32,
    pub month: u32,
    pub day: u32,
}

impl Date {
    /// `date.min`, the placeholder of an invalid date.
    pub const MIN: Date = Date { year: 1, month: 1, day: 1 };

    /// A valid date, or `None`.
    pub fn new(year: i32, month: u32, day: u32) -> Option<Date> {
        if !(1..=9999).contains(&year) || !(1..=12).contains(&month) || day == 0
            || day > days_in_month(year, month)
        {
            return None;
        }
        Some(Date { year, month, day })
    }

    /// Strict `YYYY-MM-DD` with ASCII digits and a real calendar day.
    pub fn parse(text: &str) -> Option<Date> {
        let bytes = text.as_bytes();
        if !text.is_ascii() || bytes.len() != 10 || bytes[4] != b'-' || bytes[7] != b'-' {
            return None;
        }
        Date::new(digits(&text[0..4])? as i32, digits(&text[5..7])?, digits(&text[8..10])?)
    }

    /// `date.isoformat()`.
    pub fn iso(&self) -> String {
        format!("{:04}-{:02}-{:02}", self.year, self.month, self.day)
    }

    /// Days since 1970-01-01.
    fn days(&self) -> i64 {
        days_from_civil(self.year, self.month, self.day)
    }
}

impl std::fmt::Display for Date {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.iso())
    }
}

/// Value of a run of ASCII digits (at least one), or `None`.
fn digits(text: &str) -> Option<u32> {
    if !text.is_empty() && text.bytes().all(|b| b.is_ascii_digit()) { text.parse().ok() } else { None }
}

fn is_leap(year: i32) -> bool {
    (year % 4 == 0 && year % 100 != 0) || year % 400 == 0
}

fn days_in_month(year: i32, month: u32) -> u32 {
    match month {
        4 | 6 | 9 | 11 => 30,
        2 if is_leap(year) => 29,
        2 => 28,
        _ => 31,
    }
}

/// Days since 1970-01-01 of a civil date (H. Hinnant's algorithm).
pub fn days_from_civil(year: i32, month: u32, day: u32) -> i64 {
    let y = i64::from(year) - i64::from(month <= 2);
    let era = if y >= 0 { y } else { y - 399 } / 400;
    let yoe = y - era * 400;
    let m = i64::from(month);
    let shifted = if m > 2 { m - 3 } else { m + 9 };
    let doy = (153 * shifted + 2) / 5 + i64::from(day) - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146_097 + doe - 719_468
}

/// Civil date of a day count since 1970-01-01.
pub fn civil_from_days(days: i64) -> (i32, u32, u32) {
    let z = days + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let month = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    let year = yoe + era * 400 + i64::from(month <= 2);
    (year as i32, month, day)
}

/// Current instant in whole seconds since the epoch (UTC).
pub fn now_seconds() -> i64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs() as i64).unwrap_or(0)
}

/// Whole seconds since the epoch of a file-system time (`fromtimestamp` then
/// `replace(microsecond=0)`).
pub fn system_seconds(time: SystemTime) -> i64 {
    match time.duration_since(UNIX_EPOCH) {
        Ok(d) => d.as_secs() as i64,
        Err(e) => {
            let before = e.duration();
            -(before.as_secs() as i64) - i64::from(before.subsec_nanos() > 0)
        }
    }
}

/// `datetime.isoformat()` of a UTC instant in whole seconds: `2026-09-23T12:00:00+00:00`.
pub fn iso_utc(seconds: i64) -> String {
    let (year, month, day) = civil_from_days(seconds.div_euclid(86_400));
    let rest = seconds.rem_euclid(86_400);
    format!("{year:04}-{month:02}-{day:02}T{:02}:{:02}:{:02}+00:00",
            rest / 3600, rest % 3600 / 60, rest % 60)
}

/// Microseconds since the epoch of an ISO 8601 instant *with* a UTC offset, as
/// `datetime.fromisoformat` reads it; `None` when invalid or naive.
pub fn parse_aware(text: &str) -> Option<i64> {
    if !text.is_ascii() || text.len() < 11 {
        return None;
    }
    let date = Date::parse(&text[..10])?;
    let rest = &text[11..];
    let split = rest.find(['+', '-', 'Z'])?;
    let (time, zone) = rest.split_at(split);
    let clock = parse_clock(time)?;
    let offset = parse_offset(zone)?;
    Some((date.days() * 86_400) * 1_000_000 + clock - offset)
}

/// Microseconds of `HH[:MM[:SS[.f{1,6}]]]` (also without colons).
fn parse_clock(text: &str) -> Option<i64> {
    let (main, fraction) = match text.split_once(['.', ',']) {
        Some((main, fraction)) => (main, Some(fraction)),
        None => (text, None),
    };
    let parts: Vec<&str> = if main.contains(':') {
        main.split(':').collect()
    } else {
        main.as_bytes().chunks(2).map(|c| std::str::from_utf8(c).unwrap_or("")).collect()
    };
    if parts.is_empty() || parts.len() > 3 || parts.iter().any(|p| p.len() != 2) {
        return None;
    }
    let hour = digits(parts[0])?;
    let minute = parts.get(1).map_or(Some(0), |p| digits(p))?;
    let second = parts.get(2).map_or(Some(0), |p| digits(p))?;
    if hour > 23 || minute > 59 || second > 59 {
        return None;
    }
    let mut micros = 0i64;
    if let Some(fraction) = fraction {
        if parts.len() != 3 || fraction.is_empty() || fraction.len() > 6 {
            return None;
        }
        let padded = format!("{fraction:0<6}");
        micros = i64::from(digits(&padded)?);
    }
    Some((i64::from(hour) * 3600 + i64::from(minute) * 60 + i64::from(second)) * 1_000_000 + micros)
}

/// Microseconds of a UTC offset: `Z`, `+HH:MM[:SS[.ffffff]]` or `+HHMM`.
fn parse_offset(text: &str) -> Option<i64> {
    if text == "Z" {
        return Some(0);
    }
    let sign = match text.as_bytes().first()? {
        b'+' => 1,
        b'-' => -1,
        _ => return None,
    };
    let micros = parse_clock(&text[1..])?;
    if micros >= 86_400 * 1_000_000 {
        return None;
    }
    Some(sign * micros)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn civil_round_trip() {
        for days in [-719_162, -1, 0, 1, 11_016, 20_720, 2_932_896] {
            let (y, m, d) = civil_from_days(days);
            assert_eq!(days_from_civil(y, m, d), days);
        }
        assert_eq!(civil_from_days(0), (1970, 1, 1));
    }

    #[test]
    fn dates_are_strict() {
        assert_eq!(Date::parse("2026-02-28").map(|d| d.iso()).as_deref(), Some("2026-02-28"));
        assert!(Date::parse("2026-02-29").is_none());
        assert!(Date::parse("2024-02-29").is_some());
        assert!(Date::parse("0000-01-01").is_none());
        assert!(Date::parse("2026-1-01").is_none());
        assert!(Date::parse("２０２６-01-01").is_none());
    }

    #[test]
    fn instants_follow_fromisoformat() {
        let base = parse_aware("2026-09-23T12:00:00+00:00").unwrap();
        assert_eq!(iso_utc(base / 1_000_000), "2026-09-23T12:00:00+00:00");
        assert_eq!(parse_aware("2026-09-23T14:00:00+02:00"), Some(base));
        assert_eq!(parse_aware("2026-09-23 12:00:00.5Z"), Some(base + 500_000));
        assert_eq!(parse_aware("2026-09-23T12+00:00"), Some(base));
        assert_eq!(parse_aware("2026-09-23T12:00:00"), None);
        assert_eq!(parse_aware("2026-09-23"), None);
        assert_eq!(parse_aware("yesterday"), None);
    }
}
