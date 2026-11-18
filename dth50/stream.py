"""選局してカードの中で復号した TS を取り出す（本筋）。

実験用リポジトリ（非公開）の tools/s4-boot.py の ts_grab（と、そこから呼ぶ ts_start / ts_submit / ts_cmd8 / ts_cmd8_sub5 /
cmd15_send / secode_blob / secode_send / force_628 / force_31c / force_4c_or / exit_pump / close_end）から、
Mirakurun 用の起動（tools/dth50-mirakurun.sh → run-ch27-vendor.sh の旗）で実際に通った文だけを移した。

流れ:
  0.  ホストのバッファを IOMMU に載せる
  0b. 0x1b（ホスト領域の登録）
  1.  チューナーの初期化 → 選局 → ロック待ち（PCIEW の 2 つ目は TunerMJ110）
  3b. PID 表 128 段を空にする
  4.  0x19（開始）→ 0x0b × 2 → 0x08 副1（PSI）
      副1 のバッファに届いた PAT/PMT から 0x31 要求を組む（open31）
      0x08 副5（下ごしらえ・0x31 で中身を組む）→ 0x15 → SECODE（0x0b）
      三つ組 2・3 回目: 0x0d → 0x09 副5 →（2 回目だけ副1 を閉じて開き直す）→ 副5 → 0x15 → SECODE
      [文脈+0x4c] に 0x04000000 を足す
  5.  文脈の欄の補い（+0x628 など・+0x31c・+0x4c）→ 0x0b の定常運転（既定 3 秒）
  6.  出口の環（平文の PSI/SI）と復号済みの環（映像・音声）をカードから直接読み、1 本の TS にして書く
  7.  XP の形で閉じる（副5 → 副1 → 0x1a）

⚠️ 実験時代の旗（三つ組 3 回・+0x628 の強制・+0x4c の OR など）は、効いているか分からないものも含めてそのまま。
   実機で動くことを確かめてから 1 つずつ外して確かめる（docs/porting.md）。
"""
import collections
import ctypes
import logging
import mmap
import os
import queue
import struct
import threading
import time
from dataclasses import dataclass

from . import card as C
from . import open31
from .boot import close_like_xp
from .tuner import Tuner, TunerMJ110, load_mj110, load_seq

log = logging.getLogger("dth50.stream")

STOP = []        # SIGTERM / SIGINT を受けたら CLI が積む（開いている途中では止めず、開き終えたら閉じる手順へ進む）

TS_ARG = C.arg_addr(1)           # 0x0b（枠 1）の引数域 0x2360
SEL_PAGE_BITS = 0x200            # 0x15 の [引数+0x10]。bit9 = 選択器の頁
SECODE_HEAD = (0x9000, 0x80)     # .scd の頭
SECODE_BODY = (0x9080, 0x10)     # 第 1 の塊（"pair"）
SECODE_BODY2 = (0x8000, 0x1000)  # 第 2 の塊（"unnamed"）
# SECODE が副5 の文脈に書く欄（文脈の頭からのずれ・時間で動く欄を除く 52 語）。2026-11-05 に 1 本だけの運転で、最初の SECODE の
# 直前と直後の文脈を丸ごと読んで差を取った（開発時の実験用スクリプト x_secdiff.py）。2 本目では SECODE を出さず（カード内の
# 保護プロセッサにコードを載せ直して 1 本目を止める）、これを 1 本目の副5 の文脈から写す。文脈の中を指す値（+0x26e0 など）は付け替える
SECODE_CTX_FIELDS = (
    0x26E0, 0x26F8, 0x26FC, 0x2780, 0x2784, 0x2790, 0x28A0, 0x28A4, 0x36C8, 0x80D4, 0x80D8, 0x80DC, 0x80E0,
    0x822C, 0x8230, 0x8234, 0x8238, 0x85D8, 0x8E40, 0x8EC0, 0x8F40, 0x8FC8, 0x8FE8, 0x9028, 0x9068, 0x90A8,
    0x90E8, 0x9128, 0x9168, 0x91A8, 0x91E8, 0x9228, 0x9268, 0x92A8, 0x92E8, 0x9328, 0x9368, 0x93A8, 0x93E8,
    0x9428, 0x9468, 0x9470, 0x9478, 0x947C, 0x9480, 0x9484, 0x9488, 0x948C, 0x9490, 0x9494, 0x9498, 0x94A0)
CTX_SPAN = 0xA0B8                # 文脈どうしの間隔（付け替えの範囲）

# B-CAS カードとの通信の表（実験の「鍵表」）。+0x118 bit1 = リセット応答（ATR）を解釈し終えた＝ECM タスクが待つ準備完了
CARD_TAB = 0xA02309F8
CARD_READY = CARD_TAB + 0x118


@dataclass
class Options:
    """本筋の旗（tools/run-ch27-vendor.sh と dth50-mirakurun.sh で渡していた値）。"""
    tuner: int = 1                   # チューナーの番号（1 = TC90507＋MJ100・2 = PCIEW の 2 つ目 SMT-MJ110）
    ts_input: int = -1               # 副1・副5 の +0x08（TS の入力番号）。-1 なら tuner - 1。DT-H50/PCIE の確認用
    # 同時受信（試験中）: 2 本目は primary=False・別の枠・別の IOVA・map_zero=0 で開く
    primary: bool = True             # カード全体の手順（0x1b・PID 表を空にする・0x19・閉じるときの 0x1a）をするか
    slot_sub1: int = C.SLOT_SUB1     # 0x08 副1 を出す枠（閉じるまで文脈を握る）
    slot_sub5: int = C.SLOT_SUB5     # 0x08 副5 を出す枠
    card_reset: bool = True          # 定常運転の最初の 0x0b で B-CAS カードを起動し直すか（他方が復号中なら止める恐れ）
    secode_src: int = 0              # secode=False のとき、SECODE の欄を写す元（1 本目の副5 の文脈）
    seat: int = 0                    # 0 = 1 本目の枠（5/12）・1 = 同時に開く 2 本目の枠（7/11・副5 の +0x010/+0xacc が違う）
    tuner_others: bool = True        # チューナー 1 の初期化列のうち 2 つ目を休ませる書き込みも流すか（2 つ目が流れている間は偽）
    multi_audio: bool = True         # 音声が 2 本以上の番組で、副5 +0x5c の bit12 を立てて 2〜4 本目の音声も通す
    secode: bool = True              # 0x15・SECODE・三つ組を出すか。2 本目で出すと 1 本目の復号が止まり、2 回目の SECODE で
                                     # ファームが応答しなくなった（2026-11-05 実験 1）。カード全体で 1 回だけの手順と見ている
    sub1_bufs: int = 256             # --ts-bufs
    dma_iova: int = 0x20000000       # --dma-iova
    map_zero: int = 0x10000          # --map-zero（IOVA 0 に張る頁）
    lock_wait: float = 4.0           # --lock-wait
    pn: int = open31.AUTO            # --open31 pn=auto
    open31_wait: float = 3.0         # --open31-wait
    triad: int = 3                   # --vendor-triad 3
    triad_pid: int = 0x0100          # --vendor-triad-pid
    vendor_0d: bool = True           # --vendor-triad-0d
    clear_stop_flag: bool = True     # --clear-stop-flag
    cmd15: bool = True               # --cmd15
    force628: bool = True            # --force628
    force31c: bool = True            # --force31c
    ctx4c_or: int = 0x04000000       # --ctx4c-or
    force4c_or: int = 0x04000000     # --force4c-or
    steady_1bc_after: int = 0        # --steady-1bc-after
    card_ready_wait: float = 0.8     # 定常 0x0b でカードを起動し直してから、準備完了を待つ秒数（過ぎたら起動し直す）。
                                     # 0.6 にすると最初の起動し直しで間に合わない回が増えた（2026-11-05）
    card_resets: int = 3             # 準備完了が立たないときに起動し直す回数の上限
    card_extra: float = 3.0          # 準備完了が立たないうちは、定常運転をこの秒数まで延ばす
    ts_seconds: float = 3.0          # --ts-seconds（定常運転の長さの上限。steady_until_dec なら復号が始まった所で抜ける）
    steady_until_dec: bool = False   # 復号済みの環（dec）の書き込み位置が動き出したら、定常運転を抜けて汲み出しへ。
                                     # ⚠️ 試験中: 有効にすると EPGStation のライブ視聴で、切り替えた直後の配信が映らなかった（F5 で映る）
    ts_wait: float = 0.2             # --ts-wait
    pump_read: int = 16              # --pump-read（fastread の読み幅・0 は string_at）
    pump_poll: float = 0.02
    pump_late: float = 0.15          # 汲み出しの周回がこれより空いたら数える（環は 14 Mbps で約 0.22 秒ぶん）
    pump_stall: float = 5.0          # どの環も進まないままこの秒数が過ぎたら、流れが止まったとして汲み出しを終える（0 で見張らない）
    settle: float = 0.0              # SECODE が返ってから次の段までの間（実験では計器の読み出しがここに入っていた）
    late_wait: float = 7.0           # 三つ組の完了が既定の時間で返らないとき、さらに待って遅れを記録する秒数
    settle_final: float = 0.0        # 最後の SECODE の後だけ空ける間（文脈の欄の補い・定常運転の前）
    secode_async_wait: float = 2.0   # SECODE が返った後、非同期の 2 段が終わるのを待つ上限
    stop_flag_wait: float = 2.0      # 三つ組の 0x09 の前に [文脈+0x3c] bit14 が立つのを待つ上限


# 席ごとの枠・ホストのバッファ。席 1 は XP のベンダが 2 本目に使った形（2026-11-05 実験 11 で 2 本同時が通った）
SEATS = ({"slot_sub1": C.SLOT_SUB1, "slot_sub5": C.SLOT_SUB5, "dma_iova": 0x20000000},
         {"slot_sub1": 7, "slot_sub5": 11, "dma_iova": 0x30000000, "seat": 1})


def seat_options(tuner, seat, secode_src=0, own_secode=False, copy=False, **kw):
    """席 seat で開く設定。secode_src = 0 なら 1 本目（SECODE・カード全体の手順を出す）。
    0 でなければ、もう 1 本が流れている最中に開く回: カード全体の手順・SECODE・B-CAS の起動し直しを出さず、
    副5 を正しくつないで（+0x028 = 自分の副1・席 1 は +0x010 = 1・+0xacc = 7・+0x08 = 入力番号）開くだけ。

    2026-11-18: 以前は SECODE の欄 52 語を流れている方の文脈から写していた（copy=True）。それだと 2 本目が流れ始めてから
    最初の鍵の切り替わりで 2 本とも復号が壊れていた（暗号の印は 0 のまま中身が壊れる・PES の始まりで確かめた）。写す欄に
    鍵の置き場（枠+0x147c〜）が入っていて、2 本の鍵の状態がぶつかる。写さずに開けば、2 本とも正しく復号し続け、1 本目を
    先に閉じても続き、SECODE を出した流れが無くなった後に開く流れも正しく復号する（5 分の試しで 100%）。"""
    if not secode_src:
        return Options(tuner=tuner, **SEATS[seat], **kw)
    if own_secode:
        # 試し（2026-11-17）: 欄を写した 2 本目は、2 本目が流れ始めてから最初の鍵の切り替わりで 2 本とも復号が壊れる
        # （PES の始まりで確かめた）。実験 1〜10 で 2 本目の SECODE が 1 本目を止めたのは、完了の待ち方の不具合
        # （2 本で流すときだけ出る）の上でのことだったので、2 本目にも 0x15・SECODE・三つ組を出して確かめ直す
        return Options(tuner=tuner, primary=False, map_zero=0, card_reset=False, tuner_others=False,
                       **SEATS[seat], **kw)
    # copy=False は試し（2026-11-18）: 欄を写さない。写す欄には鍵の置き場（枠+0x147c〜）が入っていて、1 本目と鍵の状態を
    # 分け合うと、次の鍵の切り替わりで互いに上書きして 2 本とも壊れる、という見立てを確かめる。正しくつないだ（+0x028）
    # 2 本目を SECODE も写しもなしで開くのは、まだ試していない（実験 1〜9 はつなぎ違い、10 は SECODE、11 は写し）
    return Options(tuner=tuner, primary=False, map_zero=0, card_reset=False, secode=False, cmd15=False, triad=1,
                   secode_src=secode_src if copy else 0, tuner_others=False, **SEATS[seat], **kw)


class StreamError(RuntimeError):
    pass


def _is_card_ptr(v):
    if (v >> 28) in (0x8, 0xA):
        v &= 0x1FFFFFFF
    elif v >> 28:
        return False
    return 0x100000 <= v < 0x04000000


def _find_sync(buf):
    """0x47 が 188 おきに 3 つ並ぶ最初の位置（0〜187）。見つからなければ None。"""
    for k in range(min(188, len(buf) - 376)):
        if buf[k] == 0x47 and buf[k + 188] == 0x47 and buf[k + 376] == 0x47:
            return k
    return None


def _drop_scrambled(data, pids=None):
    """188 の区切りに揃った TS から、暗号の印（transport_scrambling_control）が 0 でないパケットを除く。
    pids（Counter）を渡すと、除いたパケットの PID を数える。"""
    if not any(data[k + 3] & 0xC0 for k in range(0, len(data), 188)):
        return data
    if pids is not None:
        for k in range(0, len(data), 188):
            if data[k + 3] & 0xC0:
                pids[((data[k + 1] & 0x1F) << 8) | data[k + 2]] += 1
    return b"".join(data[k:k + 188] for k in range(0, len(data), 188) if not data[k + 3] & 0xC0)


def _fastread():
    so = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fastread.so")
    if not os.path.exists(so):
        return None
    fn = ctypes.CDLL(so).rd
    fn.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_size_t, ctypes.c_int]
    fn.restype = ctypes.c_int
    return fn


class HostBuffers:
    """カードが DMA で読み書きするホストのバッファ（0xA5 で埋めておき、書き換わった所を見る）。"""

    def __init__(self, dev, nb, iova, map_zero):
        self.nb, self.iova = nb, iova
        total = (8 * 0x4000 + 4 * 0x10000 + 8 * 0x4000 + nb * 0x4000
                 + 8 * 0x10000 + 4 * 0x10000      # 0x08 副5 用（64 KB × 12）
                 + 4 * 0x4000                     # 制御 0x19 / 0x08 副1 / 0x08 副5 / 0x1b
                 + 0x4000)                        # 0x15 の選択器の頁
        self.size = (total + 0xFFFF) & ~0xFFFF
        self.buf = mmap.mmap(-1, self.size)
        self.buf.write(b"\xA5" * self.size)
        vaddr = ctypes.addressof(ctypes.c_char.from_buffer(self.buf))
        dev.dma_map(vaddr, iova, self.size)
        self.zero = None
        if map_zero:
            # IOVA 0 への DMA を受ける頁（無いと IOMMU が DMAR を出す）
            self.zero = mmap.mmap(-1, map_zero)
            self.zero.write(b"\xA5" * map_zero)
            dev.dma_map(ctypes.addressof(ctypes.c_char.from_buffer(self.zero)), 0, map_zero)
        self.a16a = [iova + i * 0x4000 for i in range(8)]
        off = 8 * 0x4000
        self.a64 = [iova + off + i * 0x10000 for i in range(4)]
        off += 4 * 0x10000
        self.a16b = [iova + off + i * 0x4000 for i in range(8)]
        self.sub1_off = 8 * 0x4000 + 4 * 0x10000 + 8 * 0x4000
        self.a8 = [iova + self.sub1_off + i * 0x4000 for i in range(nb)]
        p = self.sub1_off + nb * 0x4000
        self.a8b8 = [iova + p + i * 0x10000 for i in range(8)]
        p += 8 * 0x10000
        self.a8b4 = [iova + p + i * 0x10000 for i in range(4)]
        self.dev, self.map_zero = dev, map_zero
        p += 4 * 0x10000
        self.ctl = iova + p
        self.ctl8 = iova + p + 0x4000
        self.ctl8b = iova + p + 0x8000
        self.ctl1b = iova + p + 0xC000
        self.sel = iova + p + 0x10000
        # 定常運転で走査する範囲（制御ブロックと選択器の頁は除く）
        self.scan = []
        q = 0
        for cnt, sz in ((8, 0x4000), (4, 0x10000), (8, 0x4000), (nb, 0x4000), (8, 0x10000), (4, 0x10000)):
            for _ in range(cnt):
                self.scan.append((q, sz))
                q += sz

    def reset_scan(self):
        """走査する範囲（TS を受けるバッファ）を 0xA5 に戻す。席のバッファを次の流れで使い回す前に。
        制御ブロック・選択器の頁は触らない（選択器の頁は、欄を写した他方の流れが使い続けている）。"""
        for off, sz in self.scan:
            self.buf[off:off + sz] = b"\xA5" * sz

    def close(self):
        """IOMMU の割り当てを外す。"""
        try:
            self.dev.dma_unmap(self.iova, self.size)
            if self.zero is not None:
                self.dev.dma_unmap(0, self.map_zero)
        except OSError as e:
            log.warning("IOMMU の割り当てを外せない: %s", e)

    def sub1_ts(self):
        """副1 のバッファにある TS パケット（読むだけ）。"""
        return open31.ts_from_buffers(self.buf, [(self.sub1_off + i * 0x4000, 0x4000) for i in range(self.nb)])

    def put(self, iova, data):
        o = iova - self.iova
        self.buf[o:o + len(data)] = data

    def take_packets(self, boff, bsz, sink=None):
        """バッファから TS パケットを拾い、拾えたらバッファを 0xA5 に戻す。戻り 拾った本数。"""
        seg = bytes(self.buf[boff:boff + bsz])
        if seg.count(0xA5) == len(seg):
            return 0
        npk = 0
        for k in range(bsz // 188):
            if seg[k * 188] == 0x47:
                if sink is not None:
                    sink.write(seg[k * 188:(k + 1) * 188])
                npk += 1
        if npk:
            self.buf[boff:boff + bsz] = b"\xA5" * bsz
        return npk


class Stream:
    def __init__(self, dev, card, vendor, opts=None, hb=None):
        """hb = 席のバッファ（常駐の橋が持ち続けて使い回す）。渡したら閉じても IOMMU の割り当てを外さない:
        欄を写した他方の流れが 1 本目の選択器の頁（SECODE の欄 +0x28a4・+0x9168 が指す）を使い続け、
        1 本目の 0x1b で登録した制御ブロックと IOVA 0 の頁はカード全体のもの。外すとカードが止まる（2026-11-05 実験 12・13）。"""
        self.dev, self.card, self.vendor = dev, card, vendor
        self.hb_ext = hb
        self.o = opts or Options()
        self.va = vendor.args()
        self.prep_ctx = None         # 副5 が返した文脈（三つ組のたびに更新）
        self.ring_ctx = None
        self.req31 = None
        self.steady_n = 0
        self.card_reset_next = False
        self.hb = None
        self.started = False         # 0x19 を出したか（出す前に失敗したなら閉じるものは無い）
        self.stop = threading.Event()  # この流れだけを止める印（STOP はプロセス全体）
        self.stalled = False         # 汲み出しが「どの環も進まない」で終わった
        self.sub5_failed = False     # 副5（下ごしらえ）が文脈を返さなかった（やり直せば通ることがある）

    # ---- コマンド（ベンダの雛形 + この回の値）----
    def cmd19(self):
        hb = self.hb
        t = dict(self.va["cmd19"])
        t[0x000] = hb.ctl
        for i, a in enumerate(hb.a16a):
            t[0x014 + i * 4] = a
        for i, a in enumerate(hb.a64):
            t[0x05C + i * 4] = a
        for i, a in enumerate(hb.a16b):
            t[0x0A4 + i * 4] = a
        self.card.issue(C.SLOT_START, C.CMD_START, t)

    def cmd0b(self, steady=False):
        if steady:
            self.card.write_words(0x20500, self.va["blk20500"])
            t = dict(self.va["cmd0b_steady"])
            if self.o.steady_1bc_after is not None:
                if (self.steady_n > 0 or not self.o.card_reset) and not self.card_reset_next:
                    t[0x1BC] = self.o.steady_1bc_after
                self.card_reset_next = False
                self.steady_n += 1
        else:
            t = dict(self.va["cmd0b"])
        self.card.issue(1, C.CMD_STEADY, t)

    def cmd08_sub1(self):
        hb = self.hb
        t = dict(self.va["cmd08"])
        t[0x018] = hb.ctl8
        # カードへの入力の番号。ベンダのドライバは 0x2E（TS を開く）の要求 +0x84 をここへ写す（BufFullSeg.sys 0x288b5）。
        # XP の採取では 1 つ目 0・2 つ目（SMT-MJ110）1。0x31 の +0x8c / +0xb4 はドライバの中の流れを引く番号でカードへは渡らない
        t[0x008] = self.o.ts_input if self.o.ts_input >= 0 else self.o.tuner - 1
        t[0x314] = len(hb.a8)
        for i, a in enumerate(hb.a8):
            t[0x31C + i * 4] = a
        self.card.issue(self.o.slot_sub1, C.CMD_SUB_OPEN, t)

    def cmd08_sub5(self, pid7c=None, set5c=None):
        hb = self.hb
        t = dict(self.va["cmd08_sub5"])
        t[0x018] = hb.ctl8b
        # 入力の番号は副5 にも入れる。XP でベンダが 2 本同時に開いたとき、2 本目の副5（枠 21）も +0x08 = 1 だった
        # （2026-11-05・ref/xp/dual）。副1 だけに入れると、2 本目の復号の文脈が 1 本目と同じ入力 0 に結ばれる
        t[0x008] = self.o.ts_input if self.o.ts_input >= 0 else self.o.tuner - 1
        for i, a in enumerate(hb.a8b8):
            t[0x31C + i * 4] = a
        for i, a in enumerate(hb.a8b4):
            t[0x3F4 + i * 4] = a
        t[0x060] = t.get(0x060, 0) & ~0x00100000      # 下ごしらえ: bit20 を落として通常路へ
        if pid7c is not None:
            t[0x07C] = pid7c
        if set5c is not None:
            t[0x05C] = set5c
        if self.req31 is None:
            self.resolve31("副5 の前", 0.0)
        ch = open31.overlay_sub5(t, self.req31 if self.req31 is not None else open31.req31_pre_pmt())
        # +0x028 = この副5（復号）がつながる副1（TS の流れ）の文脈。ドライバは 0x31 の処理で流れの文脈から埋める。
        # 雛形の値は 1 本目の副1 の文脈なので、2 本目でもそのまま使うと 2 本目の復号が 1 本目の流れにつながり、
        # 2 本目の SECODE が 1 本目の復号を壊していた（2026-11-05 実験 1〜9）。XP のベンダの 2 本目（枠 21）は自分の副1 の文脈。
        s1 = self.card.opened_ctx(C.arg_addr(self.o.slot_sub1))
        if s1 and _is_card_ptr(s1):
            t[0x028] = s1
        if self.o.multi_audio and t.get(0x824, 0) > 1:
            # 0x29a6c は +0x5c を 0 にするので、ファームは 1 本目の音声（+0x828）しか PID の照合に入れない。2 本目以降
            # （+0x82c/+0x830/+0x834）は +0x5c の bit12 で入る（0x80186350）。音声 1 本の番組はベンダと同じ形のまま
            t[0x05C] = t.get(0x05C, 0) | open31.SET5C_AUDIO
        if self.o.seat == 1:
            # ベンダの 2 本目の副5 で 1 本目と違ったのは、この 2 欄だけ（ほかは PID・番組・ホストのバッファ）
            t[0x010] = 1
            t[0xACC] = 7
        log.info("0x31 で副5 を組んだ: %d 欄・+0x60=0x%x +0x7c=0x%x +0x5c=0x%x 音声 %s ES %d 本・つなぐ副1 0x%08x", len(ch),
                 t[0x060], t[0x07C], t.get(0x05C, 0),
                 " ".join(f"0x{t.get(0x828 + 4 * i, 0):x}" for i in range(max(1, min(t.get(0x824, 0), 4)))),
                 t[0x9A8], t.get(0x028, 0))
        self.card.issue(self.o.slot_sub5, C.CMD_SUB_OPEN, t)

    def cmd15(self, ctx):
        """0x15（枠 6・+0x1C = 0x0D）: 選択器の頁を渡す。"""
        c, sel = self.card, self.hb.sel
        c.write_words(C.arg_addr(C.SLOT_SEL), {0x000: ctx, 0x004: 0, 0x008: sel & 0xFFFFFFFF,
                                               0x00C: (sel >> 32) & 0xFFFFFFFF, 0x010: SEL_PAGE_BITS})
        sa = C.slot_addr(C.SLOT_SEL)
        # 実験では、枠 6 をファームが使っていないかを印を書いて 20 ms 見てから枠を書いていた（そのまま残す）
        c.write(sa, struct.pack("<8I", *(0xC0DE1500 + i for i in range(8))))
        time.sleep(0.02)
        c.write_slot(C.SLOT_SEL, C.CMD_SELECT, tag=0x0D)
        c.kick(sa, C.CMD_SELECT)
        t0 = time.time()
        st = None
        while time.time() - t0 < 0.05:
            st = c.read32(sa + 0x10)
            if st in (1, 2):
                break
        if st != 1:
            log.warning("0x15: 枠+0x10 = %s", "--" if st is None else hex(st))

    def secode_blob(self):
        d = open(self.vendor.secode, "rb").read()
        (ho, hl), (bo, bl), (b2o, b2l) = SECODE_HEAD, SECODE_BODY, SECODE_BODY2
        if len(d) < max(ho + hl, bo + bl, b2o + b2l):
            raise StreamError("secode.scd が短い")
        return d[ho:ho + hl] + d[bo:bo + bl] + d[b2o:b2o + b2l]

    def secode(self, ctx, blob):
        """SECODE（0x0b・枠 3）: 面 #0 に置き、書き戻し先は面 #1。"""
        hb = self.hb
        buf, out = hb.a8b4[0], hb.a8b4[1]
        hb.put(buf, blob)
        t = {0x000: ctx, 0x004: 0, 0x0A0: 0x00100000, 0x0A4: 0, 0x0AC: 0, 0x0B0: len(blob),
             0x1C0: max(len(blob) - SECODE_HEAD[1] - SECODE_BODY[1], 0),   # 2 段目の照合の長さ
             0x1C4: out}                                                   # 0 にするとカードが番地 0 へ DMA する
        self.card.issue(3, C.CMD_STEADY, t)
        if not self._await("SECODE", C.CMD_STEADY, 3, 6.0):
            log.warning("SECODE の完了が来ない")
        self.secode_async()
        if self.o.settle:
            time.sleep(self.o.settle)

    def secode_async(self):
        """SECODE は返った後も非同期の 2 段が続く（実験の secode_report）。その完了数（gp-0x7da4 / gp-0x7da0）が
        ハンドラに入った回数（gp-0x7db8）に追いつくまで待つ。待たずに文脈の欄の補いと定常運転へ進むと、dec の環に何も出ない
        （2026-11-04 実機: 最後の SECODE の後に 0.5 秒空けると復号・空けないと dec 0 本）。"""
        c = self.card

        def gp(off):
            return c.read32(C.FW_GP + off)
        t0 = time.time()
        while True:
            entry, err, a1, a2 = gp(-0x7DB8), gp(-0x7DAC), gp(-0x7DA4), gp(-0x7DA0)
            done = a1 >= entry and a2 >= entry
            if done or time.time() - t0 >= self.o.secode_async_wait:
                break
            time.sleep(0.002)
        (log.info if done else log.warning)(
            "SECODE の後処理: 入った %d・非同期① %d・非同期② %d・失敗コード %d（%s・%.0f ms）",
            entry, a1, a2, err, "終わった" if done else "終わらない", (time.time() - t0) * 1000)

    def _await(self, name, code, slotnum, timeout, late=0.0):
        """完了を待ち、かかった時間と途中で片付けた別の完了を記録する。late > 0 なら、返らなくてもさらに待って遅れを記録する。"""
        t0 = time.time()
        _b, got, _s, _u, picked = self.card.await_cmd(code, C.slot_addr(slotnum), timeout=timeout)
        extra = []
        if got != code and late > 0:
            log.warning("%s: %.1f 秒で返らない。さらに %.0f 秒待って様子を見る", name, timeout, late)
            end = time.time() + late
            while time.time() < end:
                _b, got, _s, _u, p2 = self.card.await_cmd(code, C.slot_addr(slotnum), timeout=end - time.time())
                extra += p2
                if got == code:
                    break
        pk = picked + extra
        log.info("%s: %s（%.0f ms）%s", name, "返った" if got == code else "返らない", (time.time() - t0) * 1000,
                 ("  途中の完了: " + " ".join(f"0x{c:x}@0x{sl or 0:x}" for c, sl in pk)) if pk else "")
        return got == code

    def resolve31(self, label, wait):
        """副1 に届いた PAT/PMT から 0x31 要求を組む。間に合わなければ None（PMT 前の形で出す）。"""
        if self.req31 is not None:
            return self.req31
        t0 = time.time()
        while True:
            data = self.hb.sub1_ts()
            req = open31.find_req31(data, self.o.pn)
            if req is not None:
                open31.sub5_fields(req)
                self.req31 = req
                log.info("0x31 %s: %d パケットから PAT/PMT（%.1f 秒）: %s", label, len(data) // 188,
                         time.time() - t0, open31.describe(req))
                return req
            if time.time() - t0 >= wait:
                log.warning("0x31 %s: 番組 %s の PMT がまだ無い（%d パケット）→ PMT 前の形で出す",
                            label, open31.pn_str(self.o.pn), len(data) // 188)
                return None
            time.sleep(0.2)

    # ---- 文脈の欄の補い（実験時代の旗。要るかは未確認）----
    def _all_ctx(self):
        out = []
        for head in (C.CTX_LIST_HEAD, C.CTX_LIST_HEAD2):
            out += self.card.ctx_list(head)
        return out

    def force_628(self):
        c = self.card
        for ctx in self._all_ctx():
            base = (ctx & 0x1FFFFFFF) + 0x8000
            before = []
            for off in (0x628, 0x618, 0x5D8, 0x5D0):
                v = c.read32(base + off)
                before.append(f"+0x{off:x}={v:x}")
                if v != 1:
                    c.write32(base + off, 1)
            log.info("補い 0x%08x: 枠 %s → 全部 1", ctx, " ".join(before))

    def force_31c(self):
        c = self.card
        vals = {ctx: c.read32((ctx & 0x1FFFFFFF) + 0x31C) for ctx in self._all_ctx()}
        donor = next((v for v in vals.values() if v), 0)
        log.info("補い +0x31c: %s（写す値 0x%x）", " ".join(f"0x{k:08x}=0x{v:x}" for k, v in vals.items()), donor)
        if not donor:
            return
        for ctx, v in vals.items():
            if not v:
                c.write32((ctx & 0x1FFFFFFF) + 0x31C, donor)

    def force_4c_or(self, bits):
        c = self.card
        for ctx in self._all_ctx():
            a = (ctx & 0x1FFFFFFF) + 0x4C
            v = c.read32(a)
            c.write32(a, v | bits)
            log.info("補い 0x%08x: +0x4c 0x%08x → 0x%08x", ctx, v, v | bits)

    # ---- 本筋 ----
    def open(self, ch):
        """選局して復号つきの流れを開く。ロックしなければ StreamError。"""
        c, b2, o = self.card, self.card.bar2, self.o
        T0 = time.time()
        if o.tuner == 2:
            tuner = TunerMJ110(b2, load_mj110(self.vendor.tuner2_seq))
        else:
            tuner = Tuner(b2, load_seq(self.vendor.tuner_seq), others=o.tuner_others)
        tuner.verify()
        if self.hb_ext is not None:
            self.hb = hb = self.hb_ext
            hb.reset_scan()
        else:
            self.hb = hb = HostBuffers(self.dev, max(2, o.sub1_bufs), o.dma_iova, o.map_zero)

        # 0b. 0x1b（ホスト領域の登録）。0x3030 に制御ブロックの IOVA を置き、0 に戻れば受理
        if not o.primary:
            log.info("2 本目: 0x1b・PID 表・0x19 を飛ばす（カード全体の手順）")
        if o.primary:
            c.kick(hb.ctl1b, 0x1B)
            endt = time.time() + 2.0
            while time.time() < endt and b2.r32(C.CMD_ADDR_REG) != 0:
                time.sleep(0.001)
            if b2.r32(C.CMD_ADDR_REG) != 0:
                log.warning("0x1b: 0x3030 が 0 に戻らない")

        # 1. 選局
        nak = tuner.init()
        tuner.tune(ch)
        locked, cnr = tuner.wait_lock(o.lock_wait)
        if not locked:
            raise StreamError(f"ch{ch} でロックしない")

        # 3b. PID 表 128 段（カード全体。2 本目でやると 1 本目の PID を消す）
        bad = 0
        for i in (range(128) if o.primary else ()):
            b2.w32(0x1A24, 0)
            b2.w32(0x1A28, 0)
            b2.w32(0x1A2C, i)
            end = time.time() + 0.05
            while time.time() < end:
                if b2.r32(0x1A2C) == (0x80 | i):
                    break
            else:
                bad += 1
        if bad:
            log.warning("PID 表: %d/128 段が応答しない", bad)
        c.drain()

        # 4. 0x19 → 0x0b × 2 → 0x08 副1
        self.started = True
        # 完了は命令と枠で合わせて待つ。以前は「次のイベント」を待っていて、もう 1 本が流れているとその流れのデータの
        # イベントで待ちが素通りし、命令が完了を待たずに続けて出ていた（2026-11-13: 副1 の待ちが 0x0b の完了を拾った）
        if o.primary:
            self.cmd19()
            if not self._await("0x19", C.CMD_START, C.SLOT_START, 2.0):
                log.warning("0x19 の完了が来ない")
        for k in range(2):
            self.cmd0b()
            if not self._await(f"0x0b（{k + 1} 回目）", C.CMD_STEADY, 1, 2.0):
                raise StreamError("0x0b の完了が来ない")
        self.cmd08_sub1()
        if not self._await("0x08 副1", C.CMD_SUB_OPEN, self.o.slot_sub1, 3.0):
            raise StreamError("0x08 副1 の完了が来ない")
        self.resolve31("副1 の後", o.open31_wait)

        # 副5（下ごしらえ）
        # 副5 は区画の表から 2 つ借りてから鍵の枠（表 A・B の 2 個連続）を確保し、確保できないと借りた区画を返さずに
        # 失敗する（ファーム 0x801863b0 → 0x80186400 → 0x80186cd4）。失敗が重なると区画が尽き、以後どの 2 本目も開けない
        # （2026-11-17 の試験）。出す前の表を控え、文脈が返らなければ増えた分だけ返す
        pool_before = c.pool_flags()
        keys_before = c.key_bytes()
        self.cmd08_sub5()
        if not self._await("副5（下ごしらえ）", C.CMD_SUB_OPEN, self.o.slot_sub5, 4.0):
            log.warning("副5（下ごしらえ）の完了が来ない")
        # 引数域 +0x18 には出すときにホストの IOVA（ctl8b）を入れている。ファームが文脈を書き戻さなかったときに
        # それを文脈と思って閉じに行くとカードが止まる（2026-11-11）。文脈の番地のときだけ持つ
        self.prep_ctx = c.opened_ctx(C.arg_addr(self.o.slot_sub5))
        if self.prep_ctx is None:
            time.sleep(0.2)                          # 遅れて書き戻す回を待つ
            self.prep_ctx = c.opened_ctx(C.arg_addr(self.o.slot_sub5))
        if self.prep_ctx is None:
            self.sub5_failed = True
            ka, kb = c.key_slots()
            log.warning("副5 が文脈を返さない（理由 [gp-0x7544] = 0x%x）: 鍵の枠 A %s / B %s", c.read32(C.SUB5_ERR), ka, kb)
            fa, fb = c.key_release(keys_before)
            if fa or fb:
                log.warning("副5 が確保したまま失敗した鍵の枠 A %s / B %s を返した", fa, fb)
            after = c.pool_flags()
            if pool_before and after and pool_before[0] == after[0]:
                leaked = [i for i, (x, y) in enumerate(zip(pool_before[1], after[1])) if not x and y]
                if leaked:
                    c.pool_release(after[0], leaked)
                    log.warning("副5 が借りたまま失敗した区画 %s を返した", leaked)
            sa = C.slot_addr(self.o.slot_sub5)
            log.warning("副5 の引数域 +0x18 = 0x%08x（文脈の番地でない）・枠 +0x08..+0x1c = %s・引数 +0x86c = 0x%x",
                        c.read32(C.arg_addr(self.o.slot_sub5) + 0x18),
                        " ".join(f"{c.read32(sa + o):x}" for o in range(0x08, 0x20, 4)),
                        c.read32(C.arg_addr(self.o.slot_sub5) + 0x86C))

        # SECODE（と 0x15）
        ctx = 0
        for k in range(3):
            ctx = c.read32(C.arg_addr(self.o.slot_sub5) + 0x18)
            if C.is_ctx(ctx):
                break
            if k < 2:
                time.sleep(0.1)
        if not C.is_ctx(ctx):
            log.warning("副5 が文脈を返さない。SECODE を出さない")
        elif not o.secode:
            log.info("0x15・SECODE・三つ組を出さない（副5 は 0x31 の中身で 1 回だけ）")
        else:
            blob = self.secode_blob()
            if o.cmd15:
                self.cmd15(ctx)
                time.sleep(0.05)
            self.secode(ctx, blob)
            for rnd in range(2, o.triad + 1):
                nctx = self._triad(rnd, ctx, blob)
                if nctx is None:
                    break
                ctx = nctx
            if o.settle_final:
                time.sleep(o.settle_final)

        if o.ctx4c_or:
            sc = c.stream_ctx()
            log.info("流れの文脈 = %s（[文脈+0x4c] に 0x%x を足す）", "--" if not sc else f"0x{sc:08x}", o.ctx4c_or)
            if sc:
                a = (sc & 0x1FFFFFFF) + 0x4C
                c.write32(a, c.read32(a) | o.ctx4c_or)
        if o.force628:
            self.force_628()
        if o.force31c:
            self.force_31c()
        if o.force4c_or:
            self.force_4c_or(o.force4c_or)
        if not o.secode and o.secode_src:
            # 実験 11（2026-11-05）と同じく開き終えた後に写す。副5 の文脈が遅れて返る回も待つ
            self.copy_secode_fields(o.secode_src)
        log.info("ch%d を開いた（%.1f 秒・CNR %s・NAK %d）", ch, time.time() - T0,
                 "--" if cnr is None else f"0x{cnr:06x}", nak)

    def _triad(self, rnd, ctx, blob):
        """三つ組の 2 回目以降: 0x0d → 0x09 副5 →（2 回目は副1 を開き直す）→ 副5 → 0x15 → SECODE。戻り 新しい文脈 or None（打ち切り）。"""
        c, o = self.card, self.o
        if not c.ctx_close_safe(ctx):
            log.warning("三つ組 %d: 0x09 を出せない文脈 0x%08x。打ち切る", rnd, ctx)
            return None
        if o.vendor_0d:
            c.cmd0d()
            if not self._await(f"三つ組 {rnd} 0x0d", C.CMD_0D, C.SLOT_0D, 3.0, o.late_wait):
                log.warning("三つ組 %d: 0x0d が返らない。打ち切る", rnd)
                return None
        if o.clear_stop_flag:
            # ファームは SECODE の後しばらくして [文脈+0x3c] に bit14（停止の印）を立てる。立つ前に落としに行くと何も落とせず、
            # その後に立った印で 0x09 が返らなくなる（2026-11-04 実機: 間を空けない回は 0 のまま → 0x09 が 8 秒返らない・
            # 0.5 秒空けた回は 0x4000 → 0 で 0x09 が 8 ms）。実験では計器の読み出しがこの待ちになっていた。立つのを見てから落とす
            t0 = time.time()
            v = c.read32(ctx + 0x3C)
            while not v & 0x4000 and time.time() - t0 < o.stop_flag_wait:
                time.sleep(0.005)
                v = c.read32(ctx + 0x3C)
            c.write32(ctx + 0x3C, v & ~0x4000)
            log.info("三つ組 %d: [文脈+0x3c] 0x%08x → 0x%08x（bit14 を %s・%.0f ms 待った）", rnd, v, c.read32(ctx + 0x3C),
                     "落とした" if v & 0x4000 else "待ったが立たない", (time.time() - t0) * 1000)
        c.cmd09(self.o.slot_sub5, ctx)
        if not self._await(f"三つ組 {rnd} 0x09 副5", C.CMD_CLOSE, self.o.slot_sub5, 3.0, o.late_wait):
            log.warning("三つ組 %d: 0x09 副5 が返らない。打ち切る（枠 12 +0x10 = 0x%x・0x30e0 = 0x%08x・0x3030 = 0x%08x）",
                        rnd, c.slot_status(self.o.slot_sub5), c.bar2.r32(C.EVT_REG), c.bar2.r32(C.CMD_ADDR_REG))
            return None
        if rnd == 2 and self.o.primary:
            # 「今の流れの文脈」はカード全体のもの。2 本目がこれを閉じると 1 本目の副1 を閉じかねないので、1 本目だけ
            c1 = c.stream_ctx()
            if c1 and c.ctx_close_safe(c1):
                c.cmd09(self.o.slot_sub1, c1)
                self._await(f"三つ組 {rnd} 0x09 副1", C.CMD_CLOSE, self.o.slot_sub1, 3.0)
                self.cmd08_sub1()
                self._await(f"三つ組 {rnd} 副1", C.CMD_SUB_OPEN, self.o.slot_sub1, 4.0)
            else:
                log.info("三つ組 %d: 副1 は閉じない（0x%08x）", rnd, c1 or 0)
        self.cmd08_sub5(pid7c=o.triad_pid & 0x1FFF, set5c=0x1000)
        ok5 = self._await(f"三つ組 {rnd} 副5", C.CMD_SUB_OPEN, self.o.slot_sub5, 4.0)
        nctx = c.read32(C.arg_addr(self.o.slot_sub5) + 0x18)
        if not ok5 or nctx in (0, self.hb.ctl8b):
            log.warning("三つ組 %d: 副5 が開かない。打ち切る", rnd)
            return None
        self.prep_ctx = nctx
        if o.cmd15:
            self.cmd15(nctx)
            time.sleep(0.05)
        self.secode(nctx, blob)
        return nctx

    def steady(self, sink=None):
        """0x0b の定常運転（ts_seconds 秒・流れが止まれば早めに抜ける）。副1 の TS は sink へ（既定は捨てる）。"""
        c, hb, o = self.card, self.hb, self.o
        t0 = last_data = time.time()
        i = -1
        total = 0
        # 最初の定常 0x0b（+0x1bc = 1）でファームは B-CAS カードを起動し直す。warm の連続では、起動し直した後に
        # リセット応答が届かず準備完了が立たない回がある（その回は ECM を 1 つも受けず、40 秒流しても復号しない。2026-11-04 実測）。
        # 立たなければ、もう一度起動し直す（失敗した回の次の起動し直しは通っていた）
        reset_at, reset_lap, resets, ready = None, 0, 0, False
        late0b = 0
        # 復号が始まったか: 副5 の文脈の dec の環（汲み出しと同じ記述子）の書き込み位置 [記述子+0x24] が動くか
        dec_desc = self._dec_desc() if o.steady_until_dec else None
        dec_pos0 = None
        while True:
            el = time.time() - t0
            if el >= o.ts_seconds and (ready or el >= o.ts_seconds + o.card_extra):
                break
            i += 1
            if reset_at is not None and not ready and time.time() - reset_at >= o.card_ready_wait:
                if resets < o.card_resets and o.card_reset:
                    resets += 1
                    self.card_reset_next = True
                    log.info("定常運転: カードの準備完了が %.1f 秒立たない → 起動し直す（%d 回目）",
                             time.time() - reset_at, resets)
                else:
                    reset_at = None              # 上限に達した。もう見張らない
            if (self.steady_n == 0 and o.card_reset) or self.card_reset_next:
                reset_at, reset_lap = time.time(), i
            elif self.steady_n == 0:
                reset_lap = -1                   # 起動し直さない（他方が使っている B-CAS カードの印をそのまま見る）
            self.cmd0b(steady=True)
            if c.await_cmd(C.CMD_STEADY, C.slot_addr(1), timeout=2.0)[1] is None:
                late0b += 1
            c.drain(rounds=4, timeout=o.ts_wait)
            c.bufs_drain()
            last_b = None
            for _ in range(512):
                r = c.bufs_drain()
                if not r or r[0] == last_b:
                    break
                last_b = r[0]
            if self.ring_ctx is None and i >= 1:
                self.ring_ctx = c.stream_ctx()
            # 起動し直した周回の読みは前の回の印が残っていることがあるので、次の周回から見る
            if not ready and i > reset_lap and c.read32(CARD_READY) & 2:
                ready = True
                log.info("定常運転: カードの準備完了（起動し直してから %.2f 秒・起動し直し %d 回）",
                         time.time() - (reset_at or t0), resets)
            if ready and dec_desc:
                pos = c.read32(dec_desc + 0x24)
                if dec_pos0 is None:
                    dec_pos0 = pos
                elif pos != dec_pos0:
                    log.info("定常運転: 復号が始まった（%.2f 秒・周回 %d）→ 汲み出しへ", time.time() - t0, i + 1)
                    break
            newpkt = sum(hb.take_packets(bo, bs, sink) for bo, bs in hb.scan)
            total += newpkt
            if newpkt:
                last_data = time.time()
            elif time.time() - last_data >= 2.0:
                # 実験の「dry >= 2 or 無データ 2 秒」は、dry が止まらない周回ごとに 0 へ戻るので、実際は無データ 2 秒だけで決まる
                log.info("定常運転: 流れが止まった（周回 %d）。作り直さずに抜ける", i + 1)
                break
        if late0b:
            log.warning("定常運転: 0x0b の完了が 2 秒で来なかった周回 %d", late0b)
        if not ready:
            log.warning("定常運転: カードの準備完了が立たないまま（起動し直し %d 回）。この回は復号しない見込み", resets)
        log.info("定常運転: %.1f 秒・周回 %d・副1 %d パケット", time.time() - t0, i + 1, total)

    def _dec_desc(self):
        """副5 の文脈の dec の環の記述子（汲み出しと同じ [文脈+0x8000+0xEC]）。読めなければ None。"""
        c = self.card
        sub5 = self.prep_ctx or c.opened_ctx(C.arg_addr(self.o.slot_sub5))
        if not sub5:
            return None
        desc = c.read32(sub5 + 0x8000 + 0xEC)
        return desc if _is_card_ptr(desc) else None

    def copy_secode_fields(self, src_ctx):
        """2 本目: SECODE が文脈に書く欄を src_ctx（1 本目の副5 の文脈）から自分の副5 の文脈へ写す。
        副5 の文脈がカードの番地として読めるまで待つ（待ちが早く切れてホストの IOVA を文脈と取り違え、関係ない番地に書いて
        カードを止めたことがある）。読めなければ StreamError。"""
        c = self.card
        dst = None
        for _ in range(30):
            v = c.read32(C.arg_addr(self.o.slot_sub5) + 0x18)
            if C.is_ctx(v) and v != src_ctx:
                dst = v
                break
            time.sleep(0.1)
        if dst is None or not C.is_ctx(src_ctx):
            raise StreamError(f"SECODE の欄を写せない（元 0x{src_ctx:08x}・先 {dst}）")
        self.prep_ctx = dst
        s0, d0 = src_ctx & 0x1FFFFFFF, dst & 0x1FFFFFFF
        moved = 0
        for off in SECODE_CTX_FIELDS:
            v = c.read32(0x80000000 | (s0 + off))
            if s0 <= (v & 0x1FFFFFFF) < s0 + CTX_SPAN:
                v = (v & 0xE0000000) | (d0 + ((v & 0x1FFFFFFF) - s0))
                moved += 1
            c.write32(0x80000000 | (d0 + off), v)
        log.info("SECODE の欄を写した: %d 語（付け替え %d）0x%08x → 0x%08x", len(SECODE_CTX_FIELDS), moved, src_ctx, dst)

    def pump(self, out, secs=-1.0):
        """出口の環（exit・平文の PSI/SI）と復号済みの環（dec・映像/音声）を読み、exit → dec の順に out へ書く。
        secs < 0 なら STOP（SIGTERM / SIGINT）か読み手が閉じるまで。戻り 書いたバイト数。"""
        c, b2, o = self.card, self.card.bar2, self.o
        if STOP or self.stop.is_set():
            log.info("汲み出しの前に止める指示を受けていた → 汲み出さずに閉じる")
            return 0
        sub5 = self.prep_ctx or c.opened_ctx(C.arg_addr(self.o.slot_sub5))
        if not sub5:
            raise StreamError("副5 の文脈が分からない")
        stream = c.read32(sub5 + 0x8000 + 0x5B8)
        descs = [("exit", c.read32(stream + 0x330) if _is_card_ptr(stream) else 0),
                 ("dec", c.read32(sub5 + 0x8000 + 0xEC))]
        rings = []
        for name, desc in descs:
            if not _is_card_ptr(desc):
                log.warning("%s: 記述子が読めない（0x%08x）", name, desc)
                continue
            base = c.read32(desc + 0x30) & 0x1FFFFFFF
            length = c.read32(desc + 0x34)
            if not (length and length % 188 == 0):
                length = c.read32(desc + 0x38) + 1
            if not (length % 188 == 0 and length <= 0x100000):
                log.warning("%s: 環の形が想定外（基点 0x%08x 長さ 0x%x）", name, base, length)
                continue
            rings.append({"name": name, "desc": desc, "base": base, "len": length, "win": base >> 20,
                          "carry": b"", "total": 0, "laps": 0, "sync": False, "resync": 0, "dropped": 0})
        if not rings:
            raise StreamError("環が 1 つも読めない")

        def pos(r):
            # 書き込み位置（記述子 +0x24）。「最後に書いたバイト」か「次に書く位置」かは 188 の境界に合う方
            raw = c.read32(r["desc"] + 0x24) & 0x1FFFFFFF
            a = (raw + 1 - r["base"]) % r["len"]
            b = (raw - r["base"]) % r["len"]
            return a if a % 188 == 0 or b % 188 != 0 else b

        map_addr = c.bar0.addr
        fr = _fastread() if o.pump_read else None
        if o.pump_read and fr is None:
            log.warning("fastread.so が無い → string_at で読む（遅い。make で作る）")
        rbuf = ctypes.create_string_buffer(0x100000)

        def grab(r, a, n):
            with c.lock:
                return _grab(r, a, n)

        def _grab(r, a, n):
            # 環が 1 MB の窓の境目をまたぐことがある（2 本目の dec の環 0x00ad6000＋0x5e000）。窓ごとに分けて読む
            parts = []
            phys = r["base"] + a
            while n > 0:
                win, off = phys >> 20, phys & 0xFFFFF
                k = min(n, 0x100000 - off)
                cur = b2.r32(C.WINDOW_REG)
                b2.w32(C.WINDOW_REG, (cur & ~C.WINDOW_MASK) | ((win << 8) & C.WINDOW_MASK))
                b2.r32(C.WINDOW_REG)
                if fr is None:
                    parts.append(ctypes.string_at(map_addr + off, k))
                else:
                    fr(rbuf, map_addr, off, k, o.pump_read)
                    parts.append(ctypes.string_at(rbuf, k))
                phys += k
                n -= k
            return b"".join(parts)

        for r in rings:
            r["rp"] = pos(r)
        log.info("汲み出し: %s", "  ".join(f"{r['name']} 0x{r['base']:08x}+0x{r['len']:x}" for r in rings))

        # 書き出しは別のスレッドで。読み手（橋 → Mirakurun）が一瞬詰まると書き込みで止まり、その間にカードの環
        # （約 0.2 秒ぶん）が追い越されてパケットが抜けた（2026-11-05・NHK 総合で映像の 0.45% が抜け、絵に帯が出た）。
        # 読むスレッドは止めずに環を追い、書けない分はメモリに溜める。
        q = queue.Queue()
        st = {"pending": 0, "max_pending": 0, "max_block": 0.0, "dead": False}
        lock = threading.Lock()

        def writer():
            while True:
                b = q.get()
                if b is None:
                    break
                t = time.time()
                try:
                    out.write(b)
                    out.flush()
                except (BrokenPipeError, OSError):
                    st["dead"] = True
                    break
                st["max_block"] = max(st["max_block"], time.time() - t)
                with lock:
                    st["pending"] -= len(b)

        wt = threading.Thread(target=writer, name="pump-writer", daemon=True)
        wt.start()
        # 試し（診断）: 環境変数 DTH50_RING_DUMP=接頭辞 で、環ごとの中身も 接頭辞-exit.ts / 接頭辞-dec.ts に書く
        prefix = os.environ.get("DTH50_RING_DUMP")
        dumps = {r["name"]: open(f"{prefix}-{r['name']}.ts", "wb") for r in rings} if prefix else {}
        t0 = time.time()
        written = 0
        last_lap, max_gap, late = None, 0.0, 0
        last_move, stalled = time.time(), False
        try:
            while not STOP and not self.stop.is_set() and not st["dead"] and (secs < 0 or time.time() - t0 < secs):
                now = time.time()
                if last_lap is not None:
                    gap = now - last_lap
                    max_gap = max(max_gap, gap)
                    if gap > o.pump_late:
                        late += 1
                last_lap = now
                moved = False
                for r in rings:
                    wp, rp, ln = pos(r), r["rp"], r["len"]
                    if wp == rp:
                        continue
                    moved = True
                    data = grab(r, rp, wp - rp) if wp > rp else grab(r, rp, ln - rp) + grab(r, 0, wp)
                    r["total"] += len(data)
                    if len(data) > ln * 3 // 4:
                        r["laps"] += 1           # 1 回の間に環の 3/4 以上進んだ＝取りこぼしの恐れ
                    r["rp"] = wp
                    # 書き込み位置がパケットの途中の周回がある。188 の区切りまで書き、端数は持ち越す
                    buf = r["carry"] + data
                    # 最初の読みは書き込み位置から始まるので、パケットの途中から始まることがある。そのまま 188 ずつ切ると
                    # 流れ全体の区切りがずれる（2026-11-17 の試験で 3 回・回の全部がずれていた）。区切りを探して合わせ、
                    # 区切りの頭が 0x47 でなくなったら探し直す
                    if not r["sync"] or buf[:1] != b"\x47":
                        k = _find_sync(buf)
                        if k is None:
                            r["carry"] = buf[-(188 * 3):]
                            r["dropped"] += max(0, len(buf) - 188 * 3)
                            continue
                        if r["sync"]:
                            r["resync"] += 1
                        r["dropped"] += k
                        r["sync"] = True
                        buf = buf[k:]
                    whole = len(buf) - len(buf) % 188
                    r["carry"] = buf[whole:]
                    if whole and r["name"] == "exit":
                        # exit の環は平文の PID の素通し。カードに渡さない ES（データ放送など）は暗号化されたまま
                        # 通ってくるので捨てる（EPGStation の「スクランブル」の数が増えないように・2026-11-18）
                        kept = _drop_scrambled(buf[:whole], r.setdefault("scr_pids", collections.Counter()))
                        r["scrambled"] = r.get("scrambled", 0) + (whole - len(kept)) // 188
                        buf, whole = kept + r["carry"], len(kept)
                        r["carry"] = buf[whole:]
                    if whole:
                        with lock:
                            st["pending"] += whole
                            st["max_pending"] = max(st["max_pending"], st["pending"])
                        q.put(buf[:whole])
                        if dumps:
                            dumps[r["name"]].write(buf[:whole])
                    written += whole
                if moved:
                    last_move = now
                elif o.pump_stall and now - last_move >= o.pump_stall:
                    # カードが流れを出さなくなった。黙って汲み続けると、読み手（Mirakurun → EPGStation）は接続が残ったまま
                    # 何も来ない状態で待ち続ける（2026-11-11 の録画は 10 分空振りした）。終えて読み手に知らせる
                    stalled = True
                    log.error("汲み出し: どの環も %.0f 秒進まない → 流れが止まったとして終える", now - last_move)
                    break
                time.sleep(o.pump_poll)
        finally:
            q.put(None)
            wt.join(timeout=5.0)
            for f in dumps.values():
                f.close()
        if st["dead"]:
            log.info("読み手が閉じた → 汲み出しを止めた")
        self.stalled = stalled
        el = max(time.time() - t0, 1e-6)
        log.info("汲み出しの間隔: 最大 %.0f ms・%.0f ms を超えた回 %d（環を追い越された恐れ）・書き出しの待ち 最長 %.0f ms・"
                 "溜まり 最大 %.1f MB", max_gap * 1000, o.pump_late * 1000, late, st["max_block"] * 1000,
                 st["max_pending"] / 1e6)
        for r in rings:
            log.info("%s: %d 本（%.1f Mbps）%s%s%s", r["name"], r["total"] // 188, r["total"] * 8 / el / 1e6,
                     (f"・暗号化されたままで捨てた {r['scrambled']} 本（"
                      + " ".join(f"{p:04x}:{n}" for p, n in r["scr_pids"].most_common(6)) + "）") if r.get("scrambled") else "",
                     f"・取りこぼしの恐れ {r['laps']} 回" if r["laps"] else "",
                     f"・区切りを合わせ直した {r['resync']} 回（捨てた {r['dropped']} バイト）" if r["resync"] else "")
        return written

    def close(self, label="（終了処理）", last=None):
        """XP の形で閉じる（副5 → 副1 → 0x1a）。次の起動が warm で 0x19 から始められるように。
        last = この後カードに流れが残らないか（既定: 1 本目なら真）。偽なら自分の枠の文脈だけを閉じ、0x1a（カード全体が
        止まる）は出さない。閉じた後に IOMMU の割り当てを外す。"""
        try:
            return self._close_card(label, last)
        finally:
            if self.hb is not None and self.hb_ext is None:
                self.hb.close()
            self.hb = None

    def _close_card(self, label, last):
        c = self.card
        if last is None:
            last = self.o.primary
        if not self.started:
            return True
        s5 = self.prep_ctx if C.is_ctx(self.prep_ctx) else c.opened_ctx(C.arg_addr(self.o.slot_sub5))
        s1 = c.opened_ctx(C.arg_addr(self.o.slot_sub1))
        if not self.o.primary or not last:
            # 推測で他方の文脈を拾わない
            return close_like_xp(c, s5, s1 if s1 != s5 else None, stop1a=last, vendor_0d=self.o.vendor_0d,
                                 clear_stop_flag=self.o.clear_stop_flag, label=label,
                                 slot_sub5=self.o.slot_sub5, slot_sub1=self.o.slot_sub1)
        if not s1 or s1 == s5:
            s1 = c.stream_ctx() or self.ring_ctx
        if not s1 or s1 == s5:
            s1 = next((x for x in c.ctx_list2() if x != s5), s1)
        return close_like_xp(c, s5, s1, vendor_0d=self.o.vendor_0d,
                             clear_stop_flag=self.o.clear_stop_flag, label=label,
                             slot_sub5=self.o.slot_sub5, slot_sub1=self.o.slot_sub1)
