#!/usr/bin/env python3
"""A stand-in for AppLoad's qtfb server, for running the apps on a PC.

Starts the app, then plays a script of input events and saves screenshots:

    cargo build
    scripts/fake-qtfb.py target/debug/rm-terminal \
        'wait 2' 'type echo hello\\n' 'wait 1' 'shot /tmp/term.png'

Script steps:
    wait SECONDS          sleep
    type TEXT             type on the virtual keyboard (\\n = Enter)
    key QTCODE            press and release one Qt key code (hex ok)
    tap X Y               finger tap
    drag X1 Y1 X2 Y2      finger drag in 10 steps
    shot FILE.png         save the framebuffer

Needs Pillow for screenshots.
"""

import mmap
import os
import socket
import struct
import subprocess
import sys
import threading
import time

SOCKET_PATH = "/tmp/qtfb.sock"
WIDTH, HEIGHT = 1620, 2160
KEY = 4242

QT_SHIFT = 0x01000020
QT_RETURN = 0x01000004
QT_TAB = 0x01000001
QT_BACKSPACE = 0x01000003


def server_message(kind, input_type, x, y, dev=0, d=0):
    return struct.pack("<B7xiiiii4x", kind, input_type, dev, x, y, d)


class Server:
    def __init__(self):
        if os.path.exists(SOCKET_PATH):
            os.unlink(SOCKET_PATH)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.sock.bind(SOCKET_PATH)
        self.sock.listen(1)
        self.conn = None
        self.shm = None
        self.updates = 0
        self.full_refreshes = 0

    def accept(self):
        self.conn, _ = self.sock.accept()
        msg = self.conn.recv(24)
        kind, key, fmt = msg[0], struct.unpack_from("<i", msg, 4)[0], msg[8]
        assert kind == 0, f"expected init, got {kind}"
        assert fmt == 3, f"expected RMPP RGB565, got format {fmt}"
        size = WIDTH * HEIGHT * 2
        fd = os.open(f"/dev/shm/qtfb_{KEY}", os.O_RDWR | os.O_CREAT, 0o600)
        os.ftruncate(fd, size)
        self.shm = mmap.mmap(fd, size)
        os.close(fd)
        self.conn.send(struct.pack("<B7xi4xQ8x", 0, KEY, size))
        threading.Thread(target=self.read_loop, daemon=True).start()

    def read_loop(self):
        while True:
            try:
                msg = self.conn.recv(24)
            except OSError:
                return
            if not msg:
                return
            if msg[0] == 1:
                self.updates += 1
            elif msg[0] == 6:
                self.full_refreshes += 1
            elif msg[0] == 3:
                return

    def send(self, input_type, x, y, dev=0):
        self.conn.send(server_message(4, input_type, x, y, dev))

    def key(self, code):
        self.send(0x40, code, 0)
        time.sleep(0.03)
        self.send(0x41, code, 0)
        time.sleep(0.03)

    def type(self, text):
        text = text.encode().decode("unicode_escape")
        for ch in text:
            if ch == "\n":
                self.key(QT_RETURN)
            elif ch == "\t":
                self.key(QT_TAB)
            elif ch == "\b":
                self.key(QT_BACKSPACE)
            elif ch.isalpha() and ch.isascii():
                if ch.isupper():
                    self.send(0x40, QT_SHIFT, 0)
                self.key(ord(ch.upper()))
                if ch.isupper():
                    self.send(0x41, QT_SHIFT, 0)
            else:
                self.key(ord(ch))

    def tap(self, x, y):
        self.send(0x10, x, y)
        time.sleep(0.05)
        self.send(0x11, x, y)

    def drag(self, x1, y1, x2, y2):
        self.send(0x10, x1, y1)
        for i in range(1, 11):
            time.sleep(0.03)
            self.send(0x12, x1 + (x2 - x1) * i // 10, y1 + (y2 - y1) * i // 10)
        self.send(0x11, x2, y2)

    def shot(self, path):
        from PIL import Image

        img = Image.frombuffer("RGB", (WIDTH, HEIGHT), bytes(self.shm), "raw", "BGR;16", 0, 1)
        img.save(path)
        print(f"saved {path} ({self.updates} updates, {self.full_refreshes} full refreshes)")


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    server = Server()
    env = dict(os.environ, QTFB_KEY=str(KEY))
    app = subprocess.Popen([sys.argv[1]], env=env)
    server.accept()
    try:
        for step in sys.argv[2:]:
            cmd, _, rest = step.partition(" ")
            args = rest.split()
            if cmd == "wait":
                time.sleep(float(rest))
            elif cmd == "type":
                server.type(rest)
            elif cmd == "key":
                server.key(int(rest, 0))
            elif cmd == "tap":
                server.tap(*map(int, args))
            elif cmd == "drag":
                server.drag(*map(int, args))
            elif cmd == "shot":
                server.shot(rest)
            else:
                sys.exit(f"unknown step: {step}")
            if app.poll() is not None:
                print(f"app exited with {app.returncode}")
                break
    finally:
        server.conn.close()
        try:
            app.wait(timeout=10)
        except subprocess.TimeoutExpired:
            app.kill()
        os.unlink(SOCKET_PATH)
        os.unlink(f"/dev/shm/qtfb_{KEY}")


if __name__ == "__main__":
    main()
