"""Docker の Mirakurun とホストのカードをつなぐ橋（python3 -m dth50 bridge）。

Mirakurun は公式の推奨どおり Docker で動かす。コンテナの中からは root・VFIO が要るカードを直接扱えないので、
ホストで root として常駐するこの橋と、コンテナの中の薄いクライアント（contrib/mirakurun/dth50-client.js）に分ける。

    Mirakurun（コンテナ）── spawn ── node /dth50/dth50-client.js <ch> <tuner>
                                         │ Unix ソケット（ホストの /run/dth50/tuner.sock）
    ホスト: python3 -m dth50 bridge（root・systemd・カードを握り続ける）

やりとり: クライアントは「チャンネル番号 [チューナー番号] + 改行」を送り、あとは TS（カード内で復号済み）を受けるだけ。
  ・クライアントが切ったら（Mirakurun が SIGTERM で止めた）、その流れを止めて XP の形で閉じる。
  ・開けなかったら（ロックしない等）、ソケットを閉じる → クライアントも終わる → Mirakurun のスキャンは no_signal。

同時受信（既定）: 橋がカードを 1 回だけ開いて握り続け、2 つのチューナーの流れを同じプロセスの中で扱う。
  ・流れが無いときに開く 1 本目は席 0 で、SECODE とカード全体の手順（0x1b・PID 表・0x19）を出す。
  ・もう 1 本が流れている最中に開く回は空いている席で、SECODE を出さず、自分の副1 につないで開くだけ
    （stream.seat_options）。閉じるときは自分の文脈だけを閉じ、最後の 1 本だけが 0x1a を出す。
  ・2 本同時の復号が崩れた状態（STUCK_COUNTER）を見張り、崩れたカードでは 2 本目を開かない（1 本なら正しく復号できる）。
    その要求は、空いている別のカードのチューナーで流す（Mirakurun は断られても別のチューナーへ移らず、同じチューナーを
    やり直し続けるため）。
  ・席のバッファ（IOMMU の割り当て）は橋が持ち続けて使い回す（外すと他方の流れとカードが止まる・Stream の hb）。
  ・カードへの命令（開く・閉じる）は 1 回ずつ通す。同じチューナーへの次の要求は、前の回が閉じ終わるまで最大 --wait-busy 秒待たせる。
  ・閉じる命令が返らなかった・カードを開けなかったときは、流れが無くなってから橋を終える（systemd が起動し直す）。
--spawn: 以前の動き（要求ごとに python3 -m dth50 stream を起動し、1 本ずつ通す）。

実験用リポジトリ（非公開）の tools/dth50-bridge.py から移した。
"""
import collections
import logging
import os
import select
import signal
import socket
import subprocess
import sys
import threading
import time

log = logging.getLogger("dth50.bridge")
# 診断の dump で読む範囲（読むだけ）: ファームの大域（鍵の枠の表 A/B・PID の表 0x8020a440・区画の表・gp の周り）と、
# 流れの文脈 5 つ（0xa02af010〜0xa02d72f0・各 0xa0b8）
DUMP_REGIONS = (("global", 0x80200000, 0x80250000), ("ctx", 0x802AF000, 0x802E2000))
# 2 本同時の復号が崩れた状態の目印（2026-11-18）。正常なカードでは、流れていない・1 本・2 本のどの場面でも 0
# （PCIEW・PCIE とも）。崩れた PCIEW では 1 秒に約 110 ずつ増え続け、閉じても止まらない。崩れると 2 本同時は
# どの局どうしでも 2 本とも壊れ、1 本だけなら正しい。電源を入れ直すまで戻らない。何の数かは未解明
STUCK_COUNTER = 0x8021F4E8
# 崩れたときにログに添える語（正常でも変わるので目印には使わない）: B-CAS カードとの通信の表の頭と +0x110・0x80227e60
STUCK_NOTE = (0x802309F8, 0x80230B08, 0x80227E60)
STUCK_POLL = 1.0                 # 見張りの間隔（秒）
SEAT_GAP = 1.5                   # 同じ席を閉じてから次に開くまでの最小の間（秒）
SUB5_RETRIES = 1                 # 副5 が文脈を返さなかったときのやり直しの回数
BUSY = threading.Lock()          # --spawn: 1 本ずつ通す
STOPPING = threading.Event()     # 橋が SIGTERM を受けた。流している回を閉じてから終わる


def read_request(conn):
    """最初の 1 行「チャンネル [チューナー]」を読む。戻り (ch, tuner) か None。"""
    conn.settimeout(5.0)
    buf = b""
    while b"\n" not in buf and len(buf) < 64:
        chunk = conn.recv(64)
        if not chunk:
            break
        buf += chunk
    words = buf.split(b"\n", 1)[0].decode("ascii", "replace").split()
    if len(words) == 2 and words[0] == "dump":
        label = "".join(c for c in words[1] if c.isalnum() or c in "-_")[:40]
        return ("dump", label) if label else None
    if not 1 <= len(words) <= 2 or not all(w.isdigit() for w in words):
        return None
    ch = int(words[0])
    tuner = int(words[1]) if len(words) == 2 else 1
    if not 13 <= ch <= 62 or not 1 <= tuner <= 16:
        return None
    return ch, tuner


# ---- --spawn（以前の動き）----

def stop(p, sid, grace):
    """stream に SIGTERM を送り、出力を読み捨てながら終わるのを待つ。"""
    if p.poll() is None:
        log.info("[%d] stream（pid %d）に SIGTERM", sid, p.pid)
        p.send_signal(signal.SIGTERM)
    t0 = time.time()
    fd = p.stdout.fileno()
    while p.poll() is None and time.time() - t0 < grace:
        r, _, _ = select.select([fd], [], [], 0.5)
        if r and not os.read(fd, 1 << 16):
            try:
                p.wait(timeout=max(0.1, grace - (time.time() - t0)))
            except subprocess.TimeoutExpired:
                pass
            break
    if p.poll() is None:
        log.warning("[%d] %.0f 秒で終わらない → SIGKILL（カードの閉じる手順が途中かもしれない。次は cold が要ることがある）",
                    sid, grace)
        p.kill()
        p.wait()
    log.info("[%d] stream 終了（%d・%.1f 秒）", sid, p.returncode, time.time() - t0)


def session_spawn(conn, sid, a):
    try:
        req = read_request(conn)
    except OSError as e:
        log.info("[%d] 要求を読めない: %s", sid, e)
        conn.close()
        return
    if req is None:
        log.info("[%d] 要求が「チャンネル [チューナー]」の形でない → 閉じる", sid)
        conn.close()
        return
    if req[0] == "dump":
        log.info("[%d] --spawn では dump を受けない", sid)
        conn.close()
        return
    ch, tuner = req
    log.info("[%d] ch%d（チューナー %d）の要求", sid, ch, tuner)
    if not BUSY.acquire(timeout=a.wait_busy):
        log.info("[%d] %.0f 秒待っても前の回が終わらない → 閉じる", sid, a.wait_busy)
        conn.close()
        return
    total = 0
    try:
        cmd = [sys.executable, "-m", "dth50", "stream", "-c", str(ch), "--tuner", str(tuner)]
        if a.vendor:
            cmd += ["--vendor", a.vendor]
        if a.bdf:
            cmd += ["--bdf", a.bdf]
        # stream の標準エラー（ログ）はそのまま橋の標準エラー（journal）へ
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stdin=subprocess.DEVNULL, start_new_session=True,
                             cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        log.info("[%d] stream を起動（pid %d）", sid, p.pid)
        conn.settimeout(None)
        fd, cfd = p.stdout.fileno(), conn.fileno()
        why = None
        while why is None:
            r, _, _ = select.select([fd, cfd], [], [], 1.0)
            if STOPPING.is_set():
                why = "橋が止まる"
                break
            if cfd in r:
                try:
                    if not conn.recv(4096):
                        why = "クライアントが閉じた"
                except OSError:
                    why = "クライアントが閉じた"
            if why is None and fd in r:
                chunk = os.read(fd, 1 << 16)
                if not chunk:
                    why = "stream が TS を閉じた"
                else:
                    try:
                        conn.sendall(chunk)
                        total += len(chunk)
                    except OSError:
                        why = "クライアントへ書けない"
        log.info("[%d] %s（%.1f MB 流した）", sid, why, total / 1e6)
        stop(p, sid, a.grace)
    finally:
        try:
            conn.close()
        finally:
            BUSY.release()


# ---- 同時受信（既定）----

class SockOut:
    """汲み出しの書き出し先（汲み出しは write と flush だけを使う）。"""

    def __init__(self, conn):
        self.conn = conn

    def write(self, b):
        self.conn.sendall(b)

    def flush(self):
        pass


class CardUnit:
    """1 枚のカード: 握る・席で流れを開く・閉じる。カードが応答しなくなっても、他のカードには影響しない。"""

    def __init__(self, owner, bdf, model):
        self.owner, self.bdf, self.m = owner, bdf, model
        self.cmd = threading.RLock()                 # このカードへの命令（開く・閉じる）を 1 回ずつ
        n = min(2, model.tuners)                     # 席は 2 つまで（席 1 は XP のベンダの 2 本目の形）
        self.seats = [None] * n                      # 席を使っている Stream
        self.closed_at = [0.0] * n                   # 席ごとの最後に閉じた時刻
        self.hbs = [None] * n                        # 席のバッファ（持ち続ける）
        self.dev = self.card = self.lockfile = None
        self.fatal = None                            # 開けなかった（握れない等）
        # カードが命令に応答しない。PCIe のリセットはチップに届かないので、橋を起動し直しても
        # warm で閉じられず同じことを繰り返す（2026-11-11 は 189 回）。このカードへの要求はすぐ断り続ける
        self.dead = None
        self.dead_logged = 0.0
        # 2 本同時の復号が崩れた（STUCK_COUNTER が 0 でない）。1 本は開くが、2 本目は断る（電源を入れ直すまで）
        self.stuck = None
        self.stuck_logged = 0.0
        self.events = collections.deque(maxlen=16)  # 最近の開く・閉じる（崩れたときにログへ出す）

    @property
    def name(self):
        return f"{self.bdf} {self.m.name}"

    def ensure_card(self):
        if self.card is not None:
            return
        from .boot import open_card
        self.lockfile = self.owner.lock(self.bdf)
        if self.lockfile is None:
            raise RuntimeError(f"{self.bdf} は別のプロセスが使っている")
        log.info("カードを開く: %s（チューナー %d）", self.name, self.m.tuners)
        try:
            self.dev, self.card, warm = open_card(self.bdf, self.owner.vendor)
        except SystemExit as e:                      # bind_vfio などは SystemExit で知らせる
            raise RuntimeError(str(e)) from None
        log.info("カードを開いた（%s・%s・区画 %s）", self.bdf, "warm" if warm else "cold", self.pool())

    def note(self, text):
        self.events.append((time.time(), text))

    def check_stuck(self):
        """STUCK_COUNTER を読む（読むだけ）。0 でなくなったら self.stuck を立て、その時の様子をログに出す。"""
        if self.card is None or self.dead or self.stuck:
            return self.stuck
        try:
            v = self.card.read32(STUCK_COUNTER)
        except Exception as e:                       # noqa: BLE001
            log.debug("%s の見張りで読めない: %s", self.name, e)
            return None
        if v == 0:
            return None
        try:
            extra = " ".join(f"0x{x:08x}={self.card.read32(x):08x}" for x in STUCK_NOTE)
        except Exception:                            # noqa: BLE001
            extra = ""
        self.stuck = time.time()
        seats = ", ".join(f"席 {s.seat} ch{getattr(s, 'ch', '?')}{'（1 本目）' if s.o.primary else ''}"
                          for s in self.seats if s is not None) or "なし"
        log.error("%s で 2 本同時の復号が崩れた（0x%08x = 0x%x・%s）。流れている回: %s。"
                  "このカードでは以後 2 本目を開かない（1 本なら復号できる）。戻すにはマシンの電源を入れ直す",
                  self.name, STUCK_COUNTER, v, extra, seats)
        for t, text in self.events:
            log.error("  直前の操作 %s %s", time.strftime("%H:%M:%S", time.localtime(t)) + f".{int(t * 1000) % 1000:03d}",
                      text)
        return self.stuck

    def refuse(self, sid):
        if self.dead:
            if time.time() - self.dead_logged >= 60:
                log.error("[%d] %s が応答しない（%s）→ 断る。マシンの再起動（電源を切る）まで戻らない", sid, self.name,
                          self.dead)
                self.dead_logged = time.time()
            return True
        if self.fatal:
            log.info("[%d] %s を開けない（%s）→ 断る", sid, self.name, self.fatal)
            return True
        return False

    def open(self, sid, ch, local, gtuner):
        """カードのチューナー local（1 始まり）で開いて定常運転まで済ませた Stream を返す。開けなければ None。"""
        from . import card as C
        from . import models
        from . import stream as S
        from .boot import BootError
        with self.cmd:
            if self.refuse(sid):
                return None
            try:
                self.ensure_card()
            except BootError as e:
                log.error("[%d] %s を開けない: %s", sid, self.name, e)
                self.dead = "warm で閉じられない"
                self.dead_logged = time.time()
                return None
            except Exception as e:                   # noqa: BLE001
                log.error("[%d] %s を開けない: %s: %s", sid, self.name, type(e).__name__, e)
                self.fatal = "カードを開けない"
                return None
            chain, ts_input = models.tuner_plan(self.m, local)
            for attempt in range(1 + SUB5_RETRIES):
                free = [i for i, s in enumerate(self.seats) if s is None]
                if not free:
                    log.error("[%d] %s に空いた席が無い", sid, self.name)
                    return None
                seat = free[0]
                other = next((s for s in self.seats if s is not None), None)
                src = 0
                if other is not None and self.check_stuck():
                    if time.time() - self.stuck_logged >= 60:
                        log.warning("[%d] %s は 2 本同時の復号が崩れている → 2 本目（ch%d）は開かない", sid, self.name, ch)
                        self.stuck_logged = time.time()
                    else:
                        log.debug("[%d] %s は 2 本同時の復号が崩れている → 断る", sid, self.name)
                    return None
                if other is not None:
                    src = other.prep_ctx or 0
                    if not C.is_ctx(src):
                        log.error("[%d] %s の流れている方の副5 の文脈が分からない → 開かない", sid, self.name)
                        return None
                # 同じ席を閉じた直後に開くと、副5 が文脈を返さずに断られることがある（2026-11-17 の試験）。少し間を空ける
                gap = SEAT_GAP - (time.time() - self.closed_at[seat])
                if gap > 0:
                    time.sleep(gap)
                opts = S.seat_options(chain, seat, secode_src=src, own_secode=self.owner.a.secondary_secode,
                                      copy=self.owner.a.secondary_copy, ts_input=ts_input)
                if self.hbs[seat] is None:
                    self.hbs[seat] = S.HostBuffers(self.dev, max(2, opts.sub1_bufs), S.SEATS[seat]["dma_iova"],
                                                   0x10000 if seat == 0 else 0)
                st = S.Stream(self.dev, self.card, self.owner.vendor, opts, hb=self.hbs[seat])
                st.seat = seat
                st.unit = self
                st.ch = ch
                self.seats[seat] = st
                log.info("[%d] ch%d をチューナー %d（%s のチューナー %d）・席 %d で開く（%s%s）", sid, ch, gtuner, self.name,
                         local, seat,
                         ("もう 1 本が流れている・" + (f"欄を 0x{src:08x} から写す" if opts.secode_src
                                                     else "SECODE も写しもしない")) if src else "1 本目・SECODE を出す",
                         f"・やり直し {attempt} 回目" if attempt else "")
                try:
                    st.open(ch)
                    st.steady()
                except Exception as e:               # noqa: BLE001
                    log.error("[%d] 開けない: %s: %s（%s・区画 %s）", sid, type(e).__name__, e, self.bdf, self.pool())
                    self.note(f"[{sid}] ch{ch} 席 {seat} 開けない（{type(e).__name__}）")
                    self.close(sid, st)
                    if self.dead or not st.sub5_failed or attempt >= SUB5_RETRIES:
                        return None
                    continue
                log.info("[%d] 開いた（%s・区画 %s）", sid, self.bdf, self.pool())
                self.note(f"[{sid}] ch{ch} チューナー {local} 席 {seat} を開いた（{'1 本目' if not src else '2 本目'}）")
                self.check_stuck()
                return st
            return None

    def pool(self):
        """区画の使用数・表 1/2 に繋がっている文脈・鍵の枠（読むだけ・診断のため）。"""
        from . import card as C
        if self.card is None:
            return "--"
        try:
            u = self.card.pool_usage()
            t1 = self.card.ctx_list(C.CTX_LIST_HEAD)
            t2 = self.card.ctx_list2()
        except Exception as e:                       # noqa: BLE001
            return f"-- {e}"
        f = (lambda xs: ",".join(f"{x & 0xFFFFF:05x}" for x in xs) or "-")
        try:
            ka, kb = self.card.key_slots()
            keys = f"・鍵 A {len(ka)}{ka} B {len(kb)}{kb}"
        except Exception:                            # noqa: BLE001
            keys = ""
        return f"{'--' if u is None else f'{u[0]}/{u[1]}'}・表1 {f(t1)}・表2 {f(t2)}{keys}"

    def close(self, sid, st):
        with self.cmd:
            last = all(s is None or s is st for s in self.seats)
            try:
                ok = st.close(label=f"[{sid}]", last=last)
            except Exception as e:                   # noqa: BLE001
                log.error("[%d] 閉じる段で失敗: %s", sid, e)
                ok = False
            self.seats[st.seat] = None
            self.closed_at[st.seat] = time.time()
            self.note(f"[{sid}] ch{getattr(st, 'ch', '?')} 席 {st.seat} を閉じた{'' if ok else '（返らない）'}")
            self.check_stuck()
            log.info("[%d] 閉じた（%s・区画 %s）", sid, self.bdf, self.pool())
            if not ok:
                log.error("[%d] 閉じる命令が返らなかった。%s が応答しない。以後このカードへの要求は断る（マシンの再起動まで）",
                          sid, self.name)
                self.dead = "閉じる命令が返らない"
                self.dead_logged = time.time()

    def idle(self):
        return all(s is None for s in self.seats)


class Owner:
    """挿さっているカードを握り、カードをまたいで通し番号を振ったチューナーで流れを開く・閉じる。

    チューナーの番号は、カードを PCI アドレスの順に並べ、カードごとのチューナー（機種表の順）を続けて数える。
    例（DT-H50/PCIEW 0000:04:00.0 ＋ DT-H50/PCIE 0000:83:00.0）: 1・2 = PCIEW のチューナー 1・2、3 = PCIE のチューナー 1。"""

    def __init__(self, a, pick, lock):
        from . import models
        from .vendor import Files
        self.a, self.pick, self.lock = a, pick, lock
        self.vendor = Files(a.vendor)
        if a.bdf:
            found = []
            for bdf in a.bdf.split(","):
                try:
                    found.append(self.pick(bdf.strip(), False))
                except SystemExit as e:
                    log.error("カード %s を使えない: %s", bdf, e)
        else:
            found = [(b, m) for b, _ss, m in models.scan() if m is not None and m.tested]
            for b, ss, m in models.scan():
                if m is None or not m.tested:
                    log.info("%s（SUBSYS %04x:%04x）は %s → 使わない（--bdf で名指しすれば試せる）", b, ss[0], ss[1],
                             "対応表に無い" if m is None else f"{m.name}・実機で未確認")
        self.units = [CardUnit(self, b, m) for b, m in sorted(found)]
        self.tuner_map = {}                          # 通し番号 → (CardUnit, カードのチューナー番号)
        for u in self.units:
            for local in range(1, u.m.tuners + 1):
                self.tuner_map[len(self.tuner_map) + 1] = (u, local)
        self.tuners = {g: threading.Lock() for g in self.tuner_map}
        for g, (u, local) in self.tuner_map.items():
            log.info("チューナー %d = %s のチューナー %d", g, u.name, local)
        if not self.tuner_map:
            log.error("使えるカードが無い")
        threading.Thread(target=self.watch_stuck, name="watch-stuck", daemon=True).start()

    def watch_stuck(self):
        """開いているカードの STUCK_COUNTER を STUCK_POLL 秒ごとに読む（崩れた時刻をログに残すため）。"""
        while not STOPPING.is_set():
            for u in self.units:
                u.check_stuck()
            STOPPING.wait(STUCK_POLL)

    def open(self, sid, ch, gtuner):
        u, local = self.tuner_map[gtuner]
        if u.stuck and not u.idle():
            # 崩れたカードの 2 本目になる。空いている別のカードのチューナーを借りて流す
            st = self.borrow(sid, ch, gtuner, u)
            if st is not None:
                return st
        return u.open(sid, ch, local, gtuner)

    def borrow(self, sid, ch, gtuner, bad):
        """bad 以外のカードで、今すぐ使えるチューナー（錠が取れて、そのカードで開ける）を借りて開く。"""
        for g, (v, local) in self.tuner_map.items():
            if v is bad or v.dead or v.fatal or (v.stuck and not v.idle()):
                continue
            if all(s is not None for s in v.seats):
                continue
            lk = self.tuners[g]
            if not lk.acquire(blocking=False):
                continue
            log.warning("[%d] %s は 2 本同時の復号が崩れている → ch%d はチューナー %d の代わりにチューナー %d（%s）で流す",
                        sid, bad.name, ch, gtuner, g, v.name)
            st = None
            try:
                st = v.open(sid, ch, local, g)
            finally:
                if st is None:
                    lk.release()
            if st is not None:
                st.borrowed = lk                     # 閉じたら返す（session）
                return st
        log.warning("[%d] %s は 2 本同時の復号が崩れていて、代わりに使えるチューナーも無い → ch%d は開かない", sid, bad.name, ch)
        return None

    def close(self, sid, st):
        st.unit.close(sid, st)

    @property
    def fatal(self):
        """どのカードも開けない（握れない等）なら、その理由。"""
        if self.units and all(u.fatal for u in self.units):
            return "; ".join(f"{u.bdf}: {u.fatal}" for u in self.units)
        return None

    def idle(self):
        return all(u.idle() for u in self.units)

    def dump(self, label):
        """診断: 開いているカードのファームの大域と流れの文脈を読み（書かない）、/tmp/dth50-dump/<label>/ に置く。"""
        import json
        base = os.path.join("/tmp/dth50-dump", label)
        os.makedirs(base, exist_ok=True)
        done = []
        for u in self.units:
            if u.card is None:
                continue
            meta = {"bdf": u.bdf, "model": u.m.name, "time": time.time(), "dead": u.dead, "stuck": u.stuck,
                    "seats": [None if s is None else {"seat": s.seat, "ch": getattr(s, "ch", None),
                                                      "primary": s.o.primary, "prep_ctx": s.prep_ctx}
                              for s in u.seats]}
            tag = u.bdf.replace(":", "-")
            for name, lo, hi in DUMP_REGIONS:
                data = bytearray()
                for a in range(lo, hi, 0x1000):
                    with u.card.lock:
                        data += u.card.read(a, min(0x1000, hi - a))
                with open(os.path.join(base, f"{tag}-{name}-{lo:08x}.bin"), "wb") as f:
                    f.write(data)
            with open(os.path.join(base, f"{tag}-meta.json"), "w") as f:
                json.dump(meta, f, ensure_ascii=False)
            done.append(u.bdf)
        log.info("dump %s: %s", base, " ".join(done) or "（開いているカードが無い）")
        return f"{base} {' '.join(done)}"


def session(conn, sid, own):
    from . import stream as S
    try:
        req = read_request(conn)
    except OSError as e:
        log.info("[%d] 要求を読めない: %s", sid, e)
        conn.close()
        return
    if req is None:
        log.info("[%d] 要求が「チャンネル [チューナー]」の形でない → 閉じる", sid)
        conn.close()
        return
    if req[0] == "dump":
        try:
            out = own.dump(req[1])
            conn.sendall((out + "\n").encode())
        except Exception as e:                       # noqa: BLE001
            log.error("[%d] dump に失敗: %s", sid, e)
        finally:
            conn.close()
        return
    ch, tuner = req
    log.info("[%d] ch%d（チューナー %d）の要求", sid, ch, tuner)
    tl = own.tuners.get(tuner)
    if tl is None:
        log.info("[%d] チューナー %d は無い（%d 個）→ 閉じる", sid, tuner, len(own.tuners))
        conn.close()
        return
    if not tl.acquire(timeout=own.a.wait_busy):
        log.info("[%d] %.0f 秒待ってもチューナー %d の前の回が終わらない → 閉じる", sid, own.a.wait_busy, tuner)
        conn.close()
        return
    st = None
    try:
        st = own.open(sid, ch, tuner)
        if st is None:
            return
        conn.settimeout(None)

        def watch():
            # クライアントは要求の後に何も送らない。読めて 0 バイトなら切れた
            try:
                while not st.stop.is_set() and not STOPPING.is_set():
                    r, _, _ = select.select([conn], [], [], 1.0)
                    if r and not conn.recv(4096):
                        break
            except OSError:
                pass
            st.stop.set()

        threading.Thread(target=watch, name=f"watch-{sid}", daemon=True).start()
        n = 0
        try:
            n = st.pump(SockOut(conn))
        except Exception as e:                       # noqa: BLE001
            log.error("[%d] 汲み出しで失敗: %s: %s", sid, type(e).__name__, e)
        st.stop.set()
        log.info("[%d] %s（%.1f MB 流した）", sid, "橋が止まる" if STOPPING.is_set() else "流れを止めた", n / 1e6)
        try:
            conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        own.close(sid, st)
    finally:
        try:
            conn.close()
        finally:
            tl.release()
            lk = getattr(st, "borrowed", None)
            if lk is not None:
                lk.release()


def main(a, pick=None, lock=None):
    from . import stream as S
    os.makedirs(os.path.dirname(a.sock), exist_ok=True)
    if os.path.exists(a.sock):
        os.unlink(a.sock)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(a.sock)
    os.chmod(a.sock, 0o666)
    srv.listen(4)
    srv.settimeout(1.0)
    own = None if a.spawn else Owner(a, pick, lock)
    log.info("待ち受け %s（%s）", a.sock, "要求ごとに stream を起動・1 本ずつ" if a.spawn else "カードを握り続ける・同時受信")

    def on_signal(n, _f):
        log.info("%s を受けた → 流している回を閉じてから終わる", signal.Signals(n).name)
        STOPPING.set()
        S.STOP.append(n)

    for s in (signal.SIGTERM, signal.SIGINT):
        signal.signal(s, on_signal)
    n = 0
    threads = []
    rc = 0
    try:
        while not STOPPING.is_set():
            if own is not None and own.fatal and own.idle():
                log.error("橋を終える: %s", own.fatal)
                rc = 1
                break
            try:
                conn, _ = srv.accept()
            except (socket.timeout, InterruptedError):
                continue
            n += 1
            target, arg = (session_spawn, a) if own is None else (session, own)
            t = threading.Thread(target=target, args=(conn, n, arg), daemon=True)
            t.start()
            threads = [x for x in threads if x.is_alive()] + [t]
    finally:
        srv.close()
        if os.path.exists(a.sock):
            os.unlink(a.sock)
        STOPPING.set()
        S.STOP.append(0)
        for t in threads:
            t.join(timeout=a.grace + 5)
    return rc
