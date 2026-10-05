import type { EngineInterface as $, Register, ToolCallInput } from 'claude-code'

type Question = { question: string; header: string; multiSelect: boolean; options: { label: string }[] }
type PollEvent = { type: 'ask_reply'; req: string; answers: unknown } | { type: 'prompt'; text: string }
type Answers = Record<string, string>

const LOOP = { plugin: 'cardbuddy', key: 'loop' } as const
const HEARTBEAT_MS = 10_000

let socket: Promise<string> | undefined
let ended = false
let sid = ''
let cwd = ''
let title = ''
let state: 'running' | 'idle' = 'idle'
let lastBeat = 0
// Calls made through an aborted dispatch's $ are dropped; those wait here for the next live one.
const outbox: [string, unknown][] = []
const askWaiters = new Map<string, (answers: unknown) => void>()

const socketPath = ($: $) =>
  (socket ??= (async () => {
    const home = (await $.env.get('CARDBUDDY_HOME')) || `${await $.env.get('HOME')}/.cardbuddy`
    return `${home}/buddyd.sock`
  })())

const call = async ($: $, method: string, path: string, body?: unknown) => {
  const res = await $.http.fetch(`http://buddyd${path}`, {
    method,
    socketPath: await socketPath($),
    ...(body !== undefined && { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }),
  })
  if (!res.ok) throw new Error(`buddyd ${method} ${path}: ${res.status}`)
  return JSON.parse(res.text) as Record<string, unknown>
}

const send = ($: $, method: string, path: string, body?: unknown) => void call($, method, path, body).catch(() => {})

const followUp = ($: $, signal: AbortSignal, path: string, body: unknown) =>
  signal.aborted ? void outbox.push([path, body]) : send($, 'POST', path, body)

const heartbeat = async ($: $) => {
  if (ended) return
  lastBeat = Date.now()
  for (const [path, body] of outbox.splice(0)) send($, 'POST', path, body)
  sid = await $.session.id()
  try {
    const { n } = await call($, 'POST', '/session', { sid, cwd, title, state })
    $.ui.status(typeof n === 'number' ? `Buddy #${n}` : undefined)
  } catch {
    $.ui.status(undefined)
  }
}

const toQs = (questions: Question[]) =>
  questions.every(q => Array.isArray(q.options) && q.options.length >= 2 && q.options.length <= 4)
    ? questions.map(q => ({ q: q.question, h: q.header, o: q.options.map(o => o.label), m: q.multiSelect }))
    : undefined

const toLabels = (questions: Question[], answers: unknown): Answers | undefined => {
  if (!Array.isArray(answers) || answers.length !== questions.length) return undefined
  const out: Answers = {}
  for (const [i, q] of questions.entries()) {
    const picks: unknown = answers[i]
    if (!Array.isArray(picks) || picks.length === 0 || (!q.multiSelect && picks.length !== 1)) return undefined
    const labels = picks.map(p => (typeof p === 'number' ? q.options[p]?.label : undefined))
    if (labels.some(l => l === undefined)) return undefined
    out[q.question] = labels.join(', ')
  }
  return out
}

const onEvent = ($: $, ev: PollEvent) => {
  if (ev.type === 'ask_reply') askWaiters.get(ev.req)?.(ev.answers)
  if (ev.type === 'prompt' && typeof ev.text === 'string') {
    void $.prompt.submit({ text: ev.text, asUser: true }).catch(() => {})
    send($, 'POST', '/ack_prompt', { sid, queued: state === 'running' })
    // The plugin's own prompt.submit hook is skipped for prompts it raised, so the device's prompt is logged here.
    if (ev.text) send($, 'POST', '/log', { sid, role: 'user', text: ev.text })
  }
}

// A hot reload raises session.start again in a fresh environment while the old loop may still be polling;
// the generation in $.state (which survives the reload) tells the old loop to stop.
const pollLoop = async ($: $, gen: number) => {
  while (!ended && (await $.state.get(LOOP)).value === gen) {
    if (Date.now() - lastBeat >= HEARTBEAT_MS) await heartbeat($)
    let events: PollEvent[]
    try {
      events = ((await call($, 'GET', `/poll?sid=${encodeURIComponent(sid)}&timeout=25`)).events ?? []) as PollEvent[]
    } catch {
      await $.clock.sleep(2_000)
      continue
    }
    for (const ev of events) onEvent($, ev)
  }
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    cwd = e.cwd
    const { value = 0 } = await $.state.get(LOOP)
    await $.state.set(LOOP, value + 1)
    await heartbeat($)
    $.clock.every(HEARTBEAT_MS, () => void heartbeat($))
    void pollLoop($, value + 1).catch(() => {})
    return next(e)
  })

  on('prompt.submit', ($, e, next) => {
    const byPerson = e.origin.kind === 'composer' || e.origin.kind === 'bridge'
    if (byPerson && e.text) send($, 'POST', '/log', { sid, role: 'user', text: e.text })
    if (!title && byPerson) {
      title = e.text.trim().split('\n')[0] ?? ''
      void heartbeat($)
    }
    return next(e)
  })

  on('turn.start', ($, e, next) => {
    state = 'running'
    void heartbeat($)
    return next(e)
  })

  on('turn.complete', ($, e, next) => {
    if (e.agentId === undefined) {
      state = 'idle'
      void heartbeat($)
      if (e.answer) send($, 'POST', '/log', { sid, role: 'assistant', text: e.answer })
    }
    return next(e)
  })

  on('session.end', async ($, e, next) => {
    const goesOn = e.reason === 'clear' || e.reason === 'resume'
    ended = !goesOn
    title = ''
    state = 'idle'
    await call($, 'DELETE', `/session/${encodeURIComponent(e.sessionId)}`).catch(() => {})
    const r = await next(e)
    if (goesOn) await heartbeat($)
    return r
  })

  on('tool.call', { tool: 'AskUserQuestion' }, async ($, e, next) => {
    const questions = e.questions as Question[]
    const qs = toQs(questions)
    if (!qs) return next(e)
    // next(e) stays in flight for the whole race, so waiting here does not spend the hook's budget.
    const terminal = next(e)
    let req: string
    try {
      req = String((await call($, 'POST', '/ask', { sid, qs })).req)
    } catch {
      return terminal
    }
    let byDevice = false
    const device = new Promise<Answers>(resolve =>
      askWaiters.set(req, answers => {
        const labels = toLabels(questions, answers)
        if (labels) resolve(labels)
      }),
    )
    try {
      const won = await Promise.race([terminal.then(r => ({ r })), device.then(answers => ({ answers }))])
      if (!('answers' in won)) return won.r
      byDevice = true
      return { result: { questions: e.questions, answers: won.answers } }
    } finally {
      askWaiters.delete(req)
      if (!byDevice) followUp($, next.signal, '/resolved', { req, by: next.signal.aborted ? 'abort' : 'terminal' })
    }
  })

  on('tool.call', async ($, e, next) => {
    if (e.tool === 'AskUserQuestion') return next(e)
    const { tool, tool_use_id: _id, agentId: _agent, consent: _consent, ...input } = e as ToolCallInput & { consent?: string }
    try {
      return await next(e)
    } finally {
      followUp($, next.signal, '/tool_done', { sid, tool, input })
    }
  })
}
