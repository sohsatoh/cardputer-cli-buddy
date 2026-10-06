# buddyd HTTP API（クライアント ⇔ buddyd）

- 経路：HTTP/1.1 over Unix socket `~/.cardbuddy/buddyd.sock`。ディレクトリ `~/.cardbuddy` は 0700、socket は 0600。
- body は JSON（`Content-Type: application/json`、`Content-Length` 必須）。応答も JSON。
- エラーは 4xx / 5xx で `{"error": "..."}` を返す。
- `sid` は Claude Code のセッション ID（mod では `$.session.id()`、コマンド hook では入力の `session_id`）。
- クライアントは 2 種類ある。
  - **mod**：各セッションで動く function hooks。セッションの登録、AskUserQuestion、プロンプト投入を担当する。
  - **perm hook**：同じプラグインに同梱する `PermissionRequest` のコマンド hook。権限ダイアログと並行して動き、デバイスの回答を待つ。
- `$.http.fetch` は 30 秒で打ち切られる。そのため、待ちを伴う API はどれも `timeout`（既定 25、上限 25）秒で一度返す。待ち続けたい場合、クライアントは同じ要求を繰り返す。

## セッション（mod）

### `POST /session`
セッションの登録と heartbeat を兼ねる（upsert）。mod は `session.start` 時、状態が変わったとき、10 秒ごとに呼ぶ。

```json
{"sid": "...", "cwd": "/abs/path", "title": "最初のプロンプト", "state": "running|idle"}
```
→ `200 {"n": 3}`

- `n` は 1〜9 の空いている最小の番号。一度付いた番号は、セッションが生きている間は変わらない。
- 9 件を超えた場合は `n: null` を返し、デバイスには表示しない。空きができたら次の `POST /session` で番号を付ける。
- デバイスへ送る `state` は buddyd が決める。そのセッションに未解決の ask があれば `ask`、perm があれば `perm`、どちらも無ければ mod が送った値を使う。
- 30 秒間 `POST /session` が来ないセッションは削除する（TTL）。そのセッションの未解決の perm / ask は `resolved{by:"abort"}` として消す。

### `DELETE /session/{sid}`
`session.end` で呼ぶ。→ `200 {}`

## 権限（perm hook）

### `POST /perm`
perm hook が起動したときに呼ぶ。
```json
{"sid": "...", "tool": "Bash", "input": {"command": "rm -rf ./build"}}
```
→ `200 {"req": "r…"}`。

- `desc` / `hint` / `full` は buddyd が `input` から作る（PROTOCOL.md の perm）。
- `sid` が未登録の場合は `404`。デバイスと接続していない場合と、セッションに番号が無い場合は `503`。どちらも perm hook は何も出力せずに終了し、ネイティブダイアログだけになる。

### `GET /perm/{req}?timeout={sec}`
デバイスの回答を待つ。

- `200 {"decision": "allow"}` / `200 {"decision": "deny"}`：デバイスが回答した。
- `200 {"released": true}`：端末側で解決された、中断された、またはセッションが消えた。perm hook は何も出力せずに終了する。
- `200 {}`：`timeout` 秒が経過した。もう一度呼ぶ。
- `404`：未知の req。perm hook は何も出力せずに終了する。
- 待っている最中でなく、最後の待ちが終わってから（まだ一度も呼ばれていなければ `POST /perm` から）60 秒を超えた未解決の perm は、perm hook が終了したとみなして `resolved{by:"abort"}` で片付ける。

### `POST /tool_done`
mod が、ツール呼び出しの終了時（`tool.call` の `next(e)` が解決した時点）に毎回呼ぶ。
```json
{"sid": "...", "tool": "Bash", "input": {"command": "rm -rf ./build"}}
```
→ `200 {}`

- 同じ `sid` と `tool` を持ち、`input` が等しい（buddyd がキーを整列した JSON で比較する）未解決の perm があれば、`resolved{by:"terminal"}` として片付ける。
- 端末のダイアログで先に答えた場合に、デバイスの表示を消すために使う。
- ツールの実行が終わるまでデバイスの表示は残る。その間にデバイスで答えても、エンジンは無視する（すでに決着している）。

## 質問（mod）

### `POST /ask`
```json
{"sid": "...", "qs": [{"q": "どれにする？", "h": "Approach", "o": ["A", "B"], "m": false}]}
```
→ `200 {"req": "r…"}`

- 平文が上限（2048 byte）を超える場合、buddyd は `q` と `o` の文字数上限を半分ずつ減らし、収まるまで切り詰めてから送る。

### `POST /resolved`
端末側で先に答えた場合や、中断された場合に呼ぶ。
```json
{"req": "r…", "by": "terminal|abort"}
```
→ `200 {}`。デバイスへ `resolved` を送る。

デバイスから返信が届いた場合は、buddyd が自分で req を片付け、デバイスへ `resolved{by:"device"}` を送る。

## 通知の受信（mod、long-poll）

### `GET /poll?sid={sid}&timeout={sec}`
そのセッション宛てのイベントが 1 件以上あれば、すぐに返す。無ければ `timeout` 秒待って、空の配列を返す。

→ `200 {"events": [...]}`

| type | fields |
| --- | --- |
| `ask_reply` | `req`, `answers`（問いごとの選択 index の配列） |
| `prompt` | `text` |

- イベントは、返した時点でキューから消す。
- 同じ `sid` の poll が同時に 2 本来た場合、古い方は空で返す。

### `POST /ack_prompt`
mod が `prompt` を投入したあとに呼ぶ。
```json
{"sid": "...", "queued": true}
```
→ `200 {}`。デバイスへ `ack_prompt{n, ok:true, queued}` を送る。

## セッションログ（mod）

### `POST /log`
```json
{"sid": "...", "role": "user|assistant", "text": "..."}
```
→ `200 {}`

- mod は `prompt.submit`（端末とデバイスの両方のプロンプト）で `user` を、`turn.complete` の `answer` で `assistant` を送る。空文字は送らない。
- buddyd はセッションごとに新しい 20 件だけを保持し、1 件は 4000 文字で切る。未登録の `sid` は無視して `200 {}` を返す。
- デバイスの `log_req` には、この保持分から PROTOCOL.md の規則で `log` を作って返す。

## 状態確認

### `GET /status`
→ `200 {"connected": true, "transport": "ble", "device": "Claude_ab12cd", "sessions": [{"n": 1, "sid": "...", "state": "idle"}]}`
