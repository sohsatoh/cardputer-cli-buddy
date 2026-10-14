"use strict";
// HTML は解釈せず、文字はすべてテキストノードとして入れる（Claude の発言にはツール出力やファイルの中身がそのまま混じる）

const Markdown = (() => {
  // 制御文字・双方向制御文字・ゼロ幅文字は、見た目と実際の文字列を食い違わせられるので記号で見せる
  const HIDDEN = /[\u0000-\u0008\u000B-\u001F\u007F-\u009F\u00AD\u061C\u200B-\u200F\u2028-\u202E\u2060-\u2064\u2066-\u206F\uFEFF]/g;
  // 行の残りは "." でなく [^\n]* で取る（"." は U+2028 で止まり、"$" と組み合わさるとバックトラックが爆発する）
  const FENCE = /^ {0,3}(?:(`{3,})[^`]*|(~{3,})[^\n]*)$/;
  const FENCE_END = /^ {0,3}(`{3,}|~{3,})[ \t]*$/;
  const HEADING = /^ {0,3}(#{1,6})(?:[ \t]+([^\n]*))?$/;
  const HR = /^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$/;
  const QUOTE = /^ {0,3}> ?([^\n]*)$/;
  const ITEM = /^([ \t]*)([-*+]|\d{1,9}[.)])(?:[ \t]+([^\n]*))?$/;
  const LINK = /^(!?)\[([^\]\n]*)\]\(\s*([^\s()]*(?:\([^\s()]*\)[^\s()]*)*)(?:\s+"[^"\n]*")?\s*\)/;
  const ESCAPABLE = /[!-\/:-@[-`{-~]/;
  const WORD = /[\p{L}\p{N}]/u;
  // 上流の transcript は 1 件を 4000 文字で切る前提で、これを超える長さは 2 乗の正規表現（LINK など）が重くなるので解釈しない
  const MAX_LEN = 8000;
  const MAX_DEPTH = 16;

  function h(tag, attrs, ...kids) {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) e.setAttribute(k, v);
    e.append(...kids);
    return e;
  }

  function showHidden(text) {
    const out = [];
    let last = 0;
    for (const m of text.matchAll(HIDDEN)) {
      out.push(text.slice(last, m.index), h("span", { class: "ctl" }, `\\u{${m[0].codePointAt(0).toString(16).toUpperCase()}}`));
      last = m.index + m[0].length;
    }
    out.push(text.slice(last));
    return out.filter((x) => x !== "");
  }

  const blank = (line) => !line.trim();
  const indentOf = (line) => /^[ \t]*/.exec(line)[0].length;

  // javascript: や data: などで開かせないよう、http(s) の絶対 URL だけをリンクにする
  function safeUrl(s) {
    try {
      const u = new URL(s);
      return u.protocol === "http:" || u.protocol === "https:" ? u : null;
    } catch (e) {
      return null;
    }
  }

  const link = (href, ...kids) => h("a", { href, target: "_blank", rel: "noopener noreferrer" }, ...kids);

  // Safari の古い版は正規表現の後読みを解釈できず、ファイルごと読み込めなくなるので 1 文字ずつ分ける
  function cells(row) {
    let s = row.trim();
    if (s.startsWith("|")) s = s.slice(1);
    const out = [""];
    for (let i = 0; i < s.length; i++) {
      if (s[i] === "\\" && s[i + 1] === "|") { out[out.length - 1] += "|"; i++; }
      else if (s[i] === "|") out.push("");
      else out[out.length - 1] += s[i];
    }
    if (out.length > 1 && !out[out.length - 1].trim()) out.pop();
    return out.map((c) => c.trim());
  }

  // 区切り行を 1 本の正規表現で判定すると、空白の分け方が複数あって 2 乗になるのでセルごとに見る
  function isTableStart(lines, i) {
    if (!lines[i].includes("|") || i + 1 >= lines.length || !lines[i + 1].includes("-")) return false;
    const delim = cells(lines[i + 1]);
    return delim.every((c) => /^:?-+:?$/.test(c)) && delim.length === cells(lines[i]).length;
  }

  function startsBlock(lines, i) {
    const l = lines[i];
    return FENCE.test(l) || HEADING.test(l) || HR.test(l) || QUOTE.test(l) || ITEM.test(l) || isTableStart(lines, i);
  }

  // 閉じの "#" 列は、前が空白のときだけ取り除く（"# C#" は残す）
  function headingText(t) {
    t = t.trim();
    let k = t.length;
    while (k > 0 && t[k - 1] === "#") k--;
    if (k === 0) return "";
    return t[k - 1] === " " || t[k - 1] === "\t" ? t.slice(0, k).trim() : t;
  }

  // 同じ記号の連なりを 1 つの単位として閉じ側を探す（"***" で ** と * を同時に閉じられるように）
  // dead[d] より後から探し直しても閉じ側は無い（"_a _a _a …" で 2 乗にならないように覚えておく）
  function closer(s, from, d, dead) {
    const c = d[0];
    if (!s[from] || /\s/.test(s[from])) return -1;
    if (c === "_" && WORD.test(s[from - d.length - 1] || "")) return -1;
    if (from + 1 >= (dead[d] ?? Infinity)) return -1;
    let j = from + 1;
    while ((j = s.indexOf(c, j)) >= 0) {
      let k = j;
      while (s[k] === c) k++;
      if (k - j >= d.length && !/\s/.test(s[j - 1]) && !(c === "_" && WORD.test(s[k] || ""))) return k - d.length;
      j = k;
    }
    dead[d] = from + 1;
    return -1;
  }

  function inline(s, inLink = false, depth = 0) {
    if (depth > MAX_DEPTH) return [s];
    const out = [];
    let buf = "";
    const dead = {};
    // リンクの文字は行き先を偽る材料になるので、見えない文字も記号で見せる
    const flush = () => { if (buf) out.push(...(inLink ? showHidden(buf) : [buf])); buf = ""; };
    const push = (node) => { flush(); out.push(node); };
    let i = 0;
    while (i < s.length) {
      const c = s[i];
      if (c === "\\" && ESCAPABLE.test(s[i + 1] || "")) {
        buf += s[i + 1];
        i += 2;
      } else if (c === "`") {
        const m = /^(`+)([\s\S]*?[^`])\1(?!`)/.exec(s.slice(i));
        if (m) {
          let code = m[2].replace(/\n/g, " ");
          if (code.length > 2 && code[0] === " " && code[code.length - 1] === " " && code.trim()) code = code.slice(1, -1);
          push(h("code", {}, code));
          i += m[0].length;
        } else {
          const run = /^`+/.exec(s.slice(i))[0];
          buf += run;
          i += run.length;
        }
      } else if (!inLink && (c === "[" || (c === "!" && s[i + 1] === "["))) {
        const m = LINK.exec(s.slice(i));
        if (!m) { buf += c; i++; continue; }
        const u = safeUrl(m[3]);
        i += m[0].length;
        // 開けない URL は、何を開かせようとしたか分かるよう元の書き方のまま見せる
        if (!u) { buf += m[1] ? m[2] : m[0]; continue; }
        push(link(u.href, ...(m[1] ? showHidden(m[2] || u.href) : inline(m[2], true, depth + 1))));
        // 文字と行き先が違うリンクは、飛び先のホストを横に書いて偽装できないようにする
        if (![m[3], u.href, u.host].includes((m[2] || u.href).trim())) buf += ` (${u.host})`;
      } else if (c === "*" || c === "_") {
        const d = s[i + 1] === c ? c + c : c;
        const end = closer(s, i + d.length, d, dead);
        if (end < 0) { buf += d; i += d.length; continue; }
        push(h(d.length === 2 ? "strong" : "em", {}, ...inline(s.slice(i + d.length, end), inLink, depth + 1)));
        i = end + d.length;
      } else {
        buf += c;
        i++;
      }
    }
    flush();
    return out;
  }

  function list(lines, i, out, depth) {
    const first = ITEM.exec(lines[i]);
    const ordered = /\d/.test(first[2]);
    const start = parseInt(first[2], 10);
    const node = h(ordered ? "ol" : "ul", ordered && start !== 1 ? { start: String(start) } : {});
    while (i < lines.length) {
      const m = ITEM.exec(lines[i]);
      if (!m || /\d/.test(m[2]) !== ordered) break;
      const base = m[1].length;
      const width = base + m[2].length + 1;
      const body = [m[3] || ""];
      i++;
      while (i < lines.length) {
        const l = lines[i];
        if (blank(l)) {
          let j = i;
          while (j < lines.length && blank(lines[j])) j++;
          if (j < lines.length && indentOf(lines[j]) > base) { body.push(...lines.slice(i, j)); i = j; continue; }
          break;
        }
        if (indentOf(l) > base) body.push(l.slice(Math.min(width, indentOf(l))));
        else if (startsBlock(lines, i)) break;
        else body.push(l);
        i++;
      }
      node.append(h("li", {}, ...blocks(body, !body.some(blank), depth + 1)));
      let j = i;
      while (j < lines.length && blank(lines[j])) j++;
      const next = j < lines.length && ITEM.exec(lines[j]);
      if (!next || /\d/.test(next[2]) !== ordered) break;
      i = j;
    }
    out.push(node);
    return i;
  }

  // tight は空行の無いリスト項目の中身で、段落を <p> で包むと行間が空きすぎる
  // 入れ子の深さは自分で打ち切る（スタックの深さは端末のエンジンごとに違い、あふれ方に頼れない）
  function blocks(lines, tight = false, depth = 0) {
    if (depth > MAX_DEPTH) {
      const text = lines.join("\n").trim();
      return text ? [tight ? text : h("p", {}, text)] : [];
    }
    const out = [];
    let i = 0;
    let m;
    while (i < lines.length) {
      const line = lines[i];
      if (blank(line)) {
        i++;
      } else if ((m = FENCE.exec(line))) {
        const fence = m[1] || m[2];
        const pad = new RegExp(`^ {0,${indentOf(line)}}`);
        const body = [];
        for (i++; i < lines.length; i++) {
          const e = FENCE_END.exec(lines[i]);
          if (e && e[1][0] === fence[0] && e[1].length >= fence.length) { i++; break; }
          body.push(lines[i].replace(pad, ""));
        }
        out.push(h("pre", {}, h("code", {}, body.join("\n"))));
      } else if ((m = HEADING.exec(line))) {
        out.push(h(`h${m[1].length}`, {}, ...inline(headingText(m[2] || ""))));
        i++;
      } else if (HR.test(line)) {
        out.push(h("hr"));
        i++;
      } else if (QUOTE.test(line)) {
        const body = [];
        for (; i < lines.length && (m = QUOTE.exec(lines[i])); i++) body.push(m[1]);
        out.push(h("blockquote", {}, ...blocks(body, false, depth + 1)));
      } else if (isTableStart(lines, i)) {
        const head = cells(lines[i]);
        const rows = [];
        for (i += 2; i < lines.length && !blank(lines[i]) && lines[i].includes("|"); i++) rows.push(cells(lines[i]));
        const tr = (tag, cs) => h("tr", {}, ...head.map((_, j) => h(tag, {}, ...inline(cs[j] || ""))));
        out.push(h("div", { class: "table-wrap" }, h("table", {},
          h("thead", {}, tr("th", head)), h("tbody", {}, ...rows.map((r) => tr("td", r))))));
      } else if (ITEM.test(line)) {
        i = list(lines, i, out, depth);
      } else {
        const para = [];
        for (; i < lines.length && !blank(lines[i]) && !(para.length && startsBlock(lines, i)); i++) para.push(lines[i].trim());
        const kids = inline(para.join("\n"));
        if (tight) out.push(...kids);
        else out.push(h("p", {}, ...kids));
      }
    }
    return out;
  }

  function render(text) {
    const s = String(text ?? "");
    if (s.length > MAX_LEN) return [s];
    try {
      return blocks(s.replace(/\r\n?/g, "\n").split("\n"));
    } catch (e) {
      // 想定外の入力で例外が出ても、ログ全体の描画を止めずに素の文字で見せる
      return [s];
    }
  }

  return { render, showHidden };
})();

if (typeof module !== "undefined") module.exports = Markdown;
