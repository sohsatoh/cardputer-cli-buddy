"use strict";
// 状態の判定を node でテストできるよう、DOM に触れない純粋関数だけを置く

const Activity = (() => {
  // バックグラウンドの subagent は turn_end に agent_id が付かないことがあるので、直近のイベントで作業中とみなす
  const SUB_RECENT = 30;

  function derive(events, now) {
    let turn = null;
    for (const e of events) if (e.type === "turn_start") turn = e;
    const mainId = turn ? turn.turn_id : null;
    const starts = new Map();
    const ends = new Map();
    for (const e of events) {
      if (e.type === "tool_start") starts.set(e.tool_use_id, e);
      if (e.type === "tool_end") ends.set(e.tool_use_id, e);
    }
    // 届く順番は前後しうるので、実行中は「start があり、同じ id の end が無いもの」として導く
    const running = [...starts.values()].filter((e) => !ends.has(e.tool_use_id));
    const mainBusy = !!turn && !events.some((e) => e.type === "turn_end" && e.turn_id === mainId);

    let lastAgent = null;
    let lastSubEnd = null;
    for (const e of events) {
      const agent = e.agent_id || (e.type === "tool_end" && starts.get(e.tool_use_id)?.agent_id);
      if (agent) lastAgent = e;
      if (e.type === "turn_end" && e.turn_id !== mainId) lastSubEnd = e;
    }
    const recent = !!lastAgent && now - lastAgent.at <= SUB_RECENT && !(lastSubEnd && lastSubEnd.at >= lastAgent.at);
    const subBusy = running.some((e) => e.agent_id) || recent;

    const rows = [];
    for (const e of events) {
      if (e.type === "turn_start") {
        rows.push({ kind: "turn", sub: false, at: e.at, text: e.prompt || "" });
      } else if (e.type === "tool_start") {
        const end = ends.get(e.tool_use_id);
        rows.push({
          kind: "tool", sub: !!e.agent_id, tool_use_id: e.tool_use_id, tool: e.tool, summary: e.summary || "",
          agent_type: e.agent_type || "", at: e.at,
          status: end ? (end.is_error ? "error" : "ok") : "running", ms: end ? end.ms : null,
        });
      } else if (e.type === "turn_end") {
        rows.push({ kind: "turn_end", sub: e.turn_id !== mainId || !!e.agent_id, at: e.at, reason: e.reason,
                    ms: e.ms, agent_type: e.agent_type || "" });
      }
    }
    return { mainBusy, mainSince: mainBusy ? turn.at : null, prompt: turn ? turn.prompt || "" : "", subBusy, running, rows };
  }

  function state(sessionState, d) {
    if (sessionState === "perm" || sessionState === "ask") return sessionState;
    if (d.mainBusy) return "main";
    if (d.subBusy) return "sub";
    return "idle";
  }

  // 位置が合っているノードには触れない（入れ直すとフォーカスが外れ、iOS ではキーボードが閉じる）
  function placeInOrder(container, nodes) {
    nodes.forEach((node, i) => {
      const at = container.children[i] || null;
      if (at !== node) container.insertBefore(node, at);
    });
  }

  return { derive, state, placeInOrder, SUB_RECENT };
})();

if (typeof module !== "undefined") module.exports = Activity;
