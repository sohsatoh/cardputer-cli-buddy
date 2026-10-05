# Cardputer CLI Buddy protocol

buddyd (ホスト、BLE central) と Cardputer (BLE peripheral) の間の通信仕様。
3 実装（`daemon/`、`device/`、テストベクタ `testvectors/envelope.json`）はこの文書に従う。

## Transport

Nordic UART Service (NUS)。GATT 構成は build-with-claude の buddy と同じ。

| Role | UUID | Flags |
| --- | --- | --- |
| Service | `6e400001-b5a3-f393-e0a9-e50e24dcca9e` | — |
| RX (host → device) | `6e400002-b5a3-f393-e0a9-e50e24dcca9e` | `WRITE`, `WRITE_NR` |
| TX (device → host) | `6e400003-b5a3-f393-e0a9-e50e24dcca9e` | `READ`, `NOTIFY` |

- 広告名は `Claude_<BT MAC 下位 6 hex>`。buddyd は `Claude_` 前方一致で探す（設定で完全一致にできる）。
- 1 行 = `base64(frame)` + `\n`。base64 は標準アルファベット、パディングあり。
- ホストは 1 行を 180 byte 以下の write に分けて書く。デバイスは `\n` まで連結する。
- デバイスは 20 byte ずつ notify する。ホストは `\n` まで連結する。
- 1 行の上限は 4096 byte（base64 後、`\n` 含まず）。超えた行は捨てる。
- 平文 JSON の上限は 2048 byte（UTF-8）。

## 鍵

- マスター鍵 `K`：32 byte の乱数。`buddy pair` がホストで生成する。
  - ホストは `~/.cardbuddy/key`（ディレクトリ 0700、ファイル 0600）に raw 32 byte で保存する。
  - デバイスへは USB シリアル（raw REPL）で `/flash/cardbuddy.key` に raw 32 byte で書き込む。
  - BLE には流さない。リポジトリにも入れない。
- 接続ごとのセッション鍵：Hello で交換した `nh`（host nonce 16 byte）と `nd`（device nonce 16 byte）から HKDF-SHA256（RFC 5869）で導出する。
  - `salt = nh ‖ nd`、`IKM = K`、`info = "cardbuddy v1"`（ASCII）、`L = 48`
  - `enc_key = OKM[0:16]`（AES-128）、`mac_key = OKM[16:48]`
- セッション鍵は接続ごとに変わる。そのため、過去の接続で録音したフレームは MAC で落ちる。カウンタを永続化する必要はない。

## Frame

全フレームの先頭は `ver = 0x01` と `kind`（1 byte）。

### Hello（`kind = 'H'` 0x48、平文）

```
ver(1) ‖ 'H' ‖ role(1) ‖ nonce(16)       = 19 byte
role: 'h' (0x68) = host, 'd' (0x64) = device
```

1. 接続し、TX の notify を購読したら、ホストは新しい `nh` を乱数で作り、Hello(role='h') を送る。
2. デバイスは Hello(role='h') を受けるたびに新しい `nd` を乱数で作り、Hello(role='d') を返す。セッション鍵を導出し、送受信カウンタを 0 に戻す。それまでのセッションは捨てる（表示中の perm / ask も消す）。
3. ホストは Hello(role='d') を受けたらセッション鍵を導出し、カウンタを 0 に戻す。Hello を送ってから 5 秒以内に返事が来なければ切断して、再接続からやり直す。
4. セッションが確立したら、ホストは `sessions` と、未解決の `perm` / `ask` を送り直す。

Hello は認証しない。Hello を偽造・改ざんされても、鍵が食い違って以後の Data がすべて MAC で落ちるだけ（DoS）になる。

### Data（`kind = 'D'` 0x44）

```
ver(1) ‖ 'D' ‖ dir(1) ‖ ctr(4, big-endian) ‖ ct(n) ‖ tag(16)
dir: 0x01 = host → device, 0x02 = device → host
```

- `ctr` は方向ごと・セッションごとのカウンタ。送信側は 1 から始めて、1 フレームごとに 1 増やす。
- 暗号化：AES-128-CTR。初期カウンタブロックは `dir(1) ‖ ctr(4) ‖ 0x00 × 7 ‖ block(4, big-endian)` で、`block` は 0 から数える。
- `tag = HMAC-SHA256(mac_key, ver ‖ 'D' ‖ dir ‖ ctr ‖ ct)` の先頭 16 byte（Encrypt-then-MAC）。
- 受信側の検証順は次のとおり。
  1. `ver`、`kind`、`dir`（自分宛ての向きか）を確認する。
  2. tag を定数時間比較で検証する。
  3. `ctr` が、最後に受理した値より大きいことを確認する。
  4. 復号し、JSON として parse する。
- 1 つでも失敗したらフレームを捨て、理由をログに出す。応答は返さない。セッション確立前に届いた Data も同様に捨てる。

### Audio（`kind = 'A'` 0x41、device → host のみ）

```
ver(1) ‖ 'A' ‖ dir(1) ‖ ctr(4) ‖ ct(n) ‖ tag(16)
平文 = seq(2, big-endian) ‖ μ-law サンプル（最大 1600 byte = 16kHz で 100ms）
```

- 暗号化・MAC・検証順は Data と同じ（MAC の対象には `kind` も含まれる）。`ctr` は Data と共有する方向ごとのカウンタで、Audio と Data は同じ列から番号を取る。
- 平文は JSON ではない。ホストは `voice_begin` を受けてから `voice_end` / `voice_cancel` までの間だけ Audio を受け付け、それ以外は捨てる。
- `seq` は録音ごとに 0 から数える。ホストは `seq` の欠けを無音で埋める。

## Messages（Data の平文 JSON）

すべて JSON object で、`t` に種別を入れる。文字列の上限を超えた部分は送信側で切り詰める（末尾に `…` を付ける）。

### host → device

| t | fields | 説明 |
| --- | --- | --- |
| `sessions` | `s: [{n, id, name, title, state, last}]` | セッション一覧の全量。変化したときと、セッション確立直後に送る。 |
| `perm` | `n, req, id, name, tool, desc, hint, full` | 権限の確認待ち。 |
| `ask` | `n, req, id, name, qs: [{q, h, o: [label…], m}]` | AskUserQuestion。 |
| `resolved` | `req, by` | `by`: `terminal` / `device` / `abort`。表示中の perm / ask を消す。 |
| `ack_prompt` | `n, ok, queued` | `prompt` の結果。`ok=false` は対象セッションが無い場合。 |
| `log` | `n, p, more, items: [{r, x, c}]` | `log_req` への応答。セッションログの 1 ページ（古い順）。 |
| `voice_text` | `vid, text` | 音声の文字起こし結果。 |
| `voice_error` | `vid, err` | 文字起こしの失敗（`err` は表示用の短い文）。 |

- `n`：セッション番号（1〜9）。端末のステータスライン `Buddy #n` と一致する。
- `id`：セッション ID の先頭 8 文字。デバイスは `prompt` にそのまま付けて返す。番号が使い回されたときの取り違えを防ぐため。
- `name`：cwd の basename（最大 16 文字）。`title`：最初のプロンプトの 1 行目（最大 24 文字）。
- `last`：最後の Claude の返答の冒頭 1 行（最大 40 文字、無ければ空文字）。`sessions` が平文 2048 byte に収まらない場合、ホストは全件の `last` と `title` の上限を半分ずつ減らして収める。
- `state`：`running` / `idle` / `perm` / `ask`。
- 一覧は最大 9 件。差分送信はしない（全量でも 1 メッセージに収まる）。
- `req`：buddyd が振る要求 ID（英数字、最大 12 文字）。
- perm / ask の `id` と `name` は、その要求を出したセッションのもの（`sessions` と同じ規則）。デバイスはこれを表示する（`sessions` の到着順に依存しない）。
- `tool`：ツール名。切り詰めない。
- `desc`：Bash のときだけ、ツール入力の `description`（最大 80 文字）。それ以外は空文字。
- `hint`：承認の判断に必要な内容の全文。
  - Bash：`command`。
  - Edit：`file_path`、`- old_string`、`+ new_string` を改行で連結（`replace_all` が true なら 1 行目に付記）。
  - Write：`file_path` と `content` を改行で連結。
  - それ以外：`input` をキー整列した JSON。
- `full`：`tool` と `hint` を切り詰めずに平文 2048 byte に収められたときだけ true。収まらなければ `hint` を切り詰めて false にする。
- **デバイスは `full` が true のときだけ allow を受け付ける。** false のときは deny だけを受け付け、端末で確認するよう表示する。
- `qs`：1〜4 問。`q` は最大 120 文字、`h`（header）は最大 12 文字。`o` は 2〜4 個で、各最大 40 文字。`m` は true のとき複数選択。

### device → host

| t | fields | 説明 |
| --- | --- | --- |
| `perm_reply` | `req, decision` | `decision`: `allow` / `deny`。 |
| `ask_reply` | `req, answers` | `answers`: 問いごとの選択 index の配列（`[[0], [1, 3]]`）。 |
| `prompt` | `n, id, text` | `text` は最大 500 文字。 |
| `log_req` | `n, id, p` | セッションログの `p` ページ目（0 が最新）を要求する。 |
| `voice_begin` | `vid, lang` | 録音の開始。`lang` は `ja-JP` / `en-US`。以降の Audio はこの録音のもの。 |
| `voice_end` | `vid` | 録音の終了。ホストは文字起こしして `voice_text` か `voice_error` を返す。 |
| `voice_cancel` | `vid` | 録音の取り消し。ホストは受け取った音声を捨て、何も返さない。 |

- ホストは、未解決でない `req` への返信を捨てる（先に端末で答えた場合など）。
- `prompt` は、`n` と `id` の両方がセッション表と一致するときだけ投入する。一致しなければ `ack_prompt{ok:false}` を返す。
- `log_req` も `n` と `id` の両方が一致するときだけ応える。一致しなければ `log{n, items:[]}` を返す。

### セッションログ

- ホストは保持しているログ（古い順）の各件を、500 文字ごとの断片に分ける。断片は `r`（`u` = ユーザーのプロンプト、`a` = Claude の返答本文）、`x`（本文の断片）、`c`（直前の断片と同じ件の続きなら true）を持つ。
- ページは新しい側から作る。p=0 は最新の断片から遡って平文 2048 byte に収まるだけ詰めたもの、p=1 はその直前から同様に詰めたもの、と続く。各ページ内は古い順に並べる。
- `more` は、そのページより古い断片が残っていれば true。
- `p` が範囲外、または `n` / `id` が一致しなければ `items: []`、`more: false`。
- デバイスは、ログ画面を開いたとき、`r` キーを押したとき、表示中のセッションの `state` が変わったときに p=0 を要求し直す。上端までスクロールして `more` が true なら p+1 を要求し、受け取ったページを先頭に足す。ホストからは自発的に送らない。
- ページの境界はログが増えるとずれる。デバイスは p=0 を受け取ったら、それまでのページを捨てる。

### 音声入力

- `vid` はデバイスが振る録音 ID（英数字、最大 8 文字）。ホストは、現在の録音と一致しない `vid` の `voice_end` / `voice_cancel` を捨てる。
- 形式は 16kHz・モノラル・G.711 μ-law 固定。1 回の録音は最大 60 秒（960,000 byte）。超えた分の Audio は捨て、`voice_end` で上限までの音声を文字起こしする。
- 録音中に新しい `voice_begin` が来たら、前の録音は取り消し扱いにする。切断・再 Hello でも取り消す。
- ホストは音声を一時ファイル（0600）に書いて文字起こしし、結果にかかわらず終了後に削除する。音声も認識結果もログに出さない（長さと所要時間だけ出す）。
- 文字起こしの結果はプロンプトとしては投入しない。デバイスは結果を入力画面に入れ、ユーザーが確認・編集して Enter を押したときに通常の `prompt` として送る。

## タイミング

- buddyd は切断を検出したら、2 秒後から指数バックオフ（上限 30 秒）で再接続する。
- セッション一覧の変化は 0.5 秒まとめてから送る。

## 脅威モデル

守るもの：BLE の到達範囲にいる第三者が、次のことをできないようにする。

- 権限を承認する
- AskUserQuestion に答える
- プロンプトを投入する
- セッション一覧・コマンド・質問の内容を読む
- 過去に録音したフレームを再送して、上記のいずれかを起こす

範囲外とするもの：

- **DoS**：NUS の接続は 1 本だけなので、第三者の central が先につなぐと正規ホストが締め出される。電波妨害や Hello の改ざんも同様で、いずれも認証では防げない。
- **トラフィック解析**：フレームの長さとタイミングは秘匿しない。
- **鍵の保護**：ホストの `~/.cardbuddy/key` と、デバイスの `/flash/cardbuddy.key` は平文で保存する。デバイスを物理的に取られた場合や、同じユーザー権限で動くプロセスからは守らない。
- **Unix socket**（`~/.cardbuddy/buddyd.sock`、0600）：同じユーザーのプロセスは信頼する。
