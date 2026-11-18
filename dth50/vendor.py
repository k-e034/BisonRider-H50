"""ベンダ由来のデータ（このリポジトリには入れない）の置き場所と読み込み。

置き場所は、--vendor DIR・環境変数 DTH50_VENDOR・/usr/local/share/dth50 の順に探す。
tools/setup_vendor.py が、利用者の持っているドライバ・アプリと、別に配っている「採った列」の束から揃える（README）。中身:

  firmware.bin        カードのファーム（BUFFALO の PCIEW 用ドライバ 1.63 の BufFullSeg.sys から取り出したもの）
  init-recipe.txt     初期化列（冷えたカードの起動を採ったもの）
  post-recipe.txt     ファーム転送の後の手順
  vendor-args.json    コマンドの引数の雛形（0x19・0x0b・0x08 副1/副5 などを採ったもの）
  tuner-seq.txt       チューナーの I2C 初期化列
  secode.scd          セキュアコード（PCastTV の Vixs_RR1_CL.scd）
  tuner2-mj110.txt    PCIEW の 2 つ目のチューナー（SMT-MJ110）の I2C 列。2 つ目を使うときだけ要る
                      （tools/extract_mj110.py で PCIEW 用ドライバ 1.63 の BufFsLiveFilter.ax から作る）

"""
import hashlib
import json
import os

DEFAULT_DIRS = ("/usr/local/share/dth50",)

FILES = {
    "firmware": "firmware.bin",
    "init_recipe": "init-recipe.txt",
    "post_recipe": "post-recipe.txt",
    "args": "vendor-args.json",
    "tuner_seq": "tuner-seq.txt",
    "secode": "secode.scd",
}

# 使うときだけ要るもの
OPTIONAL = {
    "tuner2_seq": "tuner2-mj110.txt",
}

# 動作を確かめたファーム（PCIEW 用ドライバ 1.63 の BufFullSeg.sys・+0x34a58 から 1,178,032 バイト）
FIRMWARE_SHA256 = "07fe92d4dfe440e4"     # 先頭 16 桁


class VendorError(RuntimeError):
    pass


def find_dir(explicit=None):
    for d in (explicit, os.environ.get("DTH50_VENDOR"), *DEFAULT_DIRS):
        if d and os.path.isdir(d):
            return d
    raise VendorError("ベンダのデータの置き場所が無い（tools/setup_vendor.py で /usr/local/share/dth50 に揃えるか、"
                      "--vendor DIR・DTH50_VENDOR で指定する）")


class Files:
    def __init__(self, directory=None):
        self.dir = find_dir(directory)
        missing = [n for n in FILES.values() if not os.path.isfile(os.path.join(self.dir, n))]
        if missing:
            raise VendorError(f"{self.dir} に足りないもの: {' '.join(missing)}（tools/setup_vendor.py で揃える）")
        self._args = None

    def path(self, key):
        return os.path.join(self.dir, FILES[key])

    @property
    def init_recipe(self):
        return self.path("init_recipe")

    @property
    def post_recipe(self):
        return self.path("post_recipe")

    @property
    def tuner_seq(self):
        return self.path("tuner_seq")

    @property
    def secode(self):
        return self.path("secode")

    @property
    def tuner2_seq(self):
        p = os.path.join(self.dir, OPTIONAL["tuner2_seq"])
        if not os.path.isfile(p):
            raise VendorError(f"{p} が無い（tools/setup_vendor.py に --filter BufFsLiveFilter.ax を付けて作る）")
        return p

    def firmware(self, check=True):
        fw = open(self.path("firmware"), "rb").read()
        if check and not hashlib.sha256(fw).hexdigest().startswith(FIRMWARE_SHA256):
            raise VendorError("firmware.bin が動作を確かめたものと違う（PCIEW 用ドライバ 1.63 から取り出したもの。"
                              "CD 収録の BufFullSeg.sys のファームは別物）")
        return fw

    def args(self):
        """{名前: {オフセット: 値}}（cmd19 / cmd0b / cmd0b_steady / blk20500 / cmd08 / cmd08_sub5）。"""
        if self._args is None:
            raw = json.load(open(self.path("args"), encoding="utf-8"))
            self._args = {k: {int(o, 16): int(v, 16) for o, v in t.items()} for k, t in raw.items()}
        return self._args
