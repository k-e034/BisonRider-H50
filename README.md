# BisonRider-H50

BUFFALO の地上デジタルチューナー **DT-H50/PCIEW** と **DT-H50/PCIE**（チップは ViXS XCode 2100）を
Linux で使うためのドライバ。B-CAS の復号は、ベンダのソフトと同じくチューナー（カード）の中で、挿してある B-CAS カードで行う。
復号済みの TS を Mirakurun に渡せる。

> **ベータ版です。** 開発者の 1 台（下の「確かめた環境」）でしか確かめていません。録画の取りこぼしやカードの停止が
> 起こり得ます。大事な録画には使わないでください。不具合の報告は Issues へ（橋のログ `journalctl -u dth50-bridge` を添えて）。

## 対応機種

| 機種 | PCI SUBSYS | チューナー | 状態 |
|---|---|---|---|
| DT-H50/PCIEW | `1154:0548` | 2 | 2 つとも受信・カード内復号・2 本同時・Mirakurun まで確認 |
| DT-H50/PCIE | `1154:0542` | 1 | 受信・カード内復号・Mirakurun（PCIEW と一緒に 3 チューナー）まで確認 |
| DT-H50/PCIE | `1154:0540` | 1 | 未確認（橋は使わない。`--bdf` で名指しすれば試せる） |

挿したカードを全部まとめて扱い、チューナーに通し番号を振る（PCIEW ＋ PCIE なら 3 本、PCIEW 2 枚なら 4 本。
PCIEW 2 枚は未確認）。

```bash
python3 -m dth50.models     # 挿さっているカードと機種を出す
```

違いと出どころは [docs/hardware.md](docs/hardware.md)。

## 要るもの

- 上のカードと、有効な B-CAS カード（カードのスロットに挿す）
- x86-64 の Linux。IOMMU が有効で（BIOS の VT-d / AMD-Vi。カーネルによっては `intel_iommu=on` が要る）、
  カードが単独の IOMMU グループにいること。カードは VFIO で握るので root が要る
- Python 3.10 以降（標準のモジュールだけで動く）、gcc（汲み出しを速くする小さな C の部品・無くても遅いが動く）
- Mirakurun で使うなら Docker
- **ベンダのデータ**（下の「ベンダのデータを揃える」）

## ベンダのデータを揃える

カードを動かすには、ベンダのファーム・SECODE と、ベンダのドライバの動きを採った列が要る。
ファームと SECODE は BUFFALO / CyberLink の著作物なので、このリポジトリにも配布物にも入れない。利用者が自分で取り出す。

製品の CD-ROM は要らない。BUFFALO のダウンロードページで配っている次の 2 つを使う（2026-09 に配布を確かめた。どちらも Windows 用のパッケージで、
Linux では展開するだけ。入れたり動かしたりはしない）。

| ダウンロードするもの | 取り出すもの |
|---|---|
| **DT-H50/PCIEW 用ドライバ 1.63**（`dth50pciewdrv_163.exe`・PCIE だけを使うときも同じもの） | `DTH50PCIEWDRV_163/Drivers/BufFullSeg.sys`（ファーム）・同じフォルダの `BufFsLiveFilter.ax`（PCIEW の 2 つ目のチューナーを使うときだけ） |
| **PCastTV for 地デジ 1.15**（`pcastforchideji_115.exe`） | 中の CyberLink TV Enhance のインストーラから `Vixs_RR1_CL.scd`（SECODE） |

ほかに、このリポジトリの Releases から **`dth50-recipes-1.tar.gz`**（「採った列」。カードの起動・選局の手順で、ベンダの
ファイルそのものは含まない）を取る。

⚠️ 製品の CD-ROM のドライバは版が違い、ファームの中身も違う（このドライバでは確かめていない）。確かめたのは上の版だけ。

```bash
sudo apt install 7zip unshield python3-unicorn python3-pefile
    # 展開の道具（unshield は InstallShield の cab を開く）と、--filter で使う unicorn・pefile。
    # 7zip が無い古い版（Ubuntu 22.04 など）は p7zip-full
7z x dth50pciewdrv_163.exe -odrv            # → drv/DTH50PCIEWDRV_163/Drivers/BufFullSeg.sys・BufFsLiveFilter.ax
7z x pcastforchideji_115.exe -opcast        # → pcast/PCastTVfor地デジ_115/TVE/TVE_7125.exe
7z x pcast/*/TVE/TVE_7125.exe -otve         # フォルダ名は日本語（展開した環境によって化ける）なので * で指す
unshield -d tve/out x tve/data1.cab         # → tve/out/Capture_File_Group/Vixs_RR1_CL.scd

sudo python3 tools/setup_vendor.py --driver drv/DTH50PCIEWDRV_163/Drivers/BufFullSeg.sys \
    --secode tve/out/Capture_File_Group/Vixs_RR1_CL.scd \
    --recipes dth50-recipes-1.tar.gz --filter drv/DTH50PCIEWDRV_163/Drivers/BufFsLiveFilter.ax
```

`--filter` は `tools/extract_mj110.py` を `sudo` の `python3` で動かすので、unicorn・pefile は apt で（システムの python3 に）入れる。
pip で入れるなら、`sudo` の `python3` から見える所に。

`/usr/local/share/dth50` に `firmware.bin`・`secode.scd`・採った列 4 つ・`tuner2-mj110.txt` が置かれる。
どれも動作を確かめたものと SHA-256 で照らし合わせ、版が違えば止める。

## 使い方

```bash
make                                                    # 汲み出しの部品（dth50/fastread.so）を作る
sudo python3 -m dth50 stream --channel 27 > ch27.ts     # 物理チャンネル 27 を受けて復号済みの TS を出す。Ctrl-C で閉じる
sudo python3 -m dth50 stream --tuner 2 --channel 27 > ch27.ts   # PCIEW の 2 つ目
```

Mirakurun（Docker）から使うときは、ホストの橋（`python3 -m dth50 bridge`）を systemd で常駐させる。
導入は [docs/mirakurun.md](docs/mirakurun.md)（`sudo bash contrib/install.sh install`）。

## 分かっている問題

- **PCIEW の 2 本同時の復号が崩れることがある。** 原因（引き金）は分かっていない。開発機では 2 回起きた
  （20 分の試験の約 1,000 秒目と、Mirakurun で 3 本同時にした最初の回）。崩れると、2 本同時はどの局どうしでも
  2 本とも映像・音声が壊れる。1 本だけなら正しい。マシンの電源を入れ直すまで戻らない（橋の再起動では戻らない）。
  橋はこの状態を見張っていて、崩れたカードでは 2 本目を開かず、その要求は空いている別のカードのチューナーで流す
  （PCIEW ＋ PCIE なら、崩れた後も 2 本までは正しく録れる）。
  ログ（`journalctl -u dth50-bridge`）に「2 本同時の復号が崩れた」と直前の操作が出るので、報告に添えてほしい。
- カードが命令に応答しなくなったときも、戻すにはマシンの電源を入れ直す（PCIe のリセットがチップに届かない）。
  橋はそのカードへの要求を断り続け、他のカードのチューナーは使い続ける。
- **データ放送には対応していない。** 出力の TS に、本放送のデータ放送（BML）は入らない（暗号化されていて、カードの中で
  復号される出口がベンダの使う経路と違うため。[docs/hardware.md](docs/hardware.md) の「データ放送」）。
  映像・音声・字幕・番組表（EIT）・ワンセグは入る。
- 地上デジタルだけ（BS・CS のチューナーは無い）。DT-H50/PCIE の `1154:0540` は未確認。

## 確かめた環境

Ubuntu 26.04 LTS（カーネル 7.0）・Python 3.14・Xeon E5-2643 v3・Mirakurun 4.2.1（Docker）・EPGStation。
DT-H50/PCIEW と DT-H50/PCIE を 1 枚ずつ、分配器で同じアンテナにつないで確かめた。

## 仕組み（概要）

1. VFIO でカードを握り、ファームを起動する（再起動なしの 2 回目以降は warm で起動を飛ばす）
2. 選局し、TS を開く（副1）。届いた PAT/PMT から番組を選ぶ
3. ベンダのドライバの「0x31（復号つきで開く）」と同じ手順で、復号器を開く（副5）。B-CAS の復号はカードの中
4. 復号済みの映像・音声と、平文の PSI/SI をカードから読み出し、1 本の TS にまとめて流す
5. Mirakurun へは、ホストの橋と Unix ソケットでつなぐ（Mirakurun は Docker で動かす）

カードの中のことは [docs/hardware.md](docs/hardware.md)、実験用のコードからの移植の記録は [docs/porting.md](docs/porting.md)。

## ライセンス

GPL-2.0（[LICENSE](LICENSE)）。ベンダのファーム・SECODE・ドライバは含まない。それらの権利は各社にある。
BUFFALO・ViXS・CyberLink とは関係のない、利用者による非公式のドライバです。
