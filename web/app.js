/* Collaborative Memory Agent V2 — read-only investigation workbench.
 *
 * The page is intentionally framework-free.  Every value shown below comes
 * from the FastAPI runtime; no fixture or demo answer is embedded here.
 * Untrusted text is always written with textContent/DOM nodes, never innerHTML.
 */
(() => {
  "use strict";

  const STORAGE_KEY = "cma.demo.workbench.v1";
  const TERMINAL = new Set([
    "succeeded", "insufficient", "conflict", "failed",
    "timed_out", "cancelled", "interrupted",
  ]);
  const ACTIVE = new Set(["queued", "running"]);
  const EVIDENCE_ID = /^ev_[0-9a-f]{64}$/;
  const HASH = /^[0-9a-f]{64}$/;
  const MAX_ANSWER_CHARS = 6000;
  const MAX_EVIDENCE_SNIPPET = 1400;
  const MAX_EVENT_PAYLOAD = 4000;
  const MAX_HISTORY_RUNS = 50;
  const STREAM_RETRY_LIMIT = 5;
  const POLL_INTERVAL_MS = 1400;

  const STATUS_META = {
    queued: { label: "排队中", tone: "pending", desc: "Run 已提交，等待运行时工作线程。" },
    running: { label: "调查中", tone: "running", desc: "Agent 正在执行 ReAct 调查与证据校验。" },
    succeeded: { label: "已完成", tone: "ok", desc: "后端报告证据充分（sufficient）。本页仅展示后端结果，未独立复核。" },
    insufficient: { label: "证据不足", tone: "warn", desc: "后端未形成可验证结论。这不是成功找到答案。" },
    conflict: { label: "存在冲突", tone: "warn", desc: "后端保留了未解决冲突，不能当作单一结论。" },
    failed: { label: "失败", tone: "error", desc: "运行失败，未产生可用的调查结论。" },
    timed_out: { label: "已超时", tone: "error", desc: "运行在协作式边界上超时终止。" },
    cancelled: { label: "已取消", tone: "warn", desc: "运行已按请求取消，不是成功结果。" },
    interrupted: { label: "已中断", tone: "error", desc: "服务重启或中断后，未完成的 Run 被标记为 interrupted。" },
  };

  const TERMINATION_META = {
    sufficient: "证据充分（sufficient）",
    insufficient: "证据不足（insufficient）",
    conflict: "未解决冲突（conflict）",
    tool_failure: "工具失败（tool_failure）",
    budget: "预算或时限耗尽（budget）",
    model_error: "模型调用失败（model_error）",
    timeout: "超时（timeout）",
    cancelled: "已取消（cancelled）",
    system_error: "系统错误（system_error）",
  };

  const SENSITIVE_PAYLOAD_KEYS = new Set([
    "content", "prompt", "prompts", "messages", "response", "raw",
    "thinking", "reasoning", "answer", "claims_text",
  ]);

  const state = {
    projectId: "",
    datasetKind: "live",
    session: null,
    sessionId: "",
    runs: [],
    run: null,
    runId: "",
    runLoading: false,
    runError: "",
    result: null,
    resultLoaded: false,
    resultLoading: false,
    resultError: "",
    events: new Map(),
    lastEventSeq: 0,
    evidenceRefs: [],
    evidenceDetails: new Map(),
    evidenceErrors: new Map(),
    evidenceError: "",
    toolCalls: [],
    toolError: "",
    contextTrace: [],
    contextError: "",
    traceError: "",
    pending: false,
    streamAbort: null,
    streamRetryTimer: null,
    streamRetryAttempt: 0,
    streamEnded: false,
    pollTimer: null,
    pollReason: "",
    finishingRunId: "",
    loadToken: 0,
    activeTab: "evidence",
  };

  const $ = (id) => document.getElementById(id);
  const els = {};

  function cacheElements() {
    [
      "connection-status", "project-id", "dataset-kind", "create-session-btn",
      "scope-hint", "session-id", "load-session-btn", "close-session-btn",
      "session-status", "session-summary", "refresh-session-btn", "run-history",
      "conversation-subtitle", "run-status-badge", "run-id-label", "notice",
      "conversation", "run-form", "question", "submit-run-btn", "cancel-run-btn",
      "composer-hint", "composer-count", "tab-evidence", "tab-tools", "tab-trace",
      "evidence-list", "tool-call-list", "trace-summary", "trace-events",
      "context-trace", "evidence-count", "tool-count", "trace-count",
      "footer-state",
    ].forEach((id) => { els[id] = $(id); });
  }

  // ------------------------------------------------------------------
  // Safe DOM helpers
  // ------------------------------------------------------------------
  function node(tag, className, textValue) {
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (textValue !== undefined && textValue !== null) {
      item.textContent = String(textValue);
    }
    return item;
  }

  function addText(parent, tag, value, className) {
    const item = node(tag, className);
    item.textContent = value === undefined || value === null ? "—" : String(value);
    parent.append(item);
    return item;
  }

  function clear(parent) {
    if (parent) parent.replaceChildren();
  }

  function textValue(value) {
    if (value === undefined || value === null) return "—";
    if (typeof value === "string") return value;
    if (typeof value === "number" || typeof value === "boolean") return String(value);
    try { return JSON.stringify(value, null, 2); } catch (_) { return String(value); }
  }

  function boundedText(parent, value, options = {}) {
    const raw = value === undefined || value === null ? "" : String(value);
    const limit = options.limit || MAX_ANSWER_CHARS;
    const className = options.className || "pre-wrap";
    if (!raw) {
      parent.append(node("div", className, options.emptyText || "—"));
      return;
    }
    const visible = raw.length > limit ? raw.slice(0, limit) : raw;
    const visibleNode = node(options.tag || "div", className, visible);
    parent.append(visibleNode);
    if (raw.length > limit) {
      visibleNode.append(node("span", "hint", " …[内容已折叠]"));
      const details = node("details", "details");
      details.append(
        node("summary", null, `${options.expandLabel || "展开完整内容"}（共 ${raw.length.toLocaleString()} 字符）`),
        node("pre", null, raw),
      );
      parent.append(details);
    }
  }

  function addKv(parent, key, value, options = {}) {
    const dt = node("dt", null, key);
    const dd = node("dd");
    if (value === undefined || value === null || value === "") {
      dd.textContent = "—";
    } else if (typeof value === "object") {
      boundedText(dd, textValue(value), {
        limit: options.limit || 900,
        className: "mono",
        expandLabel: "展开字段",
      });
    } else {
      dd.textContent = String(value);
    }
    parent.append(dt, dd);
  }

  function addDetails(parent, summary, value, limit = MAX_EVENT_PAYLOAD) {
    if (value === undefined || value === null) return;
    const details = node("details", "details");
    details.append(
      node("summary", null, summary),
      node("pre", null, String(value).slice(0, limit)),
    );
    parent.append(details);
  }

  function formatTime(value) {
    if (!value) return "—";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    try {
      return date.toLocaleString("zh-CN", { hour12: false });
    } catch (_) {
      return date.toISOString();
    }
  }

  function formatDurationMs(value) {
    const ms = Number(value);
    if (!Number.isFinite(ms)) return "—";
    if (ms < 1000) return `${ms.toFixed(0)} ms`;
    return `${(ms / 1000).toFixed(1)} s`;
  }

  function runElapsed(run) {
    if (!run) return "—";
    const start = run.started_at || run.queued_at || run.created_at;
    if (!start) return "—";
    const end = run.finished_at || (ACTIVE.has(run.status) ? new Date().toISOString() : null);
    if (!end) return "—";
    const ms = new Date(end).getTime() - new Date(start).getTime();
    return Number.isFinite(ms) && ms >= 0 ? formatDurationMs(ms) : "—";
  }

  function shortId(value, length = 14) {
    if (!value) return "—";
    const text = String(value);
    return text.length <= length ? text : `${text.slice(0, length)}…`;
  }

  function isActive(run) {
    return Boolean(run && ACTIVE.has(run.status));
  }

  function isTerminal(run) {
    return Boolean(run && TERMINAL.has(run.status));
  }

  function setPill(element, label, tone) {
    if (!element) return;
    element.textContent = label;
    element.className = `status-pill status-${tone || "neutral"}`;
  }

  function setConnection(label, tone) {
    setPill(els["connection-status"], label, tone);
    if (els["footer-state"]) els["footer-state"].textContent = label;
  }

  function setNotice(message, tone = "") {
    const notice = els.notice;
    if (!notice) return;
    if (!message) {
      notice.textContent = "";
      notice.className = "notice hidden";
      return;
    }
    notice.textContent = String(message);
    notice.className = `notice ${tone || ""}`.trim();
  }

  function clearNotice() {
    setNotice("");
  }

  // ------------------------------------------------------------------
  // API
  // ------------------------------------------------------------------
  class ApiError extends Error {
    constructor(status, code, message, body) {
      super(message || "API request failed");
      this.name = "ApiError";
      this.status = status;
      this.code = code || "";
      this.body = body;
    }
  }

  async function api(path, options = {}) {
    const headers = Object.assign({ Accept: "application/json" }, options.headers || {});
    let body;
    if (options.body !== undefined) {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(options.body);
    }
    const response = await fetch(path, {
      method: options.method || "GET",
      headers,
      body,
      cache: "no-store",
      signal: options.signal,
    });
    const contentType = response.headers.get("content-type") || "";
    let payload = null;
    if (contentType.includes("application/json")) {
      try { payload = await response.json(); } catch (_) { payload = null; }
    } else {
      try { payload = await response.text(); } catch (_) { payload = null; }
    }
    if (!response.ok) {
      let code = "";
      let message = "";
      if (payload && typeof payload === "object" && payload.detail !== undefined) {
        if (typeof payload.detail === "string") {
          message = payload.detail;
        } else if (Array.isArray(payload.detail)) {
          message = payload.detail.map((item) => {
            if (!item || typeof item !== "object") return String(item);
            const where = Array.isArray(item.loc) ? item.loc.join(".") : "";
            return `${where ? `${where}: ` : ""}${item.msg || "invalid value"}`;
          }).join("; ");
        } else if (typeof payload.detail === "object") {
          code = String(payload.detail.code || "");
          message = String(payload.detail.message || "");
        }
      } else if (typeof payload === "string") {
        message = payload;
      }
      throw new ApiError(response.status, code, message.slice(0, 700), payload);
    }
    return payload;
  }

  function describeError(error) {
    if (!(error instanceof ApiError)) {
      const message = error && error.message ? String(error.message) : "未知错误";
      return `网络或浏览器错误：${message.slice(0, 500)}`;
    }
    const byStatus = {
      404: "未找到请求的 Session、Run 或 Evidence（404）",
      409: "当前状态不允许该操作（409）",
      422: "请求参数或 Scope 未通过校验（422）",
      503: "服务暂不可用（503）",
      504: "请求超时（504）",
    };
    const byCode = {
      session_closed: "Session 已关闭，不能继续提交 Run。",
      session_busy: "该 Session 已有运行中的 Run。",
      scope_mismatch: "project_id / dataset_kind 与 Session 不一致。",
      idempotency_conflict: "Idempotency-Key 对应的请求不一致。",
      run_not_terminal: "Run 尚未结束，结果暂不可用。",
      evidence_not_found: "Evidence 不在当前 Scope，或指定 content_hash 不存在。",
      invalid_scope: "Evidence Scope 无效。",
      invalid_run_request: "Run 请求无效。",
      invalid_cursor: "SSE 游标无效。",
    };
    const head = byCode[error.code] || byStatus[error.status] || `API 错误（${error.status}）`;
    return error.message ? `${head}：${error.message}` : head;
  }

  function scopeQuery(projectId, datasetKind) {
    const params = new URLSearchParams();
    params.set("project_id", projectId);
    if (datasetKind) params.set("dataset_kind", datasetKind);
    return params.toString();
  }

  function runQuery(run) {
    return scopeQuery(run.project_id, null);
  }

  function runPath(runId) {
    return `/runs/${encodeURIComponent(runId)}`;
  }

  function runApiPath(runId, suffix, run) {
    return `${runPath(runId)}${suffix}?${runQuery(run)}`;
  }

  function streamPath(runId, afterSeq, run) {
    const params = new URLSearchParams({ project_id: run.project_id });
    params.set("after_seq", String(Math.max(0, Number(afterSeq) || 0)));
    return `${runPath(runId)}/stream?${params.toString()}`;
  }

  function evidencePath(ref, run) {
    const params = new URLSearchParams({
      project_id: run.project_id,
      dataset_kind: run.dataset_kind,
    });
    if (ref.content_hash) params.set("content_hash", ref.content_hash);
    return `/evidence/${encodeURIComponent(ref.evidence_id)}?${params.toString()}`;
  }

  // ------------------------------------------------------------------
  // Persistence (no credentials, no content)
  // ------------------------------------------------------------------
  function persistState() {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify({
        projectId: state.projectId,
        datasetKind: state.datasetKind,
        sessionId: state.sessionId,
        runId: state.runId,
      }));
    } catch (_) { /* storage may be unavailable in private mode */ }
  }

  function saveUrl() {
    const params = new URLSearchParams();
    if (state.projectId) params.set("project_id", state.projectId);
    if (state.datasetKind) params.set("dataset_kind", state.datasetKind);
    if (state.sessionId) params.set("session_id", state.sessionId);
    if (state.runId) params.set("run_id", state.runId);
    const truth = params.toString();
    try {
      history.replaceState(null, "", `${location.pathname}${truth ? `?${truth}` : ""}`);
    } catch (_) { /* history may be unavailable in embedded browsers */ }
    persistState();
  }

  function restoredState() {
    const params = new URLSearchParams(location.search);
    let saved = {};
    try { saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}") || {}; } catch (_) { saved = {}; }
    const projectId = params.get("project_id") || saved.projectId || "";
    const datasetKind = params.get("dataset_kind") || saved.datasetKind || "live";
    return {
      projectId: String(projectId || "").slice(0, 200),
      datasetKind: datasetKind === "fixture" ? "fixture" : "live",
      sessionId: String(params.get("session_id") || saved.sessionId || "").slice(0, 200),
      runId: String(params.get("run_id") || saved.runId || "").slice(0, 200),
    };
  }

  // ------------------------------------------------------------------
  // Session / Run controls
  // ------------------------------------------------------------------
  function currentProject() {
    return state.session?.project_id || state.projectId;
  }

  function updateControls() {
    const hasSession = Boolean(state.session);
    const sessionOpen = state.session?.status === "open";
    const activeRun = isActive(state.run);
    const question = els.question ? els.question.value.trim() : "";

    if (els["create-session-btn"]) els["create-session-btn"].disabled = state.pending;
    if (els["load-session-btn"]) els["load-session-btn"].disabled = state.pending;
    if (els["close-session-btn"]) {
      els["close-session-btn"].disabled = !hasSession || !sessionOpen || state.pending || activeRun;
    }
    if (els["submit-run-btn"]) {
      els["submit-run-btn"].disabled = !sessionOpen || state.pending || activeRun || !question;
    }
    if (els["cancel-run-btn"]) els["cancel-run-btn"].disabled = !activeRun || state.pending;
    if (els.question) els.question.disabled = hasSession && !sessionOpen;
    if (els["refresh-session-btn"]) els["refresh-session-btn"].disabled = !hasSession;

    let hint = "请先创建或加载 Session。";
    if (hasSession && !sessionOpen) hint = "Session 已关闭，不能继续提交 Run。";
    else if (activeRun) hint = "当前 Run 正在执行；SSE 断开不会取消后台 Run。";
    else if (hasSession) hint = `同一 Session 内可继续追问；已加载 ${state.runs.length} 个 Run。`;
    if (els["composer-hint"]) els["composer-hint"].textContent = hint;
    if (els["scope-hint"]) {
      els["scope-hint"].textContent = hasSession
        ? "提交使用当前 Session 绑定的 Scope；左侧 project/dataset 仅用于新建 Session。"
        : "创建 Session 后才能提交调查问题。";
    }
    updateCounter();
  }

  function updateCounter() {
    const length = els.question ? els.question.value.length : 0;
    if (els["composer-count"]) els["composer-count"].textContent = `${length} / 4000`;
  }

  function setPending(value) {
    state.pending = Boolean(value);
    updateControls();
  }

  function applySession(session) {
    state.session = session;
    state.sessionId = session.session_id;
    state.projectId = session.project_id;
    state.datasetKind = session.dataset_kind;
    if (els["session-id"]) els["session-id"].value = session.session_id;
    if (els["project-id"]) els["project-id"].value = session.project_id;
    if (els["dataset-kind"]) els["dataset-kind"].value = session.dataset_kind;
    renderSessionMeta();
    updateControls();
  }

  function renderSessionMeta() {
    const session = state.session;
    if (!session) {
      setPill(els["session-status"], "未加载", "neutral");
      const summary = els["session-summary"];
      if (summary) {
        clear(summary);
        [["项目", "—"], ["数据集", "—"], ["创建时间", "—"], ["Run 数", "—"]].forEach(([key, value]) => {
          const item = node("div");
          item.append(node("dt", null, key), node("dd", null, value));
          summary.append(item);
        });
      }
      return;
    }
    setPill(els["session-status"], session.status === "open" ? "进行中" : "已关闭",
      session.status === "open" ? "ok" : "neutral");
    const summary = els["session-summary"];
    if (!summary) return;
    clear(summary);
    const rows = [
      ["项目", session.project_id],
      ["数据集", session.dataset_kind],
      ["创建时间", formatTime(session.created_at)],
      ["Run 数", String(state.runs.length)],
    ];
    rows.forEach(([key, value]) => {
      const item = node("div");
      item.append(node("dt", null, key), node("dd", null, value));
      summary.append(item);
    });
  }

  function statusMeta(status) {
    return STATUS_META[status] || {
      label: status || "未知", tone: "neutral", desc: "后端返回了未识别的 Run 状态。",
    };
  }

  function renderRunBadge() {
    if (!state.run) {
      setPill(els["run-status-badge"], state.runError ? "加载失败" : "无 Run",
        state.runError ? "error" : "neutral");
      if (els["run-id-label"]) els["run-id-label"].textContent = "run_id: —";
      return;
    }
    const meta = statusMeta(state.run.status);
    setPill(els["run-status-badge"], meta.label, meta.tone);
    if (els["run-id-label"]) els["run-id-label"].textContent = `run_id: ${state.run.run_id}`;
  }

  function renderRunHistory() {
    const host = els["run-history"];
    if (!host) return;
    clear(host);
    if (!state.session) {
      host.append(node("p", "muted", "加载 Session 后显示 Run 历史。"));
      return;
    }
    if (!state.runs.length) {
      host.append(node("p", "muted", "当前 Session 尚无 Run。"));
      return;
    }
    state.runs.forEach((run) => {
      const meta = statusMeta(run.status);
      const button = node("button", `run-item${run.run_id === state.runId ? " active" : ""}`);
      button.type = "button";
      button.title = run.question || run.run_id;
      const top = node("div", "run-item-top");
      top.append(
        node("span", "run-item-status", meta.label),
        node("span", "run-item-meta", formatTime(run.created_at)),
      );
      button.append(top, node("p", "run-item-question", run.question || "（无问题文本）"));
      button.append(node("div", "run-item-meta",
        `${shortId(run.run_id, 22)} · ${isActive(run) ? "进行中" : (run.termination_reason || run.status)}`));
      button.addEventListener("click", () => {
        if (state.runId === run.run_id) return;
        selectRun(run.run_id);
      });
      host.append(button);
    });
  }

  // ------------------------------------------------------------------
  // Conversation rendering
  // ------------------------------------------------------------------
  function renderEmptyConversation(reason) {
    const host = els.conversation;
    if (!host) return;
    clear(host);
    const wrap = node("div", "empty-state");
    wrap.append(node("div", "empty-icon", "⌕"));
    if (state.session) {
      wrap.append(
        node("h3", null, state.session.status === "closed" ? "该 Session 已关闭" : "该 Session 尚无调查记录"),
        node("p", null, state.session.status === "closed"
          ? "可以恢复已有 Run 查看结果，但不能再提交新的问题。"
          : "在下方输入研发历史问题，提交第一个 Run。"),
      );
    } else {
      wrap.append(
        node("h3", null, "从一次可追溯的调查开始"),
        node("p", null, "创建 Session 后输入研发历史问题。提交后可实时查看 SSE 事件、最终答案、Evidence 引用、Tool Calls 与 Trace。"),
      );
      const list = node("ul", "checklist");
      ["回答与证据分离展示，未通过证据校验的内容不会标为“已验证结论”。",
        "insufficient 表示证据不足，不是成功找到答案。",
        "刷新页面不会取消后台 Run；可用 session_id / run_id 恢复。"].forEach((text) => {
        list.append(node("li", null, text));
      });
      wrap.append(list);
    }
    if (reason) wrap.append(node("p", "inline-error", reason));
    host.append(wrap);
  }

  function runRoundNumber(run) {
    if (!run) return "—";
    const ordered = [...state.runs].sort((a, b) => String(a.created_at).localeCompare(String(b.created_at)));
    const index = ordered.findIndex((item) => item.run_id === run.run_id);
    return index >= 0 ? String(index + 1) : "—";
  }

  function offlineSkip() {
    for (const event of state.events.values()) {
      const payload = event.payload || {};
      if (event.event_type === "model_skipped" && (payload.reason === "no_credential" || payload.mode === "offline_fallback")) {
        return payload;
      }
    }
    return null;
  }

  function buildAgentCard(run) {
    const meta = statusMeta(run.status);
    const card = node("div", `agent-card${meta.tone === "warn" ? " warn" : meta.tone === "error" ? " error" : meta.tone === "ok" ? " ok" : ""}`);
    const head = node("div", "agent-card-head");
    const pill = node("span", `status-pill status-${meta.tone}`);
    pill.textContent = meta.label;
    head.append(pill, node("span", "agent-card-title", "Agent 调查结果"));
    if (isActive(run)) head.append(node("span", "spinner"));
    card.append(head, node("p", "agent-sub", meta.desc));

    if (state.runError) {
      card.append(node("div", "inline-error", state.runError));
      return card;
    }

    if (isActive(run)) {
      const loading = node("div", "agent-answer");
      loading.append(node("span", null, "Agent 正在执行调查。"));
      loading.append(node("div", "meta-row",
        `state: ${run.status} · elapsed: ${runElapsed(run)} · 已接收 ${state.events.size} 个事件`));
      card.append(loading);
    } else if (state.resultLoading) {
      card.append(node("div", "agent-answer", "正在读取后端最终结果…"));
    } else if (state.resultError) {
      card.append(node("div", "inline-error", state.resultError));
    } else if (state.result && state.result.result_available) {
      const answer = state.result.answer || "";
      if (run.status === "insufficient") {
        card.append(node("p", "agent-sub", "以下内容是后端返回的说明，不是已验证结论，也不表示已找到答案："));
      } else if (run.status === "succeeded") {
        card.append(node("p", "agent-sub", "后端报告证据充分；本页没有独立复核该结论。"));
      }
      if (answer) {
        boundedText(card, answer, {
          limit: MAX_ANSWER_CHARS,
          className: "agent-answer",
          expandLabel: "展开完整回答",
          emptyText: "（后端返回空回答）",
        });
      } else {
        card.append(node("div", "agent-answer", "（后端返回空回答）"));
      }
      const resultPayload = state.result.result || {};
      const gaps = Array.isArray(resultPayload.gaps) ? resultPayload.gaps : [];
      const conflicts = Array.isArray(resultPayload.conflicts) ? resultPayload.conflicts : [];
      const claims = Array.isArray(resultPayload.claims) ? resultPayload.claims : [];
      if (gaps.length) {
        const gapBox = node("div", "agent-sub");
        gapBox.append(node("strong", null, `后端记录的未解决缺口（${gaps.length}）：`));
        gaps.slice(0, 8).forEach((gap) => gapBox.append(node("div", null, textValue(gap))));
        card.append(gapBox);
      }
      if (conflicts.length) {
        card.append(node("div", "inline-warning",
          `后端记录了 ${conflicts.length} 条未解决冲突；不能把本结果当作单一确定结论。`));
      }
      if (claims.length) {
        addDetails(card, `查看后端返回的 claim 字段（${claims.length} 条，未在本页二次核验）`,
          textValue(claims), 5000);
      }
    } else if (state.result && state.result.result_available === false) {
      card.append(node("div", "inline-warning", "后端没有为该 Run 保存可用结果；不能展示或推断 Agent 回答。"));
    } else if (isTerminal(run)) {
      card.append(node("div", "agent-answer", "后端未返回可展示的最终结果。"));
    }

    if (run.error_message) {
      const errorBox = node("div", "inline-error", `error_message: ${String(run.error_message).slice(0, 900)}`);
      card.append(errorBox);
    }

    if (isTerminal(run)) {
      const term = TERMINATION_META[run.termination_reason] || run.termination_reason || "—";
      const metaRow = node("div", "meta-row");
      metaRow.append(
        node("span", null, `termination: ${term}`),
        node("span", null, `termination_detail: ${textValue(run.termination_detail)}`),
        node("span", null, `tool_calls: ${run.tool_call_count ?? "—"}`),
        node("span", null, `events: ${state.events.size}`),
        node("span", null, `elapsed: ${runElapsed(run)}`),
      );
      card.append(metaRow);
    }

    const skip = offlineSkip();
    if (skip) {
      card.append(node("div", "inline-warning",
        "离线回退：缺少模型凭据（no_credential），本次未调用模型。该结果只验证调查链，不代表真实模型检索或回答质量。"));
    }
    if (run.status === "insufficient") {
      card.append(node("div", "inline-warning",
        "insufficient ≠ 成功找到答案。请结合 Evidence、缺口与冲突判断证据是否足够。"));
    }
    return card;
  }

  function renderCurrentRun() {
    const host = els.conversation;
    if (!host) return;
    if (!state.run && !state.runError) {
      renderEmptyConversation();
      return;
    }
    clear(host);
    if (state.runError || !state.run) {
      renderEmptyConversation(state.runError || "Run 数据不可用。");
      return;
    }
    const run = state.run;
    const turn = node("div", "turn");
    turn.append(node("div", "turn-label",
      `用户 · 第 ${runRoundNumber(run)} 轮 · ${formatTime(run.created_at)}`));
    const question = node("div", "turn-question");
    question.textContent = run.question || "（无问题文本）";
    turn.append(question);

    const answerWrap = node("div", "turn");
    answerWrap.append(node("div", "turn-label", "Agent · 后端结果"));
    answerWrap.append(buildAgentCard(run));
    turn.append(answerWrap);
    host.append(turn);
    host.scrollTop = host.scrollHeight;
  }

  // ------------------------------------------------------------------
  // Trace / Tool Calls / Evidence rendering
  // ------------------------------------------------------------------
  function payloadForDisplay(payload, depth = 0) {
    if (depth > 5) return "[depth-limited]";
    if (Array.isArray(payload)) return payload.slice(0, 100).map((item) => payloadForDisplay(item, depth + 1));
    if (payload && typeof payload === "object") {
      const result = {};
      Object.entries(payload).forEach(([key, value]) => {
        if (SENSITIVE_PAYLOAD_KEYS.has(String(key).toLowerCase())) return;
        result[key] = payloadForDisplay(value, depth + 1);
      });
      return result;
    }
    if (typeof payload === "string") {
      return payload.length > 2000 ? `${payload.slice(0, 2000)}…[truncated]` : payload;
    }
    return payload;
  }

  function eventSummary(event) {
    const payload = event.payload || {};
    const parts = [];
    const fields = [
      ["status", payload.status],
      ["reason", payload.reason],
      ["mode", payload.mode],
      ["tool", payload.tool || payload.tool_name],
      ["elapsed_ms", payload.elapsed_ms],
      ["attempt", payload.attempt],
      ["accepted", payload.accepted],
      ["conflicts", payload.conflicts],
      ["stop_detail", payload.stop_detail],
      ["selected", payload.selected_evidence_count],
      ["candidates", payload.candidate_evidence_count],
      ["tokens", payload.estimated_input_tokens],
      ["limit", payload.limit_reason],
      ["fallback", payload.fallback_reason],
    ];
    fields.forEach(([key, value]) => {
      if (value !== undefined && value !== null && value !== "") {
        parts.push(`${key}=${typeof value === "object" ? textValue(value) : value}`);
      }
    });
    return parts.join(" · ");
  }

  function renderTraceSummary() {
    const host = els["trace-summary"];
    if (!host) return;
    clear(host);
    if (!state.run) {
      host.textContent = "暂无 Trace。";
      return;
    }
    const run = state.run;
    const dl = node("dl", "kv");
    addKv(dl, "Run", run.run_id);
    addKv(dl, "状态", `${run.status} / ${statusMeta(run.status).label}`);
    addKv(dl, "终止原因", TERMINATION_META[run.termination_reason] || run.termination_reason || "—");
    addKv(dl, "终止详情", run.termination_detail);
    addKv(dl, "检索模式", run.retrieval_mode);
    addKv(dl, "回退原因", run.fallback_reason);
    addKv(dl, "索引版本", run.index_version);
    addKv(dl, "来源版本", run.source_revision);
    addKv(dl, "模型", run.model_name);
    addKv(dl, "Tool Calls", run.tool_call_count);
    addKv(dl, "事件数", state.events.size);
    addKv(dl, "最后 event_seq", state.lastEventSeq);
    addKv(dl, "SSE", state.streamEnded ? "已收到 stream_end" : (state.pollTimer ? "轮询回退" : "连接中/未连接"));
    addKv(dl, "耗时", runElapsed(run));
    addKv(dl, "创建", formatTime(run.created_at));
    addKv(dl, "开始", formatTime(run.started_at));
    addKv(dl, "结束", formatTime(run.finished_at));
    if (run.error_code) addKv(dl, "错误码", run.error_code);
    host.append(dl);
    if (state.runError) host.append(node("div", "inline-error", state.runError));
    if (state.traceError) host.append(node("div", "inline-error", state.traceError));
  }

  function renderTraceEvents() {
    const host = els["trace-events"];
    if (!host) return;
    clear(host);
    const events = [...state.events.values()].sort((a, b) => Number(a.event_seq) - Number(b.event_seq));
    if (!events.length) {
      host.append(node("li", "muted", state.run ? "尚未收到事件。" : "暂无事件。"));
      return;
    }
    events.slice(0, 400).forEach((event) => {
      const type = String(event.event_type || "unknown");
      const item = node("li", type.includes("failed") || type.includes("error") ? "event-error" :
        (type === "run_finished" || type === "stream_end") ? "event-ok" : "event-running");
      const top = node("div", "event-top");
      top.append(
        node("span", "event-type", type),
        node("span", "event-seq", `#${event.event_seq}`),
        node("span", "event-time", formatTime(event.timestamp)),
      );
      item.append(top);
      const summary = eventSummary(event);
      if (summary) item.append(node("div", "event-detail", summary));
      if (event.payload && typeof event.payload === "object") {
        addDetails(item, "查看事件 payload", textValue(payloadForDisplay(event.payload)), 3000);
      }
      host.append(item);
    });
    if (events.length > 400) {
      host.append(node("li", "muted", `仅显示前 400 条事件；后端共返回 ${events.length} 条。`));
    }
  }

  function renderContextTrace() {
    const host = els["context-trace"];
    if (!host) return;
    clear(host);
    if (state.contextError) {
      host.append(node("p", "inline-error", state.contextError));
      return;
    }
    if (!state.contextTrace.length) {
      host.append(node("p", "muted", state.run ? "后端未返回 Context Trace。" : "暂无 Context Trace。"));
      return;
    }
    state.contextTrace.forEach((event) => {
      const card = node("div", "card");
      const head = node("div", "card-head");
      head.append(
        node("p", "card-title", `${event.event_type || "context"} · #${event.event_seq ?? "—"}`),
        node("span", "badge info", "Context"),
      );
      card.append(head);
      card.append(node("div", "card-sub", formatTime(event.timestamp)));
      const payload = event.payload || {};
      const keys = [
        "round", "candidate_evidence_count", "selected_evidence_count",
        "deduped_evidence_count", "pruned_count", "important_conflicts_retained",
        "estimated_input_tokens", "input_bytes", "max_input_tokens",
        "max_context_bytes", "limit_reason", "tokenizer_mode",
      ];
      const dl = node("dl", "kv");
      let hasFields = false;
      keys.forEach((key) => {
        if (payload[key] !== undefined) {
          hasFields = true;
          addKv(dl, key, payload[key]);
        }
      });
      if (hasFields) card.append(dl);
      addDetails(card, "查看完整 Context payload", textValue(payloadForDisplay(payload)), 3000);
      host.append(card);
    });
  }

  function toolStatusTone(status) {
    if (status === "completed") return "ok";
    if (status === "failed") return "error";
    if (status === "timeout" || status === "cancelled") return "warn";
    return "info";
  }

  function deepFind(value, names, depth = 0) {
    if (!value || typeof value !== "object" || depth > 5) return undefined;
    for (const name of names) {
      if (Object.prototype.hasOwnProperty.call(value, name) &&
          value[name] !== undefined && value[name] !== null && value[name] !== "") {
        return value[name];
      }
    }
    for (const child of Object.values(value)) {
      const found = deepFind(child, names, depth + 1);
      if (found !== undefined) return found;
    }
    return undefined;
  }

  function renderToolCalls() {
    const host = els["tool-call-list"];
    if (!host) return;
    clear(host);
    if (state.toolError) {
      host.append(node("p", "inline-error", state.toolError));
      return;
    }
    if (!state.toolCalls.length) {
      host.append(node("p", "muted", state.run
        ? (isActive(state.run) ? "Run 执行中，尚未返回 Tool Call。" : "后端未返回 Tool Call。")
        : "暂无 Tool Call。"));
      return;
    }
    state.toolCalls.forEach((call) => {
      const card = node("div", "card");
      const head = node("div", "card-head");
      head.append(
        node("p", "card-title", `#${call.ordinal ?? "—"} ${call.tool_name || "unknown tool"}`),
        node("span", `badge ${toolStatusTone(call.status)}`, call.status || "unknown"),
      );
      card.append(head);
      const meta = node("div", "card-sub",
        `attempt ${call.attempt ?? "—"} · elapsed ${formatDurationMs(call.elapsed_ms)} · ${formatTime(call.started_at)}`);
      card.append(meta);
      const mode = state.run?.retrieval_mode || deepFind(call.result_summary, ["retrieval_mode", "effective_mode", "mode"]);
      const fallback = state.run?.fallback_reason || deepFind(call.result_summary, ["fallback_reason", "fallback"]);
      if (mode || fallback) {
        const dl = node("dl", "kv");
        addKv(dl, "检索模式", mode);
        addKv(dl, "回退原因", fallback);
        card.append(dl);
      }
      const query = call.query || "—";
      card.append(node("div", "event-detail", `query: ${String(query).slice(0, 600)}`));
      if (call.filters && Object.keys(call.filters).length) {
        addDetails(card, "查看 filters", textValue(payloadForDisplay(call.filters)), 1500);
      }
      if (call.error_code || call.error_message) {
        card.append(node("div", "inline-error",
          `${call.error_code || "tool_error"}: ${String(call.error_message || "").slice(0, 700)}`));
      }
      if (call.retryable !== null && call.retryable !== undefined) {
        card.append(node("div", "card-sub", `retryable: ${call.retryable}`));
      }
      if (call.result_summary && Object.keys(call.result_summary).length) {
        addDetails(card, "查看 result_summary", textValue(payloadForDisplay(call.result_summary)), 3000);
      }
      host.append(card);
    });
  }

  function verificationMeta(detail) {
    if (!detail) return { label: "详情未加载", tone: "info" };
    if (detail.verification === "current_active") return { label: "当前有效版本", tone: "ok" };
    if (detail.verification === "historical") return { label: "历史版本", tone: "warn" };
    if (detail.verification === "current_inactive") {
      return { label: `当前状态：${detail.current_state?.state || "inactive"}`, tone: "warn" };
    }
    if (detail.verification === "projection_unavailable") return { label: "无当前状态投影", tone: "info" };
    return { label: detail.verification || "未知状态", tone: "info" };
  }

  function renderEvidence() {
    const host = els["evidence-list"];
    if (!host) return;
    clear(host);
    if (state.evidenceError) {
      host.append(node("p", "inline-error", state.evidenceError));
      return;
    }
    if (!state.run) {
      host.append(node("p", "muted", "提交并完成 Run 后显示 Evidence 引用。"));
      return;
    }
    if (state.run.status === "insufficient") {
      host.append(node("p", "inline-warning",
        "本 Run 终止为 insufficient：以下 Evidence 引用只是调查记录，不构成完整答案或已验证结论。"));
    }
    if (!state.evidenceRefs.length) {
      host.append(node("p", "muted", isActive(state.run)
        ? "Run 执行中，尚未返回 Evidence 引用。"
        : "后端未返回 Evidence 引用。"));
      return;
    }
    state.evidenceRefs.forEach((ref, index) => {
      const key = `${ref.evidence_id}|${ref.content_hash}`;
      const detail = state.evidenceDetails.get(key);
      const detailError = state.evidenceErrors.get(key);
      const verify = verificationMeta(detail);
      const card = node("div", "card");
      const head = node("div", "card-head");
      head.append(
        node("p", "card-title", `Evidence ${index + 1}`),
        node("span", `badge ${verify.tone}`, verify.label),
      );
      card.append(head);
      card.append(node("div", "card-sub",
        `${ref.source_type || "unknown"} · observed ${formatTime(ref.observed_at)} · tool ${ref.tool_name || "—"}`));
      const dl = node("dl", "kv");
      addKv(dl, "evidence_id", ref.evidence_id);
      addKv(dl, "content_hash", ref.content_hash);
      addKv(dl, "引用", ref.evidence_id && ref.content_hash
        ? `[${ref.evidence_id}@${ref.content_hash}]` : "—");
      addKv(dl, "source_type", detail?.source_type || ref.source_type);
      addKv(dl, "source_uri", detail?.source_uri ?? ref.metadata?.source_uri);
      addKv(dl, "original_id", detail?.original_record_id ?? ref.metadata?.original_record_id);
      if (detail) {
        addKv(dl, "当前 hash", detail.current_content_hash);
        addKv(dl, "状态", detail.current_state?.state || detail.verification);
        addKv(dl, "revision", detail.current_state?.revision);
        addKv(dl, "event_time", formatTime(detail.event_time));
        addKv(dl, "ingested_at", formatTime(detail.ingested_at));
      }
      card.append(dl);
      if (detail) {
        if (detail.content_hash_matches_current === false) {
          card.append(node("div", "inline-warning",
            `该引用的 hash 不是当前版本；当前 hash 为 ${detail.current_content_hash || "—"}。页面不会用新版本替换本次 Run 实际看到的版本。`));
        }
        if (detail.content_truncated) {
          card.append(node("div", "inline-warning",
            `后端只返回前 ${detail.content_limit?.toLocaleString?.() || detail.content_limit || "—"} 字符（原文 ${detail.content_chars?.toLocaleString?.() || detail.content_chars || "—"} 字符）。`));
        }
        const snippet = node("div", "evidence-snippet");
        boundedText(snippet, detail.content || "", {
          limit: MAX_EVIDENCE_SNIPPET,
          className: "evidence-snippet-text",
          expandLabel: "展开 Evidence 内容",
          tag: "div",
        });
        card.append(snippet);
        if (detail.locator && Object.keys(detail.locator).length) {
          addDetails(card, "查看 locator", textValue(detail.locator), 2000);
        }
        if (detail.metadata && Object.keys(detail.metadata).length) {
          addDetails(card, "查看 metadata", textValue(detail.metadata), 2500);
        }
      } else if (detailError) {
        card.append(node("div", "inline-error", `Evidence 详情不可用：${detailError}`));
        card.append(node("div", "agent-sub",
          "仍显示 Run 实际记录的引用与 hash；不会用其他版本或跨 Scope 内容补全。"));
      } else {
        card.append(node("p", "muted", "正在读取只读 Evidence 详情…"));
      }
      host.append(card);
    });
  }

  function renderInspector() {
    if (els["evidence-count"]) els["evidence-count"].textContent = String(state.evidenceRefs.length);
    if (els["tool-count"]) els["tool-count"].textContent = String(state.toolCalls.length);
    if (els["trace-count"]) els["trace-count"].textContent = String(state.events.size);
    renderEvidence();
    renderToolCalls();
    renderTraceSummary();
    renderTraceEvents();
    renderContextTrace();
  }

  function switchTab(tabName) {
    state.activeTab = tabName;
    document.querySelectorAll("[data-tab]").forEach((button) => {
      const active = button.dataset.tab === tabName;
      button.classList.toggle("active", active);
      button.setAttribute("aria-selected", active ? "true" : "false");
    });
    [["evidence", "tab-evidence"], ["tools", "tab-tools"], ["trace", "tab-trace"]].forEach(([name, id]) => {
      const panel = $(id);
      if (!panel) return;
      panel.hidden = name !== tabName;
      panel.classList.toggle("active", name === tabName);
    });
  }

  // ------------------------------------------------------------------
  // SSE stream (fetch streaming, named events, after_seq resume)
  // ------------------------------------------------------------------
  function stopPolling() {
    if (state.pollTimer) {
      clearTimeout(state.pollTimer);
      state.pollTimer = null;
    }
  }

  function stopTransports() {
    if (state.streamAbort) {
      try { state.streamAbort.abort(); } catch (_) { /* already closed */ }
      state.streamAbort = null;
    }
    if (state.streamRetryTimer) {
      clearTimeout(state.streamRetryTimer);
      state.streamRetryTimer = null;
    }
    stopPolling();
  }

  function parseSseBlock(block) {
    let event = "message";
    let id = null;
    const dataLines = [];
    for (const line of block.split(/\r?\n/)) {
      if (line.startsWith(":")) continue;
      if (line.startsWith("id:")) id = line.slice(3).trim();
      else if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) dataLines.push(line.slice(5).replace(/^ /, ""));
    }
    if (!dataLines.length) return null;
    const raw = dataLines.join("\n");
    let data = raw;
    try { data = JSON.parse(raw); } catch (_) { /* keep raw for diagnostics */ }
    return { event, id, data };
  }

  function mergeEvent(event) {
    if (!event || typeof event !== "object") return false;
    const seq = Number(event.event_seq);
    if (!Number.isFinite(seq) || seq <= 0) return false;
    if (!state.events.has(seq)) state.events.set(seq, event);
    if (seq > state.lastEventSeq) state.lastEventSeq = seq;
    return true;
  }

  function handleStreamEvent(runId, parsed) {
    if (state.runId !== runId) return;
    const event = parsed.data;
    if (!event || typeof event !== "object") return;
    mergeEvent(event);
    if (event.event_type === "run_finished" && event.payload && event.payload.status && state.run) {
      state.run.status = String(event.payload.status);
      if (event.payload.termination_reason) state.run.termination_reason = event.payload.termination_reason;
    }
    renderInspector();
    renderCurrentRun();
    updateControls();
  }

  async function connectStream(runId, afterSeq, attempt) {
    if (state.runId !== runId || !state.run) return;
    const controller = new AbortController();
    state.streamAbort = controller;
    setConnection(attempt ? "SSE 重连中" : "SSE 连接中", attempt ? "warn" : "running");
    let receivedEnd = false;
    try {
      const response = await fetch(streamPath(runId, afterSeq, state.run), {
        method: "GET",
        headers: { Accept: "text/event-stream" },
        cache: "no-store",
        signal: controller.signal,
      });
      if (!response.ok) {
        let details = null;
        try { details = await response.json(); } catch (_) { details = null; }
        const detail = details && typeof details === "object" ? details.detail : null;
        const code = detail && typeof detail === "object" ? String(detail.code || "") : "";
        const message = typeof detail === "string" ? detail
          : (detail && typeof detail === "object" ? String(detail.message || "") : "");
        throw new ApiError(response.status, code, message, details);
      }
      if (!response.body) throw new Error("SSE 响应没有可读流");
      setConnection("SSE 已连接", "ok");
      const reader = response.body.getReader();
      const decoder = new TextDecoder("utf-8");
      let buffer = "";
      while (true) {
        const chunk = await reader.read();
        if (chunk.done) break;
        buffer += decoder.decode(chunk.value, { stream: true });
        let boundary = buffer.indexOf("\n\n");
        while (boundary !== -1) {
          const block = buffer.slice(0, boundary);
          buffer = buffer.slice(boundary + 2);
          const parsed = parseSseBlock(block);
          if (parsed) {
            if (parsed.event === "stream_end") {
              receivedEnd = true;
              if (parsed.data && typeof parsed.data === "object") {
                const endSeq = Number(parsed.data.last_event_seq);
                if (Number.isFinite(endSeq) && endSeq > state.lastEventSeq) state.lastEventSeq = endSeq;
              }
              finishRun(runId, "sse");
            } else {
              handleStreamEvent(runId, parsed);
            }
          }
          boundary = buffer.indexOf("\n\n");
        }
      }
      buffer += decoder.decode();
      if (buffer.trim()) {
        const parsed = parseSseBlock(buffer);
        if (parsed) {
          if (parsed.event === "stream_end") {
            receivedEnd = true;
            finishRun(runId, "sse");
          } else {
            handleStreamEvent(runId, parsed);
          }
        }
      }
      if (!receivedEnd) throw new Error("SSE 连接关闭但未收到 stream_end");
    } catch (error) {
      if (error && error.name === "AbortError") return;
      if (state.runId !== runId) return;
      scheduleReconnect(runId, attempt, error);
    } finally {
      if (state.streamAbort === controller) state.streamAbort = null;
    }
  }

  function scheduleReconnect(runId, attempt, error) {
    if (state.runId !== runId || state.streamEnded) return;
    if (error instanceof ApiError && [400, 404, 409, 422].includes(error.status)) {
      setConnection("SSE 不可用", "error");
      setNotice(describeError(error), "error");
      state.traceError = describeError(error);
      renderInspector();
      return;
    }
    if (attempt >= STREAM_RETRY_LIMIT) {
      state.pollReason = describeError(error);
      setConnection("SSE 重连失败，改用轮询", "warn");
      setNotice(`SSE 暂不可用，已切换为状态/事件轮询。${state.pollReason}`, "warn");
      startPolling(runId);
      return;
    }
    const delay = Math.min(8000, 600 * (2 ** attempt));
    state.streamRetryAttempt = attempt + 1;
    setConnection(`SSE 断开，${(delay / 1000).toFixed(1)}s 后重连`, "warn");
    state.streamRetryTimer = setTimeout(() => {
      state.streamRetryTimer = null;
      connectStream(runId, state.lastEventSeq, attempt + 1);
    }, delay);
  }

  async function startPolling(runId) {
    stopPolling();
    if (state.runId !== runId || !state.run) return;
    const tick = async () => {
      if (state.runId !== runId || !state.run) return;
      try {
        const payload = await api(runApiPath(runId, "", state.run));
        if (state.runId !== runId) return;
        state.run = payload.run;
        renderRunBadge();
        renderCurrentRun();
        renderTraceSummary();
        updateControls();
        await loadEventsFallback(runId);
        if (state.runId !== runId) return;
        if (isActive(state.run)) {
          state.pollTimer = setTimeout(tick, POLL_INTERVAL_MS);
        } else {
          await finishRun(runId, "poll");
        }
      } catch (error) {
        if (state.runId !== runId) return;
        state.pollReason = describeError(error);
        setConnection("状态轮询失败", "error");
        setNotice(state.pollReason, "error");
        if (!(error instanceof ApiError) || ![400, 404, 409, 422].includes(error.status)) {
          state.pollTimer = setTimeout(tick, POLL_INTERVAL_MS * 2);
        }
      }
    };
    state.pollTimer = setTimeout(tick, 0);
  }

  async function loadEventsFallback(runId, options = {}) {
    if (state.runId !== runId || !state.run) return;
    let after = options.fromStart ? 0 : state.lastEventSeq;
    const limit = 500;
    for (let page = 0; page < 6; page += 1) {
      const params = new URLSearchParams({
        project_id: state.run.project_id,
        after_seq: String(after),
        limit: String(limit),
      });
      const payload = await api(`${runPath(runId)}/events?${params.toString()}`);
      if (state.runId !== runId) return;
      const events = Array.isArray(payload.events) ? payload.events : [];
      events.forEach(mergeEvent);
      const next = Number(payload.next_after_seq);
      if (!events.length || !Number.isFinite(next) || next <= after) break;
      after = next;
      if (events.length < limit) break;
    }
    renderInspector();
  }

  async function loadResult(runId) {
    if (state.runId !== runId || !state.run || !isTerminal(state.run)) return;
    state.resultLoading = true;
    state.resultError = "";
    renderCurrentRun();
    try {
      const payload = await api(runApiPath(runId, "/result", state.run));
      if (state.runId !== runId) return;
      state.result = payload;
      state.resultLoaded = true;
    } catch (error) {
      if (state.runId !== runId) return;
      state.resultError = describeError(error);
      state.resultLoaded = true;
    } finally {
      if (state.runId === runId) {
        state.resultLoading = false;
        renderCurrentRun();
      }
    }
  }

  async function loadToolCalls(runId) {
    if (state.runId !== runId || !state.run) return;
    try {
      const payload = await api(runApiPath(runId, "/tool-calls", state.run));
      if (state.runId !== runId) return;
      state.toolCalls = Array.isArray(payload.tool_calls) ? payload.tool_calls : [];
      state.toolError = "";
    } catch (error) {
      if (state.runId !== runId) return;
      state.toolError = describeError(error);
    }
    renderInspector();
  }

  async function loadContextTrace(runId) {
    if (state.runId !== runId || !state.run) return;
    try {
      const payload = await api(runApiPath(runId, "/context-trace", state.run));
      if (state.runId !== runId) return;
      state.contextTrace = Array.isArray(payload.context_trace) ? payload.context_trace : [];
      state.contextError = "";
    } catch (error) {
      if (state.runId !== runId) return;
      state.contextError = describeError(error);
    }
    renderInspector();
  }

  async function loadEvidenceRefs(runId) {
    if (state.runId !== runId || !state.run) return;
    state.evidenceError = "";
    state.evidenceDetails.clear();
    state.evidenceErrors.clear();
    try {
      const payload = await api(runApiPath(runId, "/evidence-refs", state.run));
      if (state.runId !== runId) return;
      state.evidenceRefs = Array.isArray(payload.evidence_refs) ? payload.evidence_refs : [];
    } catch (error) {
      if (state.runId !== runId) return;
      state.evidenceRefs = [];
      state.evidenceError = describeError(error);
      renderInspector();
      return;
    }
    renderInspector();
    const run = state.run;
    await Promise.all(state.evidenceRefs.map(async (ref) => {
      const key = `${ref.evidence_id}|${ref.content_hash}`;
      if (!EVIDENCE_ID.test(String(ref.evidence_id || "")) || !HASH.test(String(ref.content_hash || ""))) {
        state.evidenceErrors.set(key, "后端引用缺少合法 evidence_id/content_hash。");
        return;
      }
      try {
        const payload = await api(evidencePath(ref, run));
        if (state.runId !== runId) return;
        if (payload && payload.evidence) state.evidenceDetails.set(key, payload.evidence);
        else state.evidenceErrors.set(key, "后端未返回 evidence 对象。");
      } catch (error) {
        if (state.runId !== runId) return;
        state.evidenceErrors.set(key, describeError(error));
      }
    }));
    if (state.runId === runId) renderInspector();
  }

  async function loadRunArtifacts(runId, options = {}) {
    if (state.runId !== runId || !state.run) return;
    const run = state.run;
    if (!isTerminal(run) && !isActive(run)) return;
    if (isActive(run) && !options.allowActive) {
      await Promise.allSettled([
        loadEventsFallback(runId, { fromStart: true }),
        loadToolCalls(runId),
        loadContextTrace(runId),
      ]);
      return;
    }
    await Promise.allSettled([
      loadEventsFallback(runId, { fromStart: true }),
      loadResult(runId),
      loadToolCalls(runId),
      loadEvidenceRefs(runId),
      loadContextTrace(runId),
    ]);
    renderInspector();
  }

  async function finishRun(runId, via) {
    if (state.runId !== runId) return;
    if (state.finishingRunId === runId) return;
    state.finishingRunId = runId;
    state.streamEnded = true;
    stopPolling();
    if (state.streamRetryTimer) {
      clearTimeout(state.streamRetryTimer);
      state.streamRetryTimer = null;
    }
    try {
      const payload = await api(runApiPath(runId, "", state.run));
      if (state.runId !== runId) return;
      state.run = payload.run;
      if (isActive(state.run)) {
        setConnection("状态仍非终态，继续轮询", "warn");
        startPolling(runId);
        return;
      }
      setConnection(via === "poll" ? "轮询完成" : "SSE 已结束", "ok");
      renderRunBadge();
      renderCurrentRun();
      updateControls();
      renderInspector();
      await loadRunArtifacts(runId);
      await refreshSessionRuns();
    } catch (error) {
      if (state.runId !== runId) return;
      state.runError = describeError(error);
      setConnection("刷新结果失败", "error");
      setNotice(`Run 已结束但刷新结果失败：${state.runError}`, "error");
      renderRunBadge();
      renderCurrentRun();
      renderInspector();
    } finally {
      if (state.finishingRunId === runId) state.finishingRunId = "";
    }
  }

  // ------------------------------------------------------------------
  // Loading runs / sessions
  // ------------------------------------------------------------------
  function clearRunState() {
    stopTransports();
    state.run = null;
    state.runId = "";
    state.runLoading = false;
    state.runError = "";
    state.result = null;
    state.resultLoaded = false;
    state.resultLoading = false;
    state.resultError = "";
    state.events = new Map();
    state.lastEventSeq = 0;
    state.evidenceRefs = [];
    state.evidenceDetails = new Map();
    state.evidenceErrors = new Map();
    state.evidenceError = "";
    state.toolCalls = [];
    state.toolError = "";
    state.contextTrace = [];
    state.contextError = "";
    state.traceError = "";
    state.streamRetryAttempt = 0;
    state.streamEnded = false;
    state.pollReason = "";
    renderRunBadge();
    renderInspector();
    updateControls();
  }

  function clearArtifactsForRun() {
    state.result = null;
    state.resultLoaded = false;
    state.resultLoading = false;
    state.resultError = "";
    state.events = new Map();
    state.lastEventSeq = 0;
    state.evidenceRefs = [];
    state.evidenceDetails = new Map();
    state.evidenceErrors = new Map();
    state.evidenceError = "";
    state.toolCalls = [];
    state.toolError = "";
    state.contextTrace = [];
    state.contextError = "";
    state.traceError = "";
    state.streamEnded = false;
    state.streamRetryAttempt = 0;
    state.pollReason = "";
    renderInspector();
  }

  async function selectRun(runId, options = {}) {
    const id = String(runId || "").trim();
    if (!id) {
      clearRunState();
      renderEmptyConversation();
      return;
    }
    if (!currentProject()) {
      setNotice("恢复 Run 需要 project_id。", "error");
      return;
    }
    stopTransports();
    state.runId = id;
    if (!options.keepSelection) {
      state.run = null;
      state.runError = "";
      clearArtifactsForRun();
      renderRunBadge();
      renderCurrentRun();
      renderTraceSummary();
      saveUrl();
    }
    state.runLoading = true;
    if (!options.silent) clearNotice();
    try {
      const payload = await api(`/runs/${encodeURIComponent(id)}?${scopeQuery(currentProject(), null)}`);
      if (state.runId !== id) return;
      state.run = payload.run;
      state.runLoading = false;
      state.runError = "";
      renderRunBadge();
      renderRunHistory();
      renderCurrentRun();
      renderTraceSummary();
      updateControls();
      saveUrl();
      if (isActive(state.run)) {
        state.streamEnded = false;
        await loadRunArtifacts(id, { allowActive: true });
        if (state.runId === id) connectStream(id, state.lastEventSeq, 0);
      } else {
        setConnection("Run 已加载", "ok");
        await loadRunArtifacts(id);
        await refreshSessionRuns({ silent: true });
      }
    } catch (error) {
      if (state.runId !== id) return;
      state.runLoading = false;
      state.runError = describeError(error);
      setNotice(state.runError, "error");
      setConnection("Run 加载失败", "error");
      renderRunBadge();
      renderCurrentRun();
      renderTraceSummary();
      renderInspector();
    }
  }

  async function loadSession(sessionId, options = {}) {
    const id = String(sessionId || "").trim();
    if (!id) {
      setNotice("session_id 不能为空。", "error");
      return;
    }
    if (id.length > 200 || /[\\/]/.test(id)) {
      setNotice("session_id 格式无效。", "error");
      return;
    }
    const token = ++state.loadToken;
    const preferredRun = options.runId || state.runId || "";
    setPending(true);
    clearNotice();
    setConnection("加载 Session", "running");
    stopTransports();
    try {
      const payload = await api(`/sessions/${encodeURIComponent(id)}?message_limit=100&run_limit=${MAX_HISTORY_RUNS}`);
      if (token !== state.loadToken) return;
      state.runs = Array.isArray(payload.runs) ? payload.runs : [];
      applySession(payload.session);
      state.sessionId = payload.session.session_id;
      state.projectId = payload.session.project_id;
      state.datasetKind = payload.session.dataset_kind;
      clearRunState();
      renderSessionMeta();
      renderRunHistory();
      const fallback = state.runs.length ? state.runs[0].run_id : "";
      const target = preferredRun || fallback;
      if (target) {
        await selectRun(target, { keepSelection: true });
      } else {
        renderEmptyConversation();
        saveUrl();
        setConnection("Session 已加载", "ok");
      }
      renderSessionMeta();
      renderRunHistory();
      updateControls();
    } catch (error) {
      if (token !== state.loadToken) return;
      setNotice(describeError(error), "error");
      setConnection("Session 加载失败", "error");
    } finally {
      if (token === state.loadToken) setPending(false);
    }
  }

  async function createSession() {
    const projectId = (els["project-id"]?.value || "").trim();
    const datasetKind = els["dataset-kind"]?.value === "fixture" ? "fixture" : "live";
    if (!projectId) {
      setNotice("project_id 不能为空。", "error");
      return;
    }
    if (projectId !== (els["project-id"]?.value || "") || projectId.length > 200) {
      setNotice("project_id 不能包含首尾空白，长度不能超过 200。", "error");
      return;
    }
    setPending(true);
    clearNotice();
    setConnection("创建 Session", "running");
    try {
      const payload = await api("/sessions", {
        method: "POST",
        body: { project_id: projectId, dataset_kind: datasetKind },
      });
      state.projectId = payload.session.project_id;
      state.datasetKind = payload.session.dataset_kind;
      state.runs = [];
      applySession(payload.session);
      clearRunState();
      renderSessionMeta();
      renderRunHistory();
      renderEmptyConversation();
      saveUrl();
      setConnection("API 已连接", "ok");
      setNotice("Session 已创建。可以提交调查问题。", "ok");
      if (els.question) els.question.focus();
    } catch (error) {
      setNotice(describeError(error), "error");
      setConnection("Session 创建失败", "error");
    } finally {
      setPending(false);
    }
  }

  async function closeSession() {
    if (!state.session) return;
    if (isActive(state.run)) {
      setNotice("当前 Run 仍在执行，等待终态后再关闭 Session。", "warn");
      return;
    }
    setPending(true);
    clearNotice();
    try {
      const payload = await api(`/sessions/${encodeURIComponent(state.sessionId)}/close`, { method: "POST" });
      applySession(payload.session);
      setNotice("Session 已关闭。已有 Run 仍可查看，但不能继续提交。", "ok");
      updateControls();
    } catch (error) {
      setNotice(describeError(error), "error");
    } finally {
      setPending(false);
    }
  }

  async function refreshSessionRuns(options = {}) {
    if (!state.sessionId) return;
    const sessionId = state.sessionId;
    try {
      const payload = await api(`/sessions/${encodeURIComponent(sessionId)}/runs?limit=${MAX_HISTORY_RUNS}`);
      if (state.sessionId !== sessionId) return;
      state.runs = Array.isArray(payload.runs) ? payload.runs : [];
      if (state.run) {
        const updated = state.runs.find((item) => item.run_id === state.run.run_id);
        if (updated) {
          state.run = updated;
          renderRunBadge();
          renderCurrentRun();
          renderTraceSummary();
          updateControls();
        }
      }
      renderSessionMeta();
      renderRunHistory();
      if (!options.silent) setConnection("Session 已刷新", "ok");
    } catch (error) {
      if (!options.silent) setNotice(describeError(error), "warn");
    }
  }

  async function submitRun(event) {
    event.preventDefault();
    if (!state.session || state.session.status !== "open") {
      setNotice("请先创建或加载一个未关闭的 Session。", "warn");
      return;
    }
    if (isActive(state.run)) {
      setNotice("当前 Session 的 Run 尚未结束。请等待或取消后再提交。", "warn");
      return;
    }
    const question = (els.question?.value || "").trim();
    if (!question) {
      setNotice("调查问题不能为空。", "warn");
      return;
    }
    if (question.length > 4000) {
      setNotice("调查问题不能超过 4000 字符。", "warn");
      return;
    }
    setPending(true);
    clearNotice();
    try {
      const payload = await api("/runs", {
        method: "POST",
        body: {
          session_id: state.sessionId,
          question,
          project_id: state.session.project_id,
          dataset_kind: state.session.dataset_kind,
        },
      });
      const run = payload.run;
      if (!run || !run.run_id) throw new Error("后端未返回 run_id");
      if (els.question) {
        els.question.value = "";
        updateCounter();
      }
      stopTransports();
      state.runId = run.run_id;
      state.run = run;
      state.runError = "";
      state.runLoading = false;
      clearArtifactsForRun();
      renderRunBadge();
      renderCurrentRun();
      renderTraceSummary();
      renderRunHistory();
      updateControls();
      saveUrl();
      setConnection("Run 已提交，连接 SSE", "running");
      setNotice("Run 已提交。正在通过 SSE 接收事件；刷新页面不会取消后台 Run。", "ok");
      await refreshSessionRuns({ silent: true });
      connectStream(run.run_id, 0, 0);
    } catch (error) {
      setNotice(describeError(error), "error");
      setConnection("Run 提交失败", "error");
    } finally {
      setPending(false);
    }
  }

  async function cancelRun() {
    if (!state.run || !isActive(state.run)) return;
    setPending(true);
    clearNotice();
    try {
      const payload = await api(runApiPath(state.run.run_id, "/cancel", state.run), { method: "POST" });
      if (state.runId === payload.run.run_id) {
        state.run = payload.run;
        renderRunBadge();
        renderCurrentRun();
        updateControls();
        renderTraceSummary();
      }
      setNotice(payload.cancel_requested
        ? "已请求取消。Run 会在协作式边界结束；最终状态以后端为准。"
        : "后端未接受新的取消请求（可能已处于终态）。", "warn");
    } catch (error) {
      setNotice(describeError(error), "error");
    } finally {
      setPending(false);
    }
  }

  async function checkHealth() {
    try {
      const payload = await api("/health");
      if (payload && payload.status === "ok") {
        if (!state.run) setConnection("API 已连接", "ok");
      } else {
        setConnection("API 状态未知", "warn");
      }
    } catch (error) {
      if (!state.run) setConnection("API 不可达", "error");
    }
  }

  // ------------------------------------------------------------------
  // Initialization
  // ------------------------------------------------------------------
  function bindEvents() {
    els["create-session-btn"]?.addEventListener("click", createSession);
    els["load-session-btn"]?.addEventListener("click", () => {
      loadSession(els["session-id"]?.value || "");
    });
    els["close-session-btn"]?.addEventListener("click", closeSession);
    els["refresh-session-btn"]?.addEventListener("click", () => refreshSessionRuns());
    els["session-id"]?.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        loadSession(els["session-id"]?.value || "");
      }
    });
    els["question"]?.addEventListener("input", updateControls);
    els["question"]?.addEventListener("keydown", (event) => {
      if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
        event.preventDefault();
        els["run-form"]?.requestSubmit();
      }
    });
    els["run-form"]?.addEventListener("submit", submitRun);
    els["cancel-run-btn"]?.addEventListener("click", cancelRun);
    els["project-id"]?.addEventListener("change", () => {
      state.projectId = (els["project-id"]?.value || "").trim();
      persistState();
    });
    els["dataset-kind"]?.addEventListener("change", () => {
      state.datasetKind = els["dataset-kind"]?.value === "fixture" ? "fixture" : "live";
      persistState();
    });
    document.querySelectorAll("[data-tab]").forEach((button) => {
      button.addEventListener("click", () => switchTab(button.dataset.tab));
    });
    window.addEventListener("beforeunload", () => {
      // Disconnecting a browser stream is not a Run cancellation request.
      stopTransports();
    });
    window.addEventListener("pagehide", stopTransports);
  }

  async function initialize() {
    cacheElements();
    bindEvents();
    const restored = restoredState();
    state.projectId = restored.projectId;
    state.datasetKind = restored.datasetKind;
    state.sessionId = restored.sessionId;
    if (els["project-id"]) els["project-id"].value = restored.projectId;
    if (els["dataset-kind"]) els["dataset-kind"].value = restored.datasetKind;
    if (els["session-id"]) els["session-id"].value = restored.sessionId;
    switchTab("evidence");
    renderSessionMeta();
    renderRunHistory();
    renderInspector();
    updateControls();
    renderEmptyConversation();
    checkHealth();
    if (restored.sessionId) {
      await loadSession(restored.sessionId, { runId: restored.runId });
    } else if (restored.runId && restored.projectId) {
      try {
        const payload = await api(`/runs/${encodeURIComponent(restored.runId)}?${scopeQuery(restored.projectId, null)}`);
        if (payload.run?.session_id) {
          await loadSession(payload.run.session_id, { runId: restored.runId });
        }
      } catch (error) {
        setNotice(`无法从 run_id 恢复：${describeError(error)}`, "error");
      }
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initialize, { once: true });
  } else {
    initialize();
  }
})();
