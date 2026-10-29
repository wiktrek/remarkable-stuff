//! Key presses to `Input.dispatchKeyEvent` parameters, and address-bar input
//! to URLs.

use rmkit::{Key, KeyPress};
use serde_json::{json, Value};

/// CDP modifier bitmask.
fn modifiers(kp: &KeyPress) -> u8 {
    kp.mods.alt as u8 | (kp.mods.ctrl as u8) << 1 | (kp.mods.shift as u8) << 3
}

/// (key, code, windows virtual key code, text)
fn describe(key: Key) -> Option<(String, String, u32, Option<&'static str>)> {
    let named = |k: &str, vk: u32, text: Option<&'static str>| Some((k.to_string(), k.to_string(), vk, text));
    match key {
        Key::Enter => named("Enter", 13, Some("\r")),
        Key::Backspace => named("Backspace", 8, None),
        Key::Tab => named("Tab", 9, None),
        Key::Escape => named("Escape", 27, None),
        Key::Up => named("ArrowUp", 38, None),
        Key::Down => named("ArrowDown", 40, None),
        Key::Left => named("ArrowLeft", 37, None),
        Key::Right => named("ArrowRight", 39, None),
        Key::Home => named("Home", 36, None),
        Key::End => named("End", 35, None),
        Key::PageUp => named("PageUp", 33, None),
        Key::PageDown => named("PageDown", 34, None),
        Key::Insert => named("Insert", 45, None),
        Key::Delete => named("Delete", 46, None),
        Key::Char(c) => {
            let upper = c.to_ascii_uppercase();
            let (code, vk) = match upper {
                'A'..='Z' => (format!("Key{upper}"), upper as u32),
                '0'..='9' => (format!("Digit{upper}"), upper as u32),
                ' ' => ("Space".into(), 32),
                _ => (String::new(), 0),
            };
            Some((c.to_string(), code, vk, None))
        }
        Key::Action(_) => None,
    }
}

/// Editing shortcuts need explicit commands in headless Chromium.
fn command(kp: &KeyPress) -> Option<&'static str> {
    if !kp.mods.ctrl {
        return None;
    }
    let Key::Char(c) = kp.key else { return None };
    Some(match c.to_ascii_lowercase() {
        'a' => "selectAll",
        'c' => "copy",
        'x' => "cut",
        'v' => "paste",
        'z' if kp.mods.shift => "redo",
        'z' => "undo",
        'y' => "redo",
        _ => return None,
    })
}

/// The keyDown/keyUp pair for one key press.
pub fn key_events(kp: KeyPress) -> Vec<Value> {
    let Some((key, code, vk, special_text)) = describe(kp.key) else {
        return Vec::new();
    };
    let mods = modifiers(&kp);
    let typed = match kp.key {
        Key::Char(_) if !kp.mods.ctrl && !kp.mods.alt => Some(key.clone()),
        _ => special_text.map(String::from),
    };
    let mut down = json!({
        "type": if typed.is_some() { "keyDown" } else { "rawKeyDown" },
        "key": key,
        "code": code,
        "windowsVirtualKeyCode": vk,
        "nativeVirtualKeyCode": vk,
        "modifiers": mods,
    });
    if let Some(t) = &typed {
        down["text"] = json!(t);
        down["unmodifiedText"] = json!(t);
    }
    if let Some(c) = command(&kp) {
        down["commands"] = json!([c]);
    }
    let up = json!({
        "type": "keyUp",
        "key": key,
        "code": code,
        "windowsVirtualKeyCode": vk,
        "nativeVirtualKeyCode": vk,
        "modifiers": mods,
    });
    vec![down, up]
}

/// Turn whatever was typed in the address bar into a URL: full URLs pass
/// through, things that look like hosts get `https://`, the rest is searched.
pub fn to_url(input: &str, search: &str) -> String {
    let s = input.trim();
    if s.contains("://") || s.starts_with("about:") || s.starts_with("data:") || s.starts_with("file:") {
        return s.to_string();
    }
    let host = s.split('/').next().unwrap_or("");
    let looks_like_host = !s.contains(' ')
        && (host.contains('.') || host.starts_with("localhost"))
        && !host.starts_with('.')
        && !host.ends_with('.');
    if looks_like_host {
        return format!("https://{s}");
    }
    let mut q = String::new();
    for b in s.bytes() {
        match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => q.push(b as char),
            b' ' => q.push('+'),
            _ => q.push_str(&format!("%{b:02X}")),
        }
    }
    search.replace("%s", &q)
}

#[cfg(test)]
mod tests {
    use super::*;
    use rmkit::Mods;

    const SEARCH: &str = "https://duckduckgo.com/?q=%s";

    #[test]
    fn urls_and_searches() {
        assert_eq!(to_url("example.com", SEARCH), "https://example.com");
        assert_eq!(to_url(" https://a.b/c ", SEARCH), "https://a.b/c");
        assert_eq!(to_url("localhost:8484/x", SEARCH), "https://localhost:8484/x");
        assert_eq!(to_url("remarkable paper pro", SEARCH), "https://duckduckgo.com/?q=remarkable+paper+pro");
        assert_eq!(to_url("c++", SEARCH), "https://duckduckgo.com/?q=c%2B%2B");
    }

    #[test]
    fn typing_and_shortcuts() {
        let a = key_events(KeyPress { key: Key::Char('A'), mods: Mods { shift: true, ..Default::default() } });
        assert_eq!(a[0]["type"], "keyDown");
        assert_eq!(a[0]["text"], "A");
        assert_eq!(a[0]["windowsVirtualKeyCode"], 65);
        let copy = key_events(KeyPress { key: Key::Char('c'), mods: Mods { ctrl: true, ..Default::default() } });
        assert_eq!(copy[0]["type"], "rawKeyDown");
        assert_eq!(copy[0]["commands"][0], "copy");
        assert!(copy[0].get("text").is_none());
        let enter = key_events(KeyPress { key: Key::Enter, mods: Mods::default() });
        assert_eq!(enter[0]["text"], "\r");
    }
}
