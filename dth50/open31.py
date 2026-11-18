#!/usr/bin/env python3
"""ベンダの 0x31（復号つきで開く）を Linux 側で再現する（2026-11-04）。

ベンダのドライバ BufFullSeg.sys（disc100）は、アプリから受けた 0x31 要求（892 B）を
0x29a6c でカード命令 0x08 副5 の引数（0xad8 B）に詰め直して送る。ここはその移植。

  req31(...)          番組の情報（PMT から）→ 0x31 要求の語 {オフセット: 値}
  req31_from_pmt(...) PMT セクションから req31 を作る
  sub5_fields(req)    0x31 要求 → 副5 引数の「中身」の欄 {オフセット: 値}
                      （文脈ポインタ +0x18/+0x20/+0x28・ホストのバッファ群は含めない。s4-boot が埋める）

検証（tools/x_open31_check.py）:
  ・XP の 0x31 採取 6 本（ref/iohook-2）を 0x29a6c の模擬実行（unicorn）に通した結果と sub5_fields が一致
  ・PN 0x408 の回は、XP で視聴中に採った副5 の枠（ref/xp/pool/pool-2-playing-full.bin・枠 12）とも
    番地・バッファ以外の 55 語が一致（+0x86c の bit3 はファームが足す・+0xacc はこの関数が書かない）
  ・PID・PN・ES 表を振った模擬入力でも模擬実行と一致

移植したのはベンダが全回で使うモード（+0x7c = 0x804141・+0x80 = 0）だけ。
このフラグ語で 0x29a6c の分岐が決まる欄（+0x5c/+0x60/+0x64 など）は、その分岐の結果を定数で持つ。
"""
import os
import struct

# --- 0x31 要求のオフセット（ref/iohook-2 の 6 本で意味を確定） ---
R_SIZE, R_CODE, R_STATE, R_WAIT = 0x000, 0x004, 0x00C, 0x068
R_FLAGS, R_FLAGS_HI = 0x07C, 0x080            # 0x804141 / 0
R_VPID, R_VPID2 = 0x090, 0x094               # 映像 PID（PMT 前は 0xffff）
R_OUTKIND = 0x09C                             # 2（副5 +0x84 の写像の元）
R_RATE, R_RATE2, R_WIDTH, R_HEIGHT = 0x0A0, 0x0A4, 0x0A8, 0x0AC   # TransInfo 8 Mbps・720・480
R_ANUM, R_APID, R_APID2 = 0x0B0, 0x0B8, 0x0C8  # 音声の数・PID（+0xb8〜+0xc4 は音声 4 本の表・+0xc8 は 1 本目の写し）
AUDIO_MAX = 4
SET5C_AUDIO = 0x1000   # 副5 +0x5c の bit12: ファームが 2〜4 本目の音声（+0x82c/+0x830/+0x834）も PID の照合に入れる
R_BUFFLAG, R_E8 = 0x0D8, 0x0E8                # 0x10000 / 0x10078
R_PCR, R_PCR2, R_PMT, R_10C, R_PN = 0x0EC, 0x104, 0x108, 0x10C, 0x110
R_128, R_130, R_PN2, R_148 = 0x128, 0x130, 0x140, 0x148
R_ESN, R_ESPID, R_ESECM, R_ESTYPE = 0x24C, 0x250, 0x2B0, 0x310   # ES 表（0x60 間隔・最大 24 本）
R_378 = 0x378
ES_MAX = 24

MODE_FLAGS = 0x00804141                       # ベンダの 6 本すべてで同じ

# 全採取で同じ値の欄（番組に依らない）
REQ31_FIXED = {
    R_SIZE: 0x37C, R_CODE: 0x31, R_STATE: 0x2, R_WAIT: 0x7D0,
    R_FLAGS: MODE_FLAGS, R_OUTKIND: 0x2,
    R_RATE: 0x7A1200, R_WIDTH: 0x2D0, R_HEIGHT: 0x1E0,
    R_BUFFLAG: 0x10000, R_E8: 0x10078, R_10C: 0x1F4,
    R_128: 0x1100, R_130: 0x1, R_148: 0x10, R_378: 0xE80,
}

VIDEO_TYPES = (0x01, 0x02, 0x1B, 0x24)        # MPEG-1/2・H.264・H.265
AUDIO_TYPES = (0x03, 0x04, 0x0F, 0x11)        # MPEG-1/2 音声・AAC（ADTS/LATM）
# カードに渡す ES の種類: 映像・音声・0x06（字幕・文字スーパー）。データ放送のカルーセル（0x0d など）は渡さない。
# 2 本の流れはファームの PID の表（0x8020a440・30 段・0x8019dcf0）を分け合い、ES の多い局どうし（14 本＋13 本）だと
# 2 本目の副5 が 0xf で断られる（2026-11-17 の試験）。カードはデータ放送の ES をもともと出さない（0x138・0x140〜 は
# 出力に無い）ので、渡しても使われない
ES_KEEP_TYPES = VIDEO_TYPES + AUDIO_TYPES + (0x06,)
# データ放送のカルーセル（DSM-CC）。本放送のものは暗号化されている（ch22 の生の TS で 0x0840 など全パケット）。
# 試し: 環境変数 DTH50_ES_DATA=1 で 0x0d もカードに渡す（ベンダは渡している。今の手順で dec の環に出るかを確かめる）
DATA_TYPES = (0x0D,)


def es_keep_types():
    return ES_KEEP_TYPES + (DATA_TYPES if os.environ.get("DTH50_ES_DATA") else ())


def req31(pn, pmt_pid, pcr_pid, es):
    """番組の情報から 0x31 要求の語を作る。

    es: [(pid, ecm_pid, stream_type), ...]（PMT の並び順のまま。ecm_pid は CA 記述子の CA_PID）
    映像 PID は es の最初の映像。音声は PMT の並び順で最大 4 本を +0xb8〜+0xc4 に並べ、+0xb0 に本数、+0xc8 に 1 本目。
    ベンダの 0x29a6c は要求 +0xb0 を副5 +0x824 に、+0xb8〜+0xd4 を +0x828〜+0x844 にそのまま写す（模擬実行・2026-11-06）。
    ファームは副5 +0x828（1 本目）を常に、+0x82c/+0x830/+0x834（2〜4 本目）は +0x5c の bit12 が立つときだけ
    PID の照合（0x801abefc）に入れる（0x80186344〜0x80186384）。2 本目の音声が出なかったのはこのため（2026-11-06）。
    """
    if len(es) > ES_MAX:
        raise ValueError(f"ES が {len(es)} 本（最大 {ES_MAX}）")
    r = dict(REQ31_FIXED)
    vids = [p for p, _e, t in es if t in VIDEO_TYPES]
    auds = [p for p, _e, t in es if t in AUDIO_TYPES]
    v = vids[0] if vids else 0xFFFF
    r[R_VPID] = r[R_VPID2] = v
    if auds:
        auds = auds[:AUDIO_MAX]
        r[R_ANUM] = len(auds)
        for i, p in enumerate(auds):
            r[R_APID + 4 * i] = p
        r[R_APID2] = auds[0]
    r[R_PCR] = r[R_PCR2] = pcr_pid
    r[R_PMT] = pmt_pid
    r[R_PN] = r[R_PN2] = pn
    r[R_ESN] = len(es)
    for i, (pid, ecm, typ) in enumerate(es):
        r[R_ESPID + 4 * i] = pid
        r[R_ESECM + 4 * i] = ecm
        r[R_ESTYPE + 4 * i] = typ
    return r


def req31_pre_pmt():
    """PMT を読む前の 0x31（ベンダの 1 回目と同じ形・映像 PID 0xffff・PN 0・ES 0 本）。"""
    r = dict(REQ31_FIXED)
    r[R_VPID] = r[R_VPID2] = 0xFFFF
    return r


def _ca_pid(desc):
    """記述子の並びから CA 記述子（0x09）の CA_PID を返す。"""
    i = 0
    while i + 2 <= len(desc):
        tag, ln = desc[i], desc[i + 1]
        if tag == 0x09 and ln >= 4:
            return ((desc[i + 4] & 0x1F) << 8) | desc[i + 5]
        i += 2 + ln
    return None


def parse_pmt(sec):
    """PMT セクション → (pn, pcr_pid, [(pid, ecm_pid, stream_type), ...])。

    ECM PID は ES の CA 記述子を優先し、無ければその番組の ECM PID（番組の CA 記述子、
    無ければ最初に見つかった ES の CA 記述子）。ベンダは CA 記述子を持たないデータ ES
    （ch27 の 0x130/0x138）にも 0x901 を入れている（ref/iohook-2）。CA が一つも無ければ 0x1fff。
    """
    if sec[0] != 0x02:
        raise ValueError("PMT ではない（table_id 0x%02x）" % sec[0])
    slen = ((sec[1] & 0x0F) << 8) | sec[2]
    end = 3 + slen - 4                          # CRC を除く
    pn = (sec[3] << 8) | sec[4]
    pcr = ((sec[8] & 0x1F) << 8) | sec[9]
    pil = ((sec[10] & 0x0F) << 8) | sec[11]
    prog_ecm = _ca_pid(sec[12:12 + pil])
    i = 12 + pil
    raw = []
    while i + 5 <= end:
        typ = sec[i]
        pid = ((sec[i + 1] & 0x1F) << 8) | sec[i + 2]
        eil = ((sec[i + 3] & 0x0F) << 8) | sec[i + 4]
        raw.append((pid, _ca_pid(sec[i + 5:i + 5 + eil]), typ))
        i += 5 + eil
    if prog_ecm is None:
        prog_ecm = next((e for _p, e, _t in raw if e is not None), 0x1FFF)
    return pn, pcr, [(pid, prog_ecm if ecm is None else ecm, typ) for pid, ecm, typ in raw]


def req31_from_pmt(sec, pmt_pid):
    pn, pcr, es = parse_pmt(sec)
    keep = es_keep_types()
    return req31(pn, pmt_pid, pcr, [e for e in es if e[2] in keep][:ES_MAX])


# --- 0x29a6c の移植（0x31 要求 → 副5 引数の中身） ---

def _w(req, off):
    return req.get(off, 0) & 0xFFFFFFFF


def sub5_fields(req):
    """0x31 要求 → カード命令 0x08 副5 の引数（0xad8）のうち、中身の欄。

    0x29a6c の書き込み（0x3e152）を順に移植。番地（+0x18/+0x20/+0x28 は 0x39bdc で文脈から）と
    ホストのバッファ群（+0x314〜/+0x35c〜/+0x3a4〜/+0x3ec〜/+0x47c〜/+0x4c4〜/+0x678〜 は文脈から）は含めない。
    +0xacc はこの関数が書かない（XP の視聴中は 1）。
    """
    if _w(req, R_FLAGS) != MODE_FLAGS or _w(req, R_FLAGS_HI) != 0:
        raise ValueError("移植したのはフラグ語 0x804141（+0x80 = 0）のモードだけ: "
                         f"+0x7c=0x{_w(req, R_FLAGS):x} +0x80=0x{_w(req, R_FLAGS_HI):x}")
    t = {}
    # 固定（0x2a1e1〜）
    t[0x058] = 5                     # 副コマンド
    t[0x0C0] = t[0x0C4] = 0xFFFFFFFF
    t[0x068] = 0x80000002
    t[0x098] = 0
    # 要求の写し
    t[0x07C] = _w(req, R_VPID)
    t[0x608] = _w(req, R_PCR)
    t[0x640] = _w(req, R_PMT)
    t[0x668] = _w(req, R_10C)
    t[0x670] = _w(req, R_PN)
    t[0x080] = _w(req, R_VPID2)
    t[0x628] = _w(req, R_PCR2)
    t[0x6CC] = _w(req, 0x118)
    t[0x61C] = _w(req, 0x100)
    # 経路の欄（フラグ語 0x804141 での分岐の結果。0x2a529〜0x2ae20）
    #   +0x5c = 0、+0x60 = 0x100000（bit20 = 復号器側）| 0x40 | 0x20、+0x64 = 0
    t[0x05C] = 0
    t[0x060] = 0x00100060
    t[0x064] = 0
    t[0x0D0] = _w(req, R_VPID2)      # 0x2a834（この分岐で +0x94 を写す）
    # +0x84 は要求 +0x9c の写像（0x2ae28）
    t[0x084] = {3: 0x80000003, 4: 2, 5: 3}.get(_w(req, R_OUTKIND), 0x80000002)
    # TransInfo（幅・高さは 0x780・0x440 で頭打ち）
    t[0x088] = _w(req, R_RATE)
    t[0x08C] = _w(req, R_RATE2)
    t[0x090] = min(_w(req, R_WIDTH), 0x780)
    t[0x094] = min(_w(req, R_HEIGHT), 0x440)
    t[0x50C] = _w(req, 0x0E4)
    # +0x5a4 は要求 +0xe8 のビットを 1 つずつ写す（0x2b046〜0x2b560）
    t[0x5A4] = _w(req, R_E8) & 0x0001C7FF
    t[0x86C] = _w(req, R_148)
    t[0x874] = _w(req, 0x11C)
    t[0x878] = _w(req, 0x120)
    t[0x5AC] = _w(req, 0x0F4)
    t[0x614] = _w(req, 0x0F8)
    t[0x618] = _w(req, 0x0FC)
    # 音声（要求 +0xb0 → 副5 +0x824・+0xb8〜+0xd4 → +0x828〜+0x844 をそのまま。+0xb4 は写らない）
    t[0x824] = _w(req, R_ANUM)
    for o in range(0x0B8, 0x0D8, 4):
        t[o + 0x770] = _w(req, o)
    # 0x848〜0x868（PN は +0x864）
    for o in range(0x848, 0x86C, 4):
        t[o] = _w(req, o - 0x724)
    # ES 表（個数・PID・ECM PID・stream_type。0x60 間隔）
    n = _w(req, R_ESN)
    if n > ES_MAX:
        raise ValueError(f"ES 個数 {n} が表の大きさ {ES_MAX} を超える")
    t[0x9A8] = n
    for i in range(n):
        t[0x9AC + 4 * i] = _w(req, R_ESPID + 4 * i)
        t[0xA0C + 4 * i] = _w(req, R_ESECM + 4 * i)
        t[0xA6C + 4 * i] = _w(req, R_ESTYPE + 4 * i)
    return t


# 副5 引数のうち s4-boot が持つ欄（文脈ポインタ・副コマンド・ホストのバッファ群）。
#   0x29a6c ではこれらは 0x39bdc で文脈から写すか、文脈 +0x9244 等（ドライバが確保したバッファ）から来る。
#   群は 0x48 間隔（個数・大きさ・アドレス 16 本）: +0x314 / +0x35c / +0x3a4 / +0x3ec / +0x47c / +0x4c4 / +0x678。
KEEP = set(range(0x000, 0x040, 4)) | {0x058}
for _g in (0x314, 0x35C, 0x3A4, 0x3EC, 0x47C, 0x4C4, 0x678):
    KEEP.update(range(_g, _g + 0x48, 4))

ACC_XP = 1     # +0xacc: 0x29a6c は書かない。XP の視聴中は 1（v28 で映像が解けた値）


def overlay_sub5(t, req):
    """s4-boot の副5 引数 t（{オフセット: 値}）を、0x31 要求から作った中身で置き換える。

    ベンダは 0xad8 を 0 で埋めてから詰める（0x3f27a）ので、KEEP 以外で sub5_fields に無い欄は 0 にする。
    戻りは変えた欄 [(オフセット, 前, 後)]。
    """
    f = sub5_fields(req)
    f[0xACC] = ACC_XP
    changed = []
    for o in sorted(set(t) | set(f)):
        if o in KEEP or not 0 <= o < 0xAD8:
            continue
        v = f.get(o, 0)
        if t.get(o, 0) != v:
            changed.append((o, t.get(o, 0), v))
        t[o] = v
    return changed


def _sections(data, pid_want):
    """TS から pid_want のセクションを順に返す（ポインタフィールド・パケット跨ぎに対応）。"""
    buf = b""
    for i in range(0, len(data) - 187, 188):
        p = data[i:i + 188]
        if p[0] != 0x47 or (((p[1] & 0x1F) << 8) | p[2]) != pid_want:
            continue
        afc = (p[3] >> 4) & 3
        if not afc & 1:
            continue
        off = 4 + (1 + p[4] if afc & 2 else 0)
        pl = p[off:]
        if p[1] & 0x40:
            pl = pl[1 + pl[0]:]
            buf = b""
        buf += pl
        while len(buf) >= 3 and buf[0] != 0xFF:
            n = (((buf[1] & 0x0F) << 8) | buf[2]) + 3
            if len(buf) < n:
                break
            yield buf[:n]
            buf = buf[n:]


AUTO = -1     # 番組の自動選択（pn=auto）


def pn_str(pn):
    return "自動" if pn == AUTO else f"0x{pn:x}"


def _pat(data):
    """TS の最初の PAT → [(番組, PMT PID), ...]（PAT の並び順・番組 0 は除く）。"""
    for sec in _sections(data, 0x0000):
        if sec[0] == 0x00:
            out = []
            for i in range(8, 3 + (((sec[1] & 0x0F) << 8) | sec[2]) - 4, 4):
                pn = (sec[i] << 8) | sec[i + 1]
                if pn:
                    out.append((pn, ((sec[i + 2] & 0x1F) << 8) | sec[i + 3]))
            return out
    return []


def _pmt(data, pn, pmt_pid):
    for sec in _sections(data, pmt_pid):
        if sec[0] == 0x02 and (sec[3] << 8) | sec[4] == pn:
            return sec
    return None


def req31_from_ts(data, pn):
    """TS（PAT・PMT が平文で載っているもの）から番組 pn の 0x31 要求を作る。

    pn = AUTO なら、PAT の並び順で最初に MPEG-2 映像（stream_type 0x02）を持つ番組を選ぶ
    （地デジの主番組。ワンセグは H.264 なので外れる）。無ければ最初に何かの映像を持つ番組。
    手前の番組の PMT がまだ届いていなければ決めない（ValueError）。
    """
    pat = _pat(data)
    if not pat:
        raise ValueError("PAT がまだ無い")
    if pn != AUTO:
        pmt_pid = dict(pat).get(pn)
        if pmt_pid is None:
            raise ValueError(f"PAT に番組 0x{pn:x} が無い")
        sec = _pmt(data, pn, pmt_pid)
        if sec is None:
            raise ValueError(f"PMT（PID 0x{pmt_pid:x}）に番組 0x{pn:x} が無い")
        return req31_from_pmt(sec, pmt_pid)
    first_video = None
    for p, pmt_pid in pat:
        sec = _pmt(data, p, pmt_pid)
        if sec is None:
            raise ValueError(f"番組 0x{p:x} の PMT がまだ無い")
        _pn, _pcr, es = parse_pmt(sec)
        types = {t for _pid, _e, t in es}
        if 0x02 in types:
            return req31_from_pmt(sec, pmt_pid)
        if first_video is None and types & set(VIDEO_TYPES):
            first_video = (sec, pmt_pid)
    if first_video:
        return req31_from_pmt(*first_video)
    raise ValueError("映像を持つ番組が無い")


def load_spec(spec):
    """s4-boot --open31 の指定を読む。戻りは (要求, 番組番号)。

      pn=0x400                走行中に受けた TS の PAT/PMT から作る（本筋）。要求は None で返し、
                              s4-boot が副1 を開いた後に find_req31 で組む
      pn=auto                 同上。番組は PAT の最初の MPEG-2 映像の番組（Mirakurun は物理 ch しか渡さない）
      ts=FILE.ts,pn=0x408     手元の TS の PAT/PMT から作る
      FILE.bin                0x31 要求そのもの（ref/iohook-2 の採取など）
    """
    if spec.endswith(".bin") and "=" not in spec:
        req = req31_words(open(spec, "rb").read())
        return req, req.get(R_PN)
    kv = dict(x.split("=", 1) for x in spec.split(",") if "=" in x)
    if "pn" in kv:
        pn = AUTO if kv["pn"] == "auto" else int(kv["pn"], 0)
        if "ts" in kv:
            return req31_from_ts(open(kv["ts"], "rb").read(), pn), pn
        return None, pn
    raise ValueError(f"--open31 の指定が読めない: {spec}（pn=N か ts=FILE,pn=N か FILE.bin）")


def ts_from_buffers(buf, ranges):
    """ホストのバッファ群（[(オフセット, 大きさ), ...]）から 188 バイト境界の TS パケットを拾う。

    s4-boot の scan_one と同じく、各バッファの先頭から 188 刻みで 0x47 を見る。読むだけ（0xA5 に戻さない）。
    """
    out = []
    for off, sz in ranges:
        seg = bytes(buf[off:off + sz])
        for k in range(sz // 188):
            if seg[k * 188] == 0x47:
                out.append(seg[k * 188:(k + 1) * 188])
    return b"".join(out)


def find_req31(data, pn):
    """TS から番組 pn の 0x31 要求を作る。PAT か PMT がまだ無ければ None。"""
    try:
        return req31_from_ts(data, pn)
    except ValueError:
        return None


def describe(req):
    n = req.get(R_ESN, 0)
    es = " ".join(f"{req[R_ESPID + 4 * i]:04x}:{req[R_ESTYPE + 4 * i]:02x}" for i in range(n))
    return (f"PN 0x{req.get(R_PN, 0):x} PMT 0x{req.get(R_PMT, 0):x} PCR 0x{req.get(R_PCR, 0):x} "
            f"映像 0x{req.get(R_VPID, 0):x} 音声 0x{req.get(R_APID, 0):x} "
            f"ECM 0x{req.get(R_ESECM, 0):x} ES {n} 本 [{es}]")


def req31_bytes(req, size=0x37C):
    b = bytearray(size)
    for o, v in req.items():
        if o + 4 <= size:
            struct.pack_into("<I", b, o, v & 0xFFFFFFFF)
    return bytes(b)


def req31_words(data):
    return {o: struct.unpack_from("<I", data, o)[0] for o in range(0, len(data) - 3, 4)}


if __name__ == "__main__":
    import sys
    if len(sys.argv) != 2:
        print("使い方: open31.py <FILE.bin | ts=FILE.ts,pn=0x408>  … 0x31 要求と副5 の中身の欄を出す",
              file=sys.stderr)
        sys.exit(2)
    req, _pn = load_spec(sys.argv[1])
    if req is None:
        print("pn=N だけの指定は走行中に PMT を読む形（s4-boot --open31 用）", file=sys.stderr)
        sys.exit(2)
    print(describe(req))
    for o, v in sorted(sub5_fields(req).items()):
        if v:
            print(f"  +0x{o:03x} = 0x{v:x}")
