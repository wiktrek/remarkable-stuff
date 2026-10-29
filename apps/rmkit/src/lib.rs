//! Shared plumbing for reMarkable Paper Pro apps running under AppLoad's qtfb.

pub mod canvas;
pub mod font;
pub mod keys;
pub mod osk;
pub mod qtfb;

pub use canvas::{Canvas, Color, Rect};
pub use font::Font;
pub use keys::{Key, KeyPress, Mods, QtKeys};
pub use osk::Keyboard;
pub use qtfb::{Display, Input, RefreshMode};

/// Wait until one of `fds` is readable or `timeout_ms` passes (-1 = forever).
/// Returns which fds are readable.
pub fn poll_readable(fds: &[std::os::fd::RawFd], timeout_ms: i32) -> Vec<bool> {
    let mut pfds: Vec<libc::pollfd> = fds.iter().map(|&fd| libc::pollfd { fd, events: libc::POLLIN, revents: 0 }).collect();
    let n = unsafe { libc::poll(pfds.as_mut_ptr(), pfds.len() as libc::nfds_t, timeout_ms) };
    if n <= 0 {
        return vec![false; fds.len()];
    }
    pfds.iter().map(|p| p.revents & (libc::POLLIN | libc::POLLHUP | libc::POLLERR) != 0).collect()
}
