from __future__ import annotations


APP_HTML = """<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Nilo View</title>
  <link rel="stylesheet" href="/assets/app.css">
</head>
<body>
  <aside class="sidebar">
    <div class="brand">Nilo</div>
    <nav>
      <button data-view="overview" class="active">概要</button>
      <button data-view="analytics">分析</button>
      <button data-view="tasks">タスク</button>
      <button data-view="todos">TODO</button>
      <button data-view="timeline">履歴</button>
    </nav>
  </aside>
  <main>
    <header>
      <div>
        <div class="eyebrow">プロジェクト</div>
        <h1 id="project-name">Nilo</h1>
        <div id="db-path" class="muted"></div>
      </div>
      <span class="badge">読み取り専用</span>
    </header>
    <section id="app" aria-live="polite"></section>
  </main>
  <script src="/assets/app.js"></script>
</body>
</html>
"""


APP_CSS = """
:root {
  --bg: #0b0d12;
  --panel: #141821;
  --panel-soft: #1b202b;
  --border: rgba(255, 255, 255, 0.08);
  --text: #f5f5f7;
  --text-muted: #a1a1aa;
  --text-subtle: #71717a;
  --accent: #38bdf8;
  --accent-soft: rgba(56, 189, 248, 0.14);
  --accent-border: rgba(56, 189, 248, 0.42);
  --highlight: #facc15;
  --success: #30d158;
  --warning: #ffd60a;
  --danger: #ff453a;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  min-height: 100vh;
  display: grid;
  grid-template-columns: 220px 1fr;
  background: var(--bg);
  color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
}
.sidebar {
  border-right: 1px solid var(--border);
  background: rgba(20, 24, 33, 0.76);
  padding: 28px 18px;
}
.brand {
  font-size: 18px;
  font-weight: 650;
  margin: 0 0 26px;
}
nav { display: grid; gap: 6px; }
button {
  border: 0;
  border-radius: 8px;
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
  font: inherit;
  padding: 10px 12px;
  text-align: left;
}
button:hover, button.active {
  background: var(--accent-soft);
  box-shadow: inset 3px 0 0 var(--accent);
  color: var(--text);
}
button:disabled { cursor: default; opacity: 0.42; }
main { min-width: 0; padding: 30px 38px 46px; }
header {
  align-items: flex-start;
  display: flex;
  justify-content: space-between;
  gap: 18px;
  margin-bottom: 28px;
}
h1, h2, h3 { font-weight: 620; letter-spacing: 0; margin: 0; }
h1 { font-size: 30px; }
h2 { font-size: 22px; margin: 26px 0 14px; }
h3 { font-size: 15px; margin-bottom: 10px; }
.eyebrow {
  color: var(--text-subtle);
  font-size: 12px;
  margin-bottom: 6px;
  text-transform: uppercase;
}
.muted { color: var(--text-muted); font-size: 13px; overflow-wrap: anywhere; }
.grid { display: grid; gap: 14px; margin-bottom: 18px; }
.summary { grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); }
.card, .panel {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 8px;
  box-shadow: 0 18px 45px rgba(0, 0, 0, 0.18);
  padding: 18px;
}
.clickable-card { cursor: pointer; transition: border-color 120ms ease, transform 120ms ease; }
.clickable-card:hover, .clickable-card:focus {
  border-color: var(--accent-border);
  box-shadow: 0 18px 45px rgba(0, 0, 0, 0.18), 0 0 0 1px var(--accent-border);
  outline: none;
  transform: translateY(-1px);
}
.notice {
  background: var(--accent-soft);
  border: 1px solid var(--accent-border);
  border-radius: 8px;
  color: var(--text);
  font-size: 13px;
  margin-bottom: 18px;
  padding: 12px 14px;
}
.number { font-size: 34px; line-height: 1; margin-bottom: 8px; }
.clickable-card .number { color: var(--highlight); }
.label { color: var(--text-muted); font-size: 13px; }
.analytics-card { align-content: start; }
.metric-grid {
  display: grid;
  gap: 10px;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  margin-top: 14px;
}
.metric {
  background: rgba(255, 255, 255, 0.025);
  border: 1px solid var(--border);
  border-radius: 8px;
  min-width: 0;
  padding: 12px;
}
.metric-value {
  color: var(--text);
  font-size: 24px;
  line-height: 1.1;
  overflow-wrap: anywhere;
}
.compact-list {
  border-top: 1px solid var(--border);
  margin-top: 14px;
  padding-top: 14px;
}
.list-row {
  color: var(--text);
  font-size: 13px;
  margin: 8px 0 3px;
  overflow-wrap: anywhere;
}
.badge {
  align-items: center;
  background: var(--accent-soft);
  border: 1px solid var(--accent-border);
  border-radius: 999px;
  color: #bae6fd;
  display: inline-flex;
  font-size: 12px;
  padding: 5px 10px;
}
.badge.warning { background: rgba(255, 214, 10, 0.1); border-color: rgba(255, 214, 10, 0.26); color: var(--warning); }
.badge.danger { background: rgba(255, 69, 58, 0.1); border-color: rgba(255, 69, 58, 0.26); color: var(--danger); }
table { border-collapse: collapse; width: 100%; }
.todo-table { table-layout: fixed; }
.todo-table .todo-main { width: 54%; }
.todo-table .todo-status { width: 10%; }
.todo-table .todo-kind { width: 10%; }
.todo-table .todo-priority { width: 8%; }
.todo-table .todo-source { width: 8%; }
.todo-table .todo-created { width: 10%; }
th, td {
  border-bottom: 1px solid var(--border);
  color: var(--text-muted);
  font-size: 13px;
  padding: 12px 10px;
  text-align: left;
  vertical-align: top;
}
th { color: var(--text-subtle); font-weight: 520; }
.nowrap { white-space: nowrap; }
.todo-preview {
  display: -webkit-box;
  line-height: 1.5;
  margin-top: 6px;
  max-width: 78ch;
  overflow: hidden;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
}
tr:hover td { background: rgba(56, 189, 248, 0.06); }
.linkish {
  color: var(--accent);
  cursor: pointer;
  text-decoration-color: transparent;
  text-decoration-line: underline;
  text-underline-offset: 3px;
}
.linkish:hover { color: #7dd3fc; text-decoration-color: currentColor; }
.dot {
  background: var(--text-subtle);
  border-radius: 999px;
  display: inline-block;
  height: 8px;
  margin-right: 8px;
  width: 8px;
}
.dot.ok { background: var(--success); }
.dot.warn { background: var(--warning); }
.dot.bad { background: var(--danger); }
.toolbar {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  margin-bottom: 14px;
}
.pager {
  align-items: center;
  display: flex;
  gap: 12px;
  justify-content: flex-end;
  margin-top: 14px;
}
select, label.filter {
  background: var(--panel-soft);
  border: 1px solid var(--border);
  border-radius: 8px;
  color: var(--text);
  font: inherit;
  font-size: 13px;
  padding: 9px 10px;
}
select:focus, label.filter:focus-within {
  border-color: var(--accent-border);
  outline: none;
}
label.filter { align-items: center; display: inline-flex; gap: 8px; }
details {
  background: rgba(255, 255, 255, 0.025);
  border: 1px solid var(--border);
  border-radius: 8px;
  margin: 10px 0;
  padding: 12px;
}
summary { cursor: pointer; color: var(--text); }
pre {
  color: var(--text-muted);
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
  overflow: auto;
  white-space: pre-wrap;
}
.record-section { margin-top: 22px; }
.record-card {
  background: rgba(255, 255, 255, 0.025);
  border: 1px solid var(--border);
  border-radius: 8px;
  line-height: 1.55;
  margin: 12px 0;
  padding: 14px;
}
.record-title {
  color: var(--text);
  font-weight: 650;
  line-height: 1.45;
  overflow-wrap: anywhere;
}
.empty.compact { padding: 8px 0; }
.task-detail {
  line-height: 1.65;
  max-width: 980px;
}
.task-detail-header {
  margin: 18px 0 14px;
}
.task-detail-header .muted { margin-top: 8px; }
.human-summary {
  border-color: var(--accent-border);
  margin-bottom: 16px;
}
.summary-line {
  color: var(--text);
  font-size: 15px;
  line-height: 1.65;
  margin: 12px 0 0;
  overflow-wrap: anywhere;
}
.meta-row {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 12px;
}
.meta-pill {
  background: rgba(255, 255, 255, 0.035);
  border: 1px solid var(--border);
  border-radius: 999px;
  color: var(--text-muted);
  font-size: 12px;
  padding: 5px 9px;
}
.detail-card { margin: 14px 0; }
.text-preview {
  color: var(--text-muted);
  font-size: 14px;
  line-height: 1.7;
  margin: 8px 0 0;
  overflow-wrap: anywhere;
}
.checklist {
  display: grid;
  gap: 9px;
  list-style: none;
  margin: 10px 0 0;
  padding: 0;
}
.checklist li {
  align-items: start;
  color: var(--text);
  display: grid;
  gap: 9px;
  grid-template-columns: 18px 1fr;
  line-height: 1.55;
  overflow-wrap: anywhere;
}
.checklist input {
  align-self: start;
  height: 14px;
  margin: 0;
  position: relative;
  top: 0.35em;
  width: 14px;
}
.technical-meta {
  color: var(--text-subtle);
  font-size: 12px;
}
.timeline { border-left: 1px solid var(--border); margin-left: 9px; padding-left: 20px; }
.event { margin: 0 0 12px; position: relative; }
.event::before {
  background: var(--accent);
  border-radius: 999px;
  content: "";
  height: 9px;
  left: -25px;
  position: absolute;
  top: 20px;
  width: 9px;
}
.empty { color: var(--text-muted); padding: 30px 0; }
"""


APP_JS = """
const app = document.querySelector("#app");
const projectName = document.querySelector("#project-name");
const dbPath = document.querySelector("#db-path");
const navButtons = Array.from(document.querySelectorAll("nav button"));
let state = { overview: null, tasks: [], todos: [], taskPage: 1, taskPageSize: 50, taskPagination: null, taskFilters: {}, todoFilters: {} };

const el = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
};

const clear = node => {
  while (node.firstChild) node.removeChild(node.firstChild);
};

const api = async path => {
  const response = await fetch(path);
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  return response.json();
};

const setProject = overview => {
  if (!overview || !overview.project) return;
  projectName.textContent = overview.project.name || overview.project.id;
  dbPath.textContent = overview.project.db_path || "";
};

const renderError = error => {
  clear(app);
  const panel = el("div", "panel");
  panel.append(el("h2", "", "確認が必要です"));
  panel.append(el("p", "muted", error.message || String(error)));
  app.append(panel);
};

const summaryCard = (label, value, tone, onClick) => {
  const card = el("div", onClick ? "card clickable-card" : "card");
  if (onClick) {
    card.tabIndex = 0;
    card.addEventListener("click", onClick);
    card.addEventListener("keydown", event => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        onClick();
      }
    });
  }
  const number = el("div", `number ${tone || ""}`, String(value));
  card.append(number, el("div", "label", label));
  return card;
};

const goTodos = filters => {
  state.todoFilters = filters || {};
  activate("todos");
};

const goTasks = filters => {
  state.taskPage = 1;
  state.taskFilters = filters || {};
  activate("tasks");
};

const badge = (text, tone) => el("span", `badge ${tone || ""}`, text || "none");

const actionText = (tag, text, onActivate) => {
  const node = el(tag, "linkish", text);
  node.tabIndex = 0;
  node.setAttribute("role", "button");
  node.addEventListener("click", onActivate);
  node.addEventListener("keydown", event => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      onActivate();
    }
  });
  return node;
};

const statusDot = status => {
  const span = el("span", "");
  const dot = el("span", "dot");
  if (String(status).includes("passed") || String(status).includes("approved") || String(status).includes("completed")) dot.className = "dot ok";
  if (String(status).includes("failed") || String(status).includes("blocking") || String(status).includes("missing")) dot.className = "dot bad";
  if (String(status).includes("requested") || String(status).includes("review")) dot.className = "dot warn";
  span.append(dot, document.createTextNode(labelFor(status || "none")));
  return span;
};

const renderOverview = async () => {
  const data = await api("/api/overview");
  state.overview = data;
  setProject(data);
  clear(app);
  const summary = el("div", "grid summary");
  summary.append(
    summaryCard("未完了タスク", data.summary.open_tasks, "", () => goTasks({ status: "open" })),
    summaryCard("完了タスク", data.summary.completed_tasks, "", () => goTasks({ status: "completed" })),
    summaryCard("未完了 TODO", data.summary.open_todos, "", () => goTodos({ status: "open" })),
    summaryCard("失敗ログあり", data.summary.open_failure_logs, "", () => goTasks({ open_failures: true })),
    summaryCard("未解決指摘あり", data.summary.open_blocking_findings, "", () => goTasks({ open_findings: true }))
  );
  app.append(summary);
  const panel = el("div", "panel");
  panel.append(el("h2", "", "現在地"));
  panel.append(el("p", "muted", data.next_action || "作業中のタスクはありません。"));
  if (data.active_task) {
    const title = actionText("h3", data.active_task.title, () => renderTaskDetail(data.active_task.id));
    panel.append(title);
    panel.append(statusDot(data.active_task.status));
  }
  app.append(panel);
};

const renderAnalytics = async () => {
  const data = await api("/api/analytics");
  clear(app);
  app.append(el("h2", "", "タスク分析"));
  app.append(el("p", "muted", "Nilo に蓄積されたタスク、検証、レビュー、失敗ログから、作業の進み方と詰まりやすい場所を読むための集計です。"));
  const grid = el("div", "grid summary");
  grid.append(
    analyticsCard("作業全体", "タスクがどれだけ完了し、どれだけ人間確認・検証・レビューを伴っているか。", [
      metric("タスク", `${data.summary.completed_count} / ${data.summary.task_count}`, "完了 / 全体"),
      metric("未完了", data.summary.open_count, "未完了タスク"),
      metric("検証済み", data.summary.completed_with_verification_count, "検証付き完了"),
      metric("レビュー済み", data.summary.completed_with_review_count, "レビュー付き完了")
    ]),
    analyticsCard("検証", "テストや確認コマンドの実行傾向。失敗や timeout が多いコマンドは作業の摩擦になりやすい場所です。", [
      metric("実行", data.verification.run_count, "検証実行数"),
      metric("成功", data.verification.passed_count, "成功"),
      metric("失敗", data.verification.failed_count, "失敗"),
      metric("Timeout", data.verification.timed_out_count, "時間切れ")
    ], commandList(data.verification.commands, "よく使われた検証コマンド")),
    analyticsCard("レビュー", "レビュー依頼、結果、未解決指摘の状態。blocking finding が残っていると完了判断の前に見るべき場所です。", [
      metric("依頼", data.review.request_count, "レビュー依頼"),
      metric("結果", data.review.result_count, "レビュー結果"),
      metric("未解決", data.review.open_finding_count, "未解決指摘"),
      metric("重要指摘", data.review.blocking_finding_count, "blocking 指摘")
    ], countList(data.review.verdict_counts, "判定分布")),
    analyticsCard("失敗ログ", "証跡不足、検証失敗、手戻りなど、後から見返すための失敗記録です。", [
      metric("該当タスク", data.summary.open_failure_task_count, "未解決 failure があるタスク"),
      metric("失敗ログ", totalCounts(data.failure.category_counts), "記録件数"),
      metric("高", valueAt(data.failure.severity_counts, "high"), "high severity"),
      metric("中", valueAt(data.failure.severity_counts, "medium"), "medium severity")
    ], countList(data.failure.category_counts, "カテゴリ")),
    analyticsCard("作業設計", "タスク種別、リスク、ロードマップ/単独、overdrive など、作業の切り方の偏りです。", [
      metric("ロードマップ", data.task_design.roadmap_task_count, "ロードマップ配下のタスク"),
      metric("単独", data.task_design.standalone_task_count, "単独タスク"),
      metric("Overdrive", data.task_design.overdrive_task_count, "overdrive mode"),
      metric("理解確認", data.task_design.requires_understanding_check_task_count, "理解確認あり")
    ], countList(data.task_design.task_type_counts, "タスク種別"))
  );
  app.append(grid);
};

const analyticsCard = (title, description, metrics, extra) => {
  const card = el("div", "card analytics-card");
  card.append(el("h3", "", title), el("p", "muted", description));
  const metricsGrid = el("div", "metric-grid");
  metrics.forEach(item => metricsGrid.append(item));
  card.append(metricsGrid);
  if (extra) card.append(extra);
  return card;
};

const metric = (label, value, hint) => {
  const box = el("div", "metric");
  box.append(el("div", "metric-value", String(value ?? 0)), el("div", "label", label), el("div", "muted", hint));
  return box;
};

const countList = (counts, title) => {
  const wrap = el("div", "compact-list");
  wrap.append(el("h3", "", title));
  const entries = Object.entries(counts || {}).sort((a, b) => b[1] - a[1]).slice(0, 6);
  if (!entries.length) {
    wrap.append(el("div", "muted", "データはありません。"));
    return wrap;
  }
  entries.forEach(([key, value]) => wrap.append(el("div", "list-row", `${key}: ${value}`)));
  return wrap;
};

const commandList = (commands, title) => {
  const wrap = el("div", "compact-list");
  wrap.append(el("h3", "", title));
  (commands || []).slice(0, 5).forEach(command => {
    wrap.append(el("div", "list-row", `${command.run_count}回 · 失敗 ${command.failed_count} · timeout ${command.timed_out_count}`));
    wrap.append(el("div", "muted", command.command));
  });
  return wrap;
};

const valueAt = (object, key) => object && object[key] ? object[key] : 0;
const totalCounts = object => Object.values(object || {}).reduce((sum, value) => sum + value, 0);

const LABELS = {
  status: "状態",
  task_type: "種別",
  risk_level: "リスク",
  roadmap: "ロードマップ",
  todo_status: "状態",
  todo_kind: "種類",
  todo_priority: "優先度",
  open: "未完了",
  completed: "完了",
  planned: "計画済み",
  agent_reported: "作業報告あり",
  instruction_generated: "指示生成済み",
  verification_passed: "検証成功",
  verification_failed: "検証失敗",
  verification_timed_out: "検証 timeout",
  review_requested: "レビュー依頼中",
  review_claimed: "レビュー対応中",
  review_in_progress: "レビュー中",
  review_stale: "レビュー stale",
  review_approved: "レビュー承認",
  review_changes_requested: "修正依頼",
  review_commented: "レビューコメント",
  completed_by_user: "人間が完了",
  completed_by_ai: "AI が完了",
  implementation: "実装",
  documentation: "ドキュメント",
  research: "調査",
  design: "設計",
  test_addition: "テスト追加",
  verification: "検証",
  review: "レビュー",
  refactor: "リファクタ",
  low: "低",
  medium: "中",
  high: "高",
  normal: "通常",
  overdrive: "Overdrive",
  standalone: "単独",
  user_request: "ユーザー依頼",
  discovered_issue: "発見した問題",
  follow_up: "フォローアップ",
  cleanup: "整理",
  question: "質問",
  roadmap_candidate: "ロードマップ候補",
  triaged: "整理済み",
  ready: "着手可能",
  ad_hoc_approved: "単発承認済み",
  requires_roadmap: "ロードマップ必要",
  blocked: "ブロック中",
  converted_to_task: "タスク化済み",
  deferred: "延期",
  rejected: "却下",
  superseded: "置き換え済み",
  none: "なし",
  passed: "成功",
  failed: "失敗",
  timed_out: "timeout",
  user_message: "ユーザー発言"
};

const labelFor = value => LABELS[value] || value || "";

const FILTER_LABELS = {
  status: "状態すべて",
  task_type: "種別すべて",
  risk_level: "リスクすべて",
  roadmap: "計画区分すべて",
  todo_status: "状態すべて",
  todo_kind: "種類すべて",
  todo_priority: "優先度すべて"
};

const formatDateTime = value => {
  if (!value) return "";
  const match = String(value).match(/^(\\d{4})-(\\d{2})-(\\d{2})T(\\d{2}):(\\d{2})/);
  if (match) return `${match[1]}/${match[2]}/${match[3]} ${match[4]}:${match[5]}`;
  return value;
};

const renderTasks = async (page = state.taskPage) => {
  state.taskPage = Math.max(1, page);
  const data = await api(`/api/tasks?${taskQueryString()}`);
  state.tasks = data.tasks || [];
  state.taskPagination = data.pagination || { page: state.taskPage, page_size: state.taskPageSize, total: state.tasks.length, total_pages: 1 };
  clear(app);
  app.append(el("h2", "", "タスク"));
  const toolbar = el("div", "toolbar");
  const status = filterSelect("status", ["", "open", "completed", ...unique(state.tasks.map(t => t.status))]);
  const type = filterSelect("task_type", ["", ...unique(state.tasks.map(t => t.task_type))]);
  const risk = filterSelect("risk_level", ["", ...unique(state.tasks.map(t => t.risk_level))]);
  const findings = checkbox("open_findings", "未解決指摘あり");
  const failures = checkbox("open_failures", "失敗ログあり");
  const reservations = checkbox("reservations", "予約付き完了");
  const roadmap = filterSelect("roadmap", ["", "roadmap", "standalone"]);
  toolbar.append(status, type, risk, findings, failures, reservations, roadmap);
  app.append(toolbar);
  applyInitialTaskFilters(toolbar);
  const holder = el("div", "panel");
  app.append(holder);
  const pager = el("div", "pager");
  app.append(pager);
  toolbar.addEventListener("change", () => {
    state.taskFilters = readTaskFilters();
    state.taskPage = 1;
    renderTasks(1);
  });
  drawTasksTable(holder, applyTaskFilters(state.tasks));
  drawPager(pager);
};

const taskQueryString = () => {
  const params = new URLSearchParams();
  params.set("page", String(state.taskPage));
  params.set("page_size", String(state.taskPageSize));
  for (const [key, value] of Object.entries(state.taskFilters || {})) {
    if (value) params.set(key, String(value));
  }
  return params.toString();
};

const unique = values => [...new Set(values.filter(Boolean))].sort();

const filterSelect = (name, values) => {
  const select = document.createElement("select");
  select.dataset.filter = name;
  for (const value of values) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = value ? labelFor(value) : FILTER_LABELS[name] || labelFor(name);
    select.append(option);
  }
  return select;
};

const checkbox = (name, text) => {
  const label = el("label", "filter");
  const input = document.createElement("input");
  input.type = "checkbox";
  input.dataset.filter = name;
  label.append(input, document.createTextNode(text));
  return label;
};

const filterValue = name => {
  const node = document.querySelector(`[data-filter="${name}"]`);
  return node && node.type === "checkbox" ? node.checked : node.value;
};

const readTaskFilters = () => ({
  status: filterValue("status"),
  task_type: filterValue("task_type"),
  risk_level: filterValue("risk_level"),
  open_findings: filterValue("open_findings"),
  open_failures: filterValue("open_failures"),
  reservations: filterValue("reservations"),
  roadmap: filterValue("roadmap")
});

const applyTaskFilters = tasks => tasks.filter(task => {
  if (filterValue("status") === "open" && task.completion.completed) return false;
  if (filterValue("status") === "completed" && !task.completion.completed) return false;
  if (filterValue("status") && !["open", "completed"].includes(filterValue("status")) && task.status !== filterValue("status")) return false;
  if (filterValue("task_type") && task.task_type !== filterValue("task_type")) return false;
  if (filterValue("risk_level") && task.risk_level !== filterValue("risk_level")) return false;
  if (filterValue("open_findings") && !task.review.open_blocking_findings) return false;
  if (filterValue("open_failures") && !task.failure.open_count) return false;
  if (filterValue("reservations") && !task.completion.completed_with_reservations) return false;
  if (filterValue("roadmap") === "roadmap" && !task.roadmap_commitment_id) return false;
  if (filterValue("roadmap") === "standalone" && task.roadmap_commitment_id) return false;
  return true;
});

const applyInitialTaskFilters = toolbar => {
  const filters = state.taskFilters || {};
  if (!Object.keys(filters).length) return;
  for (const [name, value] of Object.entries(filters)) {
    const node = toolbar.querySelector(`[data-filter="${name}"]`);
    if (!node) continue;
    if (node.type === "checkbox") node.checked = Boolean(value);
    else node.value = value;
  }
};

const drawTasksTable = (holder, tasks) => {
  clear(holder);
  if (!tasks.length) {
    holder.append(el("div", "empty", "条件に合うタスクはありません。"));
    return;
  }
  const table = el("table");
  const head = el("tr");
  ["タスク", "状態", "種別", "リスク", "モード", "検証", "レビュー", "失敗", "作成日"].forEach(text => head.append(el("th", "", text)));
  table.append(head);
  for (const task of tasks) {
    const row = el("tr");
    const id = el("td");
    const link = actionText("span", `${task.id.slice(0, 14)} ${task.title}`, () => renderTaskDetail(task.id));
    link.title = task.id;
    id.append(link);
    row.append(id);
    const status = el("td");
    status.append(statusDot(task.status));
    row.append(status);
    row.append(el("td", "", labelFor(task.task_type)), el("td", "", labelFor(task.risk_level)), el("td", "", labelFor(task.mode)));
    row.append(el("td", "", task.verification.latest_status ? labelFor(task.verification.latest_status) : String(task.verification.run_count)));
    row.append(el("td", "", `${task.review.result_count} / ${task.review.open_blocking_findings}`));
    row.append(el("td", "", String(task.failure.open_count)));
    row.append(el("td", "", formatDateTime(task.created_at)));
    table.append(row);
  }
  holder.append(table);
};

const drawPager = holder => {
  clear(holder);
  const pagination = state.taskPagination || { page: 1, total_pages: 1, total: state.tasks.length };
  const prev = el("button", "", "前へ");
  prev.disabled = pagination.page <= 1;
  prev.addEventListener("click", () => renderTasks(pagination.page - 1));
  const next = el("button", "", "次へ");
  next.disabled = pagination.page >= pagination.total_pages;
  next.addEventListener("click", () => renderTasks(pagination.page + 1));
  const label = el("span", "muted", `${pagination.page} / ${pagination.total_pages} ページ · ${pagination.total} 件`);
  holder.append(prev, label, next);
};

const renderTodos = async () => {
  const data = await api("/api/todos");
  state.todos = data.todos || [];
  clear(app);
  app.append(el("h2", "", "TODO"));
  const summary = el("div", "grid summary");
  summary.append(
    summaryCard("全体", data.summary.total),
    summaryCard("未完了", data.summary.open),
    summaryCard("着手可能", data.summary.ready),
    summaryCard("ブロック中", data.summary.blocked),
    summaryCard("タスク化済み", data.summary.converted)
  );
  app.append(summary);
  const toolbar = el("div", "toolbar");
  const status = filterSelect("todo_status", ["", ...unique(state.todos.map(todo => todo.status))]);
  const kind = filterSelect("todo_kind", ["", ...unique(state.todos.map(todo => todo.kind))]);
  const priority = filterSelect("todo_priority", ["", ...unique(state.todos.map(todo => todo.priority))]);
  toolbar.append(status, kind, priority);
  app.append(toolbar);
  applyInitialTodoFilters(toolbar);
  const holder = el("div", "panel");
  app.append(holder);
  const redraw = () => drawTodos(holder, applyTodoFilters(state.todos));
  toolbar.addEventListener("change", redraw);
  redraw();
};

const applyInitialTodoFilters = toolbar => {
  const filters = state.todoFilters || {};
  if (!Object.keys(filters).length) return;
  for (const [name, value] of Object.entries(filters)) {
    const node = toolbar.querySelector(`[data-filter="todo_${name}"]`);
    if (node) node.value = value;
  }
};

const applyTodoFilters = todos => todos.filter(todo => {
  if (filterValue("todo_status") && todo.status !== filterValue("todo_status")) return false;
  if (filterValue("todo_kind") && todo.kind !== filterValue("todo_kind")) return false;
  if (filterValue("todo_priority") && todo.priority !== filterValue("todo_priority")) return false;
  return true;
});

const drawTodos = (holder, todos) => {
  clear(holder);
  if (!todos.length) {
    holder.append(el("div", "empty", "条件に合う TODO はありません。"));
    return;
  }
  const table = el("table", "todo-table");
  const colgroup = document.createElement("colgroup");
  ["todo-main", "todo-status", "todo-kind", "todo-priority", "todo-source", "todo-created"].forEach(name => colgroup.append(el("col", name)));
  table.append(colgroup);
  const head = el("tr");
  ["TODO", "状態", "種類", "優先度", "元", "作成"].forEach(text => head.append(el("th", "", text)));
  table.append(head);
  for (const todo of todos) {
    const row = el("tr");
    const title = el("td");
    const link = actionText("div", `${todo.id.slice(0, 14)} ${todo.title}`, () => renderTodoDetail(todo.id));
    title.append(link);
    if (todo.description) title.append(el("div", "muted todo-preview", todo.description));
    if (todo.acceptance_hint) title.append(el("div", "muted todo-preview", `受け入れ目安: ${todo.acceptance_hint}`));
    row.append(title);
    const status = el("td");
    status.append(statusDot(todo.status));
    row.append(status);
    row.append(
      el("td", "", labelFor(todo.kind)),
      el("td", "", labelFor(todo.priority)),
      el("td", "", todo.source_task_id || labelFor(todo.source_type) || ""),
      el("td", "nowrap", formatDateTime(todo.created_at))
    );
    table.append(row);
  }
  holder.append(table);
};

const renderTodoDetail = todoId => {
  const todo = (state.todos || []).find(item => item.id === todoId);
  if (!todo) return;
  state.todoFilters = readTodoFilters();
  clear(app);
  const back = el("button", "", "TODO 一覧");
  back.addEventListener("click", renderTodos);
  const detail = el("div", "task-detail");
  const header = el("div", "task-detail-header");
  header.append(el("h2", "", todo.title), el("div", "muted technical-meta", `TODO ID: ${todo.id}`));
  detail.append(header);
  detail.append(todoSummaryCard(todo));
  detail.append(collapsibleTextCard("説明", todo.description || "説明はありません。"));
  detail.append(collapsibleTextCard("受け入れ目安", todo.acceptance_hint || "受け入れ目安はありません。"));
  detail.append(todoTechnicalMetaCard(todo));
  app.append(back, detail);
};

const readTodoFilters = () => ({
  status: filterValue("todo_status"),
  kind: filterValue("todo_kind"),
  priority: filterValue("todo_priority")
});

const todoSummaryCard = todo => {
  const card = el("div", "card human-summary");
  card.append(el("h3", "", "概要"));
  const status = el("div", "list-row");
  status.append(statusDot(todo.status));
  card.append(status);
  card.append(el("p", "summary-line", summaryText(todo.description) || "説明は登録されていません。"));
  const meta = el("div", "meta-row");
  meta.append(
    el("span", "meta-pill", `種類: ${labelFor(todo.kind) || "なし"}`),
    el("span", "meta-pill", `優先度: ${labelFor(todo.priority) || "なし"}`),
    el("span", "meta-pill", `発生元: ${todo.source_task_id || labelFor(todo.source_type) || "なし"}`),
    el("span", "meta-pill", `作成: ${formatDateTime(todo.created_at) || "不明"}`)
  );
  card.append(meta);
  return card;
};

const todoTechnicalMetaCard = todo => {
  const card = el("div", "card detail-card technical-meta");
  card.append(el("h3", "", "補助情報"));
  [
    `TODO ID: ${todo.id}`,
    `発生元種別: ${labelFor(todo.source_type) || "なし"}`,
    `発生元タスク: ${todo.source_task_id || "なし"}`,
    `ロードマップ commitment: ${todo.roadmap_commitment_id || "なし"}`,
    `ロードマップ revision: ${todo.roadmap_revision_id || "なし"}`,
    `変換先タスク: ${todo.converted_task_id || "なし"}`,
    `整理日時: ${formatDateTime(todo.triaged_at) || "なし"}`,
    `整理理由: ${todo.triage_reason || "なし"}`
  ].forEach(line => card.append(el("div", "muted", line)));
  return card;
};

const renderTaskDetail = async taskId => {
  const data = await api(`/api/tasks/${encodeURIComponent(taskId)}`);
  clear(app);
  const back = el("button", "", "タスク一覧");
  back.addEventListener("click", renderTasks);
  const detail = el("div", "task-detail");
  const header = el("div", "task-detail-header");
  header.append(el("h2", "", data.task.title), el("div", "muted technical-meta", `タスク ID: ${data.task.id}`));
  detail.append(header);
  detail.append(humanSummaryCard(data));
  detail.append(acceptanceCard(data.task.acceptance_criteria || []));
  detail.append(collapsibleTextCard("説明", data.task.description || "説明はありません。"));
  detail.append(technicalMetaCard(data));
  detail.append(recordSection("検証履歴", data.verification_history, renderVerificationRun));
  detail.append(recordSection("レビュー結果", data.review_results, renderReviewResult));
  detail.append(recordSection("レビュー指摘", data.review_findings, renderFinding));
  detail.append(recordSection("失敗ログ", data.failure_logs, renderFailure));
  detail.append(recordSection("遷移イベント", data.transition_events, renderTransition));
  app.append(back, detail);
};

const humanSummaryCard = data => {
  const task = data.task;
  const card = el("div", "card human-summary");
  card.append(el("h3", "", "概要"));
  const status = el("div", "list-row");
  status.append(statusDot(task.status));
  card.append(status);
  card.append(el("p", "summary-line", summaryText(task.description) || "説明は登録されていません。"));
  const meta = el("div", "meta-row");
  meta.append(
    el("span", "meta-pill", `種別: ${labelFor(task.task_type) || "なし"}`),
    el("span", "meta-pill", `リスク: ${labelFor(task.risk_level) || "なし"}`),
    el("span", "meta-pill", `モード: ${labelFor(task.mode) || "通常"}`),
    el("span", "meta-pill", `作成: ${formatDateTime(task.created_at) || "不明"}`),
    el("span", "meta-pill", `検証: ${(data.verification_history || []).length} 件`),
    el("span", "meta-pill", `レビュー: ${(data.review_results || []).length} 件`),
    el("span", "meta-pill", `指摘: ${(data.review_findings || []).length} 件`),
    el("span", "meta-pill", `失敗ログ: ${(data.failure_logs || []).length} 件`)
  );
  card.append(meta);
  return card;
};

const acceptanceCard = criteria => {
  const card = el("div", "card detail-card");
  card.append(el("h3", "", "受け入れ条件"));
  const items = normalizeAcceptanceCriteria(criteria);
  if (!items.length) {
    card.append(el("p", "muted", "受け入れ条件はありません。"));
    return card;
  }
  const list = el("ul", "checklist");
  items.forEach(item => list.append(checklistItem(item)));
  card.append(list);
  card.append(collapsibleRawText("詳細を表示", criteria.join("\\n")));
  return card;
};

const normalizeAcceptanceCriteria = criteria => {
  const items = [];
  (criteria || []).forEach(item => {
    const normalized = String(item || "").replace(/\\r\\n/g, "\\n").trim();
    if (!normalized) return;
    const bulletized = normalized.replace(/\\s+-\\s+/g, "\\n- ");
    const parts = bulletized.split("\\n").map(part => part.trim()).filter(Boolean);
    if (parts.length > 1) {
      parts.forEach(part => items.push(part));
    } else {
      items.push(normalized);
    }
  });
  return items;
};

const checklistItem = item => {
  const text = String(item || "").replace(/^[-*]\\s*/, "");
  const done = /^\\[[xX]\\]\\s*/.test(text);
  const clean = text.replace(/^\\[[ xX]\\]\\s*/, "");
  const row = el("li");
  const checkbox = document.createElement("input");
  checkbox.type = "checkbox";
  checkbox.disabled = true;
  checkbox.checked = done;
  row.append(checkbox, el("span", "", clean || "未記入"));
  return row;
};

const collapsibleTextCard = (title, text) => {
  const card = el("div", "card detail-card");
  card.append(el("h3", "", title));
  card.append(el("p", "text-preview", summaryText(text) || "内容はありません。"));
  card.append(collapsibleRawText("詳細を表示", text));
  return card;
};

const collapsibleRawText = (title, text) => {
  const details = document.createElement("details");
  const summary = document.createElement("summary");
  summary.textContent = title;
  details.append(summary);
  details.append(el("pre", "", text || ""));
  return details;
};

const technicalMetaCard = data => {
  const task = data.task;
  const dbPathText = dbPath.textContent || (state.overview && state.overview.project ? state.overview.project.db_path : "");
  const card = el("div", "card detail-card technical-meta");
  card.append(el("h3", "", "補助情報"));
  const lines = [
    `タスク ID: ${task.id}`,
    `プロジェクト ID: ${task.project_id || ""}`,
    dbPathText ? `DB パス: ${dbPathText}` : "",
    `ロードマップ commitment: ${task.roadmap_commitment_id || "なし"}`,
    `ロードマップ item: ${task.roadmap_item_id || "なし"}`
  ].filter(Boolean);
  lines.forEach(line => card.append(el("div", "muted", line)));
  return card;
};

const summaryText = text => {
  const normalized = String(text || "").replace(/\\s+/g, " ").trim();
  if (!normalized) return "";
  return normalized.length > 220 ? `${normalized.slice(0, 220)}...` : normalized;
};

const recordSection = (title, records, renderer) => {
  const wrap = el("div", "record-section");
  wrap.append(el("h3", "", title));
  if (!records || !records.length) {
    wrap.append(el("div", "empty compact", "記録はありません。"));
    return wrap;
  }
  records.forEach(record => wrap.append(renderer(record)));
  return wrap;
};

const recordCard = (title, meta, body) => {
  const card = el("div", "record-card");
  card.append(el("div", "record-title", title));
  if (meta) card.append(el("div", "muted", meta));
  if (body) card.append(body);
  return card;
};

const renderVerificationRun = run => {
  const body = el("div");
  body.append(el("div", "list-row", `状態: ${labelFor(run.status)} / 終了コード: ${run.exit_code ?? "なし"} / 時間切れ: ${run.timed_out ? "あり" : "なし"}`));
  if (run.stdout && run.stdout.length) body.append(textPreview("標準出力", run.stdout));
  if (run.stderr && run.stderr.length) body.append(textPreview("標準エラー", run.stderr));
  return recordCard(run.command || run.id, `${formatDateTime(run.created_at)} · ${run.id}`, body);
};

const renderReviewResult = result => {
  const body = el("div");
  body.append(el("div", "list-row", `判定: ${labelFor(result.verdict)} / レビュアー: ${result.reviewer || ""}`));
  if (result.summary) body.append(el("div", "muted", result.summary));
  if (result.body_md && result.body_md.length) body.append(textPreview("本文", result.body_md));
  return recordCard(result.id, formatDateTime(result.created_at), body);
};

const renderFinding = finding => {
  const place = [finding.file_path, finding.line].filter(Boolean).join(":");
  const body = el("div");
  body.append(el("div", "list-row", `重要度: ${labelFor(finding.severity)} / 状態: ${labelFor(finding.status)} / 完了阻止: ${finding.blocking ? "あり" : "なし"}`));
  if (place) body.append(el("div", "muted", place));
  if (finding.description) body.append(el("div", "muted", finding.description));
  return recordCard(finding.title || finding.id, formatDateTime(finding.created_at), body);
};

const renderFailure = failure => {
  const body = el("div");
  body.append(el("div", "list-row", `カテゴリ: ${labelFor(failure.category)} / 重要度: ${labelFor(failure.severity)} / 状態: ${labelFor(failure.status)}`));
  if (failure.message) body.append(el("div", "muted", failure.message));
  if (failure.resolution_note) body.append(el("div", "muted", `解決メモ: ${failure.resolution_note}`));
  return recordCard(failure.id, formatDateTime(failure.created_at), body);
};

const renderTransition = event => {
  const body = el("div");
  if (event.summary) body.append(el("div", "list-row", event.summary.split(" -> ").map(labelFor).join(" -> ")));
  if (event.type) body.append(el("div", "muted", `種別: ${labelFor(event.type)}`));
  return recordCard(labelFor(event.title || event.id), formatDateTime(event.created_at), body);
};

const textPreview = (title, payload) => {
  const details = document.createElement("details");
  const summary = document.createElement("summary");
  summary.textContent = `${title} (${payload.length || 0} 文字${payload.truncated ? ", 省略あり" : ""})`;
  details.append(summary);
  details.append(el("pre", "", payload.preview || ""));
  return details;
};

const renderTimeline = async () => {
  const data = await api("/api/timeline");
  clear(app);
  app.append(el("h2", "", "履歴"));
  const timeline = el("div", "timeline");
  for (const event of data.events || []) {
    const card = el("div", "card event");
    card.append(el("div", "muted", formatDateTime(event.created_at)), el("h3", "", event.title || event.type));
    card.append(el("p", "muted", `${event.entity_id || ""} ${event.summary || ""}`));
    timeline.append(card);
  }
  if (!timeline.children.length) timeline.append(el("div", "empty", "履歴イベントはありません。"));
  app.append(timeline);
};

const routes = { overview: renderOverview, analytics: renderAnalytics, tasks: renderTasks, todos: renderTodos, timeline: renderTimeline };

const activate = async name => {
  navButtons.forEach(button => button.classList.toggle("active", button.dataset.view === name));
  try {
    await routes[name]();
  } catch (error) {
    renderError(error);
  }
};

navButtons.forEach(button => button.addEventListener("click", () => activate(button.dataset.view)));
activate("overview");
"""
