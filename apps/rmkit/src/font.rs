//! Glyph rasterizing and caching for the embedded JetBrains Mono.

use std::collections::HashMap;
use std::rc::Rc;

use fontdue::{Font as Face, FontSettings, Metrics};

use crate::canvas::{Canvas, Color};

static REGULAR: &[u8] = include_bytes!("../fonts/JetBrainsMono-Regular.ttf");
static BOLD: &[u8] = include_bytes!("../fonts/JetBrainsMono-Bold.ttf");

pub struct Glyph {
    pub metrics: Metrics,
    /// Coverage, `metrics.width` × `metrics.height`, top row first.
    pub coverage: Vec<u8>,
}

pub struct Font {
    regular: Face,
    bold: Face,
    cache: HashMap<(char, bool, u32), Rc<Glyph>>,
    /// Maps raw coverage to ink. E-ink renders thin anti-aliased stems pale,
    /// so coverage is pushed towards solid.
    ramp: [u8; 256],
}

impl Default for Font {
    fn default() -> Self {
        Self::new()
    }
}

impl Font {
    pub fn new() -> Font {
        let load = |b| Face::from_bytes(b, FontSettings::default()).expect("embedded font");
        let mut ramp = [0u8; 256];
        for (i, v) in ramp.iter_mut().enumerate() {
            *v = ((i as f32 / 255.0).powf(0.7) * 255.0).round() as u8;
        }
        Font { regular: load(REGULAR), bold: load(BOLD), cache: HashMap::new(), ramp }
    }

    pub fn has_glyph(&self, c: char) -> bool {
        self.regular.lookup_glyph_index(c) != 0
    }

    /// Width of one cell of the monospace font.
    pub fn advance(&self, px: f32) -> f32 {
        self.regular.metrics('M', px).advance_width
    }

    /// (ascent, descent) in pixels; descent is negative.
    pub fn vertical(&self, px: f32) -> (f32, f32) {
        let m = self.regular.horizontal_line_metrics(px).expect("horizontal metrics");
        (m.ascent, m.descent)
    }

    pub fn glyph(&mut self, c: char, bold: bool, px: f32) -> Rc<Glyph> {
        let key = (c, bold, px.to_bits());
        if let Some(g) = self.cache.get(&key) {
            return g.clone();
        }
        let face = if bold { &self.bold } else { &self.regular };
        let (metrics, mut coverage) = face.rasterize(c, px);
        for v in coverage.iter_mut() {
            *v = self.ramp[*v as usize];
        }
        let g = Rc::new(Glyph { metrics, coverage });
        self.cache.insert(key, g.clone());
        g
    }

    /// Draw one character with its origin at (`x`, `baseline`).
    pub fn draw_char(&mut self, cv: &mut Canvas, x: i32, baseline: i32, c: char, bold: bool, px: f32, fg: Color) {
        let g = self.glyph(c, bold, px);
        let m = &g.metrics;
        let left = x + m.xmin;
        let top = baseline - m.ymin - m.height as i32;
        for row in 0..m.height {
            for col in 0..m.width {
                let a = g.coverage[row * m.width + col];
                if a != 0 {
                    cv.blend(left + col as i32, top + row as i32, fg, a);
                }
            }
        }
    }

    /// Draw a string; returns its width.
    pub fn draw_text(&mut self, cv: &mut Canvas, x: i32, baseline: i32, text: &str, bold: bool, px: f32, fg: Color) -> i32 {
        let adv = self.advance(px);
        let mut pen = x as f32;
        for c in text.chars() {
            self.draw_char(cv, pen.round() as i32, baseline, c, bold, px, fg);
            pen += adv;
        }
        (pen - x as f32).round() as i32
    }

    pub fn text_width(&self, text: &str, px: f32) -> i32 {
        (self.advance(px) * text.chars().count() as f32).round() as i32
    }
}
