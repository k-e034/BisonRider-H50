#!/usr/bin/env python3
"""常駐の橋（同時受信）を、2 つのチューナーで開く・閉じるを繰り返して叩く（root は要らない・ソケットにつなぐだけ）。

    python3 tools/stress_bridge.py [--minutes 30] [--sock /run/dth50/tuner.sock] [--channels 13,18,...] [--log FILE]

Mirakurun も同じ橋を使うので、試す間は Mirakurun を止める（docker stop mirakurun）。止めないと、チューナー 1 の要求が
Mirakurun の回と取り合って待たされ、失敗に数えてしまう。

2 つのチューナーそれぞれで、次の形を混ぜて繰り返す:
  ・番組表の取得のような短い回（5〜20 秒）と、視聴・録画のような長めの回（30〜90 秒）
  ・同じチューナーで 0〜0.3 秒あけてすぐ開き直す（チャンネルの切り替え）
  ・ときどき 2 つのチューナーで同時に開く（足並みを揃える）
回ごとに見るもの: 最初のバイトまでの時間・量・平均の速さ・暗号のまま（scrambling ≠ 0）のパケット・PID ごとの
連続カウンタの飛び・橋が先に切ったか。開けない回が続いたら（橋がカードを断っている）止める。
結果は 1 回 1 行の JSON（--log）と、最後のまとめ。
"""
import argparse
import collections
import json
import os
import random
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dth50 import open31  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--minutes", type=float, default=30.0)
ap.add_argument("--sock", default="/run/dth50/tuner.sock")
ap.add_argument("--channels", default="13,18,19,20,21,22,23,24,25,26,27")
ap.add_argument("--log", default="/tmp/stress-bridge.jsonl")
ap.add_argument("--seed", type=int, default=None)
ap.add_argument("--max-fail", type=int, default=6, help="続けて開けない回がこれだけ出たら止める")
ap.add_argument("--tuners", default="1,2", help="叩くチューナーの通し番号（カンマ区切り・橋のチューナー番号）")
a = ap.parse_args()
CHS = [int(x) for x in a.channels.split(",")]
rnd = random.Random(a.seed)
T0 = time.time()
END = T0 + a.minutes * 60
LOCK = threading.Lock()
STOP = threading.Event()
TUNERS = [int(x) for x in a.tuners.split(",")]
SYNC = threading.Barrier(len(TUNERS))
fails = collections.Counter()
out = open(a.log, "w")


class TSCheck:
    """流れを 188 ずつ見て、PID ごとの本数・暗号のまま・連続カウンタの飛びを数える（最初の 3 秒は数えない）。"""

    def __init__(self):
        self.carry = b""
        self.n = collections.Counter()
        self.sc = collections.Counter()
        self.gap = collections.Counter()
        self.cc = {}
        self.head = bytearray()          # 番組の構成（PAT/PMT）を読むための頭の 4 MB
        self.offsync = 0                 # 188 の区切りが 0x47 でないパケット（汲み出しの区切りのずれ）
        self.pes = collections.defaultdict(lambda: [0, 0])   # PID → [本文が 00 00 01 で始まる PUSI, PUSI]

    def feed(self, data, count):
        if len(self.head) < 4_000_000:
            self.head += data
        buf = self.carry + data
        k = len(buf) - len(buf) % 188
        self.carry = buf[k:]
        mv = memoryview(buf)
        for i in range(0, k, 188):
            if mv[i] != 0x47:
                if count:
                    self.offsync += 1
                continue
            b1, b2, b3 = mv[i + 1], mv[i + 2], mv[i + 3]
            pid = ((b1 & 0x1F) << 8) | b2
            if pid == 0x1FFF:
                continue
            last = self.cc.get(pid)
            cc = b3 & 0x0F
            if b3 & 0x10:
                if count and last is not None and cc != (last + 1) & 0x0F and cc != last:
                    self.gap[pid] += 1
                self.cc[pid] = cc
            if count:
                self.n[pid] += 1
                if b3 >> 6:
                    self.sc[pid] += 1
                if b1 & 0x40 and b3 & 0x10:
                    off = i + 4 + (1 + mv[i + 4] if b3 & 0x20 else 0)
                    st = self.pes[pid]
                    st[0] += bytes(mv[off:off + 3]) == b"\0\0\1"
                    st[1] += 1


def session(tuner, ch, secs):
    rec = {"t": round(time.time() - T0, 1), "tuner": tuner, "ch": ch, "secs": secs}
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    t0 = time.time()
    first = None
    total = 0
    chk = TSCheck()
    why = "時間"
    try:
        s.connect(a.sock)
        s.sendall(f"{ch} {tuner}\n".encode())
        s.settimeout(0.5)
        while time.time() - t0 < secs + 45:      # 開くまでの待ち（他方の開く・閉じるで待たされる）も含めて
            if first is not None and time.time() - first >= secs:
                break
            if STOP.is_set():
                why = "止める"
                break
            try:
                b = s.recv(1 << 16)
            except socket.timeout:
                continue
            if not b:
                why = "橋が切った"
                break
            now = time.time()
            if first is None:
                first = now
            total += len(b)
            chk.feed(b, now - first >= 3.0)
    except OSError as e:
        why = f"エラー {e}"
    finally:
        s.close()
    dur = (time.time() - first) if first else 0.0
    # 橋が選んだ番組（PAT の並びで最初に MPEG-2 映像を持つ番組・open31 と同じ選び方）の ES だけで判定する。
    # 1 つのチャンネルに複数の番組がある局（ch13・ch19・ch20）では、選んでいない番組は暗号のまま流れてくる
    pids = None
    av = ()
    try:
        req = open31.find_req31(bytes(chk.head), -1)
        if req is not None:
            n_es = req.get(open31.R_ESN, 0)
            pids = {req[open31.R_ESPID + 4 * i] for i in range(n_es)}
            av = tuple(p for p in (req.get(open31.R_VPID), req.get(open31.R_APID)) if p not in (None, 0xFFFF))
    except Exception:                                # noqa: BLE001
        pids = None
    # 本当に復号できているか: 暗号の印はカードが復号しなくても 0 になることがある（2026-11-17）。映像・音声の PES の始まりで見る
    pes_ok = sum(chk.pes[p][0] for p in av)
    pes_n = sum(chk.pes[p][1] for p in av)
    sel = (lambda c: sum(v for p, v in c.items() if pids is None or p in pids))  # noqa: E731
    sc = sel(chk.sc)
    npk = sel(chk.n)
    gaps = sel(chk.gap)
    rec.update({"first": round(first - t0, 2) if first else None, "bytes": total, "dur": round(dur, 1),
                "mbps": round(total * 8 / dur / 1e6, 2) if dur > 0 else 0, "why": why,
                "es": sorted(f"{p:04x}" for p in pids) if pids else None,
                "pkts": npk, "scrambled": sc, "gaps": gaps, "offsync": chk.offsync, "pes": [pes_ok, pes_n],
                "gap_pids": {f"{p:04x}": n for p, n in chk.gap.most_common(4) if pids is None or p in pids}})
    bad = []
    if not first:
        bad.append("開けない")
    if why == "橋が切った" and dur < secs - 1:
        bad.append("途中で切れた")
    if npk and sc > npk * 0.01:
        bad.append("暗号のまま")
    if rec["gaps"]:
        bad.append("抜け")
    if chk.offsync:
        bad.append("区切りのずれ")
    if pes_n and pes_ok < pes_n * 0.99:
        bad.append("復号が崩れた")
    if first and dur > 5 and rec["mbps"] < 5:
        bad.append("遅い")
    rec["bad"] = bad
    with LOCK:
        out.write(json.dumps(rec, ensure_ascii=False) + "\n")
        out.flush()
        print(f"{rec['t']:7.1f}s T{tuner} ch{ch:<3} {secs:3.0f}s → 最初 {rec['first']}s・{total / 1e6:6.1f} MB・"
              f"{rec['mbps']:5.2f} Mbps・PES {pes_ok}/{pes_n}・暗号 {sc}・飛び {rec['gaps']}・ずれ {chk.offsync} {why} {'✗ ' + ','.join(bad) if bad else ''}",
              flush=True)
    return rec


def worker(tuner):
    n = 0
    consecutive = 0
    while time.time() < END and not STOP.is_set():
        n += 1
        if n % 12 == 0:
            try:
                SYNC.wait(timeout=120)               # 2 つのチューナーで同時に開く
            except threading.BrokenBarrierError:
                pass
        r = rnd.random()
        secs = rnd.uniform(5, 20) if r < 0.6 else rnd.uniform(30, 90)
        rec = session(tuner, rnd.choice(CHS), secs)
        if "開けない" in rec["bad"]:
            consecutive += 1
            if consecutive >= a.max_fail:
                print(f"T{tuner}: 続けて {consecutive} 回開けない → 止める", flush=True)
                STOP.set()
        else:
            consecutive = 0
        for b in rec["bad"]:
            fails[b] += 1
        gap = rnd.choice((0.0, 0.1, 0.3)) if rnd.random() < 0.5 else rnd.uniform(0.5, 3.0)
        time.sleep(gap)
    try:
        SYNC.abort()
    except Exception:                                # noqa: BLE001
        pass


print(f"{a.minutes:.0f} 分・チャンネル {CHS}・ログ {a.log}", flush=True)
ts = [threading.Thread(target=worker, args=(t,), daemon=True) for t in TUNERS]
for t in ts:
    t.start()
try:
    for t in ts:
        while t.is_alive():
            t.join(1.0)
except KeyboardInterrupt:
    STOP.set()
    for t in ts:
        t.join(60)
out.close()
recs = [json.loads(x) for x in open(a.log) if x.strip()]
ok = sum(1 for r in recs if not r["bad"])
mb = sum(r["bytes"] for r in recs) / 1e6
print(f"\n=== まとめ: {len(recs)} 回（問題なし {ok}）・{mb:.0f} MB・問題 {dict(fails)}・途中で止めた {STOP.is_set()}")
sys.exit(1 if fails or STOP.is_set() else 0)
