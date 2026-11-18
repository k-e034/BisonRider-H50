#!/usr/bin/env python3
"""橋の dump（/tmp/dth50-dump/<ラベル>/）を比べる（診断用・2026-11-18）。

    python3 tools/dump_diff.py A B [--bdf 0000:04:00.0] [--limit 80]

大域（0x80200000〜）は番地ごとに、文脈は「流れの役目」で揃えて比べる（席 0 / 席 1 の副5 の文脈は開くたびに
0xa02c3180 と 0xa02d72f0 が入れ替わるので、meta.json の prep_ctx で対応させる）。
"""
import argparse
import json
import os
import struct

ap = argparse.ArgumentParser()
ap.add_argument("a")
ap.add_argument("b")
ap.add_argument("--bdf", default="0000:04:00.0")
ap.add_argument("--limit", type=int, default=80)
ap.add_argument("--root", default="/tmp/dth50-dump")
args = ap.parse_args()
TAG = args.bdf.replace(":", "-")
GLO, CLO, SPAN = 0x80200000, 0x802AF000, 0xA0B8


def load(label):
    d = os.path.join(args.root, label)
    g = open(os.path.join(d, f"{TAG}-global-{GLO:08x}.bin"), "rb").read()
    c = open(os.path.join(d, f"{TAG}-ctx-{CLO:08x}.bin"), "rb").read()
    m = json.load(open(os.path.join(d, f"{TAG}-meta.json")))
    return g, c, m


def words(b):
    return struct.unpack(f"<{len(b) // 4}I", b[:len(b) // 4 * 4])


ga, ca, ma = load(args.a)
gb, cb, mb = load(args.b)
print(f"A {args.a}: {ma['seats']}")
print(f"B {args.b}: {mb['seats']}")

wa, wb = words(ga), words(gb)
diff = [(i * 4, x, y) for i, (x, y) in enumerate(zip(wa, wb)) if x != y]
print(f"\n=== 大域 0x{GLO:08x}〜: 違う語 {len(diff)}")
for off, x, y in diff[:args.limit]:
    print(f"  0x{GLO + off:08x}  {x:08x} → {y:08x}")


def ctx_of(c, ctx):
    o = (ctx & 0x1FFFFFFF) - (CLO & 0x1FFFFFFF)
    return c[o:o + SPAN]


for role in (0, 1):
    sa = ma["seats"][role] if role < len(ma["seats"]) else None
    sb = mb["seats"][role] if role < len(mb["seats"]) else None
    if not sa or not sb or not sa.get("prep_ctx") or not sb.get("prep_ctx"):
        continue
    xa, xb = words(ctx_of(ca, sa["prep_ctx"])), words(ctx_of(cb, sb["prep_ctx"]))
    d = [(i * 4, x, y) for i, (x, y) in enumerate(zip(xa, xb)) if x != y]
    print(f"\n=== 席 {role} の副5 の文脈（A 0x{sa['prep_ctx']:08x} / B 0x{sb['prep_ctx']:08x}）: 違う語 {len(d)}")
    for off, x, y in d[:args.limit]:
        print(f"  +0x{off:04x}  {x:08x} → {y:08x}")
