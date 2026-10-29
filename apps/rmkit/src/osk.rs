//! On-screen keyboard: a full US layout with sticky modifiers and a top row
//! of navigation keys plus app-defined actions.

use crate::canvas::{gray, Canvas, Rect, BLACK, WHITE};
use crate::font::Font;
use crate::keys::{Key, KeyPress, Mods};

const UNITS_PER_ROW: f32 = 16.0;
const LABEL_PX: f32 = 38.0;
const SMALL_LABEL_PX: f32 = 28.0;

#[derive(Clone, Copy, PartialEq, Eq)]
enum Kind {
    Key(Key, Option<Key>),
    Shift,
    Ctrl,
    Alt,
}

struct KeyDef {
    label: String,
    shift_label: Option<String>,
    kind: Kind,
    width: f32,
    rect: Rect,
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Sticky {
    Off,
    /// Applies to the next key only.
    Once,
    Locked,
}

impl Sticky {
    fn on(self) -> bool {
        self != Sticky::Off
    }

    fn cycle(self) -> Sticky {
        match self {
            Sticky::Off => Sticky::Once,
            Sticky::Once => Sticky::Locked,
            Sticky::Locked => Sticky::Off,
        }
    }

    fn consume(self) -> Sticky {
        if self == Sticky::Once { Sticky::Off } else { self }
    }
}

pub struct Keyboard {
    pub rect: Rect,
    keys: Vec<Vec<KeyDef>>,
    shift: Sticky,
    ctrl: Sticky,
    alt: Sticky,
    pressed: Option<(usize, usize)>,
}

fn special(label: &str, key: Key, width: f32) -> KeyDef {
    KeyDef { label: label.into(), shift_label: None, kind: Kind::Key(key, None), width, rect: Rect::default() }
}

fn modifier(label: &str, kind: Kind, width: f32) -> KeyDef {
    KeyDef { label: label.into(), shift_label: None, kind, width, rect: Rect::default() }
}

/// Plain characters; each pair in `pairs` is (normal, shifted).
fn chars(pairs: &str) -> Vec<KeyDef> {
    let cs: Vec<char> = pairs.chars().collect();
    cs.chunks(2)
        .map(|p| {
            let (n, s) = (p[0], p[1]);
            KeyDef {
                label: n.to_string(),
                shift_label: Some(s.to_string()),
                kind: Kind::Key(Key::Char(n), Some(Key::Char(s))),
                width: 1.0,
                rect: Rect::default(),
            }
        })
        .collect()
}

impl Keyboard {
    /// `actions` are (label, id) pairs appended to the top row; pressing one
    /// yields `Key::Action(id)`.
    pub fn new(actions: &[(&str, u8)]) -> Keyboard {
        let mut top = vec![
            special("Home", Key::Home, 1.0),
            special("End", Key::End, 1.0),
            special("PgUp", Key::PageUp, 1.0),
            special("PgDn", Key::PageDown, 1.0),
        ];
        for (label, id) in actions {
            top.push(special(label, Key::Action(*id), 1.0));
        }
        let n = top.len() as f32;
        for k in top.iter_mut() {
            k.width = UNITS_PER_ROW / n;
        }

        let row = |mut v: Vec<KeyDef>, before: Vec<KeyDef>, after: Vec<KeyDef>| {
            let mut out = before;
            out.append(&mut v);
            out.extend(after);
            out
        };
        let keys = vec![
            top,
            row(chars("`~1!2@3#4$5%6^7&8*9(0)-_=+"), vec![special("Esc", Key::Escape, 1.25)], vec![special("⌫", Key::Backspace, 1.75)]),
            row(chars("qQwWeErRtTyYuUiIoOpP[{]}"), vec![special("⇥", Key::Tab, 2.0)], {
                let mut v = chars("\\|");
                v[0].width = 2.0;
                v
            }),
            row(chars("aAsSdDfFgGhHjJkKlL;:'\""), vec![modifier("Ctrl", Kind::Ctrl, 2.0)], vec![special("⏎", Key::Enter, 3.0)]),
            row(
                chars("zZxXcCvVbBnNmM,<.>/?"),
                vec![modifier("⇧", Kind::Shift, 2.5)],
                vec![special("↑", Key::Up, 1.0), special("Del", Key::Delete, 2.5)],
            ),
            vec![
                modifier("Alt", Kind::Alt, 2.5),
                special("", Key::Char(' '), 10.5),
                special("←", Key::Left, 1.0),
                special("↓", Key::Down, 1.0),
                special("→", Key::Right, 1.0),
            ],
        ];

        Keyboard { rect: Rect::default(), keys, shift: Sticky::Off, ctrl: Sticky::Off, alt: Sticky::Off, pressed: None }
    }

    /// Height the keyboard wants at a given screen width.
    pub fn height_for(&self, width: i32) -> i32 {
        let key_h = (width as f32 / UNITS_PER_ROW * 0.92).round() as i32;
        key_h * self.keys.len() as i32 + 16
    }

    pub fn layout(&mut self, rect: Rect) {
        self.rect = rect;
        let inner = Rect::new(rect.x + 6, rect.y + 10, rect.w - 12, rect.h - 16);
        let rows = self.keys.len() as i32;
        let unit = inner.w as f32 / UNITS_PER_ROW;
        for (r, row) in self.keys.iter_mut().enumerate() {
            let y0 = inner.y + inner.h * r as i32 / rows;
            let y1 = inner.y + inner.h * (r as i32 + 1) / rows;
            let mut x = inner.x as f32;
            for k in row.iter_mut() {
                let x1 = x + k.width * unit;
                k.rect = Rect::new(x.round() as i32, y0, (x1.round() - x.round()) as i32, y1 - y0);
                x = x1;
            }
        }
    }

    pub fn draw(&self, cv: &mut Canvas, font: &mut Font) {
        cv.fill(self.rect, gray(0xd8));
        cv.fill(Rect::new(self.rect.x, self.rect.y, self.rect.w, 3), BLACK);
        for r in 0..self.keys.len() {
            for c in 0..self.keys[r].len() {
                self.draw_key(cv, font, r, c);
            }
        }
    }

    fn draw_key(&self, cv: &mut Canvas, font: &mut Font, r: usize, c: usize) {
        let k = &self.keys[r][c];
        let inked = self.pressed == Some((r, c))
            || match k.kind {
                Kind::Shift => self.shift == Sticky::Locked,
                Kind::Ctrl => self.ctrl == Sticky::Locked,
                Kind::Alt => self.alt == Sticky::Locked,
                Kind::Key(..) => false,
            };
        let once = match k.kind {
            Kind::Shift => self.shift == Sticky::Once,
            Kind::Ctrl => self.ctrl == Sticky::Once,
            Kind::Alt => self.alt == Sticky::Once,
            Kind::Key(..) => false,
        };
        let face = k.rect.inset(4);
        cv.fill(k.rect, gray(0xd8));
        let (bg, fg) = if inked {
            (BLACK, WHITE)
        } else if once {
            (gray(0x90), BLACK)
        } else {
            (WHITE, BLACK)
        };
        cv.fill(face, bg);
        cv.stroke(face, 2, BLACK);

        let label = match (&k.shift_label, self.shift.on()) {
            (Some(s), true) => s.as_str(),
            _ => k.label.as_str(),
        };
        let px = if label.chars().count() > 2 { SMALL_LABEL_PX } else { LABEL_PX };
        let (asc, desc) = font.vertical(px);
        let w = font.text_width(label, px);
        let x = face.x + (face.w - w) / 2;
        let baseline = face.y + (face.h as f32 / 2.0 + (asc + desc) / 2.0).round() as i32;
        font.draw_text(cv, x, baseline, label, false, px, fg);
    }

    fn hit(&self, x: i32, y: i32) -> Option<(usize, usize)> {
        for (r, row) in self.keys.iter().enumerate() {
            for (c, k) in row.iter().enumerate() {
                if k.rect.contains(x, y) {
                    return Some((r, c));
                }
            }
        }
        None
    }

    /// Handle a finger landing at (`x`, `y`). Keys fire on press for snappy
    /// e-ink feedback. Returns the key (if any) and the area to refresh.
    pub fn press(&mut self, cv: &mut Canvas, font: &mut Font, x: i32, y: i32) -> (Option<KeyPress>, Rect) {
        let Some((r, c)) = self.hit(x, y) else {
            return (None, Rect::default());
        };
        let before = (self.shift, self.ctrl, self.alt);
        let mut out = None;
        match self.keys[r][c].kind {
            Kind::Shift => self.shift = self.shift.cycle(),
            Kind::Ctrl => self.ctrl = self.ctrl.cycle(),
            Kind::Alt => self.alt = self.alt.cycle(),
            Kind::Key(key, shifted) => {
                let mods = Mods { shift: self.shift.on(), ctrl: self.ctrl.on(), alt: self.alt.on() };
                let key = match shifted {
                    Some(s) if mods.shift => s,
                    _ => key,
                };
                out = Some(KeyPress { key, mods });
                self.shift = self.shift.consume();
                self.ctrl = self.ctrl.consume();
                self.alt = self.alt.consume();
                self.pressed = Some((r, c));
            }
        }
        if before != (self.shift, self.ctrl, self.alt) {
            self.draw(cv, font);
            return (out, self.rect);
        }
        self.draw_key(cv, font, r, c);
        (out, self.keys[r][c].rect)
    }

    /// Forget the pressed key without drawing, e.g. after the keyboard was hidden.
    pub fn cancel(&mut self) {
        self.pressed = None;
    }

    /// Finger lifted: un-highlight the pressed key.
    pub fn release(&mut self, cv: &mut Canvas, font: &mut Font) -> Rect {
        match self.pressed.take() {
            Some((r, c)) => {
                self.draw_key(cv, font, r, c);
                self.keys[r][c].rect
            }
            None => Rect::default(),
        }
    }
}
