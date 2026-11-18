#!/usr/bin/env python3
"""ベンダのデータの置き場所（既定 /usr/local/share/dth50）を揃える。標準のモジュールだけで動く（--filter を除く）。

    sudo python3 tools/setup_vendor.py --driver BufFullSeg.sys --secode Vixs_RR1_CL.scd \\
        --recipes dth50-recipes-1.tar.gz [--filter BufFsLiveFilter.ax] [--out /usr/local/share/dth50]

  --driver   BUFFALO の DT-H50/PCIEW 用ドライバ 1.63 の BufFullSeg.sys（DT-H50/PCIE だけを使うときも同じもの）。
             ここから firmware.bin を取り出す。製品 CD のドライバは版が違い、ファームも別物（確かめていない）
  --secode   PCastTV for 地デジ 1.15（pcastforchideji_115.exe）の中の TVE_7125.exe → data1.cab を unshield で開いた
             Capture_File_Group/Vixs_RR1_CL.scd（PCastTV を入れた Windows なら Kernel\\TV\\ にある）→ secode.scd
  --recipes  別に配っている「採った列」の束（init-recipe.txt・post-recipe.txt・tuner-seq.txt・vendor-args.json）
  --filter   PCIEW の 2 つ目のチューナーを使うときだけ: 同じドライバの BufFsLiveFilter.ax から tuner2-mj110.txt を作る
             （tools/extract_mj110.py を呼ぶ。pip install unicorn pefile が要る）

ファーム・SECODE・フィルタは BUFFALO / CyberLink の著作物なので、このリポジトリにも束にも入れない。
利用者が自分の持っているドライバ・アプリから取り出す。
"""
import argparse
import hashlib
import os
import subprocess
import sys
import tarfile

HERE = os.path.dirname(os.path.abspath(__file__))

# 動作を確かめたもの
FW_OFFSET, FW_SIZE = 0x34A58, 1178032                  # BufFullSeg.sys（1.63・1,411,640 バイト）の中のファーム
FW_SHA256 = "07fe92d4dfe440e48e451eac6fb405b4dee21948851d497519592b68590bb9b4"
DRIVER_SHA256 = "1dbf2ad1b7819d4e3f58e3ea6b7463664bd6b2d32a67561bd0b2c9bab5d5573d"
SECODE_SHA256 = "534f9591aaa7d009c67ed16c041fbebd4d4744077a7e0c0a993641c5afda6127"
MJ110_SHA256 = "fd6dd9935cad30224d80fb27a0ca66c5dc49e1f5a31dcd981d719ba70c48899f"
# 束 1 の中身（tools/pack_recipes.py が作る）
RECIPES = {
    "init-recipe.txt": "af72528f67f58922b5a8a2446296da39b264c5d93188e558b9267fa210f2b82e",
    "post-recipe.txt": "fd3829b463dc55e7a77ae83207b866d4afc2701da397a1255f0a0d750ebc2157",
    "tuner-seq.txt": "334b3c8979edbb09ca48d78cd4e3d512f0b382d3b067b4e69d7b38ad10f34b95",
    "vendor-args.json": "d4225d1fb077e61c74d452b68b4c2b7ed0baa7a3f420c157b60bfe65e02e4cc3",
}


def sha(b):
    return hashlib.sha256(b).hexdigest()


def put(out, name, data):
    p = os.path.join(out, name)
    with open(p, "wb") as f:
        f.write(data)
    os.chmod(p, 0o644)
    print(f"  {p}（{len(data):,} バイト）")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default="/usr/local/share/dth50")
    ap.add_argument("--driver", required=True)
    ap.add_argument("--secode", required=True)
    ap.add_argument("--recipes", required=True)
    ap.add_argument("--filter")
    a = ap.parse_args()

    drv = open(a.driver, "rb").read()
    fw = drv[FW_OFFSET:FW_OFFSET + FW_SIZE]
    if sha(fw) != FW_SHA256:
        sys.exit(f"{a.driver} から動作を確かめたファームが取り出せない（ドライバの版が違う? 1.63 の BufFullSeg.sys・"
                 f"{len(drv):,} バイト・sha256 {sha(drv)[:16]}…。確かめたもの: 1,411,640 バイト・{DRIVER_SHA256[:16]}…）")
    scd = open(a.secode, "rb").read()
    if sha(scd) != SECODE_SHA256:
        print(f"⚠️ {a.secode} は動作を確かめた SECODE と違う（sha256 {sha(scd)[:16]}…）。そのまま置くが、復号できないかもしれない",
              file=sys.stderr)

    recipes = {}
    with tarfile.open(a.recipes) as t:
        for m in t.getmembers():
            name = os.path.basename(m.name)
            if m.isfile() and name in RECIPES:
                recipes[name] = t.extractfile(m).read()
    for name, want in RECIPES.items():
        if name not in recipes:
            sys.exit(f"{a.recipes} に {name} が無い")
        if sha(recipes[name]) != want:
            sys.exit(f"{a.recipes} の {name} が壊れている・版が違う（sha256 {sha(recipes[name])[:16]}…）")

    mj110 = None
    if a.filter:
        r = subprocess.run([sys.executable, os.path.join(HERE, "extract_mj110.py"), a.filter], capture_output=True)
        if r.returncode != 0:
            sys.exit(f"tools/extract_mj110.py が失敗した:\n{r.stderr.decode(errors='replace')}")
        mj110 = r.stdout
        if sha(mj110) != MJ110_SHA256:
            print(f"⚠️ 作った tuner2-mj110.txt が確かめたものと違う（sha256 {sha(mj110)[:16]}…）", file=sys.stderr)

    os.makedirs(a.out, exist_ok=True)
    print(f"{a.out} に置く:")
    put(a.out, "firmware.bin", fw)
    put(a.out, "secode.scd", scd)
    for name, data in recipes.items():
        put(a.out, name, data)
    if mj110 is not None:
        put(a.out, "tuner2-mj110.txt", mj110)
    elif not os.path.isfile(os.path.join(a.out, "tuner2-mj110.txt")):
        print("（tuner2-mj110.txt は作っていない。PCIEW の 2 つ目のチューナーを使うなら --filter を付けてもう一度）")


if __name__ == "__main__":
    main()
