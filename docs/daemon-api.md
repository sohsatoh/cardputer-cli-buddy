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

## 処理の様子（mod）

### `POST /activity`
```json
{"sid": "...", "ev": {"type": "tool_start", "tool_use_id": "toolu_…", "tool": "Bash", "summary": "npm test"}}
```
→ `200 {}`

mod は、ターンとツール呼び出しの開始・終了を、待たずに送る。

| type | fields | 送るとき |
| --- | --- | --- |
| `turn_start` | `turn_id`, `prompt`（先頭 80 文字。続きのターンでは空） | main loop のターンの開始（`turn.start`） |
| `turn_end` | `turn_id`, `reason`（`answer` / `aborted` / `refusal` / `error`）, `ms`, `agent_id`?, `agent_type`? | ターンの終了（`turn.complete`）。subagent のターンなら `agent_id` が付く |
| `tool_start` | `tool_use_id`, `tool`, `summary`?, `agent_id`?, `agent_type`? | ツール呼び出しの開始（`tool.call`） |
| `tool_end` | `tool_use_id`, `is_error`, `ms` | ツール呼び出しの終了。拒否・中断も `is_error: true` |

- `summary` は、Bash なら `command` の先頭 120 文字、ファイル系のツールなら `file_path`（`notebook_path`）。それ以外のツールには付けない。
- `agent_id` は subagent の中の呼び出しとターンにだけ付く。`agent_type` は、その subagent の種類（`Explore` など）が分かるときだけ付く。
- `turn_start` は main loop でだけ送る。main のターンの `turn_end` は、直前の `turn_start` と同じ `turn_id` を持つ。それ以外の `turn_end` は subagent のターン。
- subagent の起動は `Agent` ツールの `tool_start` で表す。subagent がバックグラウンドで動く場合、`Agent` の `tool_end` はすぐに届き、subagent の終わりはその subagent の `turn_end` で分かる。ただし、バックグラウンドの subagent の `turn_end` には `agent_id` が付かないことがある（Claude Code がその turn に agentId を渡さないため）。
- AskUserQuestion の呼び出しも `tool_start` / `tool_end` で送る。
- 文字数はコードポイントで数える。各フィールドは型と長さを検証し、合わなければ `400`（`turn_id` 64、`prompt` 80、`reason` 32、`tool_use_id` 128、`tool` 128、`summary` 1024、`agent_id` 64、`agent_type` 128 文字まで。`ms` は 0 以上の整数）。
- buddyd は、受信時刻 `at`（UNIX 秒）を付けて、セッションごとに新しい 200 件を保持する。`turn_start` を受けたら、それまでの分を捨てる（最新のターンだけ残る）。未登録の `sid` は無視して `200 {}` を返す。
- 送信は 1 件ずつ独立しているので、届く順番は前後しうる。実行中のツールは、「`tool_start` があり、同じ `tool_use_id` の `tool_end` が無いもの」として、順番に依らず導出する。
- buddyd の中では、`Hub.subscribe_activity(cb)` で新しいイベントを `cb(sid, item)` として受け取れる（戻り値を呼ぶと購読をやめる）。`Hub.activity(sid)` は `{"events": [...], "running": [...]}` を返す。

## 状態確認

### `GET /status`
→ `200 {"connected": true, "transport": "ble", "device": "Claude_ab12cd", "sessions": [{"n": 1, "sid": "...", "state": "idle"}]}`
