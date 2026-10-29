//! RGB565 drawing primitives.

pub type Color = u16;

pub const fn rgb(r: u8, g: u8, b: u8) -> Color {
    ((r as u16 & 0xf8) << 8) | ((g as u16 & 0xfc) << 3) | (b as u16 >> 3)
}

pub const fn gray(v: u8) -> Color {
    rgb(v, v, v)
}

pub const WHITE: Color = rgb(255, 255, 255);
pub const BLACK: Color = rgb(0, 0, 0);

pub fn unpack(c: Color) -> (u8, u8, u8) {
    let r = ((c >> 11) & 0x1f) as u8;
    let g = ((c >> 5) & 0x3f) as u8;
    let b = (c & 0x1f) as u8;
    ((r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2))
}

/// Mix `fg` over `bg` with coverage `a` (0 = bg, 255 = fg).
pub fn mix(bg: Color, fg: Color, a: u8) -> Color {
    match a {
        0 => bg,
        255 => fg,
        _ => {
            let (br, bgr, bb) = unpack(bg);
            let (fr, fgr, fb) = unpack(fg);
            let a = a as u32;
            let m = |b: u8, f: u8| ((b as u32 * (255 - a) + f as u32 * a + 127) / 255) as u8;
            rgb(m(br, fr), m(bgr, fgr), m(bb, fb))
        }
    }
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Rect {
    pub x: i32,
    pub y: i32,
    pub w: i32,
    pub h: i32,
}

impl Rect {
    pub const fn new(x: i32, y: i32, w: i32, h: i32) -> Rect {
        Rect { x, y, w, h }
    }

    pub fn right(&self) -> i32 {
        self.x + self.w
    }

    pub fn bottom(&self) -> i32 {
        self.y + self.h
    }

    pub fn is_empty(&self) -> bool {
        self.w <= 0 || self.h <= 0
    }

    pub fn contains(&self, x: i32, y: i32) -> bool {
        x >= self.x && x < self.right() && y >= self.y && y < self.bottom()
    }

    pub fn intersect(&self, o: Rect) -> Rect {
        let x = self.x.max(o.x);
        let y = self.y.max(o.y);
        let r = self.right().min(o.right());
        let b = self.bottom().min(o.bottom());
        Rect::new(x, y, (r - x).max(0), (b - y).max(0))
    }

    pub fn union(&self, o: Rect) -> Rect {
        if self.is_empty() {
            return o;
        }
        if o.is_empty() {
            return *self;
        }
        let x = self.x.min(o.x);
        let y = self.y.min(o.y);
        Rect::new(x, y, self.right().max(o.right()) - x, self.bottom().max(o.bottom()) - y)
    }

    pub fn inset(&self, d: i32) -> Rect {
        Rect::new(self.x + d, self.y + d, self.w - 2 * d, self.h - 2 * d)
    }
}

pub struct Canvas<'a> {
    pub buf: &'a mut [Color],
    pub width: i32,
    pub height: i32,
}

impl<'a> Canvas<'a> {
    pub fn new(buf: &'a mut [Color], width: i32, height: i32) -> Canvas<'a> {
        Canvas { buf, width, height }
    }

    pub fn bounds(&self) -> Rect {
        Rect::new(0, 0, self.width, self.height)
    }

    pub fn get(&self, x: i32, y: i32) -> Color {
        self.buf[(y * self.width + x) as usize]
    }

    pub fn blend(&mut self, x: i32, y: i32, c: Color, a: u8) {
        if x >= 0 && y >= 0 && x < self.width && y < self.height {
            let i = (y * self.width + x) as usize;
            self.buf[i] = mix(self.buf[i], c, a);
        }
    }

    pub fn fill(&mut self, r: Rect, c: Color) {
        let r = r.intersect(self.bounds());
        for y in r.y..r.bottom() {
            let row = (y * self.width) as usize;
            self.buf[row + r.x as usize..row + r.right() as usize].fill(c);
        }
    }

    pub fn stroke(&mut self, r: Rect, t: i32, c: Color) {
        self.fill(Rect::new(r.x, r.y, r.w, t), c);
        self.fill(Rect::new(r.x, r.bottom() - t, r.w, t), c);
        self.fill(Rect::new(r.x, r.y, t, r.h), c);
        self.fill(Rect::new(r.right() - t, r.y, t, r.h), c);
    }

    /// Copy a `src_w`-wide RGB565 image into `dst`, clipped to both.
    pub fn blit(&mut self, dst: Rect, src: &[Color], src_w: i32) {
        let r = dst.intersect(self.bounds());
        for y in r.y..r.bottom() {
            let s = ((y - dst.y) * src_w + (r.x - dst.x)) as usize;
            let d = (y * self.width + r.x) as usize;
            self.buf[d..d + r.w as usize].copy_from_slice(&src[s..s + r.w as usize]);
        }
    }
}
