"""カードとのやりとりの土台: 窓越しの読み書き・コマンドの起動と完了待ち・イベント・バッファの受け渡し。

実験用リポジトリ（非公開）の tools/s4-boot.py（read_window / write_window / write_window_words / cmd_complete / cmd_await /
evt_wait / evt_drain / bufs_drain / ts_status / opened_ctx と各 ts_cmd* の起動の形）から移した。
"""
import logging
import struct
import threading
import time

log = logging.getLogger("dth50.card")

# ---- BAR2 のレジスタ ----
WINDOW_REG = 0x43C          # ビット 8..14 が BAR0 の窓（1 MB 単位）の番号
WINDOW_MASK = 0x7F00
WINDOW_BYTES = 1 << 20
COUNTER_REG = 0x6130        # 自走カウンタ。冷えたカードでは 0 のまま動かない

CMD_CODE_REG = 0x3020       # コマンド番号。書くと起動する
CMD_ADDR_REG = 0x3030       # 枠のカード側アドレス
EVT_REG = 0x30E0            # bit31 = 発生・下位 = 種別のビット。書いたビットが消える
CMD_DONE = 0x302C           # 完了したコマンドの番号
SLOT_DONE = 0x3048          # 完了した枠のアドレス
BUF_STAT = 0x3028           # 0xf = データあり / 5 = 待機
BUF_GET = 0x3040            # 埋まったバッファ（カード → ホスト）
BUF_PUT = 0x3038            # 返すバッファ（ホスト → カード）
BUF_ACK = 0x3024            # 受領の確認
EVT_DATA = 0x01
EVT_CMD = 0x02

# ---- コマンドの枠（カード内メモリ）。枠 n = 0x1840 + n * 0xAFC、引数域 = 枠 + 0x24 ----
KEY_TAB_A = 0x802023CC       # 鍵の枠の表 A（0x1f バイト・0 以外が使用中）。0x8012b3bc(n) が n 個連続の空きを探す
KEY_TAB_B = 0x802023EC       # 表 B（同じ形・0x8012b4ac）。副5 の復号路は A・B から 2 個ずつ借りる（0x8012e034）
SUB5_ERR = 0x8021F1DC        # [gp-0x7544]: 副5 の処理（0x801860a8）が失敗の理由を置く（2 / 6 / 0xe / 0x10 / 0x11 / 0x12 …）
POOL_TAB = 0x8021FB88        # [gp-0x6b98]: 区画の貸し出し表のアドレス（gp = 0x80226720）
POOL_LAST = 0x8021FA80       # [gp-0x6ca0]: 表の最後の番号
SLOT_BASE = 0x1840
SLOT_STRIDE = 0xAFC
ARG_OFF = 0x24
TS_BUF = 0x2AF010           # カード内の構造体（ファーム所有。書いてはいけない）。バッファの番号としては返さない

# 使う枠（番号）。0x19/0x1a は枠 0、0x0d は枠 2、0x08 副1 は枠 5、0x15 と SECODE は枠 6、0x08 副5 は枠 12
SLOT_START = 0
SLOT_0D = 2
SLOT_SUB1 = 5
SLOT_SEL = 6
SLOT_SUB5 = 12

CMD_SUB_OPEN = 0x08
CMD_CLOSE = 0x09
CMD_STEADY = 0x0B
CMD_0D = 0x0D
CMD_SELECT = 0x15
CMD_START = 0x19
CMD_STOP = 0x1A

# 文脈の輪の番人。表 1 は 0x19 が作る・表 2 は副 6/4/0 が作る
CTX_LIST_HEAD = 0x802473F8
CTX_LIST_HEAD2 = 0x8023AB58
FW_GP = 0x80226720          # ファームの gp


def slot_addr(n):
    return SLOT_BASE + n * SLOT_STRIDE


def arg_addr(n):
    return slot_addr(n) + ARG_OFF


def slot_num(addr):
    """枠のアドレス → 枠番号（枠でなければ -1）。"""
    if addr is None or addr < SLOT_BASE:
        return -1
    n, r = divmod(addr - SLOT_BASE, SLOT_STRIDE)
    return n if r == 0 else -1


def is_ctx(v):
    """ファームが返す流れの文脈の番地か（0xa02xxxxx / 0x802xxxxx）。0・ホストの IOVA・埋め草を閉じる命令に渡すと
    ファームが応答しなくなる（2026-11-11: 副5 が文脈を返さなかった回に、引数域に残ったホストの IOVA 0x30548000 へ 0x09 を
    出してカードが止まった。PCIe のリセットはチップに届かないので、止まるとマシンの再起動まで戻らない）。"""
    return v is not None and (v & 0xFFF00000) in (0xA0200000, 0x80200000)


class Card:
    def __init__(self, bar0, bar2):
        self.bar0, self.bar2 = bar0, bar2
        # 窓レジスタは 1 つしかない。2 本の流れ（汲み出しのスレッドと、もう一方を開く手順）が同時に窓を動かすと
        # 互いの読み書きが別の番地へ行くので、窓の切り替えと読み書きをまとめて排他する
        self.lock = threading.RLock()

    # ---- 窓越しの読み書き（MIPS の物理アドレス。kseg0/kseg1 の上位は窓の計算で落ちる）----
    def _window(self, mips_addr):
        b2 = self.bar2
        cur = b2.r32(WINDOW_REG)
        b2.w32(WINDOW_REG, (cur & ~WINDOW_MASK) | (((mips_addr >> 20) << 8) & WINDOW_MASK))
        b2.r32(WINDOW_REG)
        return mips_addr & 0xFFFFF

    def read(self, mips_addr, length):
        with self.lock:
            offs = self._window(mips_addr)
            u = self.bar0.u32
            return b"".join(struct.pack("<I", u[(offs >> 2) + i]) for i in range(length // 4))

    def read32(self, mips_addr):
        return struct.unpack("<I", self.read(mips_addr, 4))[0]

    def write(self, mips_addr, data):
        if mips_addr & 3 or len(data) & 3:
            raise ValueError("4 バイト境界・4 の倍数の長さだけ")
        with self.lock:
            offs = self._window(mips_addr)
            u = self.bar0.u32
            for i, w in enumerate(struct.unpack(f"<{len(data) // 4}I", data)):
                u[(offs >> 2) + i] = w

    def write32(self, mips_addr, val):
        self.write(mips_addr, struct.pack("<I", val & 0xFFFFFFFF))

    def write_words(self, mips_addr, table):
        """飛び飛びの語だけを書く（{オフセット: 値}）。ベンダは引数域を一様には埋めず、書いた語だけを書く。
        0 で塗ると、ファームが持っている欄まで消える。"""
        if not table:
            return
        with self.lock:
            base = self._window(mips_addr)
            u = self.bar0.u32
            for off, val in sorted(table.items()):
                u[(base + off) >> 2] = val & 0xFFFFFFFF

    # ---- コマンド ----
    def issue(self, slotnum, code, args=None, tag=None):
        """枠 slotnum でコマンド code を起動する。args（{オフセット: 値}）を引数域に書いてから、
        枠（+0x08 = 1・+0x0C = code・+0x14 = 引数域・+0x1C = 枠番号）を書き、0x3030 → 0x3020 の順に叩く。
        tag は +0x1C に枠番号と違う値を入れるとき（0x15 は枠 6 で +0x1C = 0x0D）。"""
        sa = slot_addr(slotnum)
        stale = self.flush_events()
        if stale:
            log.warning("命令 0x%x（枠 %d）の前に取り残しの完了: %s", code, slotnum,
                        " ".join(f"0x{c:x}@枠{slot_num(s)}" for c, s in stale))
        if args:
            self.write_words(arg_addr(slotnum), args)
        self.write_slot(slotnum, code, tag)
        self.kick(sa, code)
        return sa

    def write_slot(self, slotnum, code, tag=None):
        slot = bytearray(0x20)
        struct.pack_into("<I", slot, 0x08, 1)
        struct.pack_into("<I", slot, 0x0C, code)
        struct.pack_into("<I", slot, 0x14, arg_addr(slotnum))
        struct.pack_into("<I", slot, 0x1C, slotnum if tag is None else tag)
        self.write(slot_addr(slotnum), bytes(slot))

    def kick(self, addr, code):
        self.bar2.w32(CMD_ADDR_REG, addr)
        self.bar2.r32(CMD_ADDR_REG)
        self.bar2.w32(CMD_CODE_REG, code)

    # ---- 名前の付いたコマンド ----
    def cmd09(self, slotnum, ctx):
        """0x09（流れを閉じる）。引数 {0: 文脈, 4: 0}。"""
        return self.issue(slotnum, CMD_CLOSE, {0x00: ctx, 0x04: 0})

    def cmd0d(self, kind=1, size=0x10000, buf=0xA02AF010):
        """0x0d（枠 2）。引数 {0: バッファ, 8: 大きさ, 0x18: 種別}。種別 1 は 0x10000。"""
        return self.issue(SLOT_0D, CMD_0D, {0x00: buf, 0x08: size, 0x18: kind})

    def cmd1a(self):
        """0x1a（停止・枠 0）。引数域は 0x19 のもの（書き直さない）。"""
        return self.issue(SLOT_START, CMD_STOP)

    def last_code(self, slotnum):
        """枠に最後に書かれたコマンド番号（+0x0C）。"""
        return self.read32(slot_addr(slotnum) + 0x0C)

    def ctx_list2(self, limit=16):
        """表 2 に繋がっている文脈（カード内のアドレス・kseg1）を並べる。"""
        return self.ctx_list(CTX_LIST_HEAD2, limit)

    def ctx_list(self, head, limit=16):
        """文脈の輪（表 1 = 0x19 が作る / 表 2 = 副 6/4/0 が作る）に繋がっている文脈を並べる。"""
        p = self.read32(head)
        out, seen = [], set()
        if p == 0:
            return out
        for _ in range(limit):
            if p == head or (p & 0x1FFFFFFF) == (head & 0x1FFFFFFF) or p in seen:
                break
            seen.add(p)
            out.append((p & 0x1FFFFFFF) | 0xA0000000)
            p = self.read32(p & 0x1FFFFFFF)
        return out

    def ctx_close_safe(self, ctx):
        """0x09 を出してよい文脈か。+0x58（輪の頭）がカードの番地でないものに出すとファームが落ちる。"""
        if not ctx:
            return False
        if ctx == 0xA02CD238:      # 副5 / SECODE の文脈。表 2 から選び直させる
            return False
        base = ctx & 0x1FFFFFFF
        head = self.read32(base + 0x58)
        if (head & 0x1FFFFFFF) == base + 0x54:
            return True             # 空の輪（正常）
        return (head >> 28) in (0x8, 0xA, 0xB)

    def ctx_close_pick(self, preferred=None):
        if self.ctx_close_safe(preferred):
            return preferred
        head = CTX_LIST_HEAD2
        p = self.read32(head)
        seen = set()
        for _ in range(16):
            if not p or p == head or p in seen or (p & 0x1FFFFFFF) == (head & 0x1FFFFFFF):
                break
            seen.add(p)
            cand = (p & 0x1FFFFFFF) | 0xA0000000
            if self.ctx_close_safe(cand):
                return cand
            p = self.read32(p & 0x1FFFFFFF)
        return None

    def stream_ctx(self):
        """今の流れの文脈（[gp-0x6c94] → +0x1c → +0x18）。辿れなければ None。"""
        root = self.read32(FW_GP - 0x6C94)
        if not root:
            return None
        o1c = self.read32(((root + 0x1C) & 0x1FFFFFFF) | 0xA0000000)
        if not o1c:
            return None
        return self.read32(((o1c & 0x1FFFFFFF) | 0xA0000000) + 0x18)

    def slot_status(self, slotnum):
        return self.read32(slot_addr(slotnum) + 0x10)

    def pool_usage(self):
        """ファームの区画の貸し出し表（0x80152854 が貸し、0x80152dbc が返す）の使用数。戻り (使用, 全体) か None。
        表 = [gp-0x6b98]（8 バイト × ([gp-0x6ca0] + 1)・頭の語が 0 以外なら貸出中）、gp = 0x80226720。
        副5 はこの表から借り、借りられないと文脈を返さずに終わる（0x801863b0）。読むだけ。"""
        tab = self.read32(POOL_TAB)
        n = self.read32(POOL_LAST) + 1
        if not (tab >> 28) in (0x8, 0xA) or not 0 < n <= 4096:
            return None
        used = 0
        for i in range(0, n, 128):
            k = min(128, n - i)
            blk = self.read(tab + i * 8, k * 8)
            used += sum(1 for j in range(k) if struct.unpack_from("<I", blk, j * 8)[0])
        return used, n

    def pool_flags(self):
        """区画の表の各項目の使用中の印。戻り (表のアドレス, [印, ...]) か None。"""
        tab = self.read32(POOL_TAB)
        n = self.read32(POOL_LAST) + 1
        if not (tab >> 28) in (0x8, 0xA) or not 0 < n <= 4096:
            return None
        flags = []
        for i in range(0, n, 128):
            k = min(128, n - i)
            blk = self.read(tab + i * 8, k * 8)
            flags += [struct.unpack_from("<I", blk, j * 8)[0] for j in range(k)]
        return tab, flags

    def pool_release(self, tab, idxs):
        """区画の表の項目 idxs の使用中の印を 0 に戻す。副5 が借りたまま失敗した分だけに使う。"""
        for i in idxs:
            self.write32(tab + i * 8, 0)

    def key_bytes(self):
        """鍵の枠の表 A・B の生のバイト（各 0x20）。"""
        return self.read(KEY_TAB_A, 32), self.read(KEY_TAB_B, 32)

    def key_release(self, before):
        """before（key_bytes の控え）から増えた鍵の枠の使用中の印を 0 に戻す。戻り ([A の番号], [B の番号])。
        副5 が失敗したときに、確保したまま返さなかった分だけを返すのに使う（他方の流れは動いている間に鍵の枠を増やさない）。"""
        now = self.key_bytes()
        freed = []
        for base, old, cur in ((KEY_TAB_A, before[0], now[0]), (KEY_TAB_B, before[1], now[1])):
            idx = [i for i in range(0x1F) if not old[i] and cur[i]]
            freed.append(idx)
            for w in sorted({i & ~3 for i in idx}):
                word = bytearray(cur[w:w + 4])
                for i in idx:
                    if w <= i < w + 4:
                        word[i - w] = 0
                self.write(base + w, bytes(word))
        return freed[0], freed[1]

    def key_slots(self):
        """鍵の枠の表 A・B で使用中の番号。戻り ([A の番号], [B の番号])。"""
        a = self.read(KEY_TAB_A, 32)
        b = self.read(KEY_TAB_B, 32)
        return [i for i in range(0x1F) if a[i]], [i for i in range(0x1F) if b[i]]

    def opened_ctx(self, argaddr):
        """開いた流れの文脈（引数域 +0x18 にファームが書き戻す）。カード内のアドレスでなければ None。"""
        v = self.read32(argaddr + 0x18)
        return v if is_ctx(v) else None

    # ---- イベント・完了 ----
    def evt_wait(self, timeout=1.0):
        """イベントを待つ（少なくとも 1 回は読む。timeout=0 なら溜まっている分を見るだけ）。"""
        end = time.time() + timeout
        while True:
            e = self.bar2.r32(EVT_REG) & 0x7FFFFFFF
            if e:
                return e
            if time.time() >= end:
                return 0
            time.sleep(0.0005)

    @staticmethod
    def buf_valid(b):
        return bool(b) and (b & 0x1FFFFFFF) != TS_BUF

    def complete(self, timeout=1.0):
        """イベントを 1 つ受けて片付ける。戻り (種別, 完了コマンド, 完了枠, 返されたバッファ)。"""
        b2 = self.bar2
        bits = self.evt_wait(timeout)
        if not bits:
            return 0, None, None, None
        code = slot = buf = None
        if bits & EVT_CMD:
            code = b2.r32(CMD_DONE)
            b2.w32(EVT_REG, EVT_CMD)
        if bits & EVT_DATA:
            b2.w32(EVT_REG, EVT_DATA)
        if bits & EVT_CMD:
            slot = b2.r32(SLOT_DONE)
        if bits & EVT_DATA:
            st = b2.r32(BUF_STAT)
            if st:
                buf = b2.r32(BUF_GET)
                if self.buf_valid(buf):
                    b2.w32(BUF_PUT, buf)
                b2.w32(BUF_ACK, st)
        return bits, code, slot, buf

    def await_cmd(self, want_code, want_slot=None, timeout=5.0):
        """コマンド want_code（枠 want_slot）の完了を timeout 秒まで待つ。途中の別の完了・データのイベントは片付ける。
        戻り (種別, 完了コマンド, 完了枠, バッファ, 片付けた完了の一覧)。

        以前は「イベント 12 回」で諦めていた。もう 1 本が流れていると、その流れのデータのイベントが数 ms で 12 回来て、
        待つ前に諦め、本当の完了が後の別の待ちに拾われていた（2026-11-13 の同時受信でカードが止まった回）。"""
        picked = []
        t0 = time.time()
        while True:
            left = timeout - (time.time() - t0)
            if left <= 0:
                break
            bits, code, slot, buf = self.complete(timeout=min(left, 0.2))
            if code is not None:
                if code == want_code and (want_slot is None or slot == want_slot):
                    return bits, code, slot, buf, picked
                picked.append((code, slot))
        return 0, None, None, None, picked

    def flush_events(self, limit=256):
        """溜まっているイベントを待たずに片付ける（バッファは complete と同じく返す）。戻り 片付けた完了 [(コマンド, 枠のアドレス)]。"""
        stale = []
        for _ in range(limit):
            bits, code, slot, _buf = self.complete(timeout=0)
            if not bits:
                break
            if code is not None:
                stale.append((code, slot))
        return stale

    def drain(self, rounds=8, timeout=0.2):
        got = []
        for _ in range(rounds):
            bits, _code, _slot, buf = self.complete(timeout)
            if not bits:
                break
            if buf is not None:
                got.append(buf)
        return got

    def bufs_drain(self):
        """埋まったバッファを 1 つ受け取って返す（ベンダは BUF_GET を 2 回読む）。"""
        b2 = self.bar2
        st = b2.r32(BUF_STAT)
        if st == 0:
            return []
        b = b2.r32(BUF_GET)
        b2.r32(BUF_GET)
        if self.buf_valid(b):
            b2.w32(BUF_PUT, b)
        b2.w32(BUF_ACK, st)
        return [b]

    def counter_moving(self, gap=0.02):
        c0 = self.bar2.r32(COUNTER_REG)
        time.sleep(gap)
        c1 = self.bar2.r32(COUNTER_REG)
        return c0 != c1 and c0 not in (0, 0xFFFFFFFF), c0, c1
