import { test, expect, mock, type Engine } from 'claude-code/testing'
import type { On } from 'claude-code'

type Seen = { method: string; path: string; body: Record<string, unknown> | undefined; socketPath: string | undefined }
type Event = { type: string; [k: string]: unknown }

const SID = 'sid-1'
const QUESTIONS = [
  {
    question: 'どれにする？',
    header: 'Approach',
    multiSelect: false,
    options: [
      { label: 'A', description: 'a' },
      { label: 'B', description: 'b' },
    ],
  },
  {
    question: 'どれを使う？',
    header: 'Lang',
    multiSelect: true,
    options: [
      { label: 'Go', description: 'go' },
      { label: 'Rust', description: 'rust' },
      { label: 'Python', description: 'py' },
    ],
  },
]

const fakeBuddyd = (on: On, statuses: (string | undefined)[] = []) => {
  const seen: Seen[] = []
  const queue: Event[] = []
  const ids = { sid: SID }
  let wake: (() => void) | undefined
  on('session.start', (_$, e) => ({ cwd: e.cwd }))
  on('session.id', () => ({ value: ids.sid }))
  on('ui.status', (_$, e) => (statuses.push(e.text), { value: undefined }))
  on('http.fetch', async (_$, e) => {
    const u = new URL(e.url)
    const s: Seen = {
      method: e.init?.method ?? 'GET',
      path: u.pathname + u.search,
      body: e.init?.body ? JSON.parse(e.init.body) : undefined,
      socketPath: e.init?.socketPath,
    }
    seen.push(s)
    if (u.pathname === '/poll') {
      while (queue.length === 0) await new Promise<void>(r => (wake = r))
      return { value: { status: 200, ok: true, headers: {}, text: JSON.stringify({ events: queue.splice(0) }) } }
    }
    const out = u.pathname === '/session' ? { n: 3 } : u.pathname === '/ask' ? { req: 'r1' } : {}
    return { value: { status: 200, ok: true, headers: {}, text: JSON.stringify(out ?? {}) } }
  })
  const push = (ev: Event) => {
    queue.push(ev)
    wake?.()
  }
  return { seen, push, ids, posted: (path: string) => seen.filter(s => s.method === 'POST' && s.path === path) }
}

const start = async ($: Engine) => $.session.start({ cwd: '/w/proj', surface: 'terminal', isInteractive: true })

test('session.start で HOME 配下の socket へ登録し、番号をステータスに出す', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { HOME: '/h' })
  const statuses: (string | undefined)[] = []
  const b = fakeBuddyd(on, statuses)
  await start($)
  await clock.advance(0)

  expect(b.posted('/session')[0]).toEqual({
    method: 'POST',
    path: '/session',
    body: { sid: SID, cwd: '/w/proj', title: '', state: 'idle' },
    socketPath: '/h/.cardbuddy/buddyd.sock',
  })
  expect(statuses).toContain('Buddy #3')

  await clock.advance(10_000)
  expect(b.posted('/session').length).toBeGreaterThanOrEqual(2)
})

test('CARDBUDDY_HOME があればその下の socket を使う', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { HOME: '/h', CARDBUDDY_HOME: '/cb' })
  const b = fakeBuddyd(on)
  await start($)
  await clock.advance(0)
  expect(b.seen[0]?.socketPath).toBe('/cb/buddyd.sock')
})

test('AskUserQuestion はデバイスの index 回答をラベルに変換して返す', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { HOME: '/h' })
  const b = fakeBuddyd(on)
  on('tool.call', { tool: 'AskUserQuestion' }, (_$, _e, next) => new Promise(() => void next))
  await start($)
  await clock.advance(0)

  const call = $.tool.call({ tool: 'AskUserQuestion', questions: QUESTIONS })
  await clock.advance(0)
  expect(b.posted('/ask')[0]?.body).toEqual({
    sid: SID,
    qs: [
      { q: 'どれにする？', h: 'Approach', o: ['A', 'B'], m: false },
      { q: 'どれを使う？', h: 'Lang', o: ['Go', 'Rust', 'Python'], m: true },
    ],
  })

  b.push({ type: 'ask_reply', req: 'r1', answers: [[1], [0, 2]] })
  const r = await call
  expect(r.result).toEqual({
    questions: QUESTIONS,
    answers: { 'どれにする？': 'B', 'どれを使う？': 'Go, Python' },
  })
})

test('AskUserQuestion の自由入力 {text} はそのまま回答の文字列にする', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { HOME: '/h' })
  const b = fakeBuddyd(on)
  on('tool.call', { tool: 'AskUserQuestion' }, () => new Promise(() => {}))
  await start($)
  await clock.advance(0)

  const call = $.tool.call({ tool: 'AskUserQuestion', questions: QUESTIONS })
  await clock.advance(0)
  b.push({ type: 'ask_reply', req: 'r1', answers: [{ text: '別の案で' }, { text: 'Go, Zig' }] })
  const r = await call
  expect(r.result).toEqual({
    questions: QUESTIONS,
    answers: { 'どれにする？': '別の案で', 'どれを使う？': 'Go, Zig' },
  })
})

test('空や長すぎる自由入力は捨てて端末を待ち続ける', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { HOME: '/h' })
  const b = fakeBuddyd(on)
  let answer: ((v: { result: unknown; text: string }) => void) | undefined
  on('tool.call', { tool: 'AskUserQuestion' }, () => new Promise(r => (answer = r)))
  await start($)
  await clock.advance(0)

  const call = $.tool.call({ tool: 'AskUserQuestion', questions: QUESTIONS })
  await clock.advance(0)
  b.push({ type: 'ask_reply', req: 'r1', answers: [{ text: '' }, [0]] })
  b.push({ type: 'ask_reply', req: 'r1', answers: [{ text: 'x'.repeat(501) }, [0]] })
  await clock.advance(0)
  answer?.({ result: { questions: QUESTIONS, answers: { 'どれにする？': 'A' } }, text: 't' })
  expect((await call).text).toBe('t')
})

test('AskUserQuestion で端末が先に答えたら resolved{terminal} を送り、端末の結果を返す', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { HOME: '/h' })
  const b = fakeBuddyd(on)
  const answered = { result: { questions: QUESTIONS, answers: { 'どれにする？': 'A' } }, text: 'answered' }
  on('tool.call', { tool: 'AskUserQuestion' }, () => answered)
  await start($)
  await clock.advance(0)

  const r = await $.tool.call({ tool: 'AskUserQuestion', questions: QUESTIONS })
  await clock.advance(0)
  expect(r.result).toEqual(answered.result)
  expect(b.posted('/resolved')[0]?.body).toEqual({ req: 'r1', by: 'terminal' })
})

test('範囲外の index を含む回答は捨てて端末を待ち続ける', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { HOME: '/h' })
  const b = fakeBuddyd(on)
  let answer: ((v: { result: unknown; text: string }) => void) | undefined
  on('tool.call', { tool: 'AskUserQuestion' }, () => new Promise(r => (answer = r)))
  await start($)
  await clock.advance(0)

  const call = $.tool.call({ tool: 'AskUserQuestion', questions: QUESTIONS })
  await clock.advance(0)
  b.push({ type: 'ask_reply', req: 'r1', answers: [[5], [0]] })
  await clock.advance(0)
  answer?.({ result: { questions: QUESTIONS, answers: { 'どれにする？': 'A' } }, text: 't' })
  expect((await call).text).toBe('t')
})

test('ツール終了後に tool_done を送り、結果はそのまま返す', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { HOME: '/h' })
  const b = fakeBuddyd(on)
  const ran = { result: { stdout: 'ok', stderr: '', interrupted: false }, text: 'ok' }
  on('tool.call', { tool: 'Bash' }, () => ran)
  await start($)
  await clock.advance(0)

  const r = await $.tool.call({ tool: 'Bash', command: 'ls', description: 'list' })
  await clock.advance(0)
  expect(r.text).toBe('ok')
  expect(b.posted('/tool_done')[0]?.body).toEqual({ sid: SID, tool: 'Bash', input: { command: 'ls', description: 'list' } })
})

test('prompt イベントは asUser で投入し、ack_prompt に queued を付ける', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { HOME: '/h' })
  const b = fakeBuddyd(on)
  const submitted: unknown[] = []
  on('prompt.submit', (_$, e) => (submitted.push(e), { text: e.text }))
  await start($)
  await clock.advance(0)

  b.push({ type: 'prompt', text: 'テストを流して' })
  await clock.advance(0)
  expect(submitted[0]).toMatchObject({ text: 'テストを流して', origin: { kind: 'plugin', asUser: true } })
  expect(b.posted('/ack_prompt')[0]?.body).toEqual({ sid: SID, queued: false })
})

test('人が最初に送ったプロンプトの 1 行目を title にし、ターン中は running を送る', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { HOME: '/h' })
  const b = fakeBuddyd(on)
  on('prompt.submit', (_$, e) => ({ text: e.text }))
  on('turn.start', () => ({ turnId: 't1' }))
  await start($)
  await clock.advance(0)

  await $.prompt.submit({ text: 'README を直して\n詳細はあとで', origin: { kind: 'composer' }, wait: false })
  await $.turn.start({ text: 'README を直して', turnId: 't1' })
  await clock.advance(0)
  expect(b.posted('/session').at(-1)?.body).toEqual({ sid: SID, cwd: '/w/proj', title: 'README を直して', state: 'running' })

  await $.prompt.submit({ text: '別の指示', origin: { kind: 'composer' }, wait: false })
  await clock.advance(10_000)
  expect(b.posted('/session').at(-1)?.body).toMatchObject({ title: 'README を直して' })
})

test('session.end で登録を消し、以後は heartbeat を送らない', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { HOME: '/h' })
  const b = fakeBuddyd(on)
  on('session.end', (_$, e) => ({ sessionId: e.sessionId }))
  await start($)
  await clock.advance(0)

  await $.session.end({ reason: 'prompt_input_exit', sessionId: SID, resume: { id: SID } })
  const beats = b.posted('/session').length
  await clock.advance(30_000)
  expect(b.seen.some(s => s.method === 'DELETE' && s.path === `/session/${SID}`)).toBe(true)
  expect(b.posted('/session').length).toBe(beats)
})

for (const reason of ['clear', 'resume'] as const) {
  test(`${reason} の後は新しい sid ですぐ登録し直し、heartbeat を続ける`, async ($, on) => {
    const clock = mock.clock(on)
    mock.env(on, { HOME: '/h' })
    const b = fakeBuddyd(on)
    on('session.end', (_$, e) => ((b.ids.sid = 'sid-2'), { sessionId: e.sessionId }))
    await start($)
    await clock.advance(0)

    await $.session.end({ reason, sessionId: SID, resume: { id: SID } })
    await clock.advance(0)
    expect(b.posted('/session').at(-1)?.body).toMatchObject({ sid: 'sid-2', title: '', state: 'idle' })
    const beats = b.posted('/session').length
    await clock.advance(10_000)
    expect(b.posted('/session').length).toBeGreaterThan(beats)
  })
}

test('選択肢が 2〜4 個でない質問はデバイスへ送らず端末に任せる', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { HOME: '/h' })
  const b = fakeBuddyd(on)
  on('tool.call', { tool: 'AskUserQuestion' }, () => ({ result: 'terminal', text: 'terminal' }))
  await start($)
  await clock.advance(0)

  const five = [{ ...QUESTIONS[0]!, options: ['1', '2', '3', '4', '5'].map(label => ({ label, description: label })) }]
  expect((await $.tool.call({ tool: 'AskUserQuestion', questions: five })).text).toBe('terminal')
  expect(b.posted('/ask')).toEqual([])
})

test('人とデバイスのプロンプトだけを user として /log に送る', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { HOME: '/h' })
  const b = fakeBuddyd(on)
  on('prompt.submit', (_$, e) => ({ text: e.text }))
  await start($)
  await clock.advance(0)

  await $.prompt.submit({ text: '端末から', origin: { kind: 'composer' }, wait: false })
  await $.prompt.submit({ text: '別プラグイン', origin: { kind: 'plugin', name: 'other', asUser: true }, wait: false })
  await $.prompt.submit({ text: '通知', origin: { kind: 'task-notification' }, wait: false })
  await $.prompt.submit({ text: '', origin: { kind: 'composer' }, wait: false })
  b.push({ type: 'prompt', text: 'デバイスから' })
  await clock.advance(0)
  expect(b.posted('/log').map(s => s.body)).toEqual([
    { sid: SID, role: 'user', text: '端末から' },
    { sid: SID, role: 'user', text: 'デバイスから' },
  ])
})

test('main loop のターンの answer だけを assistant として /log に送る', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { HOME: '/h' })
  const b = fakeBuddyd(on)
  on('turn.start', (_$, e) => ({ turnId: e.turnId }))
  on('turn.complete', (_$, e) => ({ text: e.answer }))
  await start($)
  await clock.advance(0)

  await $.turn.start({ text: 'p', turnId: 't1' })
  const base = { durationMs: 1, isAborted: false, turnId: 't1', reason: 'answer' as const }
  await $.turn.complete({ ...base, answer: '直しました' })
  await $.turn.complete({ ...base, answer: 'subagent の答え', agentId: 'a1' })
  await $.turn.complete({ ...base, answer: '' })
  await clock.advance(0)
  expect(b.posted('/log').map(s => s.body)).toEqual([{ sid: SID, role: 'assistant', text: '直しました' }])
})

const activities = (b: ReturnType<typeof fakeBuddyd>) =>
  b.posted('/activity').map(s => s.body as { sid: string; ev: Record<string, unknown> })

test('turn_start は prompt を 80 文字（コードポイント）で切り、subagent の turn_end には agent_id を付ける', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { HOME: '/h' })
  const b = fakeBuddyd(on)
  on('agent.list', () => ({ value: [{ id: 'ag1', type: 'Explore', description: 'd', status: 'running' }] }))
  on('turn.start', (_$, e) => ({ turnId: e.turnId }))
  on('turn.complete', (_$, e) => ({ text: e.answer }))
  await start($)
  await clock.advance(0)

  await $.turn.start({ text: 'あ'.repeat(79) + '😀z', turnId: 't1' })
  const base = { answer: 'a', durationMs: 1234, isAborted: false, reason: 'answer' as const }
  await $.turn.complete({ ...base, turnId: 'sub', agentId: 'ag1' })
  await $.turn.complete({ ...base, turnId: 't1' })
  await clock.advance(0)
  expect(activities(b)).toEqual([
    { sid: SID, ev: { type: 'turn_start', turn_id: 't1', prompt: 'あ'.repeat(79) + '😀' } },
    { sid: SID, ev: { type: 'turn_end', turn_id: 'sub', reason: 'answer', ms: 1234, agent_id: 'ag1', agent_type: 'Explore' } },
    { sid: SID, ev: { type: 'turn_end', turn_id: 't1', reason: 'answer', ms: 1234 } },
  ])
})

test('tool_start / tool_end を送り、Bash は command 先頭 120 文字、ファイル系は file_path を要約にする', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { HOME: '/h' })
  const b = fakeBuddyd(on)
  on('tool.call', (_$, e) =>
    e.tool === 'Write' ? { isError: true as const, result: 'x', text: 'x' } : { result: 'ok', text: 'ok' },
  )
  await start($)
  await clock.advance(0)

  await $.tool.call({ tool: 'Bash', command: 'x'.repeat(130) })
  await $.tool.call({ tool: 'Write', file_path: '/w/a.txt', content: 'c' })
  await $.tool.call({ tool: 'CronList' })
  await clock.advance(0)
  const evs = activities(b).map(a => a.ev)
  expect(evs.filter(e => e.type === 'tool_start').map(({ tool_use_id: _, ...rest }) => rest)).toEqual([
    { type: 'tool_start', tool: 'Bash', summary: 'x'.repeat(120) },
    { type: 'tool_start', tool: 'Write', summary: '/w/a.txt' },
    { type: 'tool_start', tool: 'CronList' },
  ])
  const ends = evs.filter(e => e.type === 'tool_end')
  expect(ends.map(e => e.is_error)).toEqual([false, true, false])
  expect(ends.every(e => typeof e.ms === 'number' && e.ms >= 0)).toBe(true)
  const starts = evs.filter(e => e.type === 'tool_start')
  expect(ends.map(e => e.tool_use_id)).toEqual(starts.map(e => e.tool_use_id))
  expect(typeof starts[0]?.tool_use_id).toBe('string')
})

test('subagent のツールには agent_id と、分かれば agent_type を付ける', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { HOME: '/h' })
  const b = fakeBuddyd(on)
  let lists = 0
  on('agent.list', () => (lists++, { value: [{ id: 'ag1', type: 'Explore', description: 'd', status: 'running' }] }))
  on('tool.call', () => ({ result: 'ok', text: 'ok' }))
  await start($)
  await clock.advance(0)

  const call = (agentId: string) => $.tool.call({ tool: 'Read', file_path: '/w/a', agentId } as never)
  await call('ag1')
  await call('ag1')
  await call('ag9')
  await clock.advance(0)
  const starts = activities(b).map(a => a.ev).filter(e => e.type === 'tool_start')
  expect(starts.map(e => [e.agent_id, e.agent_type])).toEqual([['ag1', 'Explore'], ['ag1', 'Explore'], ['ag9', undefined]])
  expect(lists).toBe(2)
})

test('AskUserQuestion も activity に出し、tool_done は送らない', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { HOME: '/h' })
  const b = fakeBuddyd(on)
  on('tool.call', { tool: 'AskUserQuestion' }, () => ({ result: 'terminal', text: 'terminal' }))
  await start($)
  await clock.advance(0)

  await $.tool.call({ tool: 'AskUserQuestion', questions: QUESTIONS })
  await clock.advance(0)
  expect(activities(b).map(a => [a.ev.type, a.ev.tool ?? a.ev.is_error])).toEqual([
    ['tool_start', 'AskUserQuestion'],
    ['tool_end', false],
  ])
  expect(b.posted('/tool_done')).toEqual([])
})

test('turn.start で始まっていないターン（agentId の無い背景 subagent）は main として扱わない', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { HOME: '/h' })
  const b = fakeBuddyd(on)
  on('turn.start', (_$, e) => ({ turnId: e.turnId }))
  on('turn.complete', (_$, e) => ({ text: e.answer }))
  await start($)
  await clock.advance(0)

  await $.turn.start({ text: 'main', turnId: 'm1' })
  const base = { durationMs: 5, isAborted: false, reason: 'answer' as const }
  await $.turn.complete({ ...base, answer: 'subagent の答え', turnId: 'bg1' })
  await clock.advance(0)
  expect(b.posted('/session').at(-1)?.body).toMatchObject({ state: 'running' })
  expect(b.posted('/log')).toEqual([])
  expect(activities(b).at(-1)?.ev).toEqual({ type: 'turn_end', turn_id: 'bg1', reason: 'answer', ms: 5 })

  await $.turn.complete({ ...base, answer: 'main の答え', turnId: 'm1' })
  await clock.advance(0)
  expect(b.posted('/session').at(-1)?.body).toMatchObject({ state: 'idle' })
  expect(b.posted('/log').map(s => s.body)).toEqual([{ sid: SID, role: 'assistant', text: 'main の答え' }])
})
