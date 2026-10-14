// app.js を最小限の偽 DOM の上で動かし、画面の遷移でヘッダと本文が正しく切り替わるかを調べる
const { boot } = require("./fakedom");
const { ctx, listeners, $: getEl, send: sendEv, session: mk } = boot();
const $ = getEl;
const view = () => ({
  list: !$("list").hidden, detail: !$("detail").hidden,
  hlist: !$("hbar-list").hidden, hdetail: !$("hbar-detail").hidden,
  title: $("h-title").textContent, current: ctx.__app.state.current,
});
const send = sendEv;
const session = mk;

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
