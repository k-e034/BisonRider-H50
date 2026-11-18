"""カードを使える状態にする: cold（初期化列の再生 → ファーム転送 → 転送後の手順）と warm（前回の流れの閉じ残しを閉じる）。

実験用リポジトリ（非公開）の tools/s4-boot.py（main の cold/warm の段・replay・load_recipe・close_like_xp・close_end）から移した。
初期化列・転送後の手順・ファームはベンダのもので、このリポジトリには入れない（vendor/ に置く。docs/hardware.md）。
"""
import logging
import struct
import time

from . import card as C
from .vfio import (BAR0_REGION, BAR2_REGION, Device, bind_vfio, iommu_group_of)

log = logging.getLogger("dth50.boot")

# 初期化列で、実機と記録が食い違ってよい欄（BAR, オフセット）。ファームが見ないビットだと確かめた欄だけ。
# ⚠️ 当て推量で増やさないこと。増やすほど「準備できていないカードに書き続ける」危険に近づく。
TOLERATE = frozenset({(2, 0x1094), (2, 0x1098), (2, 0x108C), (2, 0x1088)})
BOARD_ID = (2, 0x478)            # 基板 ID（& 0xf3ffffff・下位バイトが基板の種類）。ベンダのドライバは毎回の MMIO の前に
BOARD_ID_FAMILY = 0xF3FF0000     # 「カードが居るか」の確かめに読むだけで、値では分岐しない。初期化列は PCIEW
                                 # （0x...b6）で採ったので、DT-H50/PCIE（0x...a3）では世代の部分（& 0xf3ff0000）だけを比べる
WINDOW_REGS = (0x43C, 0x53C, 0x63C, 0x73C)

# warm の入口で 0x1a が出ていないとき、副5 として閉じる文脈（実験の既定）
WARM_SUB5_CTX = 0xA02C3180


class BootError(RuntimeError):
    """カードを使える状態にできない。多くは再起動（cold）が要る。"""


def load_recipe(path):
    """初期化列: 1 行 1 操作「W|R BAR オフセット(16進) 値(16進)|-」。# から行末は注釈。"""
    steps = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            f = line.split()
            op, reg, off = f[0], int(f[1]), int(f[2], 16)
            val = None if f[3] == "-" else int(f[3], 16)
            steps.append((op, reg, off, val))
    return steps


def replay(label, steps, bars, poll_ms=200.0, tolerate=TOLERATE):
    """初期化列を再生する。W は書く。R は値が一致するまで poll_ms 待つ。食い違えば止める（許した欄を除く）。"""
    reads = waited = tolerated = 0
    for i, (op, reg, off, val) in enumerate(steps):
        bar = bars.get(reg)
        if bar is None or off + 4 > bar.size:
            raise BootError(f"{label} の {i} 件目: BAR{reg}+0x{off:x} は範囲外")
        if op == "W":
            bar.w32(off, val)
            continue
        if (reg, off) == BOARD_ID and val is not None:
            got = bar.r32(off)
            reads += 1
            if got != val and (got & BOARD_ID_FAMILY) == (val & BOARD_ID_FAMILY) and got != 0xFFFFFFFF:
                tolerated += 1
                continue
        deadline = time.time() + poll_ms / 1000.0
        tries = 0
        while True:
            got = bar.r32(off)
            reads += 1
            tries += 1
            if got == 0xFFFFFFFF:
                raise BootError(f"{label} の {i} 件目: BAR{reg}+0x{off:x} が 0xffffffff（カードが応答しない）")
            if val is None or got == val or time.time() > deadline:
                break
            time.sleep(0.001)
        if tries > 1 and got == val:
            waited += 1
        if val is not None and got != val:
            if (reg, off) in tolerate:
                tolerated += 1
                log.debug("%s [%d] 許容 BAR%d+0x%05x: 実機 0x%08x / 記録 0x%08x", label, i, reg, off, got, val)
                continue
            raise BootError(f"{label} の {i} 件目で発散: BAR{reg}+0x{off:05x} 実機 0x{got:08x} / 記録 0x{val:08x}"
                            f"（{poll_ms:.0f} ms 待っても変わらず）。ここから先を書き続けるとバスを固める")
    log.info("%s: %d 操作・読み %d 件・待って一致 %d 件・許容 %d 件", label, len(steps), reads, waited, tolerated)


def load_firmware(card, fw):
    """ファームを BAR0 の窓へ流し込む（1 MB ずつ。窓番号は 1 から）。"""
    b2, b0 = card.bar2, card.bar0
    t0 = time.time()
    for base in range(0, len(fw), C.WINDOW_BYTES):
        window = base // C.WINDOW_BYTES + 1
        cur = b2.r32(C.WINDOW_REG)
        b2.w32(C.WINDOW_REG, (cur & ~C.WINDOW_MASK) | ((window << 8) & C.WINDOW_MASK))
        b2.r32(C.WINDOW_REG)
        chunk = fw[base:base + C.WINDOW_BYTES]
        for j, w in enumerate(struct.unpack(f"<{len(chunk) // 4}I", chunk[:len(chunk) // 4 * 4])):
            b0.u32[j] = w
    log.info("ファーム %d バイトを転送（%.1f 秒）", len(fw), time.time() - t0)


def core_running(card, fw):
    """窓 1 を読み返し、流し込んだ内容から書き換わった語があるか（＝MIPS が走っている）を見る。"""
    b2, b0 = card.bar2, card.bar0
    cur = b2.r32(C.WINDOW_REG)
    b2.w32(C.WINDOW_REG, (cur & ~C.WINDOW_MASK) | (1 << 8))
    b2.r32(C.WINDOW_REG)
    n = min(len(fw), C.WINDOW_BYTES) // 4
    expect = struct.unpack(f"<{n}I", fw[:n * 4])
    return sum(1 for j in range(n) if b0.u32[j] != expect[j])


def cold(card, bars, init_recipe, fw, post_recipe, poll_ms=200.0):
    """冷えたカードを初期化してファームを走らせる。"""
    for off in WINDOW_REGS:            # VFIO のリセットで窓レジスタが残ることがある。記録は 0 から始まる
        card.bar2.w32(off, 0)
    replay("初期化列", load_recipe(init_recipe), bars, poll_ms)
    load_firmware(card, fw)
    replay("転送後の手順", load_recipe(post_recipe), bars, poll_ms)
    moving, c0, c1 = card.counter_moving(0.05)
    log.info("カウンタ 0x6130: 0x%08x → 0x%08x（%s）", c0, c1, "動いている" if moving else "止まっている")
    diffs = core_running(card, fw)
    if not diffs:
        raise BootError("ファームが走っていない（流し込んだ内容が書き換わらない）。転送後の手順とカードが冷えているかを確かめる")
    log.info("ファームが走っている（窓 1 で %d 語が書き換わった）", diffs)


# ---- 閉じる ----

def close_like_xp(card, sub5_ctx, sub1_ctx, stop1a=True, vendor_0d=True, clear_stop_flag=True, label="",
                  slot_sub5=C.SLOT_SUB5, slot_sub1=C.SLOT_SUB1):
    """ベンダ（XP）の形で閉じる: 副5 → 副1 → 0x19 の対の 0x1a（枠 0）。全部返ったかを返す。

    副5 の前は三つ組と同じく 0x0d と [文脈+0x3c] bit14 落とし（これが無いと 0x09 が返らない）。
    次のプロセスが warm で 0x19 から始められる状態にするのが目的。"""
    ok_all = True

    def step(name, code, slotnum):
        nonlocal ok_all
        _b, got, _s, _u, _p = card.await_cmd(code, C.slot_addr(slotnum), timeout=3.0)
        ok = got == code
        ok_all &= ok
        (log.info if ok else log.warning)("閉じる%s %s: %s", label, name, "返った" if ok else "返らない")
        card.drain(rounds=4, timeout=0.05)

    for name, v in (("副5", sub5_ctx), ("副1", sub1_ctx)):
        if v and not C.is_ctx(v):
            log.error("閉じる%s %s の 0x%08x は文脈の番地でない → 出さない（出すとカードが止まる）", label, name, v)
    if sub5_ctx and not C.is_ctx(sub5_ctx):
        sub5_ctx = None
    if sub1_ctx and not C.is_ctx(sub1_ctx):
        sub1_ctx = None
    if sub5_ctx:
        if vendor_0d:
            card.cmd0d()
            step("0x0d（枠 2）", C.CMD_0D, C.SLOT_0D)
        if clear_stop_flag:
            v = card.read32(sub5_ctx + 0x3C)
            card.write32(sub5_ctx + 0x3C, v & ~0x4000)
        card.cmd09(slot_sub5, sub5_ctx)
        step(f"0x09 副5（0x{sub5_ctx:08x}）", C.CMD_CLOSE, slot_sub5)
    if sub1_ctx and sub1_ctx == sub5_ctx:
        log.warning("副1 の文脈が副5 と同じ 0x%08x。副1 は閉じない", sub1_ctx)
        ok_all = False
    elif sub1_ctx:
        card.cmd09(slot_sub1, sub1_ctx)
        step(f"0x09 副1（0x{sub1_ctx:08x}）", C.CMD_CLOSE, slot_sub1)
    if stop1a:
        card.cmd1a()
        step("0x1a 停止（枠 0）", C.CMD_STOP, C.SLOT_START)
    return ok_all


def warm(card, **close_opts):
    """前回のファームが生きているカードを、0x19 から始められる状態にする。閉じられなければ BootError（再起動が要る）。"""
    head = card.read(C.TS_BUF, 16)
    if all(b == 0xFF for b in head):
        raise BootError("warm: カード内メモリが読めない（0xff）。再起動（cold）が要る")
    card.drain(rounds=8, timeout=0.1)
    last = card.last_code(C.SLOT_START)
    ctx = card.ctx_close_pick(None)
    if last == 0 and not card.ctx_list2():
        # 枠 0 にまだ何も出していない（cold の後に 0x19 を出さずに終わった回・tuner-probe など）。閉じるものは無い。
        # ここで閉じに行くと、開いていない流れへの 0x09 / 0x1a が返らず止まる（2026-11-04 実機）
        log.info("warm: 前回は流れを開いていない（枠 0 に命令なし）")
    elif last != C.CMD_STOP:
        log.info("warm: 前回は閉じていない（枠 0 の最後 0x%x）→ ここで閉じる", last)
        if not close_like_xp(card, WARM_SUB5_CTX, ctx, label="（warm の入口）", **close_opts):
            # v42 は入口で 4 段とも返らないまま先へ進み、ファームがパニックした
            raise BootError("warm: 前回の流れを閉じられない（コマンドが返らない）。再起動（cold）が要る")
    else:
        # 0x1a で閉じていても、表 2 に文脈が残っていれば閉じ残し。副5 を先に、副1 を後に閉じる（0x1a は出さない）
        left = card.ctx_list2()
        if left:
            def link(c):
                return card.read32(c + 0x8000 + 0x5B8)    # 副5 が結んだ流れ（副1 の文脈）
            sub5s = [c for c in left if link(c) in left and link(c) != c]
            sub1s = [c for c in left if c not in sub5s]
            log.warning("warm: 表 2 に閉じ残し（副5 %s / 副1 %s）を閉じる",
                        " ".join(hex(c) for c in sub5s) or "--", " ".join(hex(c) for c in sub1s) or "--")
            for c in sub5s:
                close_like_xp(card, c, None, stop1a=False, label="（閉じ残しの副5）", **close_opts)
            for c in sub1s:
                close_like_xp(card, None, c, stop1a=False, label="（閉じ残しの副1）", **close_opts)
            if card.ctx_list2():
                raise BootError("warm: 閉じても表 2 が空にならない。再起動（cold）が要る")
    card.drain(rounds=8, timeout=0.1)


def is_warm(bar2):
    """0x6130 が動いていれば、前回のファームが生きている。0 / 0xffffffff で止まっていれば冷えている。"""
    c0 = bar2.r32(C.COUNTER_REG)
    time.sleep(0.02)
    c1 = bar2.r32(C.COUNTER_REG)
    return not (c0 == c1 and c0 in (0, 0xFFFFFFFF))


def open_card(bdf, vendor, poll_ms=200.0, force_cold=False, **close_opts):
    """カードを握って使える状態にする。戻り (Device, Card, warm だったか)。

    vendor は vendor.Files（初期化列・転送後の手順・ファームのパス）。"""
    bind_vfio(bdf)
    dev = Device(iommu_group_of(bdf), bdf)
    bar2 = dev.bar(BAR2_REGION)
    was_warm = not force_cold and is_warm(bar2)
    if not was_warm:
        dev.reset()
    dev.enable_bus_master()
    bar0 = dev.bar(BAR0_REGION)
    card = C.Card(bar0, bar2)
    bars = {0: bar0, 2: bar2}
    if was_warm:
        log.info("warm: 前回のファームが生きている（0x6130 が動いている）")
        warm(card, **close_opts)
    else:
        log.info("cold: 初期化する")
        cold(card, bars, vendor.init_recipe, vendor.firmware(), vendor.post_recipe, poll_ms)
    return dev, card, was_warm
