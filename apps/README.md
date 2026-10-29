# apps

Apps for the **reMarkable Paper Pro**, launched from
[AppLoad](https://github.com/asivery/rm-appload):

- 🖥️ **Terminal**: a real shell on the tablet, with an on-screen keyboard
- 🌐 **Chromium**: a web browser. Real Chromium runs on the tablet and
  paints the page onto the e-ink screen.
- ❌ **Tic Tac Toe**: against the CPU or a friend

Terminal and Chromium are small Rust programs that draw through AppLoad's
`qtfb` framebuffer. They only repaint the parts of the screen that changed,
which keeps e-ink fast. Tic Tac Toe is a plain QML app that AppLoad loads
itself, so there's nothing to cross-compile.

## What you need

1. **Developer mode** on the tablet, so you can SSH in.
   *Settings → General → Software → Developer mode*.

   > [!WARNING]
   > Turning on developer mode **erases the tablet**. Sync your notebooks
   > to the cloud first.

2. **xovi + AppLoad** on the tablet. The easiest way is
   [remagic](https://github.com/maximerivest/remagic), which installs both.
   [vellum](https://github.com/vellum-dev/vellum) works too.
3. On your PC: Rust, [zig](https://ziglang.org), and the arm64 target:

   ```bash
   rustup target add aarch64-unknown-linux-musl
   cargo install cargo-zigbuild
   ```

   For the browser you also need `python3`, `bsdtar` and `readelf`.
   For Tic Tac Toe you need Qt 6's `rcc` (`qt6-base` on Arch).

## Install

Plug the tablet in over USB, then:

```bash
cd apps
scripts/install.sh             # all apps
scripts/install.sh tictactoe   # or just one
```

Open AppLoad on the tablet and press its reload button. **Terminal**,
**Chromium** and **Tic Tac Toe** show up in the list. 🎉

> [!TIP]
> Over Wi-Fi: `TABLET=root@<tablet-ip> scripts/install.sh`

The first browser install builds the Chromium engine. This downloads about
300 MB and puts ~330 MB on the tablet, so it takes a few minutes. Later
installs skip the engine unless its version changed.

## Terminal

| You do this                    | You get                                      |
| ------------------------------ | -------------------------------------------- |
| Tap the keys                   | Typing. `Ctrl`, `Alt` and `⇧` are sticky: one tap for the next key, two taps to lock |
| Swipe up / down on the text    | Scroll back through the output (arrow keys inside `vim`, `less`, …) |
| `A-` / `A+`                    | Smaller / bigger text                        |
| `Rfsh`                         | Full e-ink refresh to clear ghosting         |
| `Hide`, then tap the text      | Hide / show the keyboard                     |
| Type Folio                     | Works as a normal keyboard                   |
| `◀ Back` or `Quit ×` (top bar), or `exit` | Closes the app and ends the shell |

## Chromium

| You do this                    | You get                                       |
| ------------------------------ | --------------------------------------------- |
| Tap                            | Click                                         |
| Swipe up / down                | Next / previous page (left / right works too) |
| Tap the address bar            | Type a URL or a search                        |
| Tap a text field on a page     | The keyboard comes up by itself               |
| `◀` `▶` `Reload`               | Back, forward, reload (`Stop` while loading)  |
| `⌨`                            | Show / hide the keyboard                      |
| `×`                            | Quit (Chromium stops too, freeing its memory) |

You stay logged in to sites between launches, and the browser reopens the
last page you were on.

The screen only updates when it has to:

- Swiping flips a whole page (85% of the screen) in one jump, instead of
  repainting while your finger moves.
- The page is drawn once it stops changing, not on every frame Chromium
  renders. Changes you didn't cause (GIFs, carousels, ads) are drawn at most
  every 5 seconds.
- CSS animations and transitions are switched off, GIFs stay on their first
  frame, and sites are asked for reduced motion and light mode.
- Pages are drawn in pure black and white (grays are dithered), so the fast
  e-ink mode shows them without smearing. The screen flashes once after a
  page loads and every few flips to clear ghosting.

> [!NOTE]
> Chromium needs ~400–900 MB of the tablet's 2 GB RAM, so expect heavy pages
> to load slowly. Video and audio aren't supported.

## Tic Tac Toe

| You do this                    | You get                                       |
| ------------------------------ | --------------------------------------------- |
| `vs CPU (easy)`                | The CPU makes random mistakes                 |
| `vs CPU (hard)`                | The CPU plays perfectly: the best you can do is draw |
| `2 players`                    | Take turns on one tablet                      |
| Tap a square                   | Your move. Against the CPU, who goes first switches each game |
| `New game`                     | Start over (the score is kept until you switch modes) |
| `Quit ×`                       | Close the app                                 |

## Settings

Edit `external.manifest.json` in the app's folder on the tablet
(`/home/root/xovi/exthome/appload/terminal` or `…/browser`), then reload
AppLoad. Edits there are overwritten by the next install; to keep them,
change `terminal/app/` or `browser/app/` here instead.

| Variable             | Default                          | Meaning                                        |
| -------------------- | -------------------------------- | ---------------------------------------------- |
| `TERMINAL_FONT_SIZE` | `30`                             | Text size in pixels                            |
| `TERMINAL_REFRESH`   | `fast`                           | E-ink mode: `ufast`, `fast`, `ui`, `content`   |
| `TERMINAL_SHELL`     | `bash`, else `sh`                | Shell to run                                   |
| `BROWSER_HOME`       | `https://duckduckgo.com/`        | First page, and the page opened when every tab is closed |
| `BROWSER_SEARCH`     | `https://duckduckgo.com/?q=%s`   | Search URL for address-bar text that isn't a URL |
| `BROWSER_ZOOM`       | `2`                              | Page zoom. `2` makes pages 810 px wide, `1.5` 1080 px |
| `BROWSER_REFRESH`    | `fast`                           | E-ink mode, as above                           |
| `BROWSER_COLORS`     | `mono`                           | `mono`: dithered black and white, crisp with `fast`. `gray`: 16 grays, pair it with `content` |
| `BROWSER_FLASH_EVERY`| `6`                              | Full refresh every N page flips to clear ghosting (`0` = only after a page loads) |
| `RM_DEVICE`          | `pro`                            | Set to `move` on a Paper Pro Move              |

The browser keeps its profile, cache and `chrome.log` in
`/home/root/.local/share/rm-browser`. Delete that folder to log out of
everything.

## Troubleshooting

| Problem                                  | Fix                                                        |
| ---------------------------------------- | ---------------------------------------------------------- |
| Apps don't show up in AppLoad            | Press AppLoad's reload button                              |
| No AppLoad at all, tablet reboots on `xovi/start` (`Couldn't resolve the hashed identifier … AppLoad hooks`) | AppLoad is too old for your firmware. Firmware 3.29 needs AppLoad ≥ v0.6.0 and qt-resource-rebuilder from xovi-extensions ≥ pre-v20. Update them, run `xovi/rebuild_hashtable`, then `xovi/start` |
| AppLoad gone after a reboot              | xovi doesn't survive reboots: triple-press the power button, or run `xovi/start` |
| "Chromium stopped"                       | Read `/home/root/.local/share/rm-browser/chrome.log`       |
| "…chrome-headless-shell is missing"      | Run `scripts/install.sh browser` again                     |
| Grey smears left on the screen           | `Rfsh` on the keyboard, or tap with five fingers           |
| The app hangs                            | Drag down from the top-center of the screen to close it    |

<details>
<summary><b>How it works</b></summary>

- `rmkit/` is shared by both apps: the qtfb client (shared-memory framebuffer
  plus a control socket to AppLoad), drawing, font rendering (JetBrains Mono,
  OFL), the on-screen keyboard and Type Folio key handling.
- `terminal/` runs your shell on a pseudo-terminal and uses
  [alacritty_terminal](https://crates.io/crates/alacritty_terminal) for the
  terminal emulation. Each frame it only redraws the cells that changed.
- `browser/` starts Google's arm64 `chrome-headless-shell` and drives it over
  the DevTools protocol on a pipe. Chromium streams the page as JPEG frames
  (`Page.startScreencast`). The app compares each frame with what's on screen
  and repaints only the changed rectangle. Taps, swipes and keys become
  DevTools input events.
- `tictactoe/` is a QML frontend for AppLoad: `ui/main.qml` draws it and
  `ui/logic.js` has the rules and a minimax CPU player. `install.sh`
  compiles `ui/` into the `resources.rcc` that AppLoad loads.
- `browser/bundle-engine.py` builds the engine on your PC, with no emulator.
  It takes Chromium and adds the shared libraries it's missing from Ubuntu
  24.04 arm64 packages. Ubuntu 24.04 uses the same glibc (2.39) as the
  tablet, so they run there unchanged.

</details>

<details>
<summary><b>Development</b></summary>

Both apps also run on a PC against a fake AppLoad, which saves screenshots:

```bash
cargo build
scripts/fake-qtfb.py target/debug/rm-terminal 'wait 1' 'type ls\n' 'wait 1' 'shot /tmp/term.png'

# the browser needs an x86 engine: unzip chrome-headless-shell-linux64.zip
# from Chrome for Testing to /tmp/engine/chrome
BROWSER_ENGINE=/tmp/engine scripts/fake-qtfb.py target/debug/rm-browser 'wait 8' 'shot /tmp/web.png'

cargo test
```

</details>
