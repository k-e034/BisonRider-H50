#!/usr/bin/env python3
"""配る側の道具: ベンダのデータの置き場所から「採った列」の束（dth50-recipes-N.tar.gz）を作る。

    python3 tools/pack_recipes.py /usr/local/share/dth50 dth50-recipes-1.tar.gz

束に入れるのは、ベンダのドライバを動かしたときのカードへの読み書きと命令の引数を採ったものだけ
（init-recipe.txt・post-recipe.txt・tuner-seq.txt・vendor-args.json）。ファーム・SECODE は入れない。
中身が tools/setup_vendor.py の RECIPES と一致しなければ作らない（束と道具の版を揃えるため）。
"""
import hashlib
import io
import os
import sys
import tarfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from setup_vendor import RECIPES  # noqa: E402

README = """dth50-recipes: BisonRider-H50（DT-H50/PCIEW・DT-H50/PCIE の Linux ドライバ。Python のパッケージ名は dth50）が使う記録データです。

ベンダのドライバがカードを起動・選局するときの読み書きと命令の引数を、実機で記録したものです。
ファームウェアと SECODE は入っていません（手元のドライバとアプリから取り出してください）。使い方は BisonRider-H50 の README を見てください。

  python3 tools/setup_vendor.py --driver BufFullSeg.sys --secode Vixs_RR1_CL.scd --recipes このファイル
"""


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    src, out = sys.argv[1], sys.argv[2]
    files = {}
    for name, want in RECIPES.items():
        data = open(os.path.join(src, name), "rb").read()
        got = hashlib.sha256(data).hexdigest()
        if got != want:
            sys.exit(f"{name} が setup_vendor.py の RECIPES と違う（{got}）。先に RECIPES を直す")
        files[name] = data
    sums = "".join(f"{RECIPES[n]}  {n}\n" for n in files).encode()
    top = os.path.basename(out).split(".tar")[0]
    now = int(time.time())
    with tarfile.open(out, "w:gz") as t:
        for name, data in [("README.txt", README.encode()), ("SHA256SUMS", sums), *files.items()]:
            ti = tarfile.TarInfo(f"{top}/{name}")
            ti.size, ti.mtime, ti.mode = len(data), now, 0o644
            t.addfile(ti, io.BytesIO(data))
    print(f"{out}: {', '.join(files)}")


if __name__ == "__main__":
    main()
