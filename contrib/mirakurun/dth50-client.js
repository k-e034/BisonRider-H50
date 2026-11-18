#!/usr/bin/env node
// dth50-client.js — Docker の Mirakurun から使うチューナーコマンド。Node の標準機能だけで動く。
//
//   tuners.yml:  command: node /dth50/dth50-client.js <channel> <チューナー番号>
//                （チューナー番号は橋の通し番号: カードを PCI アドレスの順に並べ、カードごとのチューナーを続けて数える。
//                 PCIEW 1 枚なら 1・2、PCIEW ＋ PCIE なら 1・2・3。省略すると 1）
//
// ホストの橋（python3 -m dth50 bridge）の Unix ソケットへ「チャンネル番号 [チューナー番号] + 改行」を送り、
// 受けた TS（カード内で復号済み）をそのまま標準出力へ流す。
//   ・SIGTERM / SIGINT（Mirakurun が止めるとき）→ ソケットを切ってすぐ終わる（橋がカード側を閉じる）。
//   ・橋がソケットを閉じたら（受信できない ch など）→ 終わる（Mirakurun のスキャンは no_signal として次へ）。
//   ・ソケットの場所は環境変数 DTH50_SOCK（既定 /dth50/run/tuner.sock）。
'use strict';
const net = require('net');

const ch = process.argv[2] || '';
const tuner = process.argv[3] || '1';
if (!/^\d+$/.test(ch) || !/^([1-9]|1[0-6])$/.test(tuner)) {
    process.stderr.write('usage: dth50-client.js <channel> [tuner 1-16]\n');
    process.exit(2);
}
const path = process.env.DTH50_SOCK || '/dth50/run/tuner.sock';
const sock = net.createConnection(path, () => sock.write(`${ch} ${tuner}\n`));

sock.pipe(process.stdout);
sock.on('close', () => process.exit(0));
sock.on('error', (e) => {
    process.stderr.write(`[dth50-client] ${path}: ${e.message}\n`);
    process.exit(1);
});
process.stdout.on('error', () => sock.destroy());   // 読み手が閉じた（EPIPE）
for (const s of ['SIGTERM', 'SIGINT']) {
    process.on(s, () => sock.destroy());
}
