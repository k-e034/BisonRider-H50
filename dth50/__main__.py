"""コマンドライン。

    python3 -m dth50 models                        挿さっているカードと機種
    sudo python3 -m dth50 stream --channel 27      選局して TS を標準出力へ（SIGTERM / SIGINT で閉じて終わる）
    sudo python3 -m dth50 tuner-probe --tuner 2 -c 27   I2C だけで選局してロックを見る（TS は開かない）
    sudo python3 -m dth50 bridge                   Docker の Mirakurun とつなぐ橋（contrib/mirakurun）

ログは標準エラーへ。標準出力は TS だけ。終了コード: 0 = 正常 / 1 = 失敗 / 2 = 引数 / 3 = 再起動（cold）が要る状態。
"""
import argparse
import logging
import os
import signal
import sys

from . import models


def _pick_card(bdf, allow_untested):
    cards = models.scan()
    if bdf:
        cards = [c for c in cards if c[0] == bdf]
    if not cards:
        raise SystemExit("ViXS XCode 2100（1745:2100）が見つからない" + (f"（{bdf}）" if bdf else ""))
    if len(cards) > 1 and not bdf:
        raise SystemExit("カードが複数ある。--bdf で選ぶ: " + " ".join(c[0] for c in cards))
    bdf, ss, m = cards[0]
    if m is None:
        raise SystemExit(f"{bdf}: SUBSYS {ss[0]:04x}:{ss[1]:04x} は対応表に無い")
    if not m.tested and not allow_untested:
        raise SystemExit(f"{bdf}: {m.name} は実機で未確認（試すなら --allow-untested）")
    return bdf, m


def _lock(bdf):
    """同じカードを 2 つのプロセスで握らない（プロセスが終われば外れる）。握れなければ None。"""
    import fcntl
    lock = open(f"/run/lock/dth50-{bdf}.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logging.getLogger("dth50").error("%s は別のプロセスが使っている", bdf)
        return None
    return lock


def cmd_tuner_probe(a):
    """カードを起動し、I2C だけで選局してロックと CNR を見る。TS のコマンド（0x19 など）は出さない。"""
    import time
    from .boot import BootError, open_card
    from .tuner import CHAINS, Tuner, TunerMJ110, load_mj110, load_seq
    from .vendor import Files
    log = logging.getLogger("dth50")
    bdf, m = _pick_card(a.bdf, a.allow_untested)
    # チューナー番号 → I2C の組（機種表 models.tuner_map）。--allow-untested なら、機種に無い番号は組の番号として試す
    if a.tuner <= m.tuners:
        chain = models.tuner_plan(m, a.tuner)[0]
    elif a.allow_untested and a.tuner in CHAINS:
        chain = a.tuner
    else:
        raise SystemExit(f"{m.name} にチューナー {a.tuner} は無い")
    lock = _lock(bdf)
    if lock is None:
        return 3
    vendor = Files(a.vendor)
    seqs2 = load_mj110(vendor.tuner2_seq) if chain == 2 and not a.replay else None
    try:
        _dev, card, _warm = open_card(bdf, vendor, force_cold=a.cold)
    except BootError as e:
        log.error("%s", e)
        return 3
    if chain == 2:
        tu = TunerMJ110(card.bar2, seqs2)
    else:
        tu = Tuner(card.bar2, load_seq(vendor.tuner_seq), others=a.others)
    tu.verify()
    rd = tu.i2c.read1
    log.info("初期化の前: 復調器 0x%02x  0x80=%s 0xB0=%s", tu.demod,
             *(("--" if v is None else f"0x{v:02x}") for v in (rd(tu.demod, 0x80), rd(tu.demod, 0xB0))))
    if a.replay:
        # 採取した I2C の書き込みを、そのまま（宛先も中身も変えずに）流す。選局もこの列に含まれる（採取と同じチャンネルで使う）
        # 行末の「# HH:MM:SS.mmm」（採取の時刻）があれば、その間隔どおりに間を空ける（--replay-fast で詰める）
        import re
        seq, stamps = [], []
        for line in (ln for path in a.replay for ln in open(path, encoding="utf-8")):
            if line.lstrip().startswith("#") or not line.strip():
                continue
            m = re.search(r"#\s*(\d+):(\d+):(\d+\.\d+)", line)
            stamps.append(int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else None)
            head = line.split("#")[0].split()
            seq.append((int(head[0], 16), *(int(x) for x in head[1:5]), bytes(int(x, 16) for x in head[5:])))
        nak = 0
        t_start = time.time()
        for k, (addr, t, fa, fb, ln, b) in enumerate(seq):
            if not a.replay_fast and k:
                if stamps[k] is not None and stamps[k - 1] is not None:
                    time.sleep(max(0.0, min(stamps[k] - stamps[k - 1], 2.0)))
                else:
                    time.sleep(0.015)    # 時刻の無い行（表から作った列）は、XP の 1 件あたりの間隔で
            _back, _rx, ok = tu.i2c.txn(addr, b, t, fa, fb, length=ln, clock=100)
            nak += 0 if ok else 1
        log.info("採取の列 %s: %d 取引・NAK %d 件（%.2f 秒%s）", " + ".join(a.replay), len(seq), nak, time.time() - t_start,
                 "・詰めて流した" if a.replay_fast else "・採取の間隔どおり")
        if not a.replay_fast:
            time.sleep(1.0)          # 採取では最後の書き込みから約 1 秒後に状態を読んでいた
    else:
        tu.init()
        tu.tune(a.channel)
    locked, _cnr = tu.wait_lock(a.lock_wait)
    for _ in range(a.samples):
        time.sleep(0.5)
        lk, cnr = tu.status()
        log.info("  0x80=%s 0xB0=%s ロック %s CNR %s",
                 *(("--" if v is None else f"0x{v:02x}") for v in (rd(tu.demod, 0x80), rd(tu.demod, 0xB0))),
                 "○" if lk else "✗", "--" if cnr is None else f"0x{cnr:06x}")
    return 0 if locked else 1


def cmd_stream(a):
    from . import stream as S
    from .boot import BootError, open_card
    from .vendor import Files

    def on_signal(n, _f):
        if not S.STOP:
            logging.getLogger("dth50").info("%s を受けた。開き終えたら汲み出さずに閉じる", signal.Signals(n).name)
        S.STOP.append(n)
    # 起動直後から覚えるだけにする。開いている途中で即座に終わると閉じ残しが残り、次の warm で閉じられなくなる
    for s in (signal.SIGTERM, signal.SIGINT):
        signal.signal(s, on_signal)

    bdf, m = _pick_card(a.bdf, a.allow_untested)
    lock = _lock(bdf)
    if lock is None:
        return 3
    if a.tuner <= m.tuners:
        chain, ts_input = models.tuner_plan(m, a.tuner)
    elif a.allow_untested and a.tuner in (1, 2):
        chain, ts_input = a.tuner, a.tuner - 1      # 未確認の組み合わせを試すとき（組と入力をそのまま）
    else:
        raise SystemExit(f"{m.name} にチューナー {a.tuner} は無い")
    if a.ts_input >= 0:
        ts_input = a.ts_input
    logging.getLogger("dth50").info("%s %s チューナー %d ch%d", bdf, m.name, a.tuner, a.channel)
    vendor = Files(a.vendor)
    try:
        dev, card, _warm = open_card(bdf, vendor, force_cold=a.cold)
    except BootError as e:
        logging.getLogger("dth50").error("%s", e)
        return 3
    opts = S.Options(tuner=chain, ts_seconds=a.steady_seconds, settle=a.settle, settle_final=a.settle_final,
                     ts_input=ts_input)
    st = S.Stream(dev, card, vendor, opts)
    log = logging.getLogger("dth50")
    rc = 0
    try:
        st.open(a.channel)
        st.steady()
        out = open(a.output, "wb") if a.output else sys.stdout.buffer
        try:
            st.pump(out, secs=a.seconds)
        finally:
            if a.output:
                out.close()
    except Exception as e:                           # noqa: BLE001
        log.error("%s: %s", type(e).__name__, e)
        rc = 1
    # 途中で落ちても XP の形で閉じる。閉じずに終わると次の warm は入口でコマンドが返らない
    try:
        if not st.close():
            log.warning("閉じる段で返らないコマンドがあった。次は cold が要るかもしれない")
            rc = 3
    except Exception as e:                           # noqa: BLE001
        log.error("閉じる段で失敗: %s", e)
        rc = 3
    return rc


def main(argv=None):
    ap = argparse.ArgumentParser(prog="dth50")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("models", help="挿さっているカードと機種")
    p = sub.add_parser("stream", help="選局して TS を出す")
    p.add_argument("--channel", "-c", type=int, required=True, help="地デジの物理チャンネル（13〜62）")
    p.add_argument("--tuner", "-t", type=int, default=1,
                   help="チューナーの番号（1 = 既定・2 = DT-H50/PCIEW の 2 つ目。vendor/tuner2-mj110.txt が要る）")
    p.add_argument("--output", "-o", help="書き出し先（既定は標準出力・名前付きパイプでもよい）")
    p.add_argument("--ts-input", type=int, default=-1,
                   help="TS の入力番号（副1・副5 の +0x08）を指定する（既定はチューナー番号 - 1）。DT-H50/PCIE の確認用")
    p.add_argument("--seconds", type=float, default=-1.0, help="汲み出す秒数（既定は止められるまで）")
    p.add_argument("--bdf", help="カードの PCI アドレス（複数あるとき）")
    p.add_argument("--vendor", help="ベンダのデータの置き場所（既定は $DTH50_VENDOR か /usr/local/share/dth50）")
    p.add_argument("--cold", action="store_true", help="カウンタが動いていても cold で初期化する")
    p.add_argument("--steady-seconds", type=float, default=3.0, help="汲み出しの前の 0x0b 定常運転の秒数（既定 3）")
    p.add_argument("--settle", type=float, default=0.0, help="SECODE が返ってから次の段までの間（秒・既定 0）")
    p.add_argument("--settle-final", type=float, default=0.0, help="最後の SECODE の後だけ空ける間（秒・既定 0）")
    p.add_argument("--allow-untested", action="store_true", help="実機で未確認の機種でも動かす")
    p = sub.add_parser("bridge", help="Docker の Mirakurun とつなぐ橋（Unix ソケットで待つ・カードを握り続けて 2 チューナー同時）")
    p.add_argument("--sock", default=os.environ.get("DTH50_SOCK", "/run/dth50/tuner.sock"))
    p.add_argument("--vendor", help="stream に渡すベンダのデータの置き場所")
    p.add_argument("--bdf", help="使うカードの PCI アドレス（カンマ区切りで複数・既定は実機で確かめた機種を全部、PCI アドレスの順）。"
                                 "チューナーはカードの順に通し番号（例: PCIEW 2 つ = 1・2、続く PCIE = 3）")
    p.add_argument("--wait-busy", type=float, default=30.0, metavar="SECS",
                   help="同じチューナーの前の回が閉じ終わるのを待つ秒数（既定 30）")
    p.add_argument("--secondary-secode", action="store_true",
                   help="試し: もう 1 本が流れている最中に開く回も、欄を写さずに自分で 0x15・SECODE・三つ組を出す")
    p.add_argument("--secondary-copy", action="store_true",
                   help="試し（以前の動き）: もう 1 本が流れている最中に開く回で、SECODE の欄を写す（鍵の切り替わりで壊れる）")
    p.add_argument("--spawn", action="store_true",
                   help="以前の動き: 要求ごとに stream を起動し、1 本ずつ通す（同時受信なし）")
    p.add_argument("--grace", type=float, default=60.0, metavar="SECS",
                   help="SIGTERM の後、stream が終わるのを待つ秒数（既定 60）。stream は開いている途中の SIGTERM を覚えて"
                        "開き終えてから閉じるので、cold の初期化と開く手順を含めて待てる長さにする")
    p = sub.add_parser("tuner-probe", help="I2C だけで選局してロックを見る（TS は開かない）")
    p.add_argument("--tuner", "-t", type=int, default=1, help="チューナーの番号（1 = 今まで使ってきた側・2 = 2 つ目）")
    p.add_argument("--channel", "-c", type=int, required=True, help="地デジの物理チャンネル（13〜62）")
    p.add_argument("--no-others", dest="others", action="store_false", default=True,
                   help="チューナー 1 の初期化列のうち、2 つ目宛て（0x20・0xC0 直・0x3C。2 つ目を休ませる書き込み）を流さない")
    p.add_argument("--replay", metavar="FILE", action="append",
                   help="初期化列と選局の代わりに、採取した I2C の列（tuner-seq.txt と同じ形式）をそのまま流す")
    p.add_argument("--replay-fast", action="store_true", help="--replay の間隔を詰めて流す（以前の動き）")
    p.add_argument("--lock-wait", type=float, default=4.0)
    p.add_argument("--samples", type=int, default=4, help="ロック待ちの後に状態を読む回数")
    p.add_argument("--bdf")
    p.add_argument("--vendor")
    p.add_argument("--cold", action="store_true")
    p.add_argument("--allow-untested", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, stream=sys.stderr,
                        format="%(asctime)s.%(msecs)03d %(name)s %(message)s", datefmt="%H:%M:%S")
    if a.cmd == "models":
        return models.main()
    if a.cmd in ("stream", "tuner-probe") and not 13 <= a.channel <= 62:
        ap.error("--channel は 13〜62")
    if a.cmd == "stream":
        return cmd_stream(a)
    if a.cmd == "bridge":
        from . import bridge
        return bridge.main(a, pick=_pick_card, lock=_lock)
    if a.cmd == "tuner-probe":
        return cmd_tuner_probe(a)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
