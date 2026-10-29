#!/usr/bin/env bash
# Build the apps and copy them to the tablet's AppLoad folder.
#
#   scripts/install.sh                     # all apps, tablet on USB
#   scripts/install.sh terminal            # just the terminal
#   TABLET=root@192.168.1.50 scripts/install.sh browser
#
# Needs: SSH access to the tablet (developer mode). The Rust apps (terminal,
# browser) also need cargo, rustup target aarch64-unknown-linux-musl and
# cargo-zigbuild + zig; the QML apps (tictactoe) need Qt 6's rcc.
set -euo pipefail
cd "$(dirname "$0")/.."

TABLET="${TABLET:-root@10.11.99.1}"
APPLOAD=/home/root/xovi/exthome/appload
TARGET=aarch64-unknown-linux-musl
RUST_APPS=(terminal browser)
QML_APPS=(tictactoe)
[ $# -gt 0 ] || set -- "${RUST_APPS[@]}" "${QML_APPS[@]}"
APPS=("$@")

is_rust() { [[ " ${RUST_APPS[*]} " == *" $1 "* ]]; }

rust=()
for app in "${APPS[@]}"; do
  if is_rust "$app"; then
    rust+=("$app")
  elif [[ " ${QML_APPS[*]} " != *" $app "* ]]; then
    echo "unknown app: $app (${RUST_APPS[*]} ${QML_APPS[*]})" >&2; exit 1
  fi
done

if [ ${#rust[@]} -gt 0 ]; then
  echo "==> Building ${rust[*]}"
  cargo zigbuild --release --target "$TARGET" $(printf -- '-p rm-%s ' "${rust[@]}")
fi

ssh "$TABLET" "test -d $APPLOAD" || {
  echo "$APPLOAD not found on the tablet; install xovi + AppLoad first (see README)." >&2
  exit 1
}

for app in "${APPS[@]}"; do
  stage="target/dist/$app"
  rm -rf "$stage" && mkdir -p "$stage"
  install -m 644 "$app"/app/* "$stage/"
  if is_rust "$app"; then
    install -m 755 "target/$TARGET/release/rm-$app" "$stage/"
    install -m 644 rmkit/fonts/OFL.txt "$stage/LICENSE.font"
  else
    # QML app: AppLoad loads the UI from a compiled Qt resource file.
    rcc="${RCC:-$([ -x /usr/lib/qt6/rcc ] && echo /usr/lib/qt6/rcc || command -v rcc)}"
    (cd "$app" && "$rcc" --binary -o "../$stage/resources.rcc" application.qrc)
  fi

  echo "==> Installing $app to $TABLET:$APPLOAD/$app"
  ssh "$TABLET" "mkdir -p $APPLOAD/$app"
  tar -C "$stage" -czf - . | ssh "$TABLET" "tar -xzf - -C $APPLOAD/$app"

  if [ "$app" = browser ]; then
    engine=browser/build/engine
    [ -f "$engine/VERSION" ] || python3 -I browser/bundle-engine.py
    local_version=$(cat "$engine/VERSION")
    remote_version=$(ssh "$TABLET" "cat $APPLOAD/browser/engine/VERSION 2>/dev/null || true")
    if [ "$local_version" != "$remote_version" ]; then
      echo "==> Uploading Chromium $local_version engine ($(du -sh "$engine" | cut -f1), takes a few minutes)"
      ssh "$TABLET" "rm -rf $APPLOAD/browser/engine && mkdir -p $APPLOAD/browser/engine"
      tar -C "$engine" -czf - . | ssh "$TABLET" "tar -xzf - -C $APPLOAD/browser/engine"
    else
      echo "==> Chromium engine $local_version already on the tablet"
    fi
  fi
done

echo "==> Done. Open AppLoad and press its reload button to see the new apps."
