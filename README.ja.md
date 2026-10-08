# Cardputer CLI Buddy

[English](README.md) | 日本語

![デモ: Cardputer から権限の承認、AskUserQuestion への回答、かなでのプロンプト送信を行う様子](docs/demo.gif)

M5Stack Cardputer-Adv から、Wi-Fi か BLE 経由で複数の Claude Code CLI セッションを操作する。

- 権限ダイアログに allow / deny で答える（allow は全文を表示できたときだけ）
- AskUserQuestion に答える
- プロンプトを送る
- セッションログを読む
- かなで入力する（Tab で 英数 / ひらがな / カタカナ を切り替える）
- 内蔵マイクで、日本語か英語のプロンプトを音声入力する（文字起こしは Mac 上でオンデバイスで行う）
- ヘッダにバッテリー残量（充電中かどうかも）を表示する

## 構成

```mermaid
flowchart LR
  subgraph host["ホスト（macOS）"]
    cc1["Claude Code セッション<br>+ mod/"]
    cc2["Claude Code セッション<br>+ mod/"]
    d["daemon/（buddyd）"]
  end
  dev["Cardputer-Adv<br>device/"]
  cc1 -- "HTTP over Unix socket<br>~/.cardbuddy/buddyd.sock" --> d
  cc2 -- "HTTP over Unix socket" --> d
  d <-- "Wi-Fi（TCP）か BLE（NUS）<br>AES-128-CTR + HMAC-SHA256" --> dev
```

| ディレクトリ | 役割 |
| --- | --- |
| `mod/` | Claude Code の plugin。function hooks がセッションの登録、AskUserQuestion の中継、プロンプトの投入、セッションログの送信を行う。同梱の `PermissionRequest` コマンド hook が、端末のダイアログと並行してデバイスの回答を待つ。 |
| `daemon/` | buddyd（Python、bleak）。Unix socket `~/.cardbuddy/buddyd.sock` で mod と話し、Wi-Fi（TCP）か BLE でデバイスとつなぐ。CLI `buddy`（`pair` / `status` / `install-agent`）を含む。 |
| `device/` | Cardputer-Adv 用の MicroPython アプリ。[moremas/build-with-claude](https://github.com/moremas/build-with-claude) の buddy（Apache-2.0）を改変したもの。 |
| `PROTOCOL.md` | buddyd とデバイスの間の通信仕様と脅威モデル。 |
| `docs/daemon-api.md` | mod と buddyd の間の HTTP API。 |
| `testvectors/` | 暗号層のテストベクタ。daemon と device の両方のテストで使う。 |

## 必要なもの

- M5Stack Cardputer-Adv
- macOS（`buddy install-agent` は launchd 用。buddyd 自体は bleak が動く環境なら動く）
- Python 3.10 以上と [uv](https://docs.astral.sh/uv/)
- Claude Code
- 音声入力を使う場合は、macOS 26 以上と Swift 6.2 以上（Xcode か Command Line Tools）

## セットアップ

以下のコマンドは、リポジトリのルートで実行する。シリアルポートは環境ごとに異なる（macOS では `/dev/cu.usbmodem*`）。

### 1. ファームウェアを書き込む

UIFlow2 の **v2.4.2** を書き込む。それより新しい版は使わない。

- v2.4.3 以降（ESP-IDF 5.5 系）には、Cardputer-Adv のマイクが無音になる回帰がある（[m5stack/uiflow-micropython#97](https://github.com/m5stack/uiflow-micropython/pull/97)、[espressif/esp-idf#18621](https://github.com/espressif/esp-idf/issues/18621)）。
- M5Burner で「UIFlow2.0 Cardputer-Adv」を選び、版に 2.4.2 を指定して書き込む。
- Cardputer-Adv は USB ネイティブのため、ダウンロードモードへはボタンでしか入れない。背面の BtnG0 を押したまま BtnRST を押して離し、その後 BtnG0 を離す。

v2.4.2 は、通常起動時の USB を TinyUSB CDC として出す。リセット後にシリアルポートが戻ってこない場合は、USB ケーブルを挿し直す。

### 2. デバイスにアプリを入れる

```sh
uv --directory daemon run python ../device/scripts/deploy.py --port /dev/cu.usbmodemXXXX
```

`device/scripts/deploy.py` は次のことを行う。

1. 大きいモジュール（`crypto`、`buddy_protocol`、`buddy_ble`、`buddy_ui_cp`、`kana`）を mpy-cross で `.mpy` にする。デバイスの空きメモリは約 60KB しかなく、`.py` のままでは import 時のコンパイルでメモリが足りなくなるため。
2. `.mpy` と、`.py` のまま入れるファイル（`main.py`）を `/flash/` に書き込む。
3. デバイス上の同名の `.py` を消す。MicroPython は `.mpy` より `.py` を優先して import するため。以前の版が入れていて今は使わないファイル（ランチャーと `apps/` のアプリ）も消す。
4. NVS の `uiflow.boot_option` を 2 にする。起動時に UIFlow のランチャーではなく `/flash/main.py` が動くようになる。
5. デバイスを再起動する。

mpy-cross の版は、デバイスの MicroPython に合わせる必要がある。UIFlow2 v2.4.2 は MicroPython 1.25（mpy v6.3）なので、既定値の `--mpy-cross 1.25` のままでよい。mpy-cross は `uvx` で取得する。`--port` を省くと、コンパイルだけを行って結果を表示する。

### 3. 鍵を共有する（`buddy pair`）

```sh
uv --directory daemon run buddy pair --port /dev/cu.usbmodemXXXX
```

- 32 byte の鍵を作り、ホストの `~/.cardbuddy/key`（0600）と、デバイスの `/flash/cardbuddy.key` に USB 経由で書き込む。鍵は BLE には流さない。
- デバイスの広告名（`Claude_` + BT MAC の下位 6 桁）を `~/.cardbuddy/device` に保存する。buddyd はこの名前に完全一致で接続する。
- 既存の鍵があればそれを使う。作り直すときは `--rotate` を付ける。
- Wi-Fi を使う場合は `--wifi` を付ける。SSID とパスワード（入力は表示しない）を尋ね、デバイスの `/flash/cardbuddy_wifi.json` に平文で書き込む。
- 書き込んだら、デバイスをリセットして鍵を読み込ませる。

### 4. buddyd を起動する

手元で動かす場合は次のとおり。

```sh
uv --directory daemon run buddyd
```

ログイン時に自動で起動させる場合は、launchd の LaunchAgent を登録する。

```sh
uv --directory daemon run buddy install-agent
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.sohsatoh.cardbuddy.buddyd.plist
```

- ログは `~/.cardbuddy/buddyd.log` に出る。
- 状態は `uv --directory daemon run buddy status` で確認できる。
- 鍵を作り直したときは、buddyd を再起動する（`launchctl kickstart -k gui/$(id -u)/com.sohsatoh.cardbuddy.buddyd`）。
- 初回は、macOS が Bluetooth の利用許可を求めることがある。

Wi-Fi について：

- buddyd は全インターフェースの TCP 47823 で待ち受け、2 秒ごとに UDP のビーコン（`cardbuddy/1 <ポート>`）を 47824 へブロードキャストする。デバイスはビーコンで Mac を見つけて接続するので、Mac とデバイスは同じネットワークセグメントにいる必要がある。
- macOS のアプリケーションファイアウォールが有効な場合、初回に buddyd（Python）への受信接続を許可するか尋ねられる。許可しないと、デバイスは Wi-Fi で接続できない。
- Wi-Fi を優先する。Wi-Fi のセッションがある間、buddyd は BLE を探さず、BLE の接続は切る。Wi-Fi のセッションが切れたら、自動で BLE に戻る。どちらでつながっているかは `buddy status` で確認できる。
- ポートは `buddyd --tcp-port <ポート>` で変えられる。BLE だけで使う場合は `buddyd --no-wifi` で起動する（launchd で使う場合は、LaunchAgent の plist の `ProgramArguments` に足す）。

### 5. Claude Code に mod を読み込ませる

起動ごとに指定する場合は次のとおり。

```sh
claude --plugin-dir /path/to/cardputer-cli-buddy/mod
```

常に読み込ませる場合は、settings の `env` に `CLAUDE_CODE_PLUGIN_DIRS` を設定する。

```json
{
  "env": {
    "CLAUDE_CODE_PLUGIN_DIRS": "/path/to/cardputer-cli-buddy/mod"
  }
}
```

buddyd に登録されたセッションには番号（1〜9）が付き、端末のステータスラインに `Buddy #n` と出る。デバイスの一覧の番号と一致する。

### 6. デバイスでアプリを起動する

デバイスは起動するとアプリ（`main.py`）を動かす。`Q` を押すとデバイスが再起動し、アプリが起動し直す。開発時は、USB シリアルから Ctrl-C を送るとアプリが止まり、REPL に入る。

### 7. 音声入力の文字起こしを用意する

buddyd は、デバイスで録音した音声を `daemon/stt/stt` で文字起こしする。macOS 26 の SpeechTranscriber をオンデバイスで使うため、音声は Mac の外に出ない。

```sh
make -C daemon/stt
```

- `swiftc` が PATH に無い場合は、`make -C daemon/stt SWIFTC=/path/to/swiftc` のように指定する。
- 言語（日本語 `ja-JP` / 英語 `en-US`）ごとの音声認識モデルは、初回の文字起こしのときに macOS がダウンロードする。ネットワークが必要で、数秒〜数十秒かかる（英語で約 12 秒だった）。初回の待ちを避けるには、先に次のコマンドでダウンロードしておく。

  ```sh
  daemon/stt/stt --lang ja-JP --prepare
  daemon/stt/stt --lang en-US --prepare
  ```

- 許可ダイアログは出ない。SpeechTranscriber は、システム設定の「音声認識」の許可も「音声入力」（Dictation）の設定も使わない（macOS 26.5 で確認）。
- モデルのダウンロード後は、60 秒までの音声を 1〜2 秒程度で文字起こしする（Apple Silicon で確認）。

### 8. iPhone 向けの Web UI（任意）

buddyd は、同じ LAN の iPhone に Web UI を出せる。端末は、ローカル CA が発行したクライアント証明書（mTLS）で認証する。

```sh
uv --directory daemon run buddy web init            # ローカル CA とサーバー証明書（~/.cardbuddy/pki）
uv --directory daemon run buddy web enroll iphone   # ~/Downloads/cardbuddy-iphone.mobileconfig を書く
launchctl kickstart -k gui/$(id -u)/com.sohsatoh.cardbuddy.buddyd
```

- `enroll` は、プロファイルのパスワードを Terminal にだけ表示する。プロファイルを AirDrop で iPhone に送ってインストールし、パスワードを入れる。そのあと、設定 > 一般 > 情報 > 証明書信頼設定で「CardBuddy Local CA」を完全に信頼する。
- `https://<LocalHostName>.local:47825/`（または `buddy web devices` に出る IP）を開く。`http://` で開いても `https://` に転送する。
- **信頼の範囲**：この CA を信頼した iPhone は、`.local`・`localhost`・プライベート IP の全体（10/8、172.16/12、192.168/16、127/8、169.254/16）について、この CA が発行した証明書を信頼する。CA にはこの範囲の名前制約を付けているので、ほかのドメインの証明書は発行できない。ただし CA の鍵を持つ人は、その iPhone に対して LAN 内の任意のホストになりすませる。
- CA の鍵は、macOS の Keychain に置いたランダムなパスフレーズで暗号化し、`buddy web init`・`enroll`・`init --renew` のときだけ開く。この仕組みより前に作った CA の鍵は、`buddy web protect-ca` でその場で暗号化する（CA 自体は変わらない）。
- `buddy web devices` は、登録した端末の一覧を出し、サーバー証明書の期限が近いときや Mac の LAN IP が証明書に含まれなくなったときに警告する。そのときは `buddy web init --renew` を実行する。`buddy web revoke <名前>` は buddyd を再起動しなくても効く。
- macOS が buddyd（Python）への受信接続を許可するか尋ねたら、許可する。Web UI を止めるときは、buddyd を `--no-web` で起動する。

## キー操作

Cardputer-Adv の矢印キーは、単体で押すと `;` `,` `.` `/` の文字になる。以下の「↑↓」は、単体の `;` `.` と Fn+↑↓ のどちらでもよい。Esc は `` ` `` の位置のキーである。

| 画面 | 操作 |
| --- | --- |
| 一覧 | `1`〜`9` / ↑↓ で選択、Enter で入力、`l` か Fn+→ でログ、`v` で入力画面を開いて録音を始める、`Q` で終了 |
| ログ | ↑↓ でスクロール（上端で古いページを読み込む）、`r` で再読み込み、Enter で入力、Esc で一覧へ |
| perm | `Y` で allow、`N` で deny、内容が長いときは ↑↓ でスクロール |
| ask | `1`〜`4` / ↑↓ で選択、Space で複数選択のトグル、Enter で確定 |
| 入力 | 下表 |

入力画面は、上の 3 行に選んだセッションのログ、下の 2 行に入力欄を出す。ログは、画面を開いたとき、そのセッションの状態が変わったとき、送信した後に読み直し、最新の行を追う。キーが効いている側の左端に橙色の印を付ける。

入力画面のキーは次のとおり。

| キー | 動作 |
| --- | --- |
| Fn+← / Fn+→ | カーソルを左右に動かす |
| Fn+↑ / Fn+↓ | カーソルを上下の行に動かす。先頭行で Fn+↑ を押すとログへ移る |
| Fn+↑ / Fn+↓（ログ側） | ログを 1 行ずつ送る（上端で古いページを読み込む）。下端で Fn+↓、Esc、その他のキーで入力欄に戻る |
| Ctrl | 録音を始める。もう一度押すと止める |
| Del | カーソルの前の 1 文字を消す（未確定のローマ字があれば、それを先に消す） |
| Enter | 送信する。画面はそのまま残り、上のログで返答を待てる |
| Esc | 録音中は録音を取り消す。それ以外は入力を捨てて元の画面に戻る |
| Tab | 英数 / ひらがな / カタカナ を切り替える |

- 入力画面では、単体の `;` `,` `.` `/` は文字として入る。
- かなはローマ字で入力する。漢字への変換はしない。未確定のローマ字は水色の下線付きで表示し、Enter・矢印・Tab・Ctrl を押す前に確定する。ログへ移るときは、入力中の文・カーソル・未確定のローマ字をそのまま残す。
- Tab は `+` と同じキーコードで届くため、`+` は入力できない。
- プロンプトは最大 500 文字。
- 音声入力は最大 60 秒。入力モードが英数（`A`）なら英語、ひらがな / カタカナなら日本語で認識する。録音中は入力欄に経過秒数と音量を、止めた後は「送信中 n%」と「認識中…」を出す。文字起こしの結果は、入力済みの文を残したままカーソル位置に入る。自動では送らず、Enter で送る。結果が入ってから 400 ms の間は Enter を受け付けない。
- perm は、全文を表示できないとき（`full` が false のとき）は「全文が長すぎます：端末で確認」と出し、`N` だけを受け付ける。
- perm / ask が届いてから 400 ms の間は、確定キー（`Y` / `N` / Enter）を受け付けない。一覧を操作していた指で誤って答えないようにするため。
- 入力中に perm / ask が届いても、入力画面はそのまま残り、件数だけを表示する。Esc で戻ると回答できる。

## セキュリティ

詳細と脅威モデルは [PROTOCOL.md](PROTOCOL.md) を参照。要点は次のとおり。

- BLE と TCP の上に、アプリ層の暗号化を重ねる。AES-128-CTR と HMAC-SHA256（Encrypt-then-MAC）を使い、接続ごとに HKDF-SHA256 でセッション鍵を導出する。過去の接続で録ったフレームを再送しても、MAC の検証で落ちる。
- 共有鍵は USB 経由で書き込み、BLE にも Wi-Fi にも流さない。
- Hello の交換で、両者が鍵を持っていることを確かめる。LAN 上の第三者が buddyd の TCP ポートにつないだ場合や、偽のビーコンでデバイスを誘導した場合も、セッションは確立できない。
- デバイスは、perm の全文を表示できたときだけ allow を送る。
- 次のものは守らない。
  - DoS（第三者の central が先につなぐ、電波妨害、偽のビーコン、TCP ポートへの大量接続など。確立前の TCP 接続は 4 本まで、それぞれ 5 秒で切る）
  - トラフィック解析
  - 鍵の保護（鍵と Wi-Fi のパスワードは、ホストとデバイスに平文で保存する）
  - 同じユーザー権限で動くプロセス（Unix socket は 0600 で、同じユーザーは信頼する）

## テスト

daemon、device、mod の Python のテストは、次のコマンドでまとめて実行する。device のテストは、M5 の API の偽物を使ってホスト上で動く。`stt` の実バイナリを使うテストは、`make -C daemon/stt` でビルドしてあるときだけ動く。

```sh
uv --directory daemon run pytest -q tests stt ../device/tests ../mod/tests
```

mod の TypeScript のテストは、次のコマンドで実行する。

```sh
claude plugin test mod
```

## 制限事項

- 表示できるセッションは 9 件まで。10 件目以降は、空きができるまで番号が付かず、デバイスに出ない。
- 計画の承認（ExitPlanMode）はデバイスでは扱わず、端末で行う。
- AskUserQuestion は、選択肢が 2〜4 個の問いだけをデバイスに送る。それ以外は端末で答える。
- buddyd がデバイスとつながっていないときや、セッションに番号が無いときは、権限ダイアログは端末だけに出る。
- 漢字変換はできない。
- UIFlow2 は v2.4.2 に固定する（「ファームウェアを書き込む」を参照）。

## ライセンス

[Apache License 2.0](LICENSE)。著作権表示と帰属は [NOTICE](NOTICE) を参照。

`device/` は [moremas/build-with-claude](https://github.com/moremas/build-with-claude)（Apache-2.0）の buddy を改変したもの。upstream の著作権表示と改変内容は [device/NOTICE](device/NOTICE) と [device/LICENSE-THIRD-PARTY.md](device/LICENSE-THIRD-PARTY.md) を参照。
