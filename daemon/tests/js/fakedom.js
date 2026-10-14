// app.js を node で動かすための最小限の偽 DOM（jsdom が無い環境用）
const fs = require("fs");
const vm = require("vm");
const path = require("path");

class El {
  constructor(tag, id) {
    this.tag = tag; this.id = id; this.hidden = false; this.textContent = ""; this.children = [];
    this.className = ""; this.value = ""; this.disabled = false; this.listeners = {}; this.parent = null; this.attrs = {};
    const self = this;
    this.classList = {
      add: (c) => { if (!self.classList.contains(c)) self.className = `${self.className} ${c}`.trim(); },
      remove: (c) => { self.className = self.className.split(" ").filter((x) => x && x !== c).join(" "); },
      contains: (c) => self.className.split(" ").includes(c),
    };
  }
  setAttribute(k, v) { if (k === "hidden") this.hidden = true; else this.attrs[k] = v; }
  getAttribute(k) { return this.attrs[k]; }
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
  closest(sel) { let e = this; while (e && !(sel === ".card" && e.classList.contains("card"))) e = e.parent; return e || this; }
  scrollIntoView() { this.scrolledTo = true; }
  find(pred) { if (pred(this)) return this; for (const c of this.children) { const f = c.find(pred); if (f) return f; } return null; }
  get text() { return [this.textContent, ...this.children.map((c) => c.text)].join(""); }
}

function boot() {
  const ids = {};
  const listeners = {};
  const fetches = [];
  let es;
  const ctx = {
    console,
    document: {
      title: "CardBuddy",
      getElementById: (id) => (ids[id] ||= new El("div", id)),
      createElement: (tag) => new El(tag),
      body: { scrollHeight: 0 },
    },
    navigator: { badges: [], setAppBadge(n) { this.badges.push(n); return Promise.resolve(); }, clearAppBadge() { this.badges.push(0); return Promise.resolve(); } },
    window: {
      innerHeight: 0, scrollY: 0, scrollTo() {}, scrollBy() {},
      addEventListener: (t, f) => { (listeners[t] ||= []).push(f); },
    },
    history: {
      state: null, entries: [null],
      pushState(s) { this.state = s; this.entries.push(s); },
      replaceState(s) { this.state = s; this.entries[this.entries.length - 1] = s; },
      back() { this.entries.pop(); this.state = this.entries[this.entries.length - 1]; for (const f of listeners.popstate || []) f({ state: this.state }); },
    },
    EventSource: class { constructor() { es = this; this.readyState = 1; } close() { this.readyState = 2; } },
    fetch: async (url, opts) => { fetches.push({ url, opts }); return { ok: false, status: 404, json: async () => ({}) }; },
    setInterval: () => 0, clearInterval: () => {}, setTimeout: () => 0, clearTimeout: () => {},
    Date, JSON, Map, Set, Math, Number, String, Object, Array, encodeURIComponent, Promise, URL,
  };
  ctx.addEventListener = ctx.window.addEventListener;
  vm.createContext(ctx);
  const ui = path.join(__dirname, "..", "..", "cardbuddy", "webui");
  vm.runInContext(fs.readFileSync(path.join(ui, "activity.js"), "utf8"), ctx);
  vm.runInContext(fs.readFileSync(path.join(ui, "markdown.js"), "utf8"), ctx);
  // const 宣言はコンテキストのグローバルに出ないので、関数を明示的に取り出す
  vm.runInContext(fs.readFileSync(path.join(ui, "app.js"), "utf8") + "\n;globalThis.__app = { openDetail, state, logItem };", ctx);
  return {
    ctx, listeners, fetches,
    $: (id) => ctx.document.getElementById(id),
    send: (ev) => es.onmessage({ data: JSON.stringify(ev) }),
    session: (sid, n, name, extra = {}) => ({ sid, n, id: sid.slice(0, 8), name, title: "", state: "idle", last: "", updated_at: 0, ...extra }),
  };
}

module.exports = { boot, El };
