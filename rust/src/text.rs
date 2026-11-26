//! Shell quoting for printed command lines, and UTC timestamps.

use std::time::SystemTime;

/// A command line as a shell would need it typed.
#[must_use]
pub fn shell_join<S: AsRef<str>>(argv: &[S]) -> String {
    // Only a NUL byte fails to quote, and no command-line argument can hold one.
    shlex::try_join(argv.iter().map(AsRef::as_ref)).unwrap_or_default()
}

/// `time` as `2026-11-26T10:00:00+00:00`; with `micros`, as `20261126T100000-123456Z` for names that sort by time.
#[must_use]
pub fn utc(time: SystemTime, micros: bool) -> String {
    let ts = jiff::Timestamp::try_from(time).unwrap_or(jiff::Timestamp::UNIX_EPOCH);
    ts.strftime(if micros {
        "%Y%m%dT%H%M%S-%6fZ"
    } else {
        "%Y-%m-%dT%H:%M:%S+00:00"
    })
    .to_string()
}

#[cfg(test)]
mod tests {
    use std::time::{Duration, SystemTime};

    use pretty_assertions::assert_eq;

    use super::{shell_join, utc};

    #[test]
    fn shell_join_round_trips() {
        let argv = ["terraform", "-chdir=/a/b", "x y", "it's", ""];
        assert_eq!(shlex::split(&shell_join(&argv)).unwrap(), argv);
        assert_eq!(shell_join(&["terraform", "plan"]), "terraform plan");
    }

    #[test]
    fn utc_formats() {
        let at = SystemTime::UNIX_EPOCH + Duration::from_millis(1_795_687_200_500);
        assert_eq!(utc(at, false), "2026-11-26T10:00:00+00:00");
        assert_eq!(utc(at, true), "20261126T100000-500000Z");
        assert_eq!(
            utc(SystemTime::UNIX_EPOCH - Duration::from_secs(5), false),
            "1969-12-31T23:59:55+00:00"
        );
    }
}
