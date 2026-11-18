"""対応機種の表と、挿さっているカードの見分け（PCI の SUBSYS で見分ける）。

    python3 -m dth50.models        # 挿さっているカードと機種を出す（/sys/bus/pci を読むだけ）

どの機種もチップは ViXS XCode 2100（PCI 1745:2100）で、VEN/DEV では区別できない。
機種は SUBSYS で決まる（ベンダの BufFullSeg.inf）。機種ごとの構成はベンダの BufFsLiveFilter.ax の
機種表（0x14c バイト × 25 機種）による。詳しくは docs/hardware.md。
"""
import os
from dataclasses import dataclass

PCI_VENDOR = 0x1745     # ViXS Systems
PCI_DEVICE = 0x2100     # XCode 2100 Series


@dataclass(frozen=True)
class Model:
    name: str
    subsys: tuple           # (サブシステムのベンダ, サブシステムの ID)
    tuners: int             # 地デジのチューナーの数（機種表 +0x120）
    i2c_switch_gpio: int    # 2 つのチューナーへ I2C バスを切り替える GPIO（無ければ -1。機種表 +0x118 のビット）
    board_code: int         # 機種コード（機種表 +0x138）。どの SUBSYS がどれかが未確認なら -1
    verified: str           # 実機で確かめた範囲
    # チューナー 1..n ごとの (I2C の組, TS の入力番号)。組 1 = 復調器 TC90507 0x30 ＋ チューナー 0xC6（tuner.Tuner）、
    # 組 2 = 復調器 0x20 ＋ チューナー 0xC0（tuner.TunerMJ110）。入力番号は副1・副5 の +0x08
    tuner_map: tuple = ((1, 0), (2, 1))
    tested: bool = False    # 実機で受信・復号まで確かめた（--allow-untested なしで使える）


MODELS = (
    Model("DT-H50/PCIEW", (0x1154, 0x0548), tuners=2, i2c_switch_gpio=4, board_code=0x10D,
          verified="チューナー 1・2 とも受信・カード内復号・2 本同時・Mirakurun まで確認",
          tuner_map=((1, 0), (2, 1)), tested=True),
    # PCIE は INF に 2 通りの SUBSYS（基板の改訂違いと見られる）。機種表にも PCIE が 2 件（機種コード 0x10f / 0x110・
    # 機種表 +0x140 = 0xa3 / 0xbe）。0542 の実機は基板 ID（BAR2+0x478）の下位が 0xa3 だったので 0x10f と見ている。
    # ベンダのドライバは基板 ID a2/a3 で「SMT-MJ100」と表示し、I2C は 0x20 / 0xC0 だけを初期化する（0x30 は無い）。
    # 実機（2026-11-17・開発機）: 組 2 の列で ch27 にロック、TS は入力 0、カード内で復号、EPGStation の ffmpeg で絵が出た。
    Model("DT-H50/PCIE", (0x1154, 0x0540), tuners=1, i2c_switch_gpio=-1, board_code=-1, verified="未確認",
          tuner_map=((2, 0),)),
    Model("DT-H50/PCIE", (0x1154, 0x0542), tuners=1, i2c_switch_gpio=-1, board_code=0x10F,
          verified="受信・カード内復号・橋で PCIEW と 3 本同時まで確認",
          tuner_map=((2, 0),), tested=True),
)


def tuner_plan(m, n):
    """機種 m のチューナー n（1 始まり）の (I2C の組, TS の入力番号)。"""
    if not 1 <= n <= len(m.tuner_map):
        raise ValueError(f"{m.name} にチューナー {n} は無い")
    return m.tuner_map[n - 1]


def lookup(subsys_vendor, subsys_device):
    """SUBSYS から機種を引く。表に無ければ None。"""
    for m in MODELS:
        if m.subsys == (subsys_vendor, subsys_device):
            return m
    return None


def _read_hex(path):
    with open(path) as f:
        return int(f.read().strip(), 16)


def scan(root="/sys/bus/pci/devices"):
    """挿さっている XCode 2100 を探す。[(BDF, (SUBSYS ベンダ, SUBSYS ID), 機種 or None), ...]"""
    found = []
    if not os.path.isdir(root):
        return found
    for bdf in sorted(os.listdir(root)):
        d = os.path.join(root, bdf)
        try:
            if (_read_hex(os.path.join(d, "vendor")), _read_hex(os.path.join(d, "device"))) != (PCI_VENDOR, PCI_DEVICE):
                continue
            ss = (_read_hex(os.path.join(d, "subsystem_vendor")), _read_hex(os.path.join(d, "subsystem_device")))
        except OSError:
            continue
        found.append((bdf, ss, lookup(*ss)))
    return found


def main():
    cards = scan()
    if not cards:
        print("ViXS XCode 2100（1745:2100）は見つからない")
        return 1
    for bdf, ss, m in cards:
        sid = f"{ss[0]:04x}:{ss[1]:04x}"
        if m is None:
            print(f"{bdf}  SUBSYS {sid}  ⚠️ 表に無い機種（対応していない）")
        else:
            print(f"{bdf}  SUBSYS {sid}  {m.name}  チューナー {m.tuners}  実機での確認: {m.verified}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
