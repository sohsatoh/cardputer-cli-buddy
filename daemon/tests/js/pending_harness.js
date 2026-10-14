// 要対応（perm / ask）の帯・一覧の固定・セッション画面の積み順・タップの抑止を偽 DOM で調べる
const { boot } = require("./fakedom");
const t = boot();
const { $, send, session, ctx } = t;
const now = Date.now() / 1000;
const kids = (id, attr) => $(id).children.map((c) => c.getAttribute(attr));
const perm = (req, sid, n, name, at, tool = "Bash") =>
  ({ t: "perm", req, sid, at, n, id: sid.slice(0, 8), name, tool, desc: "説明", hint: "ls", extra: "" });

const out = {};
send({ t: "sessions", now, s: [session("cccccccc-3", 3, "gamma", { updated_at: now }),
  session("aaaaaaaa-1", 1, "alpha", { updated_at: now - 100 }), session("bbbbbbbb-2", 2, "beta", { updated_at: now - 50 })] });
out.none = { bar: !$("pending-bar").hidden, title: ctx.document.title };

send(perm("r1", "aaaaaaaa-1", 1, "alpha", now - 10));
send({ t: "ask", req: "r2", sid: "bbbbbbbb-2", at: now - 30, n: 2, id: "bbbbbbbb", name: "beta",
  qs: [{ q: "どれにする？", h: "H", o: ["a", "b"], m: false }] });
send(perm("r3", "aaaaaaaa-1", 1, "alpha", now - 60, "Edit"));
out.list = {
  bar: !$("pending-bar").hidden, barText: $("pending-bar").text, title: ctx.document.title,
  order: kids("sessions", "data-sid"),
  attention: $("sessions").children.map((c) => c.classList.contains("attention")),
};
$("pending-bar").dispatch("click");
out.items = { open: !$("pending-list").hidden, reqs: kids("pending-list", "data-req"), text: $("pending-list").text };
$("pending-list").children[1].dispatch("click");
out.jump = { current: ctx.__app.state.current, cards: kids("d-reqs", "data-req"), listOpen: !$("pending-list").hidden };

ctx.__app.openDetail("aaaaaaaa-1");
out.detail = { barText: $("pending-bar").text, cards: kids("d-reqs", "data-req") };
send(perm("r4", "cccccccc-3", 3, "gamma", now));
out.other = $("pending-bar").text;

ctx.__app.state.guardUntil = 0;
send(perm("r5", "aaaaaaaa-1", 1, "alpha", now));
const card = $("d-reqs").children.find((c) => c.getAttribute("data-req") === "r5");
out.flash = card.classList.contains("flash");
const allow = card.find((e) => e.tag === "button" && e.textContent === "許可");
const replies = () => t.fetches.filter((f) => f.url === "/api/perm_reply").length;
allow.dispatch("click");
out.guarded = replies();
ctx.__app.state.guardUntil = 0;
allow.dispatch("click");
out.allowed = replies();
out.badge = ctx.navigator.badges[ctx.navigator.badges.length - 1];
out.title = ctx.document.title;
console.log(JSON.stringify(out));
process.exit(0);
