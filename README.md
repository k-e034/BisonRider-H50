# BisonRider-H50

BUFFALO の地上デジタルチューナー **DT-H50/PCIEW** と **DT-H50/PCIE**（チップは ViXS XCode 2100）をLinux で使うためのドライバ。
B-CAS の復号は、ベンダのソフトと同様、チューナーに挿した B-CAS カードで行います。
復号済みの TS を Mirakurun に渡します。

> **ベータ版です。** 開発者の環境 1 台（「開発環境」参照）のみで動作確認を行っていますので録画の取りこぼしやカードの停止が
> 起きる危険性があります。大事な録画には使わないでください。不具合を見つけたら、橋のログ（`journalctl -u dth50-bridge`）を
> 添えて Issues でお知らせください。

## 対応機種

| 機種 | PCI SUBSYS | チューナー | 状態 |
|---|---|---|---|
| DT-H50/PCIEW | `1154:0548` | 2 | 受信、カード内での復号、2 番組同時、Mirakurun からの利用まで確認済み |
| DT-H50/PCIE | `1154:0542` | 1 | 受信、カード内での復号、Mirakurun からの利用（PCIEW と合わせて 3 チューナー）まで確認済み |
| DT-H50/PCIE | `1154:0540` | 1 | 未確認（橋では使いません。`--bdf` で指定すれば試せます） |

挿してあるカードはまとめて扱い、チューナーには通し番号を振ります。PCIEW と PCIE を 1 枚ずつなら 3 本、PCIEW を 2 枚なら
4 本（PCIEW 2 枚の組み合わせは未確認です）。

```bash
python3 -m dth50.models     # 挿さっているカードと機種を表示する
```

機種ごとの違いなどについては [docs/hardware.md](docs/hardware.md) をご覧ください。

## 必要なもの

- 対応するチューナー本体・有効な B-CAS カード（青）
- x86-64 の Linux。IOMMU を有効にし（BIOS の VT-d / AMD-Vi。カーネルによっては `intel_iommu=on` も必要です）、
  チューナーが単独の IOMMU グループに入っている必要があります。チューナーは VFIO で扱うので、root 権限が要ります。
- Python 3.10 以降（標準ライブラリだけで動きます）
- gcc（読み出しを速くする小さな C のモジュールに使います。無くても動きますが遅くなります）
- Mirakurun で使う場合は Docker
- ベンダのデータ（次の「ベンダのデータを用意する」を参照）

## ベンダのデータを用意する

カードを動かすには、ベンダのファームウェアと SECODE、それにベンダのドライバの動作を記録したデータが必要です。
ファームウェアと SECODE は BUFFALO と CyberLink の著作物なので、このリポジトリにも配布物にも含めていません。
メーカーの配布ページからダウンロードしてください。

製品の CD-ROM は必要ありません。メーカーのダウンロードページで公開されている次の 2 つのファイルを使います
（2026 年 9 月の時点で公開を確認しています）。どちらも Windows 用のパッケージです。Linux では展開するだけで、
インストールや実行はしません。

| ダウンロードするファイル | 取り出すもの |
|---|---|
| **DT-H50/PCIEW 用ドライバ 1.63**（`dth50pciewdrv_163.exe`。PCIE だけを使う場合も同じファイル） | `DTH50PCIEWDRV_163/Drivers/BufFullSeg.sys`（ファームウェア）と、同じフォルダの `BufFsLiveFilter.ax`（PCIEW の 2 つ目のチューナーを使う場合だけ） |
| **PCastTV for 地デジ 1.15**（`pcastforchideji_115.exe`） | 中に入っている CyberLink TV Enhance のインストーラーから取り出す `Vixs_RR1_CL.scd`（SECODE） |

あわせて、このリポジトリの Releases から **`dth50-recipes-1.tar.gz`** をダウンロードしてください。カードの起動や選局の
手順を記録したデータで、ベンダのファイルそのものは含んでいません。

⚠️ 製品の CD-ROM に入っているドライバは版が違い、ファームウェアの中身も異なります（このドライバでは互換性未確認）。
動作を確かめているのは、上の版だけです。

```bash
sudo apt install 7zip unshield python3-unicorn python3-pefile
    # 展開に使うツール（unshield は InstallShield の cab を開くのに使います）と、--filter で使う unicorn・pefile
    # 7zip が無い古いバージョン（Ubuntu 22.04 など）では p7zip-full を使ってください
7z x dth50pciewdrv_163.exe -odrv            # → drv/DTH50PCIEWDRV_163/Drivers/BufFullSeg.sys と BufFsLiveFilter.ax
7z x pcastforchideji_115.exe -opcast        # → pcast/PCastTVfor地デジ_115/TVE/TVE_7125.exe
7z x pcast/*/TVE/TVE_7125.exe -otve         # フォルダ名が日本語で、環境によって文字化けするので * で指定する
unshield -d tve/out x tve/data1.cab         # → tve/out/Capture_File_Group/Vixs_RR1_CL.scd

sudo python3 tools/setup_vendor.py --driver drv/DTH50PCIEWDRV_163/Drivers/BufFullSeg.sys \
    --secode tve/out/Capture_File_Group/Vixs_RR1_CL.scd \
    --recipes dth50-recipes-1.tar.gz --filter drv/DTH50PCIEWDRV_163/Drivers/BufFsLiveFilter.ax
```

`--filter` を付けると、`tools/extract_mj110.py` を `sudo` の `python3` で実行します。そのため unicorn と pefile は、
上のように apt でシステムの Python に入れてください。pip で入れる場合は、`sudo` の `python3` から読み込める場所に入れる必要があります。

`/usr/local/share/dth50` に、`firmware.bin`、`secode.scd`、記録データ 4 つ、`tuner2-mj110.txt` が置かれます。
どのファイルも、動作を確かめたものと SHA-256 で照合し、版が違えば処理を止めます。

## 使い方

```bash
make                                                    # 読み出し用のモジュール（dth50/fastread.so）をビルドする
sudo python3 -m dth50 stream --channel 27 > ch27.ts     # 物理チャンネル 27 を受信し、復号済みの TS を出力する（Ctrl-C で終了）
sudo python3 -m dth50 stream --tuner 2 --channel 27 > ch27.ts   # PCIEW の 2 つ目のチューナーを使う
```

Mirakurun（Docker）から使う場合は、ホスト側で橋（`python3 -m dth50 bridge`）を systemd で常駐させます。
導入の手順は [docs/mirakurun.md](docs/mirakurun.md) を見てください（`sudo bash contrib/install.sh install`）。

## 既知の問題

- **PCIEW で 2 番組を同時に受信すると、復号が崩れることがある（対策中）。**
  崩れると、PCIEW で 2 番組を同時に受信したときに、どの局の組み合わせでも両方の映像と音声が壊れます（1 番組だけなら
  正常）。マシンの電源を入れ直すまで元に戻りません（橋を再起動しても戻りません）。
  開発環境で崩れたのは、すべて ch22（関東の TBS）を含む組み合わせでした。この局の暗号化された文字スーパーの ES を
  カードに渡していたことが原因と見ています。2026 年 11 月 18 日に、カードに渡す ES を映像と音声だけにしました。
  その後、ch22 を含む 2 番組同時の受信を 12 回試して、一度も崩れていません（対策の前は 16 回中 4 回崩れていました）。
  ほかの地域の局で同じことが起きるかは不明。
  万一崩れた場合も、橋がそれを検出し、そのカードでは 2 番組目を受けずに、空いている別のカードのチューナーで受信します
  （PCIEW と PCIE が 1 枚ずつなら、崩れた後も 2 番組までは正しく録画できます）。そのときは橋のログに
  「2 本同時の復号が崩れた」と、直前の操作が記録されるので、報告の際に添えてください。
- カードが命令に応答しなくなった場合も、元に戻すにはマシンの電源を入れ直す必要があります（PCIe のリセットがチップまで
  届かないためです）。橋はそのカードへの要求を断り続け、ほかのカードのチューナーは引き続き使えます。
- **データ放送には対応していません。** 出力する TS に、データ放送（BML）は含まれません。データ放送は暗号化されていて、
  ベンダのソフトとは別の方法でカードから取り出しているこのドライバでは、復号されたものを受け取れないためです
  （詳しくは [docs/hardware.md](docs/hardware.md) の「データ放送」）。映像、音声、字幕、番組表（EIT）、PCR、
  ワンセグは含まれます。
- 地上デジタル放送専用です（BS・CS のチューナーはありません）。DT-H50/PCIE の `1154:0540` は未確認です。

## 開発環境

Ubuntu 26.04 LTS（カーネル 7.0）、Python 3.14、Xeon E5-2643 v3、Mirakurun 4.2.1（Docker）、EPGStation。
DT-H50/PCIEW と DT-H50/PCIE を 1 枚ずつ挿し、分配器で同じアンテナにつないで確かめています。

## 仕組み（概要）

1. VFIO でカードを扱い、ファームウェアを起動します（再起動せずに 2 回目以降に開くときは、起動を省きます）。
2. 選局して TS を開き（副1）、届いた PAT と PMT から番組を選びます。
3. ベンダのドライバの「0x31（復号つきで開く）」と同じ手順で、復号器を開きます（副5）。B-CAS の復号はカードの中で行います。
4. 復号済みの映像と音声、平文の PSI/SI をカードから読み出し、1 本の TS にまとめて出力します。
5. Mirakurun とは、ホスト側の橋と Unix ソケットでつなぎます（Mirakurun は Docker で動かします）。

カードの内部については [docs/hardware.md](docs/hardware.md)、実験用のコードから移植したときの記録は
[docs/porting.md](docs/porting.md) にあります。

## ライセンス

GPL-2.0 です（[LICENSE](LICENSE)）。ベンダのファームウェア、SECODE、ドライバは含んでいません。
BUFFALO、ViXS、CyberLink とは一切関係ありません。
