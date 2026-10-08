// app.js を最小限の偽 DOM の上で動かし、画面の遷移でヘッダと本文が正しく切り替わるかを調べる
const fs = require("fs");
const vm = require("vm");
const path = require("path");

class El {
  constructor(tag, id) {
    this.tag = tag; this.id = id; this.hidden = false; this.textContent = ""; this.children = [];
    this.className = ""; this.value = ""; this.disabled = false; this.listeners = {}; this.parent = null;
    this.classList = { add: () => {}, remove: () => {} };
  }
  setAttribute(k, v) { if (k === "hidden") this.hidden = true; else this[k] = v; }
  addEventListener(t, f) { (this.listeners[t] ||= []).push(f); }
  dispatch(t, ev = {}) { for (const f of this.listeners[t] || []) f({ preventDefault() {}, target: this, ...ev }); }
  // 実際の DOM と同じく、ノード以外（文字列や null）は文字列にしたテキストとして入れる
  append(...xs) {
    for (let x of xs) {
      if (!(x instanceof El)) { const t = new El("#text"); t.textContent = String(x); x = t; }
      x.remove(); x.parent = this; this.children.push(x);
    }
  }
  prepend(...xs) { this.append(...xs); }
  insertBefore(x, ref) { x.remove(); x.parent = this; const i = ref ? this.children.indexOf(ref) : -1; this.children.splice(i < 0 ? this.children.length : i, 0, x); }
  replaceChildren(...xs) { this.children.forEach((c) => { c.parent = null; }); this.children = []; this.append(...xs); }
  remove() { if (this.parent) { this.parent.children = this.parent.children.filter((c) => c !== this); this.parent = null; } }
  querySelector() { return null; }
  querySelectorAll() { return []; }
  closest() { return this; }
  get text() { return [this.textContent, ...this.children.map((c) => c.text)].join(""); }
}

const ids = {};
const listeners = {};
let es;
const ctx = {
  console,
  document: {
    getElementById: (id) => (ids[id] ||= new El("div", id)),
    createElement: (tag) => new El(tag),
    body: { scrollHeight: 0 },
  },
  window: {
    innerHeight: 0, scrollY: 0, scrollTo() {}, scrollBy() {},
    addEventListener: (t, f) => { (listeners[t] ||= []).push(f); },
  },
  history: { state: null, entries: [null],
    pushState(s) { this.state = s; this.entries.push(s); },
    replaceState(s) { this.state = s; this.entries[this.entries.length - 1] = s; },
    back() { this.entries.pop(); this.state = this.entries[this.entries.length - 1]; for (const f of listeners.popstate || []) f({ state: this.state }); } },
  EventSource: class { constructor() { es = this; this.readyState = 1; } close() { this.readyState = 2; } },
  fetch: async () => ({ ok: false, status: 404, json: async () => ({}) }),
  setInterval: () => 0, clearInterval: () => {}, setTimeout: () => 0, clearTimeout: () => {},
  Date, JSON, Map, Set, Math, Number, String, Object, Array, encodeURIComponent, Promise,
};
ctx.addEventListener = ctx.window.addEventListener;
vm.createContext(ctx);
const ui = path.join(__dirname, "..", "..", "cardbuddy", "webui");
vm.runInContext(fs.readFileSync(path.join(ui, "activity.js"), "utf8"), ctx);
// const 宣言はコンテキストのグローバルに出ないので、関数を明示的に取り出す
vm.runInContext(fs.readFileSync(path.join(ui, "app.js"), "utf8") + "\n;globalThis.__app = { openDetail, state };", ctx);

const $ = (id) => ctx.document.getElementById(id);
const view = () => ({
  list: !$("list").hidden, detail: !$("detail").hidden,
  hlist: !$("hbar-list").hidden, hdetail: !$("hbar-detail").hidden,
  title: $("h-title").textContent, current: ctx.__app.state.current,
});
const send = (ev) => es.onmessage({ data: JSON.stringify(ev) });
const session = (sid, n, name) => ({ sid, n, id: sid.slice(0, 8), name, title: "", state: "idle", last: "", updated_at: 0 });

const out = {};
send({ t: "sessions", s: [session("aaaaaaaa-1", 1, "alpha"), session("bbbbbbbb-2", 2, "beta")], now: Date.now() / 1000 });
out.start = view();
ctx.__app.openDetail("aaaaaaaa-1");
out.openA = view();
$("back").dispatch("click");
out.back = view();
ctx.__app.openDetail("aaaaaaaa-1");
ctx.__app.openDetail("bbbbbbbb-2");
out.openB = view();
send({ t: "sessions", s: [session("bbbbbbbb-2", 2, "beta2"), session("aaaaaaaa-1", 1, "alpha")], now: Date.now() / 1000 });
out.sseUpdate = view();
ctx.history.back();
out.popToA = view();
ctx.history.back();
out.popToList = view();
ctx.__app.openDetail("aaaaaaaa-1");
for (const f of listeners.pageshow || []) f({ persisted: true });
out.pageshow = view();
send({ t: "sessions", s: [session("bbbbbbbb-2", 2, "beta2")], now: Date.now() / 1000 });
out.vanished = view();
ctx.__app.openDetail("bbbbbbbb-2");
send({ t: "activity", sid: "bbbbbbbb-2", events: [], running: [], now: Date.now() / 1000 });
out.activityText = $("activity").text;
console.log(JSON.stringify(out));
process.exit(0);
