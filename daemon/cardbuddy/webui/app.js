"use strict";
// 表示はすべて textContent で組み立てる（hint などはツールの入力そのままで、HTML として解釈させない）

const $ = (id) => document.getElementById(id);
const state = { sessions: [], reqs: new Map(), current: null, guardUntil: 0 };
const cards = { list: new Map(), detail: new Map() };
// 制御文字・双方向制御文字・ゼロ幅文字は、見た目と実際の文字列を食い違わせられるので記号で見せる
const HIDDEN = /[\u0000-\u0008\u000B-\u001F\u007F-\u009F\u00AD\u061C\u200B-\u200F\u2028-\u202E\u2060-\u2064\u2066-\u206F\uFEFF]/g;
const STATE_LABEL = { running: "実行中", idle: "待機", perm: "許可待ち", ask: "質問" };

function el(tag, attrs = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v;
    else if (k === "text") e.textContent = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v);
  }
  for (const c of children) if (c) e.append(c);
  return e;
}

function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { t.hidden = true; }, 2500);
}

async function post(path, body) {
  try {
    const res = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CardBuddy": "1" },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) toast(data.error || `エラー (${res.status})`);
    return res.ok ? data : null;
  } catch (e) {
    toast("送信できませんでした");
    return null;
  }
}

function visible(text) {
  const pre = el("pre");
  let last = 0;
  for (const m of text.matchAll(HIDDEN)) {
    pre.append(text.slice(last, m.index));
    pre.append(el("span", { class: "ctl", text: `\\u{${m[0].codePointAt(0).toString(16).toUpperCase()}}` }));
    last = m.index + m[0].length;
  }
  pre.append(text.slice(last));
  return pre;
}

function label(s) {
  return `${s.n ? "#" + s.n : "#-"} ${s.name}`;
}

const ACTIVITY_KEEP = 200;
const acts = new Map(); // sid -> { events, offset }  offset は buddyd の時計と端末の時計の差（秒）

function serverNow(a) {
  return Date.now() / 1000 + (a ? a.offset : 0);
}

function onActivity(ev) {
  const local = Date.now() / 1000;
  if (ev.events) {
    acts.set(ev.sid, { events: ev.events, offset: ev.now - local });
    return;
  }
  const a = acts.get(ev.sid) || { events: [], offset: 0 };
  a.events = ev.item.type === "turn_start" ? [ev.item] : [...a.events, ev.item].slice(-ACTIVITY_KEEP);
  a.offset = ev.item.at - local;
  acts.set(ev.sid, a);
}

function derived(s) {
  const a = acts.get(s.sid);
  const d = Activity.derive(a ? a.events : [], serverNow(a));
  return { d, st: Activity.state(s.state, d), now: serverNow(a) };
}

function ago(sec) {
  const s = Math.max(0, Math.floor(sec));
  return s < 60 ? `${s}秒` : `${Math.floor(s / 60)}分${String(s % 60).padStart(2, "0")}秒`;
}

const BUSY_LABEL = { main: "処理中", sub: "subagent 作業中", perm: "承認待ち", ask: "回答待ち", idle: "待機中" };

function badge(s) {
  const { d, st, now } = derived(s);
  if (st === "idle") return null;
  const text = st === "main" ? `処理中 ${ago(now - d.mainSince)}${d.subBusy ? " ＋subagent" : ""}` : BUSY_LABEL[st];
  return el("span", { class: `busy ${st}`, text });
}

function renderActivity(s) {
  const { d, st, now } = derived(s);
  const head = st === "main"
    ? `処理中（main）　${ago(now - d.mainSince)}${d.subBusy ? "　＋ subagent が作業中" : ""}`
    : BUSY_LABEL[st];
  const running = d.running.map((r) => el("div", { class: `run${r.agent_id ? " sub" : ""}` },
    el("span", { class: "who", text: r.agent_id ? `↳ ${r.agent_type || "subagent"}` : "main" }),
    el("span", { text: ` ${r.tool}${r.summary ? "  " + r.summary : ""}` }),
    el("span", { class: "sub", text: `  ${ago(now - r.at)}` })));
  const rows = d.rows.map((r) => {
    let text;
    if (r.kind === "turn") text = `▶ ${r.text || "（続きのターン）"}`;
    else if (r.kind === "turn_end") text = `■ ${r.sub ? `${r.agent_type || "subagent"} の終了` : "ターン終了"}（${r.reason}、${(r.ms / 1000).toFixed(1)}秒）`;
    else {
      const status = r.status === "running" ? `実行中 ${ago(now - r.at)}` : r.status === "error" ? "エラー" : `${(r.ms / 1000).toFixed(1)}秒`;
      text = `${r.sub ? `↳ [${r.agent_type || "subagent"}] ` : ""}${r.tool}${r.summary ? "  " + r.summary : ""}  — ${status}`;
    }
    return el("div", { class: `row${r.sub ? " sub" : ""}${r.status === "error" ? " err" : ""}`, text });
  });
  // replaceChildren は null を "null" という文字として入れるので、空の部分は先に除く
  $("activity").replaceChildren(...[
    el("div", { class: `top` }, el("span", { class: `busy ${st}`, text: head })),
    d.prompt ? el("p", { class: "sub", text: `最新のプロンプト: ${d.prompt}` }) : null,
    running.length ? el("p", { class: "sub", text: "実行中のツール" }) : null,
    ...running,
    rows.length ? el("details", {}, el("summary", { text: `最新ターンの処理ログ（${rows.length} 件）` }),
      el("div", { class: "rows" }, ...rows)) : null,
  ].filter(Boolean));
}

const list = { order: "", guardUntil: 0, offset: 0 };

function relative(at) {
  if (!at) return "";
  const sec = Date.now() / 1000 + list.offset - at;
  if (sec < 60) return "たった今";
  if (sec < 3600) return `${Math.floor(sec / 60)} 分前`;
  if (sec < 86400) return `${Math.floor(sec / 3600)} 時間前`;
  return `${Math.floor(sec / 86400)} 日前`;
}

function sortedSessions() {
  const rows = [...state.sessions].sort((a, b) => (b.updated_at || 0) - (a.updated_at || 0));
  const order = rows.map((s) => s.sid).join(",");
  // 並びが変わった直後は行の位置がずれているので、別のセッションを開かないようタップを受け付けない
  if (order !== list.order) {
    list.order = order;
    list.guardUntil = Date.now() + 500;
  }
  return rows;
}

function sessionCard(s) {
  const open = () => { if (Date.now() >= list.guardUntil) openDetail(s.sid); };
  return el("div", { class: "card session", onclick: open },
    el("div", { class: "top" },
      el("span", { class: "num", text: s.n ? `#${s.n}` : "#-" }),
      el("span", { class: "name", text: s.name }),
      badge(s),
      el("span", { class: `state ${s.state}`, text: STATE_LABEL[s.state] || s.state })),
    s.title ? el("p", { class: "sub", text: s.title }) : null,
    s.last ? el("p", { class: "sub", text: "↳ " + s.last }) : null,
    s.updated_at ? el("p", { class: "ago", text: relative(s.updated_at) }) : null);
}

// 見出しを付けた別々の <pre> に分け、本文の中の "- " や "+ " で差分の向きを偽造させない
function section(title, text) {
  const lines = text.split("\n").length;
  return el("div", { class: "section" },
    el("p", { class: "sub", text: `${title}（${lines} 行・${text.length} 文字、全文）` }), visible(text));
}

function diffBlocks(d) {
  const head = el("p", { class: "path", text: d.path + (d.replace_all ? "（すべて置き換え）" : "") });
  if ("content" in d) return [head, section("書き込む内容", d.content)];
  return [head, section("変更前（削除）", d.old), section("変更後（追加）", d.new)];
}

function permCard(r) {
  const buttons = el("div", { class: "row" },
    el("button", { class: "deny", type: "button", text: "拒否", onclick: (e) => answer(e, "/api/perm_reply", { req: r.req, decision: "deny" }) }),
    el("button", { class: "allow", type: "button", text: "許可", onclick: (e) => answer(e, "/api/perm_reply", { req: r.req, decision: "allow" }) }));
  return el("div", { class: "card req" },
    el("div", { class: "top" }, el("span", { class: "name", text: `${label(r)} — ${r.tool}` })),
    r.desc ? el("p", { text: r.desc }) : null,
    ...(r.diff ? diffBlocks(r.diff) : [section("内容", r.hint)]),
    r.extra ? section("その他の入力", r.extra) : null,
    buttons);
}

const TEXT_MAX = 500;

function askCard(r) {
  const form = el("form", { class: "card req" },
    el("div", { class: "top" }, el("span", { class: "name", text: `${label(r)} — 質問` })));
  r.qs.forEach((q, i) => {
    const type = q.m ? "checkbox" : "radio";
    const box = el("div", { class: "q" }, el("p", { class: "sub", text: q.h }), el("p", { text: q.q }));
    q.o.forEach((o, j) => {
      box.append(el("label", {}, el("input", { type, name: `q${i}`, value: String(j) }), el("span", { text: o })));
    });
    // Claude Code の "Other" に当たる自由入力。入力すると「その他」が選ばれる
    const other = el("input", { type, name: `q${i}`, value: "other" });
    const text = el("input", { type: "text", class: "other", maxlength: String(TEXT_MAX), placeholder: "その他（自由入力）" });
    text.addEventListener("input", () => { if (text.value) other.checked = true; });
    box.append(el("label", {}, other, text));
    if (q.m) box.append(el("p", { class: "sub", text: "複数選べます（その他は選んだ項目と一緒に送ります）" }));
    form.append(box);
  });
  form.append(el("div", { class: "row" }, el("span"), el("button", { class: "primary", type: "submit", text: "回答" })));
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const answers = [];
    for (const [i, q] of r.qs.entries()) {
      const checked = [...form.querySelectorAll(`input[name="q${i}"]:checked`)];
      const free = checked.some((x) => x.value === "other");
      const picks = checked.filter((x) => x.value !== "other").map((x) => Number(x.value));
      if (!free) {
        if (!picks.length) return toast("すべての問いに答えてください");
        answers.push(picks);
        continue;
      }
      const typed = form.querySelectorAll("input.other")[i].value.trim();
      if (!typed) return toast("「その他」の内容を入力してください");
      // multi では Claude Code と同じく、選んだラベルと入力を ", " でつないだ 1 つの回答にする
      const value = [...picks.map((j) => q.o[j]), typed].join(", ");
      if (value.length > TEXT_MAX) return toast(`回答は ${TEXT_MAX} 文字までです`);
      answers.push({ text: value });
    }
    answer(e, "/api/ask_reply", { req: r.req, answers });
  });
  return form;
}

async function answer(e, path, body) {
  // カードが増減した直後はボタンの位置がずれているので、押し間違いとみなして受け付けない
  if (Date.now() < state.guardUntil) return toast("表示が変わりました。もう一度確認してください");
  const card = e.target.closest(".card");
  card.querySelectorAll("button").forEach((b) => { b.disabled = true; });
  const res = await post(path, body);
  if (res && !res.ok) toast("すでに解決済みでした");
  if (!res) card.querySelectorAll("button").forEach((b) => { b.disabled = false; });
}

function reqCard(r) {
  return r.t === "perm" ? permCard(r) : askCard(r);
}

function syncCards(container, reqs, cache) {
  const keep = new Set(reqs.map((r) => r.req));
  let changed = false;
  for (const req of [...cache.keys()]) {
    if (!keep.has(req)) { cache.get(req).remove(); cache.delete(req); changed = true; }
  }
  for (const r of reqs) {
    if (!cache.has(r.req)) { cache.set(r.req, reqCard(r)); changed = true; }
  }
  Activity.placeInOrder(container, reqs.map((r) => cache.get(r.req)));
  if (changed) state.guardUntil = Date.now() + 800;
}

// 画面の表示は state.current だけから導く（遷移・SSE・1 秒ごとの描き直しのどれからでも同じ結果になる）
function syncView() {
  const detail = !!state.current;
  $("list").hidden = detail;
  $("detail").hidden = !detail;
  $("hbar-list").hidden = detail;
  $("hbar-detail").hidden = !detail;
}

function render() {
  if (state.current && !state.sessions.some((x) => x.sid === state.current)) {
    history.replaceState(null, "");
    showList();
    return;
  }
  syncView();
  const reqs = [...state.reqs.values()];
  syncCards($("reqs"), reqs, cards.list);
  const cardsInOrder = sortedSessions().map(sessionCard);
  $("sessions").replaceChildren(...(cardsInOrder.length ? cardsInOrder : [el("p", { class: "empty", text: "セッションはありません" })]));
  if (state.current) {
    const s = state.sessions.find((x) => x.sid === state.current);
    $("d-title").textContent = label(s);
    $("h-title").textContent = label(s);
    $("h-badge").replaceChildren(...[badge(s)].filter(Boolean));
    $("d-sub").textContent = `${STATE_LABEL[s.state] || s.state}　${s.title}`;
    const open = $("activity").querySelector("details")?.open;
    renderActivity(s);
    if (open) $("activity").querySelector("details").open = true;
    syncCards($("d-reqs"), reqs.filter((r) => r.sid === s.sid), cards.detail);
  }
}

const POLL_MS = 4000;
const HEAD_LINES = 4;
const tx = { sid: null, start: 0, end: 0, timer: null, busy: false };

// transcript が見つからないとき（別の場所に保存するプロファイルなど）は、buddyd が持つ要約のログを出す
async function loadSummaryLog(sid) {
  const res = await fetch(`/api/sessions/${encodeURIComponent(sid)}/log`);
  if (!res.ok || sid !== state.current) return;
  const { items } = await res.json();
  const nodes = items.map((it) => logItem(it.r === "u" ? "user" : "assistant", it.x));
  $("log").replaceChildren(...(nodes.length ? nodes : [el("p", { class: "empty", text: "ログはまだありません" })]));
  window.scrollTo(0, document.body.scrollHeight);
}

function logItem(kind, text) {
  return el("div", { class: `log-item ${kind === "user" ? "u" : "a"}` },
    el("div", { class: "who", text: kind === "user" ? "あなた" : "Claude" }),
    el("div", { class: "x", text }));
}

function foldable(cls, head, full) {
  const body = visible(full);
  body.hidden = true;
  const box = el("div", { class: `fold ${cls}` }, el("div", { class: "head", text: head }), body);
  box.addEventListener("click", () => { body.hidden = !body.hidden; });
  return box;
}

function txItem(it) {
  if (it.kind === "user" || it.kind === "assistant") return logItem(it.kind, it.text);
  if (it.kind === "tool_use") return foldable("tool", `▸ ${it.name}  ${it.summary}`, it.text);
  const lines = it.text.split("\n");
  const head = lines.slice(0, HEAD_LINES).join("\n") + (lines.length > HEAD_LINES ? `\n…（全 ${lines.length} 行、タップで展開）` : "");
  return foldable(it.is_error ? "result err" : "result", head || "（出力なし）", it.text);
}

async function fetchTx(query) {
  const sid = state.current;
  const res = await fetch(`/api/transcript?sid=${encodeURIComponent(sid)}&${query}`);
  if (sid !== state.current) return null;
  return res.ok ? res.json() : (res.status === 404 ? "missing" : null);
}

async function loadLatest() {
  const page = await fetchTx("limit=200");
  if (page === "missing") return loadSummaryLog(state.current);
  if (!page) return;
  tx.start = page.start;
  tx.end = page.end;
  const nodes = page.items.map(txItem);
  $("log").replaceChildren(...(nodes.length ? nodes : [el("p", { class: "empty", text: "ログはまだありません" })]));
  $("older").hidden = tx.start === 0;
  window.scrollTo(0, document.body.scrollHeight);
  clearInterval(tx.timer);
  tx.timer = setInterval(pollTx, POLL_MS);
}

async function loadOlder() {
  const page = await fetchTx(`before=${tx.start}&limit=200`);
  if (!page || page === "missing") return;
  const before = document.body.scrollHeight;
  $("log").prepend(...page.items.map(txItem));
  tx.start = page.start;
  $("older").hidden = tx.start === 0;
  // 古い分を上に足しても、読んでいた位置が動かないようにする
  window.scrollBy(0, document.body.scrollHeight - before);
}

async function pollTx() {
  if (tx.busy || !state.current) return;
  tx.busy = true;
  try {
    const page = await fetchTx(`after=${tx.end}&limit=1000`);
    if (!page || page === "missing" || !page.items.length) {
      if (page && page !== "missing") tx.end = page.end;
      return;
    }
    const atBottom = window.innerHeight + window.scrollY >= document.body.scrollHeight - 40;
    $("log").querySelector(".empty")?.remove();
    $("log").append(...page.items.map(txItem));
    tx.end = page.end;
    if (atBottom) window.scrollTo(0, document.body.scrollHeight);
  } finally {
    tx.busy = false;
  }
}

// iOS のスワイプで戻ったときにアプリから離れず一覧に戻れるよう、セッション画面は履歴に積む
function openDetail(sid, push = true) {
  if (push && history.state?.sid !== sid) history.pushState({ sid }, "");
  if (state.current !== sid) {
    $("log").replaceChildren();
    $("older").hidden = true;
  }
  state.current = sid;
  render();
  loadLatest();
}

function showList() {
  clearInterval(tx.timer);
  state.current = null;
  render();
}

function closeDetail() {
  if (history.state?.sid) history.back();
  else showList();
}

window.addEventListener("popstate", (e) => {
  const sid = e.state?.sid;
  if (sid && state.sessions.some((x) => x.sid === sid)) openDetail(sid, false);
  else showList();
});

function onEvent(ev) {
  if (ev.t === "sessions") {
    const prev = state.sessions.find((x) => x.sid === state.current);
    state.sessions = ev.s;
    if (typeof ev.now === "number") list.offset = ev.now - Date.now() / 1000;
    const now = state.sessions.find((x) => x.sid === state.current);
    if (prev && now && (prev.last !== now.last || prev.state !== now.state)) pollTx();
  } else if (ev.t === "perm" || ev.t === "ask") {
    state.reqs.set(ev.req, ev);
  } else if (ev.t === "resolved") {
    state.reqs.delete(ev.req);
  } else if (ev.t === "ack_prompt") {
    toast(ev.ok ? "送信しました" : "送れませんでした");
  } else if (ev.t === "activity") {
    onActivity(ev);
  }
  render();
}

let es = null;

function connect() {
  es = new EventSource("/api/events");
  es.onopen = () => {
    // 再接続のたびにスナップショットが届くので、それまでの要求は捨てて作り直す
    state.reqs.clear();
    $("conn").textContent = "接続済み";
    $("conn").classList.add("on");
  };
  es.onmessage = (m) => onEvent(JSON.parse(m.data));
  es.onerror = () => {
    $("conn").textContent = "再接続中…";
    $("conn").classList.remove("on");
  };
}

$("back").addEventListener("click", closeDetail);
$("latest").addEventListener("click", () => window.scrollTo({ top: document.body.scrollHeight, behavior: "smooth" }));
$("reload").addEventListener("click", loadLatest);
$("older").addEventListener("click", loadOlder);
$("prompt").addEventListener("input", () => { $("count").textContent = `${$("prompt").value.length} / 500`; });
$("prompt-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const s = state.sessions.find((x) => x.sid === state.current);
  const text = $("prompt").value.trim();
  if (!s || !text) return;
  $("send").disabled = true;
  const res = await post("/api/prompt", { sid: s.sid, text });
  $("send").disabled = false;
  if (res) {
    $("prompt").value = "";
    $("count").textContent = "0 / 500";
  }
});
// 「n 分前」を進めるため、処理中でなくても 30 秒ごとに描き直す
setInterval(render, 30000);
// 経過時間は端末側で 1 秒ごとに進める（処理中のセッションがあるときだけ描き直す）
setInterval(() => {
  if (state.sessions.some((s) => derived(s).st !== "idle")) render();
}, 1000);
// bfcache から戻ったときは、止まっていた SSE をつなぎ直し、画面を状態に合わせ直す
window.addEventListener("pageshow", (e) => {
  if (e.persisted && es && es.readyState === 2) connect();
  render();
});
render();
connect();
