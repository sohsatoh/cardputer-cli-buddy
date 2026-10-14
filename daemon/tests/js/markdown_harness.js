// 標準入力の JSON { md: {名前: Markdown}, log: [[kind, text], ...] } を描画し、要素の木を JSON で返す
const fs = require("fs");
const vm = require("vm");
const { boot } = require("./fakedom");
const { ctx } = boot();
const Markdown = vm.runInContext("Markdown", ctx);

function ser(n) {
  if (typeof n === "string") return n;
  if (n.tag === "#text") return n.textContent;
  const attrs = { ...n.attrs, ...(n.className ? { class: n.className } : {}) };
  const kids = n.textContent ? [n.textContent] : n.children.map(ser);
  return [n.tag, ...(Object.keys(attrs).length ? [attrs] : []), ...kids];
}

const input = JSON.parse(fs.readFileSync(0, "utf8"));
const out = { md: {}, log: [], ms: {} };
for (const [name, text] of Object.entries(input.md || {})) out.md[name] = Markdown.render(text).map(ser);
for (const [kind, text] of input.log || []) out.log.push(ser(ctx.__app.logItem(kind, text)));
for (const [name, text] of Object.entries(input.time || {})) {
  const t0 = process.hrtime.bigint();
  Markdown.render(text);
  out.ms[name] = Number(process.hrtime.bigint() - t0) / 1e6;
}
console.log(JSON.stringify(out));
process.exit(0);
