//! A Chromium-based web browser for the reMarkable Paper Pro.
//!
//! Headless Chromium runs on the tablet and streams the page as JPEG frames
//! (`Page.startScreencast`). We paint changed regions into AppLoad's qtfb
//! framebuffer and turn touch, pen and keys into DevTools input events.
//!
//! Everything is tuned so the e-ink panel updates as rarely as possible:
//! frames are held back until the page stops changing, pages are flipped a
//! screen at a time instead of scrolled, CSS animations are switched off,
//! and pixels are snapped to black and white (dithered) so the fast
//! waveform can draw them cleanly.

mod chrome;
mod keys;

use std::collections::HashMap;
use std::path::PathBuf;
use std::time::{Duration, Instant};

use base64::Engine as _;
use rmkit::canvas::{gray, Canvas, Color, BLACK, WHITE};
use rmkit::{poll_readable, Display, Font, Input, Key, KeyPress, Keyboard, QtKeys, Rect, RefreshMode};
use serde_json::{json, Value};
use zune_jpeg::zune_core::bytestream::ZCursor;
use zune_jpeg::zune_core::colorspace::ColorSpace;
use zune_jpeg::zune_core::options::DecoderOptions;
use zune_jpeg::JpegDecoder;

use chrome::Chrome;

const TOOLBAR_H: i32 = 120;
const BUTTON_W: i32 = 130;
/// Paint once the page has stopped changing for this long...
const QUIET: Duration = Duration::from_millis(150);
/// ...or once a change has waited this long (pages that are still loading).
const MAX_WAIT: Duration = Duration::from_millis(1000);
/// Changes this soon after a tap, swipe, key or page load are painted quickly.
const ACTIVE_WINDOW: Duration = Duration::from_secs(3);
/// Changes nobody asked for (GIFs, carousels, ads, a blinking caret) are
/// painted at most this often.
const IDLE_INTERVAL: Duration = Duration::from_secs(5);
/// Finger travel that turns a tap into a page flip.
const DRAG_THRESHOLD: i32 = 40;
/// How much of the screen a flip moves; the rest stays on screen for context.
const FLIP_STEP: f32 = 0.85;
/// If no screencast frame shows up this long after input, take a screenshot.
const CAPTURE_FALLBACK: Duration = Duration::from_millis(900);

/// Injected into every page: no CSS animations, transitions or smooth scrolling.
const EINK_SCRIPT: &str = r#"(() => {
  const css = '*,*::before,*::after{animation-duration:0s!important;animation-delay:0s!important;' +
    'animation-iteration-count:1!important;transition-duration:0s!important;transition-delay:0s!important;' +
    'scroll-behavior:auto!important}';
  const add = () => {
    if (!document.documentElement) return false;
    const s = document.createElement('style');
    s.textContent = css;
    (document.head || document.documentElement).appendChild(s);
    return true;
  };
  if (!add()) new MutationObserver((_, o) => { if (add()) o.disconnect(); }).observe(document, { childList: true });
})()"#;

const ACTION_REFRESH: u8 = 1;
const ACTION_HIDE: u8 = 2;

const FOCUS_CHECK: &str = "(() => { const e = document.activeElement; if (!e) return false; \
    if (e.isContentEditable) return true; if (e.tagName === 'TEXTAREA') return true; \
    if (e.tagName !== 'INPUT') return false; \
    return !['button','checkbox','radio','submit','reset','file','image','range','color','hidden'] \
    .includes((e.type || '').toLowerCase()); })()";

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum Button {
    Back,
    Forward,
    Reload,
    Address,
    Keyboard,
    Quit,
}

const BUTTONS: [Button; 6] = [Button::Back, Button::Forward, Button::Reload, Button::Address, Button::Keyboard, Button::Quit];

enum Pending {
    Version,
    Attach(String),
    History(i64),
    FocusCheck,
    Screenshot,
}

struct Page {
    target: String,
    session: String,
    url: String,
    loading: bool,
}

struct AddressEdit {
    text: String,
    /// The pre-filled URL is replaced by the first key typed.
    fresh: bool,
}

#[derive(PartialEq)]
enum Gesture {
    Keyboard,
    Toolbar,
    Tap,
    Scroll,
}

struct Touch {
    id: i32,
    gesture: Gesture,
    start: (i32, i32),
    last: (i32, i32),
}

#[derive(Clone, Copy, PartialEq)]
enum Colors {
    /// Dithered black and white: crisp with the fast waveform.
    Mono,
    /// 16 shades of gray: nicer photos, wants a slower refresh mode.
    Gray,
}

struct Config {
    home: String,
    search: String,
    zoom: f32,
    colors: Colors,
    /// Full flashing refresh every this many page flips (0 = never).
    flash_every: u32,
    engine: PathBuf,
    data: PathBuf,
}

impl Config {
    fn from_env() -> Config {
        let var = |k: &str| std::env::var(k).ok().filter(|v| !v.is_empty());
        let exe_dir = std::env::current_exe().ok().and_then(|p| p.parent().map(PathBuf::from)).unwrap_or_default();
        let home = std::env::var("HOME").unwrap_or_else(|_| "/home/root".into());
        Config {
            home: var("BROWSER_HOME").unwrap_or_else(|| "https://duckduckgo.com/".into()),
            search: var("BROWSER_SEARCH").unwrap_or_else(|| "https://duckduckgo.com/?q=%s".into()),
            zoom: var("BROWSER_ZOOM").and_then(|z| z.parse().ok()).filter(|z: &f32| *z >= 0.5).unwrap_or(2.0),
            colors: match var("BROWSER_COLORS").as_deref() {
                Some("gray") | Some("grey") => Colors::Gray,
                _ => Colors::Mono,
            },
            flash_every: var("BROWSER_FLASH_EVERY").and_then(|n| n.parse().ok()).unwrap_or(6),
            engine: var("BROWSER_ENGINE").map(PathBuf::from).unwrap_or_else(|| exe_dir.join("engine")),
            data: var("BROWSER_DATA").map(PathBuf::from).unwrap_or_else(|| PathBuf::from(home).join(".local/share/rm-browser")),
        }
    }
}

struct App {
    cfg: Config,
    display: Display,
    font: Font,
    chrome: Chrome,
    keyboard: Keyboard,
    keyboard_visible: bool,
    qt: QtKeys,
    pending: HashMap<u64, Pending>,
    user_agent: Option<String>,
    /// Attached pages; the last one is on screen. Popups stack on top.
    pages: Vec<Page>,
    attaching: bool,
    address: Option<AddressEdit>,
    view: Rect,
    /// What the viewport currently shows, to repaint only what changed.
    shown: Vec<Color>,
    /// Luminance -> screen color, per 4x4 dither cell position.
    palette: Vec<Color>,
    /// Newest frame not yet painted, when the first unpainted one arrived,
    /// and when the newest one did.
    frame: Option<String>,
    frame_first: Instant,
    frame_last: Instant,
    last_paint: Instant,
    /// Until when changes count as a reaction to the user.
    active_until: Instant,
    flips: u32,
    capture_due: Option<Instant>,
    refresh_after_paint: bool,
    touch: Option<Touch>,
    quit: bool,
}

fn button_rect(b: Button, width: i32) -> Rect {
    match b {
        Button::Back => Rect::new(0, 0, BUTTON_W, TOOLBAR_H),
        Button::Forward => Rect::new(BUTTON_W, 0, BUTTON_W, TOOLBAR_H),
        Button::Reload => Rect::new(2 * BUTTON_W, 0, BUTTON_W, TOOLBAR_H),
        Button::Address => Rect::new(3 * BUTTON_W + 10, 0, width - 5 * BUTTON_W - 20, TOOLBAR_H),
        Button::Keyboard => Rect::new(width - 2 * BUTTON_W, 0, BUTTON_W, TOOLBAR_H),
        Button::Quit => Rect::new(width - BUTTON_W, 0, BUTTON_W, TOOLBAR_H),
    }
}

/// Show a message in the middle of the screen.
fn message(display: &mut Display, font: &mut Font, lines: &[&str]) {
    let (w, h) = (display.width, display.height);
    let mut cv = display.canvas();
    cv.fill(Rect::new(0, TOOLBAR_H, w, h - TOOLBAR_H), WHITE);
    let px = 34.0;
    let line_h = 56;
    let mut y = h / 2 - line_h * lines.len() as i32 / 2;
    for line in lines {
        let tw = font.text_width(line, px);
        font.draw_text(&mut cv, ((w - tw) / 2).max(20), y, line, false, px, BLACK);
        y += line_h;
    }
    display.update_all();
}

/// Show an error until the user taps the screen (or a minute passes).
fn fatal(display: &mut Display, font: &mut Font, lines: &[&str]) -> ! {
    message(display, font, lines);
    let deadline = Instant::now() + Duration::from_secs(60);
    while Instant::now() < deadline {
        if poll_readable(&[display.fd()], 1000)[0] {
            match display.read_input() {
                Ok(inputs) if inputs.iter().any(|i| matches!(i, Input::TouchUp { .. } | Input::PenUp { .. })) => break,
                Err(_) => break,
                _ => {}
            }
        }
    }
    std::process::exit(1);
}

impl App {
    fn new() -> App {
        let cfg = Config::from_env();
        let mut display = match Display::open() {
            Ok(d) => d,
            Err(e) => {
                eprintln!("rm-browser: {e}");
                std::process::exit(1);
            }
        };
        display.set_refresh_mode(RefreshMode::from_env("BROWSER_REFRESH", RefreshMode::Fast));
        let mut font = Font::new();
        let mut keyboard = Keyboard::new(&[("Rfsh", ACTION_REFRESH), ("Hide", ACTION_HIDE)]);
        let kb_h = keyboard.height_for(display.width);
        keyboard.layout(Rect::new(0, display.height - kb_h, display.width, kb_h));

        message(&mut display, &mut font, &["Starting Chromium…"]);
        let view = Rect::new(0, TOOLBAR_H, display.width, display.height - TOOLBAR_H);
        let (css_w, css_h) = ((view.w as f32 / cfg.zoom) as u32, (view.h as f32 / cfg.zoom) as u32);
        let chrome = match Chrome::spawn(&cfg.engine, &cfg.data, css_w, css_h) {
            Ok(c) => c,
            Err(e) => fatal(&mut display, &mut font, &["Could not start Chromium:", &e.to_string(), "", "Tap to close."]),
        };

        let palette = palette(cfg.colors);
        let now = Instant::now();
        let mut app = App {
            cfg,
            display,
            font,
            chrome,
            keyboard,
            keyboard_visible: false,
            qt: QtKeys::default(),
            pending: HashMap::new(),
            user_agent: None,
            pages: Vec::new(),
            attaching: false,
            address: None,
            view,
            shown: Vec::new(),
            palette,
            frame: None,
            frame_first: now,
            frame_last: now,
            last_paint: now,
            active_until: now + ACTIVE_WINDOW,
            flips: 0,
            capture_due: None,
            refresh_after_paint: false,
            touch: None,
            quit: false,
        };
        app.shown = vec![WHITE; (view.w * view.h) as usize];
        app.draw_toolbar(true);
        let id = app.chrome.send("Browser.getVersion", json!({}), None);
        app.pending.insert(id, Pending::Version);
        app
    }

    fn page(&self) -> Option<&Page> {
        self.pages.last()
    }

    fn session(&self) -> Option<String> {
        self.page().map(|p| p.session.clone())
    }

    /// Send a command to the page on screen.
    fn page_cmd(&mut self, method: &str, params: Value) -> Option<u64> {
        let s = self.session()?;
        Some(self.chrome.send(method, params, Some(&s)))
    }

    fn css_size(&self) -> (u32, u32) {
        ((self.view.w as f32 / self.cfg.zoom) as u32, (self.view.h as f32 / self.cfg.zoom) as u32)
    }

    fn to_css(&self, x: i32, y: i32) -> (f32, f32) {
        ((x - self.view.x) as f32 / self.cfg.zoom, (y - self.view.y) as f32 / self.cfg.zoom)
    }

    /// The user did something: paint what follows without the idle delay.
    fn wake(&mut self) {
        self.active_until = self.active_until.max(Instant::now() + ACTIVE_WINDOW);
        self.capture_due = Some(Instant::now() + CAPTURE_FALLBACK);
    }

    /// When the pending frame should go on screen.
    fn paint_due(&self) -> Option<Instant> {
        self.frame.as_ref()?;
        let settled = (self.frame_last + QUIET).min(self.frame_first + MAX_WAIT);
        if self.frame_first <= self.active_until {
            Some(settled)
        } else {
            Some(settled.max(self.last_paint + IDLE_INTERVAL))
        }
    }

    /// Size the page to the viewport and start streaming it.
    fn start_streaming(&mut self) {
        let (w, h) = self.css_size();
        self.page_cmd(
            "Emulation.setDeviceMetricsOverride",
            json!({ "width": w, "height": h, "deviceScaleFactor": self.cfg.zoom, "mobile": false }),
        );
        self.page_cmd(
            "Page.startScreencast",
            json!({ "format": "jpeg", "quality": 85, "maxWidth": self.view.w, "maxHeight": self.view.h, "everyNthFrame": 1 }),
        );
        self.shown.fill(0x0001);
        self.wake();
    }

    fn setup_page(&mut self, first: bool) {
        self.page_cmd("Page.enable", json!({}));
        self.page_cmd("Emulation.setFocusEmulationEnabled", json!({ "enabled": true }));
        self.page_cmd(
            "Emulation.setEmulatedMedia",
            json!({ "features": [
                { "name": "prefers-reduced-motion", "value": "reduce" },
                { "name": "prefers-color-scheme", "value": "light" },
            ] }),
        );
        self.page_cmd("Page.addScriptToEvaluateOnNewDocument", json!({ "source": EINK_SCRIPT, "runImmediately": true }));
        if let Some(ua) = self.user_agent.clone() {
            self.page_cmd("Emulation.setUserAgentOverride", json!({ "userAgent": ua }));
        }
        self.start_streaming();
        if first {
            let url = std::fs::read_to_string(self.cfg.data.join("last-url"))
                .ok()
                .map(|s| s.trim().to_string())
                .filter(|s| !s.is_empty())
                .unwrap_or_else(|| self.cfg.home.clone());
            self.navigate(&url);
        }
    }

    fn navigate(&mut self, url: &str) {
        self.page_cmd("Page.navigate", json!({ "url": url }));
        self.wake();
        if let Some(p) = self.pages.last_mut() {
            p.url = url.to_string();
            p.loading = true;
        }
        self.draw_toolbar(false);
    }

    // ---- DevTools messages -------------------------------------------------

    fn on_message(&mut self, msg: Value) {
        if let Some(id) = msg.get("id").and_then(Value::as_u64) {
            if let Some(p) = self.pending.remove(&id) {
                self.on_reply(p, msg.get("result").cloned().unwrap_or(Value::Null));
            }
            return;
        }
        let method = msg["method"].as_str().unwrap_or("");
        let params = &msg["params"];
        let from_screen = msg["sessionId"].as_str().is_some_and(|s| self.page().is_some_and(|p| p.session == s));
        match method {
            "Target.targetCreated" => {
                let info = &params["targetInfo"];
                let popup = info.get("openerId").is_some();
                if info["type"] == "page" && !self.attaching && (self.pages.is_empty() || popup) {
                    let target = info["targetId"].as_str().unwrap_or("").to_string();
                    self.attaching = true;
                    let id = self.chrome.send("Target.attachToTarget", json!({ "targetId": target, "flatten": true }), None);
                    self.pending.insert(id, Pending::Attach(target));
                }
            }
            "Target.targetDestroyed" | "Target.detachedFromTarget" => {
                let target = params["targetId"].as_str().unwrap_or("");
                let session = params["sessionId"].as_str().unwrap_or("");
                let was_top = self.page().is_some_and(|p| p.target == target || p.session == session);
                self.pages.retain(|p| p.target != target && p.session != session);
                if self.pages.is_empty() {
                    let home = self.cfg.home.clone();
                    self.chrome.send("Target.createTarget", json!({ "url": home }), None);
                } else if was_top {
                    self.start_streaming();
                    self.draw_toolbar(false);
                }
            }
            "Page.screencastFrame" => {
                if let Some(s) = msg["sessionId"].as_str() {
                    self.chrome.send("Page.screencastFrameAck", json!({ "sessionId": params["sessionId"] }), Some(s));
                }
                if from_screen {
                    self.take_frame(params["data"].as_str());
                }
            }
            "Page.frameNavigated" if from_screen => {
                let frame = &params["frame"];
                if frame.get("parentId").is_none() {
                    self.set_url(frame["url"].as_str().unwrap_or(""));
                }
            }
            "Page.navigatedWithinDocument" if from_screen => {
                if self.page().is_some_and(|p| params["frameId"] == p.target.as_str()) {
                    self.set_url(params["url"].as_str().unwrap_or(""));
                }
            }
            "Page.frameStartedLoading" | "Page.frameStoppedLoading" if from_screen => {
                let loading = method == "Page.frameStartedLoading";
                if let Some(p) = self.pages.last_mut().filter(|p| params["frameId"] == p.target.as_str()) {
                    p.loading = loading;
                    if !loading {
                        // One flash once the page is done clears what loading left behind.
                        self.refresh_after_paint = true;
                    }
                    self.wake();
                    self.draw_toolbar(false);
                }
            }
            "Page.javascriptDialogOpening" => {
                if let Some(s) = msg["sessionId"].as_str() {
                    self.chrome.send("Page.handleJavaScriptDialog", json!({ "accept": true }), Some(s));
                }
            }
            "Inspector.targetCrashed" if from_screen => {
                self.page_cmd("Page.reload", json!({}));
            }
            _ => {}
        }
    }

    fn on_reply(&mut self, pending: Pending, result: Value) {
        match pending {
            Pending::Version => {
                self.user_agent = result["userAgent"].as_str().map(|ua| ua.replace("HeadlessChrome", "Chrome"));
                self.chrome.send("Target.setDiscoverTargets", json!({ "discover": true }), None);
            }
            Pending::Attach(target) => {
                self.attaching = false;
                let Some(session) = result["sessionId"].as_str() else { return };
                if let Some(old) = self.session() {
                    self.chrome.send("Page.stopScreencast", json!({}), Some(&old));
                }
                let first = self.pages.is_empty();
                self.pages.push(Page { target, session: session.to_string(), url: String::new(), loading: false });
                self.setup_page(first);
            }
            Pending::History(step) => {
                let current = result["currentIndex"].as_i64().unwrap_or(0);
                let entries = result["entries"].as_array().cloned().unwrap_or_default();
                let wanted = current + step;
                if let Some(entry) = usize::try_from(wanted).ok().and_then(|i| entries.get(i)) {
                    self.page_cmd("Page.navigateToHistoryEntry", json!({ "entryId": entry["id"] }));
                } else if step < 0 && self.pages.len() > 1 {
                    // Back from the first page of a popup returns to its opener.
                    let target = self.page().map(|p| p.target.clone()).unwrap_or_default();
                    self.chrome.send("Target.closeTarget", json!({ "targetId": target }), None);
                }
            }
            Pending::FocusCheck => {
                if result["result"]["value"] == true && !self.keyboard_visible {
                    self.set_keyboard(true);
                }
            }
            Pending::Screenshot => {
                if self.frame.is_none() {
                    self.take_frame(result["data"].as_str());
                }
            }
        }
    }

    fn set_url(&mut self, url: &str) {
        if let Some(p) = self.pages.last_mut() {
            p.url = url.to_string();
        }
        if url.starts_with("http") {
            let _ = std::fs::create_dir_all(&self.cfg.data);
            let _ = std::fs::write(self.cfg.data.join("last-url"), url);
        }
        self.draw_toolbar(false);
    }

    // ---- Drawing ----------------------------------------------------------

    fn draw_toolbar(&mut self, full: bool) {
        let w = self.display.width;
        let page = self.pages.last();
        let loading = page.is_some_and(|p| p.loading);
        let url = page.map(|p| p.url.clone()).unwrap_or_default();
        let mut cv = self.display.canvas();
        cv.fill(Rect::new(0, 0, w, TOOLBAR_H), WHITE);
        cv.fill(Rect::new(0, TOOLBAR_H - 3, w, 3), BLACK);
        for b in BUTTONS {
            let r = button_rect(b, w);
            let label = match b {
                Button::Back => "◀",
                Button::Forward => "▶",
                Button::Reload if loading => "Stop",
                Button::Reload => "Reload",
                Button::Keyboard => "⌨",
                Button::Quit => "×",
                Button::Address => {
                    draw_address(&mut cv, &mut self.font, r, &url, self.address.as_ref(), loading);
                    continue;
                }
            };
            let px = if label.chars().count() > 1 { 30.0 } else { 52.0 };
            let (asc, desc) = self.font.vertical(px);
            let tw = self.font.text_width(label, px);
            let baseline = r.y + ((r.h - 3) as f32 / 2.0 + (asc + desc) / 2.0).round() as i32;
            let inked = b == Button::Keyboard && self.keyboard_visible;
            if inked {
                cv.fill(r.inset(14), BLACK);
            }
            self.font.draw_text(&mut cv, r.x + (r.w - tw) / 2, baseline, label, false, px, if inked { WHITE } else { BLACK });
        }
        if full {
            self.display.update_all();
        } else {
            self.display.update(Rect::new(0, 0, w, TOOLBAR_H));
        }
    }

    fn take_frame(&mut self, data: Option<&str>) {
        let Some(data) = data else { return };
        let now = Instant::now();
        if self.frame.is_none() {
            self.frame_first = now;
        }
        self.frame_last = now;
        self.frame = Some(data.to_string());
    }

    /// Decode the newest frame and repaint the parts of the page that changed.
    fn paint_frame(&mut self) {
        let Some(data) = self.frame.take() else { return };
        self.last_paint = Instant::now();
        self.capture_due = None;
        let Ok(jpeg) = base64::engine::general_purpose::STANDARD.decode(data) else { return };
        let opts = DecoderOptions::default().jpeg_set_out_colorspace(ColorSpace::RGB);
        let mut dec = JpegDecoder::new_with_options(ZCursor::new(&jpeg), opts);
        let Ok(pixels) = dec.decode() else { return };
        let Some(info) = dec.info() else { return };
        let (iw, ih) = (info.width as usize, info.height as usize);
        if iw == 0 || ih == 0 || pixels.len() < iw * ih * 3 {
            return;
        }

        let (vw, vh) = (self.view.w as usize, self.view.h as usize);
        let mut next = vec![WHITE; vw * vh];
        // Frames normally match the viewport exactly; stretch the odd one
        // that was captured mid-resize.
        let xs: Vec<usize> = (0..vw).map(|x| x * iw / vw).collect();
        for y in 0..vh {
            let sy = y * ih / vh;
            let src = &pixels[sy * iw * 3..(sy + 1) * iw * 3];
            let dst = &mut next[y * vw..(y + 1) * vw];
            // Dither in screen coordinates, so unchanged content stays identical.
            let cells = &self.palette[((self.view.y as usize + y) & 3) * 4 * 256..][..4 * 256];
            for (x, (d, &sx)) in dst.iter_mut().zip(&xs).enumerate() {
                let (r, g, b) = (src[sx * 3] as u32, src[sx * 3 + 1] as u32, src[sx * 3 + 2] as u32);
                let lum = (r * 77 + g * 150 + b * 29) >> 8;
                *d = cells[(x & 3) * 256 + lum as usize];
            }
        }

        let (mut top, mut bottom, mut left, mut right) = (usize::MAX, 0, usize::MAX, 0);
        for y in 0..vh {
            let (a, b) = (&next[y * vw..(y + 1) * vw], &self.shown[y * vw..(y + 1) * vw]);
            if a == b {
                continue;
            }
            let first = a.iter().zip(b).position(|(p, q)| p != q).unwrap_or(0);
            let last = vw - 1 - a.iter().rev().zip(b.iter().rev()).position(|(p, q)| p != q).unwrap_or(0);
            top = top.min(y);
            bottom = y;
            left = left.min(first);
            right = right.max(last);
        }
        if top == usize::MAX {
            return;
        }
        let dirty = Rect::new(left as i32, top as i32, (right - left + 1) as i32, (bottom - top + 1) as i32);
        let screen = Rect::new(self.view.x + dirty.x, self.view.y + dirty.y, dirty.w, dirty.h);
        let mut cv = self.display.canvas();
        for y in 0..dirty.h {
            let row = (dirty.y + y) as usize * vw;
            let src = &next[row + left..row + right + 1];
            cv.blit(Rect::new(screen.x, screen.y + y, dirty.w, 1), src, dirty.w);
        }
        self.shown = next;
        if self.refresh_after_paint {
            self.refresh_after_paint = false;
            self.display.update(screen);
            self.display.full_refresh();
        } else {
            self.display.update(screen);
        }
    }

    fn set_keyboard(&mut self, visible: bool) {
        if self.keyboard_visible == visible {
            return;
        }
        self.keyboard_visible = visible;
        let kb_h = if visible { self.keyboard.rect.h } else { 0 };
        self.view = Rect::new(0, TOOLBAR_H, self.display.width, self.display.height - TOOLBAR_H - kb_h);
        self.shown = vec![0x0001; (self.view.w * self.view.h) as usize];
        if visible {
            let mut cv = self.display.canvas();
            self.keyboard.draw(&mut cv, &mut self.font);
        }
        // The keyboard leaves ghosts where it was; flash once the page is back.
        self.refresh_after_paint |= !visible;
        self.start_streaming();
        self.draw_toolbar(true);
    }

    // ---- Input ------------------------------------------------------------

    fn on_key(&mut self, kp: KeyPress) {
        match kp.key {
            Key::Action(ACTION_REFRESH) => return self.display.full_refresh(),
            Key::Action(ACTION_HIDE) => return self.set_keyboard(false),
            Key::Action(_) => return,
            _ => {}
        }
        if let Some(edit) = self.address.as_mut() {
            match kp.key {
                Key::Char(c) if !kp.mods.ctrl => {
                    if edit.fresh {
                        edit.text.clear();
                    }
                    edit.text.push(c);
                }
                Key::Backspace if edit.fresh => edit.text.clear(),
                Key::Backspace => {
                    edit.text.pop();
                }
                Key::Enter => {
                    let input = std::mem::take(&mut edit.text);
                    self.address = None;
                    if input.trim().is_empty() {
                        self.page_cmd("Page.reload", json!({}));
                    } else {
                        let url = keys::to_url(&input, &self.cfg.search);
                        self.navigate(&url);
                    }
                    self.set_keyboard(false);
                    return;
                }
                Key::Escape => self.address = None,
                _ => {}
            }
            if let Some(e) = self.address.as_mut() {
                e.fresh = false;
            }
            self.draw_toolbar(false);
            return;
        }
        for event in keys::key_events(kp) {
            self.page_cmd("Input.dispatchKeyEvent", event);
        }
        self.wake();
    }

    fn on_button(&mut self, b: Button) {
        match b {
            Button::Back | Button::Forward => {
                if let Some(id) = self.page_cmd("Page.getNavigationHistory", json!({})) {
                    self.pending.insert(id, Pending::History(if b == Button::Back { -1 } else { 1 }));
                }
            }
            Button::Reload => {
                let loading = self.page().is_some_and(|p| p.loading);
                self.page_cmd(if loading { "Page.stopLoading" } else { "Page.reload" }, json!({}));
            }
            Button::Address => {
                let url = self.page().map(|p| p.url.clone()).unwrap_or_default();
                self.address = Some(AddressEdit { text: url, fresh: true });
                self.draw_toolbar(false);
                self.set_keyboard(true);
            }
            Button::Keyboard => self.set_keyboard(!self.keyboard_visible),
            Button::Quit => self.quit = true,
        }
    }

    fn mouse(&mut self, kind: &str, x: i32, y: i32, extra: Value) {
        let (cx, cy) = self.to_css(x, y);
        let mut params = json!({ "type": kind, "x": cx, "y": cy });
        if let (Some(p), Some(e)) = (params.as_object_mut(), extra.as_object()) {
            p.extend(e.clone());
        }
        self.page_cmd("Input.dispatchMouseEvent", params);
    }

    /// Turn a swipe into one page-sized jump in the direction of the swipe,
    /// so the screen redraws once instead of following the finger.
    fn flip(&mut self, from: (i32, i32), to: (i32, i32)) {
        let (dx, dy) = (to.0 - from.0, to.1 - from.1);
        let (w, h) = self.css_size();
        let (wheel_x, wheel_y) = if dy.abs() >= dx.abs() {
            (0.0, -dy.signum() as f32 * h as f32 * FLIP_STEP)
        } else {
            (-dx.signum() as f32 * w as f32 * FLIP_STEP, 0.0)
        };
        self.mouse("mouseWheel", from.0, from.1, json!({ "deltaX": wheel_x, "deltaY": wheel_y }));
        self.flips += 1;
        if self.cfg.flash_every > 0 && self.flips.is_multiple_of(self.cfg.flash_every) {
            self.refresh_after_paint = true;
        }
        self.wake();
    }

    fn touch_down(&mut self, id: i32, x: i32, y: i32) {
        if self.touch.is_some() {
            return;
        }
        let gesture = if y < TOOLBAR_H {
            Gesture::Toolbar
        } else if self.keyboard_visible && self.keyboard.rect.contains(x, y) {
            Gesture::Keyboard
        } else {
            Gesture::Tap
        };
        self.touch = Some(Touch { id, gesture, start: (x, y), last: (x, y) });
        match self.touch.as_ref().map(|t| &t.gesture) {
            Some(Gesture::Toolbar) => {
                let w = self.display.width;
                if let Some(b) = BUTTONS.into_iter().find(|b| button_rect(*b, w).contains(x, y)) {
                    self.on_button(b);
                }
            }
            Some(Gesture::Keyboard) => {
                let mut cv = self.display.canvas();
                let (key, dirty) = self.keyboard.press(&mut cv, &mut self.font, x, y);
                self.display.update(dirty);
                if let Some(kp) = key {
                    self.on_key(kp);
                }
            }
            _ => {}
        }
    }

    fn touch_move(&mut self, id: i32, x: i32, y: i32) {
        let Some(t) = self.touch.as_mut().filter(|t| t.id == id) else { return };
        if !matches!(t.gesture, Gesture::Tap | Gesture::Scroll) {
            return;
        }
        if (x - t.start.0).abs().max((y - t.start.1).abs()) >= DRAG_THRESHOLD {
            t.gesture = Gesture::Scroll;
        }
        t.last = (x, y);
    }

    fn touch_up(&mut self, id: i32) {
        if self.touch.as_ref().is_none_or(|t| t.id != id) {
            return;
        }
        let Some(t) = self.touch.take() else { return };
        match t.gesture {
            Gesture::Scroll => self.flip(t.start, t.last),
            Gesture::Keyboard if self.keyboard_visible => {
                let mut cv = self.display.canvas();
                let dirty = self.keyboard.release(&mut cv, &mut self.font);
                self.display.update(dirty);
            }
            Gesture::Keyboard => self.keyboard.cancel(),
            Gesture::Tap => {
                if self.address.take().is_some() {
                    self.draw_toolbar(false);
                }
                let (x, y) = t.start;
                self.mouse("mouseMoved", x, y, json!({}));
                self.mouse("mousePressed", x, y, json!({ "button": "left", "buttons": 1, "clickCount": 1 }));
                self.mouse("mouseReleased", x, y, json!({ "button": "left", "buttons": 0, "clickCount": 1 }));
                if let Some(id) = self.page_cmd("Runtime.evaluate", json!({ "expression": FOCUS_CHECK, "returnByValue": true })) {
                    self.pending.insert(id, Pending::FocusCheck);
                }
                self.wake();
            }
            _ => {}
        }
    }

    fn on_input(&mut self, input: Input) {
        match input {
            Input::KeyDown(code) => {
                if let Some(kp) = self.qt.key_down(code) {
                    self.on_key(kp);
                }
            }
            Input::KeyUp(code) => self.qt.key_up(code),
            Input::TouchDown { id, x, y } => self.touch_down(id, x, y),
            Input::PenDown { x, y } => self.touch_down(-1, x, y),
            Input::TouchMove { id, x, y } => self.touch_move(id, x, y),
            Input::PenMove { x, y } => self.touch_move(-1, x, y),
            Input::TouchUp { id, .. } => self.touch_up(id),
            Input::PenUp { .. } => self.touch_up(-1),
        }
    }

    fn run(&mut self) {
        while !self.quit {
            let now = Instant::now();
            let deadlines: Vec<Instant> = [self.paint_due(), self.capture_due].into_iter().flatten().collect();
            let timeout = deadlines.iter().min().map_or(-1, |d| d.saturating_duration_since(now).as_millis() as i32);

            let ready = poll_readable(&[self.display.fd(), self.chrome.fd()], timeout);
            if ready[1] {
                for msg in self.chrome.receive() {
                    if msg.is_null() {
                        let log = self.cfg.data.join("chrome.log");
                        fatal(&mut self.display, &mut self.font, &["Chromium stopped.", &format!("See {}", log.display()), "", "Tap to close."]);
                    }
                    self.on_message(msg);
                }
            }
            if ready[0] {
                match self.display.read_input() {
                    Ok(inputs) => inputs.into_iter().for_each(|i| self.on_input(i)),
                    Err(_) => return,
                }
            }

            let now = Instant::now();
            // Hold the page still while a finger is down: it's about to change again.
            if self.touch.is_none() && self.paint_due().is_some_and(|d| now >= d) {
                self.paint_frame();
            }
            if self.capture_due.is_some_and(|c| now >= c) {
                self.capture_due = None;
                if let Some(id) = self.page_cmd("Page.captureScreenshot", json!({ "format": "jpeg", "quality": 90 })) {
                    self.pending.insert(id, Pending::Screenshot);
                }
            }
        }
    }
}

fn draw_address(cv: &mut Canvas, font: &mut Font, r: Rect, url: &str, edit: Option<&AddressEdit>, loading: bool) {
    let field = Rect::new(r.x, r.y + 18, r.w, r.h - 36 - 3);
    let px = 32.0;
    let (asc, desc) = font.vertical(px);
    let baseline = field.y + (field.h as f32 / 2.0 + (asc + desc) / 2.0).round() as i32;
    let room = ((field.w - 40) as f32 / font.advance(px)) as usize;

    let (text, border, bg): (String, i32, Color) = match edit {
        Some(e) => {
            let chars: Vec<char> = e.text.chars().collect();
            // Keep the end of long input visible, it's where typing happens.
            let tail: String = chars[chars.len().saturating_sub(room.saturating_sub(1))..].iter().collect();
            (tail, 4, WHITE)
        }
        None => {
            let shown = url.strip_prefix("https://").unwrap_or(url);
            let shown = shown.strip_prefix("www.").unwrap_or(shown);
            let text = if shown.chars().count() > room {
                shown.chars().take(room.saturating_sub(1)).chain(['…']).collect()
            } else {
                shown.to_string()
            };
            (text, 2, WHITE)
        }
    };
    // A pre-filled URL is shown inverted, like selected text, since typing replaces it.
    let selected = edit.is_some_and(|e| e.fresh);
    cv.fill(field, if selected { BLACK } else { bg });
    cv.stroke(field, border, BLACK);
    let w = font.draw_text(cv, field.x + 20, baseline, &text, false, px, if selected { WHITE } else { BLACK });
    if edit.is_some() {
        cv.fill(Rect::new(field.x + 22 + w, field.y + 14, 3, field.h - 28), BLACK);
    }
    if loading {
        cv.fill(Rect::new(field.x, field.bottom() + 6, field.w, 6), BLACK);
    }
}

/// Lookup table from luminance to screen color: 4 rows x 4 columns of the
/// dither cell, 256 levels each. Near-white and near-black snap to pure
/// white and black, which also hides JPEG noise from the change detection.
fn palette(colors: Colors) -> Vec<Color> {
    const BAYER: [[u32; 4]; 4] = [[0, 8, 2, 10], [12, 4, 14, 6], [3, 11, 1, 9], [15, 7, 13, 5]];
    let (lo, hi) = (56, 208);
    let mut out = vec![WHITE; 16 * 256];
    for (cy, row) in BAYER.iter().enumerate() {
        for (cx, &t) in row.iter().enumerate() {
            for lum in 0..256u32 {
                out[(cy * 4 + cx) * 256 + lum as usize] = if lum >= hi {
                    WHITE
                } else if lum < lo {
                    BLACK
                } else {
                    match colors {
                        Colors::Mono => {
                            let level = (lum - lo) * 256 / (hi - lo);
                            if level > t * 16 + 8 { WHITE } else { BLACK }
                        }
                        Colors::Gray => gray(((lum >> 4) * 17) as u8),
                    }
                };
            }
        }
    }
    out
}

fn main() {
    // Chromium can take the whole process group down with SIGPIPE otherwise.
    unsafe { libc::signal(libc::SIGPIPE, libc::SIG_IGN) };
    // Dropping the app stops Chromium and closes the AppLoad window.
    App::new().run();
}
