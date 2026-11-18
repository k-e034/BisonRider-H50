"""チューナー: カードの I2C コントローラ越しに復調器（0x30）とその奥のチューナー（ゲート 0xFE 越しの 0xC6）を叩く。

実験用リポジトリ（非公開）の tools/s4-boot.py（i2c_cmd_word / i2c_txn / i2c_read1 / i2c_verify / load_vendor_seq / _vendor_init /
vendor_freq_bytes / isdbt_freq と ts_grab の「1. ベンダの初期化列 → 選局」）から移した。
初期化列はベンダのドライバの I2C を採ったもので、このリポジトリには入れない（vendor/tuner-seq.txt）。

DT-H50/PCIEW の 2 つ目（SMT-MJ110・復調器 0x20・チューナー 0xC0）は TunerMJ110。列はベンダのフィルタの MJ110 の処理を
動かして取ったもの（tools/extract_mj110.py → vendor/tuner2-mj110.txt）。DT-H50/PCIE のチューナーも TunerMJ110 で動く（docs/hardware.md）。
"""
import logging
import time

log = logging.getLogger("dth50.tuner")

# ---- BAR2 の I2C コントローラ ----
I2C_CLK = 0x1000        # (クロック << 16) | 分周
I2C_DATA = 0x1004       # 送るバイト列（リトルエンディアン・最大 4）
I2C_RX = 0x1008         # 受信欄（送受共用。0 を書けば消える）
I2C_CMD = 0x100C        # 書くと起動。読み返すと結果（bit31 = NAK）
I2C_STAT = 0x1094       # 割り込み状態と確認応答（0x44 = 済み）
I2C_CLK_HI = 0x64
I2C_RD_TYP, I2C_RD_FA, I2C_RD_FB = 0, 1, 1
I2C_NO_READ = {0xC0, 0xC6}

DEMOD = 0x30            # 復調器（8 ビットアドレス）
TC_THRU = 0xFE          # 復調器の I2C ゲート（この後ろにチューナー 0xC6）
TUNER = 0xC6

# DT-H50/PCIEW の 2 系統（復調器, ゲート越しのチューナー）。ベンダのフィルタ（BufFsLiveFilter.ax 0x1000f710）が
# チューナー番号から決める組。PCIEW は GPIO で切り替えない（I2C の入口 0x1001f4f0 が GPIO 4 を叩くのは種別 4/8 = Dual・U2W だけ）。
# ベンダの番号 1 = 今まで使ってきた組（ベンダの選局列はこちらを採ったもの）、番号 0 = 2 つ目。
# 中身（基板の目視）: 1 つ目 = TC90507（0x30）＋ SMT-MJ100（0xC6）、2 つ目 = 復調器を内蔵した SMT-MJ110（0x20・0xC0）
CHAINS = {
    1: (0x30, 0xC6),
    2: (0x20, 0xC0),
}
TC_CNR = 0x8B           # CNR 3 バイト

# 取引の波形が既知のとおりに返るか（計測を信用してよいか）。実験の本筋はこれが通らなければ止める
I2C_KNOWN = [
    ("書き込み 4B（初期化 1 発目）", DEMOD, bytes([TC_THRU, TUNER, 0x10, 0x40]), 1, 1, 1, 0x00181304),
    ("書き込み 1B（走査）", DEMOD, bytes([0x00]), 1, 1, 1, 0x00181301),
    ("読み出し 第 1 段", DEMOD, bytes([0x00]), 1, 0, 1, 0x30181101),
]


class TunerError(RuntimeError):
    pass


def i2c_cmd_word(addr8, length, typ=1, f_a=1, f_b=1):
    v = addr8 & 0xFE
    v = (v << 3) | (typ & 7)
    v = (v << 3) | (f_a & 1)
    v = (v << 1) | (f_b & 1)
    return ((v << 8) | (length & 0xFF)) & 0xFFFFFFFF


def isdbt_freq(ch):
    """地デジの物理チャンネル（13〜62）の中心周波数（Hz）。"""
    if not 13 <= ch <= 62:
        raise ValueError(f"物理チャンネル {ch} は 13〜62 の外")
    return 473142857 + 6000000 * (ch - 13)


def freq_bytes(freq_hz):
    """チューナーの周波数欄（1/64 MHz 単位）。戻り (下位, 上位)。"""
    v = (freq_hz * 0x40 + 500000) // 1000000
    return v & 0xFF, (v >> 8) & 0xFF


def load_seq(path):
    """ベンダの I2C 列: 1 行「8bitアドレス 型 旗A 旗B 長さ バイト列…」。# から行末は注釈。"""
    out = []
    for line in open(path, encoding="utf-8"):
        if line.lstrip().startswith("#"):
            continue
        head = line.split("#")[0].split()
        if len(head) < 6:
            continue
        a = int(head[0], 16)
        t, fa, fb, ln = (int(x) for x in head[1:5])
        out.append((a, t, fa, fb, ln, bytes(int(x, 16) for x in head[5:])))
    return out


class I2C:
    def __init__(self, bar2):
        self.bar2 = bar2

    def txn(self, addr8, payload, typ=1, f_a=1, f_b=1, length=None, clock=100, timeout=0.2):
        """1 取引。戻り (読み返したコマンド欄, 受信欄, 成功したか)。"""
        b2 = self.bar2
        if length is None:
            length = max(len(payload), 1)
        div = 0x28870 // (clock * 4)
        data = int.from_bytes(bytes(payload).ljust(4, b"\0"), "little")
        cmd = i2c_cmd_word(addr8, length, typ, f_a, f_b)
        b2.r32(I2C_RX)
        b2.w32(I2C_DATA, data)
        b2.w32(I2C_CLK, (I2C_CLK_HI << 16) | div)
        b2.w32(I2C_CMD, cmd)
        b2.r32(0x414)                 # ベンダは取引の直後に読む（0x80）
        b2.r32(0x418)
        b2.r32(I2C_RX)
        t0 = time.time()
        while time.time() - t0 < timeout:
            if b2.r32(I2C_STAT) == 0x44:
                break
        b2.w32(I2C_STAT, 0x300040)
        b2.r32(I2C_STAT)
        b2.w32(I2C_STAT, 0x80)
        b2.r32(I2C_STAT)
        b2.w32(I2C_STAT, 0x4)
        b2.r32(0x414)
        rx = b2.r32(I2C_RX)
        back = b2.r32(I2C_CMD)
        nak = bool(back & 0x80000000)
        return back, rx, (not nak) and (back & 0xFFFFFF) == (cmd & 0xFFFFFF)

    def read1(self, addr8, reg):
        """1 バイト読む（第 1 段でレジスタ番号を送り、受信欄を消してから第 2 段で受ける）。読めなければ None。"""
        self.txn(addr8, bytes([reg]), 1, 0, 1)
        self.bar2.w32(I2C_RX, 0)
        _back, rx, ok = self.txn(addr8, b"\0", I2C_RD_TYP, I2C_RD_FA, I2C_RD_FB)
        return (rx & 0xFF) if ok else None

    def verify(self):
        """既知の波形が返るか。返らなければ I2C の計測を信用できない。"""
        bad = []
        for name, addr8, pl, typ, f_a, f_b, exp in I2C_KNOWN:
            back, _rx, _ok = self.txn(addr8, pl, typ, f_a, f_b)
            if back != exp:
                bad.append(f"{name}: 0x{back:08x}（期待 0x{exp:08x}）")
        return bad


class Tuner:
    """DT-H50/PCIEW の 1 つ目のチューナー（TC90507 = 復調器 0x30 ＋ SMT-MJ100 = ゲート 0xFE 越しのチューナー 0xC6）。

    ベンダの初期化列（vendor/tuner-seq.txt）は 1 つ目を使うときのものを採ったもの。この列は 2 つ目（0x20・0xC0）にも
    書いていて（c0 ← 95 fb / 09 41・20 ← 03 70 など。2 つ目を休ませる設定）、others=False でそれを飛ばす。
    2 つ目は TunerMJ110。"""

    def __init__(self, bar2, seq, chain=1, others=True):
        if chain != 1:
            raise TunerError("Tuner は 1 つ目専用（2 つ目は TunerMJ110）")
        self.i2c = I2C(bar2)
        self.seq = seq
        self.demod, self.tuner = CHAINS[chain]
        self.chain = chain
        self.others = others

    def verify(self):
        """I2C の波形の確認（実験の本筋では、ホスト側の準備より前に 1 回）。違えば TunerError。"""
        bad = self.i2c.verify()
        if bad:
            raise TunerError("I2C の波形が既知と違う: " + " / ".join(bad))

    def init(self):
        """ベンダの初期化列を流す（周波数の 5 発は tune が出すので飛ばす）。戻り NAK の数（0x3c は不在なので 1 件は正常）。"""
        nak = sent = 0
        for a, t, fa, fb, ln, b in self.seq:
            if (a == DEMOD and ln == 4 and len(b) == 4 and b[0] == TC_THRU and b[1] == TUNER
                    and b[2] in (0x11, 0x13, 0x14, 0x15)):
                continue
            if a in I2C_NO_READ and t != 1:
                continue
            if a != DEMOD and not self.others:
                continue
            _back, _rx, ok = self.i2c.txn(a, b, t, fa, fb, length=ln, clock=100)
            sent += 1
            if not ok:
                nak += 1
        log.info("チューナー %d（復調器 0x%02x・チューナー 0x%02x）の初期化列: %d 取引・NAK %d 件",
                 self.chain, self.demod, self.tuner, sent, nak)
        return nak

    def tune(self, ch):
        """物理チャンネル ch へ選局する。"""
        lo, hi = freq_bytes(isdbt_freq(ch))
        for reg, val in ((0x11, 0x00), (0x13, 0x15), (0x14, lo), (0x15, hi), (0x11, 0x02)):
            self.i2c.txn(self.demod, bytes([TC_THRU, self.tuner, reg, val]), clock=100)
        time.sleep(0.5)

    def status(self):
        """戻り (ロックしたか, CNR の生の値 or None)。"""
        rd = self.i2c.read1
        r80, rb0 = rd(self.demod, 0x80), rd(self.demod, 0xB0)
        cnr = [rd(self.demod, TC_CNR + i) for i in range(3)]
        c = None if any(x is None for x in cnr) else (cnr[0] << 16) | (cnr[1] << 8) | cnr[2]
        locked = r80 is not None and rb0 is not None and (r80 & 0x28) == 0 and (rb0 & 0x0F) >= 8
        return locked, c

    def wait_lock(self, timeout=4.0):
        endt = time.time() + timeout
        tries = 0
        while True:
            tries += 1
            locked, cnr = self.status()
            if locked or time.time() >= endt:
                break
            time.sleep(0.25)
        log.info("チューナー %d: ロック%s（%d 回目・CNR %s）", self.chain, "成立" if locked else "せず", tries,
                 "--" if cnr is None else f"0x{cnr:06x}")
        return locked, cnr


def load_mj110(path):
    """tools/extract_mj110.py の出力を読む。戻り {"init": 列, 物理チャンネル: 列}。列の要素は (addr, 型, 旗A, 旗B, 長さ, バイト列)
    か ("sleep", ミリ秒)。"""
    out, cur = {}, None
    for line in open(path, encoding="utf-8"):
        line = line.split("#")[0].strip()
        if not line:
            continue
        if line.startswith("["):
            name = line.strip("[]").split()
            cur = out.setdefault("init" if name[0] == "init" else int(name[1]), [])
            continue
        head = line.split()
        if head[0] == "sleep":
            cur.append(("sleep", int(head[1])))
            continue
        a = int(head[0], 16)
        t, fa, fb, ln = (int(x) for x in head[1:5])
        cur.append((a, t, fa, fb, ln, bytes(int(x, 16) for x in head[5:])))
    if "init" not in out or any(ch not in out for ch in range(13, 63)):
        raise TunerError(f"{path} に初期化か ch13〜62 の選局が足りない（tools/extract_mj110.py で作り直す）")
    return out


class TunerMJ110(Tuner):
    """DT-H50/PCIEW の 2 つ目のチューナー（SMT-MJ110・復調器 0x20 を内蔵・チューナー 0xC0 は主バスに直接）。

    ベンダのフィルタの MJ110 の処理（初期化 = チューナーを起こす → 表 → 復調器の設定 → 一度選局する、選局 = 周波数ごとの
    チューナーの分周値 → 復調器の捕捉開始 20 ← 1e 80 / 1f 08）を、待ち時間も含めてそのまま流す。
    状態の読み（0x80・0xB0・CNR 0x8B〜）とロックの判定は 1 つ目と同じ（ベンダの 0x1000dfa0 も同じ判定）。
    2026-11-04 実機（開発機・ch27）でロック成立・CNR 0x00d1xx〜0x00e2xx。"""

    def __init__(self, bar2, seqs):
        self.i2c = I2C(bar2)
        self.seqs = seqs
        self.chain = 2
        self.demod, self.tuner = CHAINS[2]

    def verify(self):
        """自分の復調器（0x20）が I2C に応答するか。1 つ目の既知の波形（0x30 宛て）は使わない:
        DT-H50/PCIE の基板には 0x30 が無く、チューナーは 0x20 / 0xC0 だけ（ベンダのドライバの基板 ID a2/a3 の分岐）。"""
        back, _rx, ok = self.i2c.txn(self.demod, bytes([0x00]), 1, 1, 1)
        if not ok:
            raise TunerError(f"復調器 0x{self.demod:02x} が I2C に応答しない（0x{back:08x}）")

    def _run(self, seq):
        nak = 0
        for item in seq:
            if item[0] == "sleep":
                time.sleep(item[1] / 1000)
                continue
            a, t, fa, fb, ln, b = item
            _back, _rx, ok = self.i2c.txn(a, b, t, fa, fb, length=ln, clock=100)
            nak += 0 if ok else 1
        return nak

    def init(self):
        seq = self.seqs["init"]
        nak = self._run(seq)
        log.info("チューナー 2（SMT-MJ110・復調器 0x%02x・チューナー 0x%02x）の初期化: %d 取引・NAK %d 件",
                 self.demod, self.tuner, sum(1 for x in seq if x[0] != "sleep"), nak)
        return nak

    def tune(self, ch):
        isdbt_freq(ch)                      # 範囲の確認
        nak = self._run(self.seqs[ch])
        if nak:
            log.warning("チューナー 2: ch%d の選局で NAK %d 件", ch, nak)
        time.sleep(0.5)
