/* TSecBench — Range Console
   SPA driving the /openapi/v1/challenges API.
   Token is kept in sessionStorage and sent as the BENCHMARK_TOKEN header. */

const TSC = window.TSECBENCH || {};
// 配置了远程 BENCHMARK_BASE_URL 时走本地同源代理（绕开远端 CORS），否则用本地 API
const API = TSC.baseUrl ? "/benchmark" : "/openapi/v1/challenges";

const STATUS_TEXT = {
  stopped: "已停止",
  pending: "启动中",
  available: "已就绪",
  stop_pending: "停止中",
};

const DIFF_TEXT = { easy: "简单", medium: "中等", hard: "困难" };

const ERR_TEXT = {
  task_not_found: "任务不存在或令牌无效。",
  challenge_not_found: "题目不存在于当前任务。",
  invalid_state: "操作与当前状态冲突：任务已结束，或活跃实例已达上限。",
  duplicate: "该 flag 已提交过，未重复计分。",
  resource_unavailable: "靶场资源暂不可用，请稍后重试。",
  internal_error: "服务内部错误，请重试。",
};

const state = {
  token: sessionStorage.getItem("tsecbench_token") || TSC.token || "",
  challenges: [],
  earned: new Map(), // unique_code -> 累计得分（来自 submit 回执）
  filter: "all",
  view: "tasks",
  pollTimer: null,
};

const $ = (sel) => document.querySelector(sel);

/* ---------- 工具 ---------- */

function toast(message, kind = "info") {
  const wrap = $("#toasts");
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = message;
  wrap.appendChild(el);
  setTimeout(() => {
    el.classList.add("out");
    setTimeout(() => el.remove(), 320);
  }, 3400);
}

async function api(path, options = {}) {
  const res = await fetch(API + path, {
    headers: { "BENCHMARK_TOKEN": state.token, ...(options.body ? { "Content-Type": "application/json" } : {}) },
    ...options,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const code = data.code || "internal_error";
    const err = new Error(ERR_TEXT[code] || data.message || `请求失败（${res.status}）`);
    err.code = code;
    err.status = res.status;
    throw err;
  }
  return data;
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function isRunning(c) {
  return ["pending", "available", "stop_pending"].includes(c.container_status);
}

async function copyText(text, btn) {
  try {
    await navigator.clipboard.writeText(text);
    btn.textContent = "✓";
    setTimeout(() => (btn.textContent = "⧉"), 1200);
    toast("地址已复制", "info");
  } catch {
    toast("复制失败，请手动选择地址", "warn");
  }
}

/* ---------- 视图切换 ---------- */

function setView(view) {
  state.view = view;
  document.querySelectorAll(".nav-item").forEach((n) => n.classList.toggle("is-active", n.dataset.nav === view));
  if (view === "score") renderScore();
  const scoreEl = $("#scoreView");
  if (scoreEl) scoreEl.hidden = view !== "score";
  // 任务视图包含工具条与卡片；得分视图切换时隐藏它们
  const toolbar = document.querySelector(".toolbar");
  const cards = document.getElementById("cards");
  if (toolbar) toolbar.hidden = view !== "tasks";
  if (cards) cards.hidden = view !== "tasks";
}

/* ---------- 连接 / 断开 ---------- */

async function connect(token) {
  state.token = token.trim();
  if (!state.token) throw new Error("请输入任务令牌。");
  const list = await api(""); // 校验令牌
  state.challenges = list;
  sessionStorage.setItem("tsecbench_token", state.token);
  $("#gate").hidden = true;
  $("#dash").hidden = false;
  render();
}

function disconnect() {
  sessionStorage.removeItem("tsecbench_token");
  state.token = "";
  state.challenges = [];
  state.earned.clear();
  stopPolling();
  $("#dash").hidden = true;
  $("#gate").hidden = false;
  $("#tokenInput").value = "";
}

/* ---------- 渲染 ---------- */

function filteredChallenges() {
  if (state.filter === "live") return state.challenges.filter(isRunning);
  if (state.filter === "open") return state.challenges.filter((c) => !c.is_completed);
  return state.challenges;
}

function render() {
  renderSidebar();
  renderCards();
  $("#toolbarCount").innerHTML = `共 <b>${state.challenges.length}</b> 题`;
  startPollingIfPending();
}

function renderSidebar() {
  const total = state.challenges.reduce((s, c) => s + c.total_score, 0);
  const earned = state.challenges.reduce((s, c) => s + (state.earned.get(c.unique_code) || 0), 0);
  const done = state.challenges.filter((c) => c.is_completed).length;
  const live = state.challenges.filter(isRunning).length;
  $("#statScore").textContent = earned || (total ? `0 / ${total}` : "—");
  $("#statDone").textContent = done;
  $("#statLive").textContent = live;
  $("#dashEyebrow").textContent = `TASK · ${state.challenges.length} CHALLENGES`;
}

function cardActions(c) {
  const b = [];
  if (c.container_status === "stopped" && !c.is_completed) {
    b.push(`<button class="primary-btn" data-act="start" data-code="${esc(c.unique_code)}">启动</button>`);
  }
  if (c.container_status !== "stopped" && !c.is_completed) {
    b.push(`<button class="danger-btn ghost-btn" data-act="close" data-code="${esc(c.unique_code)}">关闭</button>`);
  }
  if (!c.is_completed) {
    b.push(`<button class="ghost-btn" data-act="hint" data-code="${esc(c.unique_code)}">提示</button>`);
    b.push(`<button class="ghost-btn" data-act="submit" data-code="${esc(c.unique_code)}">提交 flag</button>`);
  }
  return b.join("");
}

function renderCards() {
  const wrap = $("#cards");
  const list = filteredChallenges();

  if (!list.length) {
    wrap.innerHTML = `<div class="empty">
        <p class="empty-title">${state.challenges.length ? "没有匹配的题目" : "任务中没有题目"}</p>
        <p class="empty-text">${state.challenges.length ? "试试切换筛选条件。" : "请在平台为该令牌配置题目后刷新。"}</p>
      </div>`;
    return;
  }

  wrap.innerHTML = list.map((c) => {
    const pct = c.flag_count ? Math.round((c.correct_flag_count / c.flag_count) * 100) : 0;
    const live = c.container_status === "available";
    const pending = ["pending", "stop_pending"].includes(c.container_status);
    const addrStrip = c.container_addr && c.container_addr.length
      ? `<div class="addr-strip">
          <span class="live-dot" aria-hidden="true"></span>
          <span class="addr-label">LIVE</span>
          <div class="addr-list">${c.container_addr.map((a) =>
            `<span class="addr-item">${esc(a)}<button class="addr-copy" data-copy="${esc(a)}" aria-label="复制地址">⧉</button></span>`
          ).join("")}</div>
        </div>`
      : "";

    return `<article class="card ${live ? "is-live" : ""} ${c.is_completed ? "is-complete" : ""}">
      <header class="card-head">
        <h3 class="card-code">${esc(c.unique_code)}</h3>
        <div class="card-tags">
          <span class="chip chip-diff-${esc(c.difficulty)}">${DIFF_TEXT[c.difficulty] || esc(c.difficulty)}</span>
          <span class="chip chip-level">L${c.level}</span>
          <span class="chip chip-score">${c.total_score} 分</span>
          <span class="status ${esc(c.container_status)}"><span class="dot"></span>${STATUS_TEXT[c.container_status] || esc(c.container_status)}</span>
        </div>
      </header>
      ${c.description ? `<p class="card-desc">${esc(c.description)}</p>` : ""}
      <div class="progress">
        <div class="progress-track"><div class="progress-fill" style="width:${pct}%"></div></div>
        <p class="progress-label">${c.correct_flag_count} / ${c.flag_count} flag</p>
      </div>
      ${addrStrip}
      <div class="card-actions">
        ${cardActions(c)}
        ${pending ? `<span class="hint-note">实例正在变更状态…</span>` : ""}
      </div>
    </article>`;
  }).join("");
}

function renderScore() {
  let view = $("#scoreView");
  if (!view) {
    view = document.createElement("section");
    view.id = "scoreView";
    $("#dash").appendChild(view);
  }
  const byDiff = {};
  state.challenges.forEach((c) => { byDiff[c.difficulty] = byDiff[c.difficulty] || []; byDiff[c.difficulty].push(c); });
  const total = state.challenges.reduce((s, c) => s + c.total_score, 0);
  const earned = state.challenges.reduce((s, c) => s + (state.earned.get(c.unique_code) || 0), 0);
  const rows = Object.entries(byDiff).map(([d, list]) => {
    const done = list.filter((c) => c.is_completed).length;
    return `<div class="score-row">
      <span class="chip chip-diff-${esc(d)}">${DIFF_TEXT[d] || esc(d)}</span>
      <div class="score-bar"><div class="progress-track"><div class="progress-fill" style="width:${list.length ? (done / list.length) * 100 : 0}%"></div></div><span>${done} / ${list.length}</span></div>
      <span class="score-pts">${list.reduce((s, c) => s + c.total_score, 0)}</span>
    </div>`;
  }).join("");
  view.innerHTML = `<div class="score-card">
    <div class="score-hero">
      <p class="dash-eyebrow">CURRENT SCORE</p>
      <p class="score-total">${earned}<span class="score-total-of">/ ${total}</span></p>
      <p class="score-sub">已从 ${state.challenges.filter((c) => (state.earned.get(c.unique_code) || 0) > 0).length} 题获得得分</p>
    </div>
    <div class="score-break">${rows}</div>
  </div>`;
}

/* ---------- 操作 ---------- */

async function startChallenge(code) {
  await api(`/start?unique_code=${encodeURIComponent(code)}`, { method: "POST" });
  toast("实例已启动，正在就绪…", "info");
  await refresh(true);
}

async function closeChallenge(code) {
  await api(`/close?unique_code=${encodeURIComponent(code)}`, { method: "POST" });
  toast("实例已关闭，资源已释放", "info");
  await refresh(true);
}

async function fetchHint(code) {
  const r = await api(`/hint?unique_code=${encodeURIComponent(code)}`);
  $("#hintBody").textContent = r.hint || "该题暂无提示。";
  $("#hintModal").hidden = false;
}

async function submitFlag(code, flag) {
  const r = await api("/submit", { method: "POST", body: JSON.stringify({ unique_code: code, flag }) });
  state.earned.set(code, (state.earned.get(code) || 0) + (r.correct ? r.awarded : 0));
  const box = $("#submitResult");
  box.classList.remove("ok", "err");
  if (r.correct) {
    box.classList.add("ok");
    box.innerHTML = `<span>✓ 正确</span><span>本次 +<b>${r.awarded}</b> 分 · 该题累计 <b>${r.cumulative_score}</b> 分 · <b>${r.correct_flag_count}/${r.total_flag_count}</b> flag</span>`;
  } else {
    box.classList.add("err");
    box.innerHTML = `<span>✕ 错误</span><span>该 flag 不正确，请重试。</span>`;
  }
  box.hidden = false;
  await refresh(true);
  return r.correct;
}

/* ---------- 刷新与轮询 ---------- */

async function refresh(silent = false) {
  try {
    state.challenges = await api("");
    render();
  } catch (e) {
    if (e.status === 404 && e.code === "task_not_found") {
      toast("令牌失效，请重新连接", "error");
      disconnect();
    } else if (!silent) {
      toast(e.message, "error");
    }
  }
}

function startPollingIfPending() {
  const pending = state.challenges.some(isRunning);
  if (pending && !state.pollTimer) {
    state.pollTimer = setInterval(() => refresh(true), 2500);
  } else if (!pending && state.pollTimer) {
    stopPolling();
  }
}

function stopPolling() {
  if (state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; }
}

/* ---------- 事件绑定 ---------- */

$("#gateForm").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const errEl = $("#gateErr");
  errEl.hidden = true;
  const btn = ev.target.querySelector("button");
  btn.disabled = true;
  try {
    await connect($("#tokenInput").value);
  } catch (e) {
    errEl.hidden = false;
    errEl.textContent = e.message;
  } finally {
    btn.disabled = false;
  }
});

$("#disconnectBtn").addEventListener("click", disconnect);
$("#refreshBtn").addEventListener("click", () => refresh(false));

$("#filterSeg").addEventListener("click", (ev) => {
  const btn = ev.target.closest(".seg-btn");
  if (!btn) return;
  state.filter = btn.dataset.filter;
  document.querySelectorAll(".seg-btn").forEach((b) => b.classList.toggle("is-active", b === btn));
  renderCards();
});

document.querySelectorAll(".nav-item").forEach((n) =>
  n.addEventListener("click", (ev) => { ev.preventDefault(); setView(n.dataset.nav); })
);

$("#cards").addEventListener("click", async (ev) => {
  const copyBtn = ev.target.closest("[data-copy]");
  if (copyBtn) { copyText(copyBtn.dataset.copy, copyBtn); return; }
  const btn = ev.target.closest("[data-act]");
  if (!btn) return;
  const code = btn.dataset.code;
  const act = btn.dataset.act;
  try {
    if (act === "start") await startChallenge(code);
    else if (act === "close") await closeChallenge(code);
    else if (act === "hint") await fetchHint(code);
    else if (act === "submit") { $("#modalCode").textContent = code; $("#flagInput").value = ""; $("#submitResult").hidden = true; $("#modal").hidden = false; }
  } catch (e) {
    toast(e.message, "error");
  }
});

$("#submitForm").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const flag = $("#flagInput").value.trim();
  if (!flag) { toast("请输入 flag", "warn"); return; }
  const code = $("#modalCode").textContent;
  const btn = ev.target.querySelector("button[type=submit]");
  btn.disabled = true;
  try {
    await submitFlag(code, flag);
  } catch (e) {
    const box = $("#submitResult");
    box.classList.remove("ok"); box.classList.add("err");
    box.hidden = false;
    box.innerHTML = `<span>✕ ${esc(e.message)}</span>`;
  } finally {
    btn.disabled = false;
  }
});

$("#modalClose").addEventListener("click", () => { $("#modal").hidden = true; });
$("#modalCancel").addEventListener("click", () => { $("#modal").hidden = true; });
$("#modal").addEventListener("click", (ev) => { if (ev.target === $("#modal")) $("#modal").hidden = true; });

$("#hintClose").addEventListener("click", () => { $("#hintModal").hidden = true; });
$("#hintModal").addEventListener("click", (ev) => { if (ev.target === $("#hintModal")) $("#hintModal").hidden = true; });

document.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape") { $("#modal").hidden = true; $("#hintModal").hidden = true; }
});

/* ---------- 启动 ---------- */

if (state.token) {
  $("#tokenInput").value = state.token;
  connect(state.token).catch(() => disconnect());
} else {
  $("#gate").hidden = false;
  $("#tokenInput").focus();
}
