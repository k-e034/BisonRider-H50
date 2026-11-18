#!/usr/bin/env python3
"""2 本同時の「本当に復号できているか」を、PES の始まり（00 00 01）で 1 秒ごとに確かめる（橋のソケットにつなぐだけ）。

    python3 tools/dual_pes.py --a 27:1 --b 25:2 [--secs 40] [--delay 6] [--sock /run/dth50/tuner.sock]

暗号の印（TS 見出しの scrambling bits）はカードが復号しなくても 0 にすることがある（2026-11-17: 欄を写して開いた 2 本目の映像は
印が全部 0 なのに PES の始まりが 14〜62% しか正しくなかった）。ここでは PUSI の立ったパケットの本文が 00 00 01 で始まる割合を
映像・音声ごとに 1 秒ずつ出す。A を先に開き（1 本目・SECODE）、delay 秒後に B を開く（2 本目・欄を写す）。
Mirakurun も同じ橋を使うので、試す間は止める（docker stop mirakurun）。
"""
import argparse
import os
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dth50 import open31  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--a", help="1 本目 ch:tuner")
ap.add_argument("--b", help="2 本目 ch:tuner")
ap.add_argument("--plan", help="名前=ch:tuner@開始-終了（秒）をカンマ区切り。例 A=27:1@0-120,B=25:2@6-200,C=22:1@140-200。"
                               "終了は最初のバイトからではなく、試しの始まりからの秒")
ap.add_argument("--secs", type=float, default=40.0)
ap.add_argument("--delay", type=float, default=6.0)
ap.add_argument("--sock", default="/run/dth50/tuner.sock")
ap.add_argument("--out", default="/tmp/dual-pes")
a = ap.parse_args()
T0 = time.time()
DATA = {}


def grab_until(name, spec, start, stop):
    """plan 用: start 秒に開き、試しの始まりから stop 秒で閉じる。"""
    ch, tuner = (int(x) for x in spec.split(":"))
    time.sleep(max(0.0, start - (time.time() - T0)))
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(a.sock)
    s.sendall(f"{ch} {tuner}\n".encode())
    s.settimeout(0.5)
    chunks = []
    while time.time() - T0 < stop:
        try:
            b = s.recv(1 << 16)
        except socket.timeout:
            continue
        if not b:
            break
        chunks.append((time.time() - T0, b))
    s.close()
    DATA[name] = (ch, tuner, chunks)


def grab(name, spec, start, secs):
    ch, tuner = (int(x) for x in spec.split(":"))
    time.sleep(max(0.0, start - (time.time() - T0)))
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(a.sock)
    s.sendall(f"{ch} {tuner}\n".encode())
    s.settimeout(0.5)
    chunks = []
    first = None
    end = None
    while True:
        if end is not None and time.time() >= end:
            break
        try:
            b = s.recv(1 << 16)
        except socket.timeout:
            if first is None and time.time() - T0 > start + 60:
                break
            continue
        if not b:
            break
        now = time.time()
        if first is None:
            first = now
            end = now + secs
        chunks.append((now - T0, b))
    s.close()
    DATA[name] = (ch, tuner, chunks)


def analyse(name):
    ch, tuner, chunks = DATA[name]
    raw = b"".join(b for _t, b in chunks)
    open(f"{a.out}-{name}.ts", "wb").write(raw)
    req = open31.find_req31(raw[:4_000_000], -1)
    if req is None:
        print(f"{name} ch{ch}/T{tuner}: PAT/PMT が見つからない（{len(raw)} バイト）")
        return
    v = req.get(open31.R_VPID, 0xFFFF)
    au = req.get(open31.R_APID, 0xFFFF)
    # 時刻ごとのバイト位置
    marks, pos = [], 0
    for t, b in chunks:
        marks.append((pos, t))
        pos += len(b)
    buckets = {}
    carry, base_t, mi = b"", 0, 0
    pos = 0
    k = 0
    while k + 188 <= len(raw):
        if raw[k] != 0x47:
            k += 1
            continue
        while mi + 1 < len(marks) and marks[mi + 1][0] <= k:
            mi += 1
        sec = int(marks[mi][1])
        p = raw[k:k + 188]
        if p[1] & 0x40:
            pid = ((p[1] & 0x1F) << 8) | p[2]
            if pid in (v, au) and (p[3] >> 4) & 1:
                off = 4 + (1 + p[4] if (p[3] >> 4) & 2 else 0)
                good = p[off:off + 3] == b"\0\0\1"
                st = buckets.setdefault(sec, {v: [0, 0], au: [0, 0]})
                st[pid][0] += good
                st[pid][1] += 1
        k += 188
    tot = {v: [0, 0], au: [0, 0]}
    line = []
    for sec in sorted(buckets):
        st = buckets[sec]
        for pid in (v, au):
            tot[pid][0] += st[pid][0]
            tot[pid][1] += st[pid][1]
        g, n = st[v]
        line.append(f"{sec}:{'-' if not n else round(100 * g / n)}")
    pv = tot[v]
    pa = tot[au]
    print(f"{name} ch{ch}/T{tuner} 映像 0x{v:x} {pv[0]}/{pv[1]}（{100 * pv[0] // max(1, pv[1])}%）"
          f" 音声 0x{au:x} {pa[0]}/{pa[1]}（{100 * pa[0] // max(1, pa[1])}%）")
    print("  1 秒ごとの映像の正しい割合（秒:%）: " + " ".join(line))


if a.plan:
    names, ts = [], []
    for item in a.plan.split(","):
        name, rest = item.split("=")
        spec, span = rest.split("@")
        st, en = (float(x) for x in span.split("-"))
        names.append(name)
        ts.append(threading.Thread(target=grab_until, args=(name, spec, st, en)))
else:
    names = ["A", "B"]
    ts = [threading.Thread(target=grab, args=("A", a.a, 0.0, a.secs + a.delay)),
          threading.Thread(target=grab, args=("B", a.b, a.delay, a.secs))]
for t in ts:
    t.start()
for t in ts:
    t.join()
for n in names:
    if n in DATA:
        analyse(n)
