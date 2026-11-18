"""VFIO でカードを握る（BAR の割り当て・DMA の写像・IOMMU グループ・vfio-pci へのつなぎ替え）。

実験用リポジトリ（非公開）の tools/s4-boot.py（Device / Region / Bar / dma_map / iommu_group_of）と
tools/s3-host-setup.sh（vfio-pci へのバインド）から移した。root が要る。
"""
import ctypes
import errno
import fcntl
import mmap
import os
import struct
import subprocess

# ---- VFIO の ioctl。_IO(';', 100+n) なので (0x3B<<8)|nr ----
VFIO_TYPE = 0x3B
VFIO_BASE = 100


def _io(nr):
    return (VFIO_TYPE << 8) | (VFIO_BASE + nr)


VFIO_GET_API_VERSION = _io(0)
VFIO_CHECK_EXTENSION = _io(1)
VFIO_SET_IOMMU = _io(2)
VFIO_GROUP_GET_STATUS = _io(3)
VFIO_GROUP_SET_CONTAINER = _io(4)
VFIO_GROUP_GET_DEVICE_FD = _io(6)
VFIO_DEVICE_GET_INFO = _io(7)
VFIO_DEVICE_GET_REGION_INFO = _io(8)
VFIO_DEVICE_RESET = _io(11)
VFIO_IOMMU_MAP_DMA = _io(13)
VFIO_IOMMU_UNMAP_DMA = _io(14)

VFIO_API_VERSION = 0
VFIO_TYPE1_IOMMU = 1
VFIO_GROUP_FLAGS_VIABLE = 1
VFIO_DEVICE_FLAGS_RESET = 1 << 0
VFIO_REGION_INFO_FLAG_MMAP = 1 << 2
VFIO_DMA_MAP_FLAG_READ = 1 << 0
VFIO_DMA_MAP_FLAG_WRITE = 1 << 1

BAR0_REGION = 0
BAR2_REGION = 2
CONFIG_REGION = 7

PCI_COMMAND = 0x04
PCI_COMMAND_MEMORY = 0x2
PCI_COMMAND_MASTER = 0x4


def iommu_group_of(bdf):
    """BDF から IOMMU グループの番号を拾う（番号は起動ごとに変わりうる）。"""
    try:
        return os.path.basename(os.path.realpath(f"/sys/bus/pci/devices/{bdf}/iommu_group"))
    except OSError:
        return None


def bind_vfio(bdf, rebind=True):
    """カードを vfio-pci につなぐ。IOMMU グループにカードだけがいることも確かめる。
    rebind=True（既定）は、すでに vfio-pci でも一度外して付け直す（実験の s3-host-setup.sh と同じ。warm はこれで通っている）。"""
    dev = f"/sys/bus/pci/devices/{bdf}"
    if not os.path.isdir(dev):
        raise SystemExit(f"{bdf} が無い")
    grp = iommu_group_of(bdf)
    members = os.listdir(f"/sys/kernel/iommu_groups/{grp}/devices")
    if members != [bdf]:
        raise SystemExit(f"IOMMU グループ {grp} にカード以外もいる（{' '.join(members)}）。単独でないと渡せない")
    drv = os.path.join(dev, "driver")
    on_vfio = os.path.exists(drv) and os.path.basename(os.path.realpath(drv)) == "vfio-pci"
    if on_vfio and os.path.exists(f"/dev/vfio/{grp}"):
        # 別のプロセスが握っているなら、ここで止める。vfio-pci の unbind は相手が手放すまで黙って待つので、
        # そのまま進むと取り合いになる（2026-11-04: 実験の橋が握っている間 60 秒止まり、その後に橋の側が待たされた）。
        # VFIO のグループは同時に 1 プロセスしか開けない（EBUSY）
        try:
            os.close(os.open(f"/dev/vfio/{grp}", os.O_RDWR))
        except OSError as e:
            if e.errno == errno.EBUSY:
                raise SystemExit(f"{bdf} は別のプロセスが使っている（/dev/vfio/{grp} が開けない）")
            raise
    if not rebind and on_vfio:
        return grp
    subprocess.run(["modprobe", "vfio-pci"], check=True)
    if os.path.exists(drv):
        with open(os.path.join(drv, "unbind"), "w") as f:
            f.write(bdf)
    with open(os.path.join(dev, "driver_override"), "w") as f:
        f.write("vfio-pci")
    with open("/sys/bus/pci/drivers_probe", "w") as f:
        f.write(bdf)
    if not os.path.exists(f"/dev/vfio/{grp}"):
        raise SystemExit(f"/dev/vfio/{grp} が生えない")
    return grp


class Region:
    def __init__(self, index, flags, size, offset):
        self.index, self.flags, self.size, self.offset = index, flags, size, offset

    @property
    def mmappable(self):
        return bool(self.flags & VFIO_REGION_INFO_FLAG_MMAP)


class Device:
    """VFIO で 1 デバイスを握る。"""

    def __init__(self, group, bdf):
        self.bdf = bdf
        self.container = os.open("/dev/vfio/vfio", os.O_RDWR)
        if fcntl.ioctl(self.container, VFIO_GET_API_VERSION) != VFIO_API_VERSION:
            raise SystemExit("VFIO API の版が違う")
        if not fcntl.ioctl(self.container, VFIO_CHECK_EXTENSION, VFIO_TYPE1_IOMMU):
            raise SystemExit("type1 IOMMU に対応していない")
        path = f"/dev/vfio/{group}"
        self.group = os.open(path, os.O_RDWR)
        st = bytearray(struct.pack("II", 8, 0))
        fcntl.ioctl(self.group, VFIO_GROUP_GET_STATUS, st)
        if not struct.unpack("II", bytes(st))[1] & VFIO_GROUP_FLAGS_VIABLE:
            raise SystemExit(f"{path} が viable でない（グループの全デバイスが vfio-pci に付いているか）")
        fcntl.ioctl(self.group, VFIO_GROUP_SET_CONTAINER, struct.pack("i", self.container))
        fcntl.ioctl(self.container, VFIO_SET_IOMMU, VFIO_TYPE1_IOMMU)
        # ⚠️ 戻り値がデバイス fd なので、可変バッファ + mutate_flag で呼ぶ（bytes だと中身が返る）
        self.fd = fcntl.ioctl(self.group, VFIO_GROUP_GET_DEVICE_FD, bytearray(bdf.encode() + b"\0"), True)
        if not isinstance(self.fd, int) or self.fd < 0:
            raise SystemExit(f"デバイス fd を取れない: {self.fd!r}")
        info = bytearray(struct.pack("IIII", 16, 0, 0, 0))
        fcntl.ioctl(self.fd, VFIO_DEVICE_GET_INFO, info)
        _, self.flags, self.num_regions, self.num_irqs = struct.unpack("IIII", bytes(info))
        self._dma = []

    def region(self, index):
        buf = bytearray(struct.pack("IIIIQQ", 32, 0, index, 0, 0, 0))
        fcntl.ioctl(self.fd, VFIO_DEVICE_GET_REGION_INFO, buf)
        _, flags, idx, _cap, size, offset = struct.unpack("IIIIQQ", bytes(buf))
        return Region(idx, flags, size, offset)

    def reset(self):
        if not self.flags & VFIO_DEVICE_FLAGS_RESET:
            return False
        fcntl.ioctl(self.fd, VFIO_DEVICE_RESET)
        return True

    def cfg_read16(self, off):
        r = self.region(CONFIG_REGION)
        return struct.unpack("<H", os.pread(self.fd, 2, r.offset + off))[0]

    def cfg_write16(self, off, val):
        r = self.region(CONFIG_REGION)
        os.pwrite(self.fd, struct.pack("<H", val), r.offset + off)

    def enable_bus_master(self):
        cmd = self.cfg_read16(PCI_COMMAND)
        want = cmd | PCI_COMMAND_MEMORY | PCI_COMMAND_MASTER
        if cmd != want:
            self.cfg_write16(PCI_COMMAND, want)
        return cmd, want

    def bar(self, index):
        return Bar(self.fd, self.region(index))

    def dma_map(self, vaddr, iova, size):
        """ホストのバッファを IOMMU に載せ、カードから見えるアドレス（IOVA）にする。同じ IOVA が残っていれば剥がしてから貼る
        （同じプロセスで 2 回目を貼ると EEXIST になる）。"""
        for m in list(self._dma):
            if m[0] == iova:
                self.dma_unmap(*m)
        buf = bytearray(struct.pack("IIQQQ", 40, VFIO_DMA_MAP_FLAG_READ | VFIO_DMA_MAP_FLAG_WRITE, vaddr, iova, size))
        fcntl.ioctl(self.container, VFIO_IOMMU_MAP_DMA, buf)
        self._dma.append((iova, size))

    def dma_unmap(self, iova, size):
        try:
            fcntl.ioctl(self.container, VFIO_IOMMU_UNMAP_DMA, bytearray(struct.pack("IIQQ", 24, 0, iova, size)))
        except OSError:
            pass
        self._dma = [m for m in self._dma if m != (iova, size)]


class Bar:
    """BAR を 32 ビット単位で確実に触る（mmap のスライス代入は memcpy になり、MMIO で幅がバイトに割れることがある）。"""

    def __init__(self, fd, region):
        self.size = region.size
        self.map = mmap.mmap(fd, region.size, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE, offset=region.offset)
        self.addr = ctypes.addressof(ctypes.c_char.from_buffer(self.map))
        self.u32 = ctypes.cast(self.addr, ctypes.POINTER(ctypes.c_uint32))

    def r32(self, off):
        return self.u32[off >> 2]

    def w32(self, off, val):
        self.u32[off >> 2] = val & 0xFFFFFFFF
