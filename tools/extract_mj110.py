#!/usr/bin/env python3
"""DT-H50/PCIEW の 2 つ目のチューナー（SMT-MJ110）の I2C 列を、ベンダの DirectShow フィルタから取り出す。

BufFsLiveFilter.ax（PCIEW 用ドライバ 1.63 のパッケージに入っているもの）の MJ110 を扱うクラスを unicorn で動かし、
I2C の書き込みと Sleep をそのまま記録して vendor/tuner2-mj110.txt に書く。フィルタの中身はこのリポジトリに入れない。

    pip install unicorn pefile
    python3 tools/extract_mj110.py BufFsLiveFilter.ax > vendor/tuner2-mj110.txt

クラス（仮想関数表 0x1003463c）で使うもの:
  +0x48 0x1000f710  番号の設定。0 → チューナー 0xC0・復調器 0x20（MJ110）
  +0x14 0x1000d8a0  初期化（チューナーを起こす → 表 0x100344d8 → 復調器の設定 → 一度選局する）
  +0x2c 0x1000e070  選局（周波数 Hz, 強制）
  +0x54 0x1000f230  I2C 書き込み (クロック, 宛先, レジスタ, バッファ, 長さ)。チューナー宛ては後ろに 0xFE が付く
  +0x50 0x1000f160  I2C 読み（初期化と選局では使わない）
書き込みは入口で横取りし、その先（ドライバへの DeviceIoControl）は動かさない。

出力の形式（dth50/tuner.py の load_mj110 が読む）:
  [init] / [ch N] の節。行は「8bitアドレス 型 旗A 旗B 長さ  バイト列」か「sleep ミリ秒」。
ベンダは周波数を kHz に丸めて渡す（ch18 = 503,143,000 Hz）ので、同じ丸めで選局する。
"""
import hashlib
import struct
import sys

import pefile
from unicorn import UC_ARCH_X86, UC_HOOK_CODE, UC_MODE_32, Uc, UcError
from unicorn.x86_const import UC_X86_REG_EAX, UC_X86_REG_ECX, UC_X86_REG_EIP, UC_X86_REG_ESP

# 動作を確かめたフィルタ（PCIEW 用ドライバ 1.63 の BufFsLiveFilter.ax・305,720 バイト）
FILTER_SHA256 = "541e302d4e40d5063c5b9b2b1608b4012c762535b39937b4051b974f21e6856b"

VTABLE = 0x1003463C
F_SET_INDEX = 0x1000F710
F_INIT = 0x1000D8A0
F_TUNE = 0x1000E070
F_WRITE = 0x1000F230
F_READ = 0x1000F160

STACK, STACK_SZ = 0x00100000, 0x00100000
THIS = 0x00400000
STUBS = 0x00500000
RET_MAGIC = 0x00700000

# _ftol: st(0) を切り捨てで 64 ビット整数にして edx:eax へ
FTOL = bytes.fromhex("83EC0CD93C24668B0424660D000C6689442402D96C2402DF7C2404D92C248B4424048B54240883C40CC3")
STDCALL = {"Sleep": 1, "EnterCriticalSection": 1, "LeaveCriticalSection": 1}


def isdbt_freq(ch):
    return 473142857 + 6000000 * (ch - 13)


class Emu:
    def __init__(self, path):
        pe = pefile.PE(path)
        img = pe.get_memory_mapped_image()
        ib = pe.OPTIONAL_HEADER.ImageBase
        uc = self.uc = Uc(UC_ARCH_X86, UC_MODE_32)
        uc.mem_map(ib, (len(img) + 0xFFF) & ~0xFFF)
        uc.mem_write(ib, img)
        for base, sz in ((STACK, STACK_SZ), (THIS, 0x10000), (STUBS, 0x10000), (RET_MAGIC, 0x1000)):
            uc.mem_map(base, sz)
        # fs:[0]（SEH の鎖）: 32 ビットの unicorn は fs の base が 0 のままなので 0 番地に置く
        uc.mem_map(0, 0x1000)
        uc.mem_write(0, struct.pack("<I", 0xFFFFFFFF))
        self.stubs = {}
        k = 0
        for e in pe.DIRECTORY_ENTRY_IMPORT:
            for i in e.imports:
                name = i.name.decode() if i.name else f"ord{i.ordinal}"
                addr = STUBS + k * 0x40
                uc.mem_write(addr, FTOL if name == "_ftol" else b"\xC3")
                uc.mem_write(i.address, struct.pack("<I", addr))
                if name != "_ftol":
                    self.stubs[addr] = name
                k += 1
        this = bytearray(0x400)
        struct.pack_into("<I", this, 0, VTABLE)
        struct.pack_into("<I", this, 0x14C, 0x10)       # 機種の GPIO ビット（PCIEW）。番号の設定がこれを見る
        struct.pack_into("<I", this, 0x16C, 1)          # デバイスのハンドル（-1 でなければよい）
        uc.mem_write(THIS, bytes(this))
        self.out = []
        uc.hook_add(UC_HOOK_CODE, self._hook)
        self.call(F_SET_INDEX, 0, check=False)     # 戻り値は無い
        self.tuner, self.demod = bytes(uc.mem_read(THIS + 0x288, 2))
        if (self.tuner, self.demod) != (0xC0, 0x20):
            raise SystemExit(f"番号の設定が想定と違う（チューナー {self.tuner:#x}・復調器 {self.demod:#x}）")

    def _rd32(self, p):
        return struct.unpack("<I", self.uc.mem_read(p, 4))[0]

    def _ret(self, nargs, eax):
        uc = self.uc
        esp = uc.reg_read(UC_X86_REG_ESP)
        ra = self._rd32(esp)
        uc.reg_write(UC_X86_REG_EAX, eax)
        uc.reg_write(UC_X86_REG_ESP, esp + 4 + 4 * nargs)
        uc.reg_write(UC_X86_REG_EIP, ra)

    def _hook(self, uc, addr, _size, _user):
        if addr == RET_MAGIC:
            uc.emu_stop()
            return
        esp = uc.reg_read(UC_X86_REG_ESP)
        if addr in (F_WRITE, F_READ):
            _clk, dev, reg, buf, ln = (self._rd32(esp + 4 + 4 * j) for j in range(5))
            dev &= 0xFF
            if addr == F_WRITE:
                body = bytes([reg & 0xFF]) + bytes(uc.mem_read(buf, ln)) + (b"\xfe" if dev == self.tuner else b"")
                self.out.append(("w", dev, body))
            else:
                uc.mem_write(buf, b"\x00" * ln)
                self.out.append(("r", dev, reg & 0xFF))
            self._ret(5, 1)
            return
        name = self.stubs.get(addr)
        if name is None:
            return
        if name == "Sleep":
            self.out.append(("s", self._rd32(esp + 4)))
        elif name not in STDCALL:
            raise SystemExit(f"未対応の API {name}（呼び元 {self._rd32(esp):#x}）")
        self._ret(STDCALL[name], 0 if name == "Sleep" else 1)

    def call(self, fn, *args, check=True):
        uc = self.uc
        esp = STACK + STACK_SZ - 0x1000
        for v in reversed(args):
            esp -= 4
            uc.mem_write(esp, struct.pack("<I", v & 0xFFFFFFFF))
        esp -= 4
        uc.mem_write(esp, struct.pack("<I", RET_MAGIC))
        uc.reg_write(UC_X86_REG_ESP, esp)
        uc.reg_write(UC_X86_REG_ECX, THIS)
        try:
            uc.emu_start(fn, RET_MAGIC, count=5_000_000)
        except UcError as e:
            raise SystemExit(f"エミュレーションが止まった: {e} @ {uc.reg_read(UC_X86_REG_EIP):#x}")
        rv = uc.reg_read(UC_X86_REG_EAX)
        if check and rv != 1:
            raise SystemExit(f"{fn:#x} が失敗を返した（{rv}）")

    def take(self):
        out, self.out = self.out, []
        if any(k == "r" for k, *_ in out):
            raise SystemExit("初期化・選局の中で I2C の読みが出た（想定外）")
        return out


def main():
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    path = sys.argv[1]
    sha = hashlib.sha256(open(path, "rb").read()).hexdigest()
    if sha != FILTER_SHA256:
        raise SystemExit(f"{path} は動作を確かめたフィルタと違う（sha256 {sha[:16]}…・"
                         "PCIEW 用ドライバ 1.63 の BufFsLiveFilter.ax を使う）")
    emu = Emu(path)
    w = sys.stdout.write
    w(f"# dth50 tuner2-mj110.txt: BufFsLiveFilter.ax（sha256 {sha[:16]}…）の SMT-MJ110 の処理を動かして取った I2C 列\n")
    w("# 形式: 8bitアドレス 型 旗A 旗B 長さ  バイト列 / sleep ミリ秒\n")
    # 初期化: [this+0x274]（最後に選局した周波数）= -1・[this+0x278]（復調器 0x52 の選択）= 0 のまま。XP の採取と同じ
    struct.pack_into("<i", b := bytearray(4), 0, -1)
    emu.uc.mem_write(THIS + 0x274, bytes(b))
    emu.call(F_INIT)
    sections = [("init", emu.take())]
    for ch in range(13, 63):
        hz = (isdbt_freq(ch) + 500) // 1000 * 1000
        emu.call(F_TUNE, hz, 1)
        sections.append((f"ch {ch}", emu.take()))
    for name, evs in sections:
        w(f"[{name}]\n")
        for ev in evs:
            if ev[0] == "s":
                w(f"sleep {ev[1]}\n")
            else:
                _k, dev, body = ev
                w(f"{dev:02x} 1 1 1 {len(body)}  {' '.join(f'{x:02x}' for x in body)}\n")


if __name__ == "__main__":
    main()
