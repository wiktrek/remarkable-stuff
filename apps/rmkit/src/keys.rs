//! Keys shared by the on-screen keyboard and hardware keyboards.

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Key {
    Char(char),
    Enter,
    Backspace,
    Tab,
    Escape,
    Up,
    Down,
    Left,
    Right,
    Home,
    End,
    PageUp,
    PageDown,
    Insert,
    Delete,
    /// An app-defined key from the keyboard's top row.
    Action(u8),
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Mods {
    pub shift: bool,
    pub ctrl: bool,
    pub alt: bool,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct KeyPress {
    pub key: Key,
    pub mods: Mods,
}

const QT_ESCAPE: i32 = 0x0100_0000;
const QT_TAB: i32 = 0x0100_0001;
const QT_BACKTAB: i32 = 0x0100_0002;
const QT_BACKSPACE: i32 = 0x0100_0003;
const QT_RETURN: i32 = 0x0100_0004;
const QT_ENTER: i32 = 0x0100_0005;
const QT_INSERT: i32 = 0x0100_0006;
const QT_DELETE: i32 = 0x0100_0007;
const QT_HOME: i32 = 0x0100_0010;
const QT_END: i32 = 0x0100_0011;
const QT_LEFT: i32 = 0x0100_0012;
const QT_UP: i32 = 0x0100_0013;
const QT_RIGHT: i32 = 0x0100_0014;
const QT_DOWN: i32 = 0x0100_0015;
const QT_PAGEUP: i32 = 0x0100_0016;
const QT_PAGEDOWN: i32 = 0x0100_0017;
const QT_SHIFT: i32 = 0x0100_0020;
const QT_CONTROL: i32 = 0x0100_0021;
const QT_META: i32 = 0x0100_0022;
const QT_ALT: i32 = 0x0100_0023;

/// Turns Qt key codes (Type Folio, AppLoad's keyboard) into key presses.
/// Qt reports letters as upper case and shifted symbols as the symbol itself,
/// so only letters need the held shift applied.
#[derive(Default)]
pub struct QtKeys {
    held: Mods,
}

impl QtKeys {
    pub fn key_down(&mut self, code: i32) -> Option<KeyPress> {
        let key = match code {
            QT_SHIFT => return self.set(|m| m.shift = true),
            QT_CONTROL | QT_META => return self.set(|m| m.ctrl = true),
            QT_ALT => return self.set(|m| m.alt = true),
            QT_ESCAPE => Key::Escape,
            QT_TAB => Key::Tab,
            QT_BACKTAB => {
                return Some(KeyPress { key: Key::Tab, mods: Mods { shift: true, ..self.held } });
            }
            QT_BACKSPACE => Key::Backspace,
            QT_RETURN | QT_ENTER => Key::Enter,
            QT_INSERT => Key::Insert,
            QT_DELETE => Key::Delete,
            QT_HOME => Key::Home,
            QT_END => Key::End,
            QT_LEFT => Key::Left,
            QT_UP => Key::Up,
            QT_RIGHT => Key::Right,
            QT_DOWN => Key::Down,
            QT_PAGEUP => Key::PageUp,
            QT_PAGEDOWN => Key::PageDown,
            0x41..=0x5a => {
                let c = char::from(code as u8);
                Key::Char(if self.held.shift { c } else { c.to_ascii_lowercase() })
            }
            0x20..=0xffff => Key::Char(char::from_u32(code as u32)?),
            _ => return None,
        };
        Some(KeyPress { key, mods: self.held })
    }

    pub fn key_up(&mut self, code: i32) {
        match code {
            QT_SHIFT => self.held.shift = false,
            QT_CONTROL | QT_META => self.held.ctrl = false,
            QT_ALT => self.held.alt = false,
            _ => {}
        }
    }

    fn set(&mut self, f: impl FnOnce(&mut Mods)) -> Option<KeyPress> {
        f(&mut self.held);
        None
    }
}
