//! Client for AppLoad's qtfb: a SOCK_SEQPACKET control socket plus a shared
//! memory framebuffer that xochitl paints for us.
//!
//! Wire format (from rm-appload `src/qtfb/common.h`, x86/aarch64 C layout):
//!   ClientMessage = 24 bytes: type u8 @0, payload @4
//!   ServerMessage = 32 bytes: type u8 @0, payload @8

use std::io;
use std::os::fd::RawFd;

use crate::canvas::{Canvas, Rect};

const SOCKET_PATH: &[u8] = b"/tmp/qtfb.sock";
const DEFAULT_KEY: i32 = 245209899;

const MESSAGE_INITIALIZE: u8 = 0;
const MESSAGE_UPDATE: u8 = 1;
const MESSAGE_TERMINATE: u8 = 3;
const MESSAGE_USERINPUT: u8 = 4;
const MESSAGE_SET_REFRESH_MODE: u8 = 5;
const MESSAGE_REQUEST_FULL_REFRESH: u8 = 6;

const UPDATE_ALL: i32 = 0;
const UPDATE_PARTIAL: i32 = 1;

const FBFMT_RMPP_RGB565: u8 = 3;
const FBFMT_RMPPM_RGB565: u8 = 6;

const INPUT_TOUCH_PRESS: i32 = 0x10;
const INPUT_TOUCH_RELEASE: i32 = 0x11;
const INPUT_TOUCH_UPDATE: i32 = 0x12;
const INPUT_PEN_PRESS: i32 = 0x20;
const INPUT_PEN_RELEASE: i32 = 0x21;
const INPUT_PEN_UPDATE: i32 = 0x22;
const INPUT_BTN_PRESS: i32 = 0x30;
const INPUT_BTN_RELEASE: i32 = 0x31;
const INPUT_VKB_PRESS: i32 = 0x40;
const INPUT_VKB_RELEASE: i32 = 0x41;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RefreshMode {
    /// Fastest, most ghosting. Good for typing.
    UltraFast = 0,
    Fast = 1,
    Animate = 2,
    /// Best quality for images.
    Content = 3,
    /// AppLoad's default.
    Ui = 4,
}

impl RefreshMode {
    pub fn from_env(var: &str, default: RefreshMode) -> RefreshMode {
        match std::env::var(var).unwrap_or_default().to_ascii_lowercase().as_str() {
            "ufast" | "ultrafast" => RefreshMode::UltraFast,
            "fast" => RefreshMode::Fast,
            "animate" => RefreshMode::Animate,
            "content" => RefreshMode::Content,
            "ui" => RefreshMode::Ui,
            _ => default,
        }
    }
}

/// Touch, pen and key input, in framebuffer pixels.
#[derive(Clone, Copy, Debug)]
pub enum Input {
    TouchDown { id: i32, x: i32, y: i32 },
    TouchMove { id: i32, x: i32, y: i32 },
    TouchUp { id: i32, x: i32, y: i32 },
    PenDown { x: i32, y: i32 },
    PenMove { x: i32, y: i32 },
    PenUp { x: i32, y: i32 },
    /// A Qt key code, from the Type Folio or AppLoad's virtual keyboard.
    KeyDown(i32),
    KeyUp(i32),
}

pub struct Display {
    fd: RawFd,
    shm: *mut u16,
    shm_len: usize,
    pub width: i32,
    pub height: i32,
}

impl Display {
    /// Connect to AppLoad. The framebuffer id comes from `QTFB_KEY`; the
    /// device from `RM_DEVICE` (`pro`, the default, or `move`).
    pub fn open() -> io::Result<Display> {
        let key = std::env::var("QTFB_KEY").ok().and_then(|k| k.parse().ok()).unwrap_or(DEFAULT_KEY);
        let (format, width, height) = match std::env::var("RM_DEVICE").as_deref() {
            Ok("move") => (FBFMT_RMPPM_RGB565, 954, 1696),
            _ => (FBFMT_RMPP_RGB565, 1620, 2160),
        };

        let fd = unsafe { libc::socket(libc::AF_UNIX, libc::SOCK_SEQPACKET | libc::SOCK_CLOEXEC, 0) };
        if fd < 0 {
            return Err(io::Error::last_os_error());
        }
        let fail = |e: io::Error| {
            unsafe { libc::close(fd) };
            Err(e)
        };

        let mut addr: libc::sockaddr_un = unsafe { std::mem::zeroed() };
        addr.sun_family = libc::AF_UNIX as libc::sa_family_t;
        for (dst, src) in addr.sun_path.iter_mut().zip(SOCKET_PATH) {
            *dst = *src as libc::c_char;
        }
        let rc = unsafe {
            libc::connect(
                fd,
                &addr as *const _ as *const libc::sockaddr,
                std::mem::size_of::<libc::sockaddr_un>() as libc::socklen_t,
            )
        };
        if rc != 0 {
            return fail(io::Error::last_os_error());
        }

        let mut msg = [0u8; 24];
        msg[0] = MESSAGE_INITIALIZE;
        msg[4..8].copy_from_slice(&key.to_ne_bytes());
        msg[8] = format;
        if let Err(e) = send_all(fd, &msg) {
            return fail(e);
        }

        let mut reply = [0u8; 32];
        let n = unsafe { libc::recv(fd, reply.as_mut_ptr().cast(), reply.len(), 0) };
        if n <= 0 {
            return fail(io::Error::new(io::ErrorKind::ConnectionRefused, "qtfb rejected the framebuffer"));
        }
        let shm_key = i32::from_ne_bytes(reply[8..12].try_into().unwrap());
        let shm_len = u64::from_ne_bytes(reply[16..24].try_into().unwrap()) as usize;
        let needed = (width * height * 2) as usize;
        if shm_len < needed {
            return fail(io::Error::new(
                io::ErrorKind::InvalidData,
                format!("qtfb framebuffer is {shm_len} bytes, need {needed}"),
            ));
        }

        let path = format!("/dev/shm/qtfb_{shm_key}\0");
        let shm_fd = unsafe { libc::open(path.as_ptr().cast(), libc::O_RDWR | libc::O_CLOEXEC) };
        if shm_fd < 0 {
            return fail(io::Error::last_os_error());
        }
        let ptr = unsafe {
            libc::mmap(std::ptr::null_mut(), shm_len, libc::PROT_READ | libc::PROT_WRITE, libc::MAP_SHARED, shm_fd, 0)
        };
        unsafe { libc::close(shm_fd) };
        if ptr == libc::MAP_FAILED {
            return fail(io::Error::last_os_error());
        }

        unsafe {
            let flags = libc::fcntl(fd, libc::F_GETFL);
            libc::fcntl(fd, libc::F_SETFL, flags | libc::O_NONBLOCK);
        }

        Ok(Display { fd, shm: ptr.cast(), shm_len, width, height })
    }

    pub fn fd(&self) -> RawFd {
        self.fd
    }

    pub fn canvas(&mut self) -> Canvas<'_> {
        let len = (self.width * self.height) as usize;
        let buf = unsafe { std::slice::from_raw_parts_mut(self.shm, len) };
        Canvas::new(buf, self.width, self.height)
    }

    pub fn update(&self, r: Rect) {
        let r = r.intersect(Rect::new(0, 0, self.width, self.height));
        if r.is_empty() {
            return;
        }
        let mut msg = [0u8; 24];
        msg[0] = MESSAGE_UPDATE;
        msg[4..8].copy_from_slice(&UPDATE_PARTIAL.to_ne_bytes());
        msg[8..12].copy_from_slice(&r.x.to_ne_bytes());
        msg[12..16].copy_from_slice(&r.y.to_ne_bytes());
        msg[16..20].copy_from_slice(&r.w.to_ne_bytes());
        msg[20..24].copy_from_slice(&r.h.to_ne_bytes());
        let _ = send_all(self.fd, &msg);
    }

    pub fn update_all(&self) {
        let mut msg = [0u8; 24];
        msg[0] = MESSAGE_UPDATE;
        msg[4..8].copy_from_slice(&UPDATE_ALL.to_ne_bytes());
        let _ = send_all(self.fd, &msg);
    }

    /// Flashing full refresh that clears ghosting. AppLoad stalls this
    /// connection for about a second afterwards, so use it sparingly.
    pub fn full_refresh(&self) {
        let mut msg = [0u8; 24];
        msg[0] = MESSAGE_REQUEST_FULL_REFRESH;
        let _ = send_all(self.fd, &msg);
    }

    /// Also stalls the connection for about a second; call once at startup.
    pub fn set_refresh_mode(&self, mode: RefreshMode) {
        let mut msg = [0u8; 24];
        msg[0] = MESSAGE_SET_REFRESH_MODE;
        msg[4..8].copy_from_slice(&(mode as i32).to_ne_bytes());
        let _ = send_all(self.fd, &msg);
    }

    /// Read every pending message. An error means AppLoad closed the window.
    pub fn read_input(&self) -> io::Result<Vec<Input>> {
        let mut out = Vec::new();
        loop {
            let mut buf = [0u8; 32];
            let n = unsafe { libc::recv(self.fd, buf.as_mut_ptr().cast(), buf.len(), 0) };
            if n == 0 {
                return Err(io::Error::new(io::ErrorKind::ConnectionReset, "qtfb closed"));
            }
            if n < 0 {
                let e = io::Error::last_os_error();
                match e.kind() {
                    io::ErrorKind::WouldBlock => return Ok(out),
                    io::ErrorKind::Interrupted => continue,
                    _ => return Err(e),
                }
            }
            if buf[0] != MESSAGE_USERINPUT || n < 28 {
                continue;
            }
            let int = |o: usize| i32::from_ne_bytes(buf[o..o + 4].try_into().unwrap());
            let (kind, id, x, y) = (int(8), int(12), int(16), int(20));
            out.push(match kind {
                INPUT_TOUCH_PRESS => Input::TouchDown { id, x, y },
                INPUT_TOUCH_UPDATE => Input::TouchMove { id, x, y },
                INPUT_TOUCH_RELEASE => Input::TouchUp { id, x, y },
                INPUT_PEN_PRESS => Input::PenDown { x, y },
                INPUT_PEN_UPDATE => Input::PenMove { x, y },
                INPUT_PEN_RELEASE => Input::PenUp { x, y },
                INPUT_BTN_PRESS | INPUT_VKB_PRESS => Input::KeyDown(x),
                INPUT_BTN_RELEASE | INPUT_VKB_RELEASE => Input::KeyUp(x),
                _ => continue,
            });
        }
    }
}

impl Drop for Display {
    fn drop(&mut self) {
        let mut msg = [0u8; 24];
        msg[0] = MESSAGE_TERMINATE;
        let _ = send_all(self.fd, &msg);
        unsafe {
            libc::munmap(self.shm.cast(), self.shm_len);
            libc::close(self.fd);
        }
    }
}

fn send_all(fd: RawFd, buf: &[u8]) -> io::Result<()> {
    loop {
        let n = unsafe { libc::send(fd, buf.as_ptr().cast(), buf.len(), libc::MSG_NOSIGNAL) };
        if n == buf.len() as isize {
            return Ok(());
        }
        if n >= 0 {
            return Err(io::Error::new(io::ErrorKind::WriteZero, "short qtfb send"));
        }
        let e = io::Error::last_os_error();
        match e.kind() {
            io::ErrorKind::Interrupted => continue,
            // The socket is non-blocking for reads; wait out a full send queue.
            io::ErrorKind::WouldBlock => std::thread::sleep(std::time::Duration::from_millis(2)),
            _ => return Err(e),
        }
    }
}
