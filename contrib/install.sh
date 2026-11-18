#!/bin/bash
# Docker の Mirakurun からこのドライバを使うための導入（橋・クライアント・Mirakurun の設定）。
#
#   sudo bash contrib/install.sh install [--vendor DIR] [--mirakurun DIR]
#   sudo bash contrib/install.sh uninstall
#
# 入れるもの:
#   /etc/systemd/system/dth50-bridge.service   橋（python3 -m dth50 bridge・root で常駐）
#   /usr/local/share/dth50/dth50-client.js     Mirakurun のコンテナの中で動かすクライアント
#   <mirakurun>/docker-compose.override.yml    公式の docker-compose.yml に重ねる（/run/dth50 とクライアントを渡す）
#   <mirakurun>/config/tuners.yml              チューナーの設定（既にあれば .bak を残して置き換える）
# 既定: --vendor = /usr/local/share/dth50・--mirakurun = /opt/mirakurun（公式の手順の置き場所）
#
# Mirakurun そのものは入れない（公式の手順: docs/mirakurun.md）。入れた後に
#   cd <mirakurun> && docker compose up -d   でコンテナを作り直す（override を効かせるため）。
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(dirname "$HERE")"
VENDOR=/usr/local/share/dth50
MK=/opt/mirakurun
SHARE=/usr/local/share/dth50
UNIT=/etc/systemd/system/dth50-bridge.service

cmd="${1:-}"
shift || true
while [ $# -gt 0 ]; do
    case "$1" in
        --vendor) VENDOR="$(realpath "$2")"; shift 2 ;;
        --mirakurun) MK="$2"; shift 2 ;;
        *) echo "知らない引数: $1" >&2; exit 2 ;;
    esac
done
[[ $EUID -eq 0 ]] || { echo "root で実行してください: sudo bash $0 $cmd" >&2; exit 1; }

case "$cmd" in
install)
    [ -d "$VENDOR" ] || { echo "ベンダのデータの置き場所が無い: $VENDOR（--vendor で指定）" >&2; exit 1; }
    install -d "$SHARE"
    # Mirakurun のコンテナはこのファイルを 1 つだけ bind しているので、置き換え（別の inode）ではなく上書きする
    cp "$HERE/mirakurun/dth50-client.js" "$SHARE/dth50-client.js" && chmod 644 "$SHARE/dth50-client.js"
    sed -e "s|@REPO@|$REPO|g" -e "s|@VENDOR@|$VENDOR|g" "$HERE/systemd/dth50-bridge.service" > "$UNIT"
    chmod 644 "$UNIT"
    systemctl daemon-reload
    systemctl enable dth50-bridge
    systemctl restart dth50-bridge
    if [ -d "$MK" ]; then
        install -m 644 "$HERE/mirakurun/docker-compose.override.yml" "$MK/docker-compose.override.yml"
        install -d "$MK/config"
        if [ -f "$MK/config/tuners.yml" ] && ! cmp -s "$HERE/mirakurun/tuners.yml" "$MK/config/tuners.yml"; then
            cp -a "$MK/config/tuners.yml" "$MK/config/tuners.yml.bak"
            echo "前の tuners.yml を $MK/config/tuners.yml.bak に残しました"
        fi
        install -m 644 "$HERE/mirakurun/tuners.yml" "$MK/config/tuners.yml"
        echo "Mirakurun の設定を置きました。作り直す: cd $MK && docker compose up -d"
    else
        echo "⚠️ $MK が無いので Mirakurun の設定は置いていません（--mirakurun で指定）"
    fi
    sleep 1
    journalctl -u dth50-bridge -n 3 --no-pager || true
    ;;
uninstall)
    systemctl disable --now dth50-bridge 2>/dev/null || true
    rm -f "$UNIT" "$SHARE/dth50-client.js"
    systemctl daemon-reload
    echo "橋とクライアントを外しました（Mirakurun の設定は残しています）"
    ;;
*)
    echo "使い方: sudo bash $0 {install|uninstall} [--vendor DIR] [--mirakurun DIR]" >&2
    exit 2
    ;;
esac
