//! Runs headless Chromium and talks the DevTools protocol to it over
//! `--remote-debugging-pipe` (NUL-separated JSON on fds 3 and 4).

use std::fs::File;
use std::io::{self, BufRead, BufReader, Read, Write};
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd, RawFd};
use std::os::unix::process::CommandExt;
use std::path::Path;
use std::process::{Child, Command, Stdio};
use std::sync::mpsc::{self, Receiver, TryRecvError};

use serde_json::{json, Value};

pub struct Chrome {
    child: Child,
    commands: File,
    messages: Receiver<Value>,
    /// Readable whenever `messages` has something; for poll().
    wake: File,
    next_id: u64,
}

fn pipe() -> io::Result<(OwnedFd, OwnedFd)> {
    let mut fds = [0; 2];
    if unsafe { libc::pipe2(fds.as_mut_ptr(), libc::O_CLOEXEC) } != 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(unsafe { (OwnedFd::from_raw_fd(fds[0]), OwnedFd::from_raw_fd(fds[1])) })
}

/// Duplicate above the fds the child needs so dup2 onto 3/4 can't clobber them.
fn high_fd(fd: OwnedFd) -> io::Result<OwnedFd> {
    let n = unsafe { libc::fcntl(fd.as_raw_fd(), libc::F_DUPFD_CLOEXEC, 10) };
    if n < 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(unsafe { OwnedFd::from_raw_fd(n) })
}

impl Chrome {
    pub fn spawn(engine: &Path, data: &Path, width: u32, height: u32) -> io::Result<Chrome> {
        let binary = engine.join("chrome/chrome-headless-shell");
        if !binary.exists() {
            return Err(io::Error::new(
                io::ErrorKind::NotFound,
                format!("{} is missing; install the engine with apps/scripts/install.sh", binary.display()),
            ));
        }
        let (cmd_r, cmd_w) = pipe()?;
        let (msg_r, msg_w) = pipe()?;
        let (cmd_r, msg_w) = (high_fd(cmd_r)?, high_fd(msg_w)?);
        let (child_in, child_out) = (cmd_r.as_raw_fd(), msg_w.as_raw_fd());

        std::fs::create_dir_all(data.join("profile"))?;
        let log = File::create(data.join("chrome.log"))?;
        let lib = engine.join("lib");
        let mut cmd = Command::new(&binary);
        cmd.args([
            "--remote-debugging-pipe",
            "--no-sandbox",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-gpu",
            "--hide-scrollbars",
            // No scroll animations, and GIFs stay on their first frame.
            "--disable-smooth-scrolling",
            "--blink-settings=imageAnimationPolicy=2",
            "--mute-audio",
            "--disable-blink-features=AutomationControlled",
            "--disable-features=Translate,MediaRouter,OptimizationHints,InterestFeedContentSuggestions",
            "--renderer-process-limit=2",
            "--disk-cache-size=104857600",
            "--js-flags=--max-old-space-size=512",
            &format!("--window-size={width},{height}"),
            &format!("--user-data-dir={}", data.join("profile").display()),
            "about:blank",
        ])
        .env("LD_LIBRARY_PATH", format!("{}:{}", lib.display(), engine.join("chrome").display()))
        .env("FONTCONFIG_FILE", engine.join("fonts/fonts.conf"))
        .env("XDG_CACHE_HOME", data.join("cache"))
        .env("HOME", data)
        .env_remove("QTFB_KEY")
        .stdin(Stdio::null())
        .stdout(log.try_clone()?)
        .stderr(log);
        unsafe {
            cmd.pre_exec(move || {
                if libc::dup2(child_in, 3) < 0 || libc::dup2(child_out, 4) < 0 {
                    return Err(io::Error::last_os_error());
                }
                // Own process group, so we can take down every helper process,
                // and die with us if AppLoad kills the app outright.
                libc::setpgid(0, 0);
                libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGKILL);
                Ok(())
            });
        }
        let child = cmd.spawn()?;
        drop((cmd_r, msg_w));

        let (wake_r, wake_w) = pipe()?;
        let (tx, rx) = mpsc::channel();
        let mut wake_w = File::from(wake_w);
        let mut reader = BufReader::new(File::from(msg_r));
        std::thread::spawn(move || {
            let mut buf = Vec::new();
            loop {
                buf.clear();
                let ok = matches!(reader.read_until(0, &mut buf), Ok(n) if n > 0);
                let msg = if ok {
                    buf.pop_if(|b| *b == 0);
                    match serde_json::from_slice(&buf) {
                        Ok(v) => v,
                        Err(_) => continue,
                    }
                } else {
                    Value::Null
                };
                let closed = msg.is_null();
                if tx.send(msg).is_err() || wake_w.write_all(&[1]).is_err() || closed {
                    return;
                }
            }
        });

        let wake = File::from(wake_r);
        unsafe {
            let flags = libc::fcntl(wake.as_raw_fd(), libc::F_GETFL);
            libc::fcntl(wake.as_raw_fd(), libc::F_SETFL, flags | libc::O_NONBLOCK);
        }
        Ok(Chrome { child, commands: File::from(cmd_w), messages: rx, wake, next_id: 1 })
    }

    pub fn fd(&self) -> RawFd {
        self.wake.as_raw_fd()
    }

    /// Send a command; returns its id so the reply can be matched.
    pub fn send(&mut self, method: &str, params: Value, session: Option<&str>) -> u64 {
        let id = self.next_id;
        self.next_id += 1;
        let mut msg = json!({ "id": id, "method": method, "params": params });
        if let Some(s) = session {
            msg["sessionId"] = json!(s);
        }
        let mut bytes = serde_json::to_vec(&msg).expect("json");
        bytes.push(0);
        let _ = self.commands.write_all(&bytes);
        id
    }

    /// Everything Chromium sent since the last call. `Value::Null` in the
    /// list means Chromium is gone.
    pub fn receive(&mut self) -> Vec<Value> {
        let mut drain = [0u8; 256];
        while matches!(self.wake.read(&mut drain), Ok(n) if n > 0) {}
        let mut out = Vec::new();
        loop {
            match self.messages.try_recv() {
                Ok(v) => out.push(v),
                Err(TryRecvError::Empty) => return out,
                Err(TryRecvError::Disconnected) => {
                    out.push(Value::Null);
                    return out;
                }
            }
        }
    }
}

impl Drop for Chrome {
    fn drop(&mut self) {
        let pid = self.child.id() as i32;
        unsafe { libc::kill(-pid, libc::SIGTERM) };
        for _ in 0..20 {
            if matches!(self.child.try_wait(), Ok(Some(_))) {
                return;
            }
            std::thread::sleep(std::time::Duration::from_millis(100));
        }
        unsafe { libc::kill(-pid, libc::SIGKILL) };
        let _ = self.child.wait();
    }
}
