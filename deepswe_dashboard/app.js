const state = {
  data: null,
  metric: "pass_rate",
  trialFilter: "all",
};

const metricLabels = {
  pass_rate: "Pass rate",
  avg_total_cost_usd: "Avg cost",
  sum_total_cost_usd: "Total cost",
  avg_main_input_tokens: "Avg input tokens",
  avg_main_output_tokens: "Avg output tokens",
  n_trials: "Trials",
};

function fmt(value, kind = "number") {
  if (value === null || value === undefined || Number.isNaN(value)) return "";
  const n = Number(value);
  if (!Number.isFinite(n)) return String(value);
  if (kind === "pct") return `${(n * 100).toFixed(0)}%`;
  if (kind === "cost") return `$${n.toFixed(3)}`;
  if (Math.abs(n) >= 1000000) return `${(n / 1000000).toFixed(2)}M`;
  if (Math.abs(n) >= 1000) return `${(n / 1000).toFixed(1)}K`;
  if (Number.isInteger(n)) return String(n);
  return n.toFixed(3);
}

function metricKind(metric) {
  if (metric === "pass_rate") return "pct";
  if (metric.includes("cost")) return "cost";
  return "number";
}

function heatColor(value, metric) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "#f3f4f6";
  const n = Number(value);
  if (metric === "pass_rate") {
    const hue = 8 + Math.max(0, Math.min(1, n)) * 135;
    return `hsl(${hue} 64% 86%)`;
  }
  if (metric.includes("cost")) {
    const x = Math.min(1, n / 1.5);
    return `hsl(${45 - x * 35} 75% 88%)`;
  }
  const x = Math.min(1, n / 1000000);
  return `hsl(${215 - x * 80} 72% 88%)`;
}

function columns() {
  const seen = new Set();
  const cols = [];
  for (const row of state.data.summary) {
    const key = row.label;
    if (!seen.has(key)) {
      seen.add(key);
      cols.push(key);
    }
  }
  return cols;
}

function tasks() {
  return [...new Set(state.data.summary.map((r) => r.task))].sort();
}

function renderHeatmap() {
  const host = document.getElementById("heatmap");
  const cols = columns();
  const ts = tasks();
  const metric = state.metric;
  const lookup = new Map(state.data.summary.map((r) => [`${r.task}:::${r.label}`, r]));

  host.innerHTML = "";
  const grid = document.createElement("div");
  grid.className = "heat-grid";
  grid.style.gridTemplateColumns = `minmax(260px, 1.3fr) repeat(${cols.length}, minmax(160px, 1fr))`;

  grid.appendChild(cell("Task", "heat-head"));
  for (const col of cols) grid.appendChild(cell(col, "heat-head"));

  for (const task of ts) {
    grid.appendChild(cell(task, "heat-task"));
    for (const col of cols) {
      const row = lookup.get(`${task}:::${col}`);
      const value = row ? row[metric] : null;
      const el = document.createElement("div");
      el.className = "heat-cell";
      el.style.background = heatColor(value, metric);
      el.innerHTML = `<span class="cell-main">${fmt(value, metricKind(metric))}</span><span class="cell-sub">${row ? `${row.n_trials} trial(s)` : ""}</span>`;
      grid.appendChild(el);
    }
  }
  host.appendChild(grid);
  document.getElementById("heatmap-note").textContent = metricLabels[metric] || metric;
}

function cell(text, className) {
  const el = document.createElement("div");
  el.className = className;
  el.textContent = text;
  return el;
}

function renderTasks() {
  const table = document.getElementById("tasks-table");
  table.innerHTML = tableHtml(
    ["Task", "Solo", "Guardian", "Delta", "Solo n", "Guardian n", "Guardian avg cost"],
    state.data.tasks.map((r) => [
      r.task,
      fmt(r.solo_pass_rate, "pct"),
      fmt(r.guardian_pass_rate, "pct"),
      fmt(r.delta_pass_rate, "pct"),
      fmt(r.solo_trials),
      fmt(r.guardian_trials),
      fmt(r.guardian_avg_cost_usd, "cost"),
    ]),
    [false, true, true, true, true, true, true],
  );
}

function filteredTrials() {
  const f = state.trialFilter;
  return state.data.trials.filter((r) => {
    if (f === "passed") return Number(r.reward) === 1;
    if (f === "failed") return Number(r.reward) !== 1;
    if (f === "guardian") return r.baseline === "guardian";
    if (f === "solo") return r.baseline === "solo";
    return true;
  });
}

function renderTrials() {
  const rows = filteredTrials();
  document.getElementById("trial-count").textContent = `${rows.length} trial(s)`;
  const table = document.getElementById("trials-table");
  table.innerHTML = tableHtml(
    [
      "Task",
      "Baseline",
      "Job",
      "Reward",
      "F2P",
      "P2P",
      "Cost",
      "Main tokens",
      "Guardian tokens",
      "Findings",
      "Artifacts",
    ],
    rows.map((r) => [
      r.task,
      r.baseline,
      r.job_id,
      `<span class="${Number(r.reward) === 1 ? "status-pass" : "status-fail"}">${fmt(r.reward)}</span>`,
      `${fmt(r.f2p, "pct")} (${fmt(r.f2p_passed)}/${fmt(r.f2p_total)})`,
      `${fmt(r.p2p, "pct")} (${fmt(r.p2p_passed)}/${fmt(r.p2p_total)})`,
      fmt(r.total_cost_usd, "cost"),
      fmt(r.main_input_tokens),
      fmt((Number(r.guardian_prompt_tokens) || 0) + (Number(r.guardian_completion_tokens) || 0)),
      fmt(r.guardian_findings),
      artifactLinks(r),
    ]),
    [false, false, false, true, true, true, true, true, true, true, false],
  );
}

function artifactLinks(r) {
  const links = [];
  if (r.pier_result_json) links.push(link("result", r.pier_result_json));
  if (r.codex_log) links.push(link("codex", r.codex_log));
  if (r.latest_guardian_findings) links.push(link("findings", r.latest_guardian_findings));
  if (r.latest_guardian_log) links.push(link("g-log", r.latest_guardian_log));
  return links.join(" ");
}

function link(label, path) {
  return `<a href="file://${path}">${label}</a>`;
}

function tableHtml(headers, rows, numeric) {
  const head = `<thead><tr>${headers.map((h) => `<th>${h}</th>`).join("")}</tr></thead>`;
  const body = rows
    .map(
      (row) =>
        `<tr>${row
          .map((v, i) => `<td class="${numeric[i] ? "num" : ""}">${v ?? ""}</td>`)
          .join("")}</tr>`,
    )
    .join("");
  return `${head}<tbody>${body}</tbody>`;
}

function render() {
  renderHeatmap();
  renderTasks();
  renderTrials();
}

async function init() {
  const response = await fetch("data.json", { cache: "no-store" });
  state.data = await response.json();
  document.getElementById("subtitle").textContent = `${state.data.output_root} · ${state.data.trials.length} fixed-slot trial(s) · ${state.data.counted_job_ids.join(", ")}`;
  document.getElementById("metric-select").addEventListener("change", (event) => {
    state.metric = event.target.value;
    renderHeatmap();
  });
  document.getElementById("trial-filter").addEventListener("change", (event) => {
    state.trialFilter = event.target.value;
    renderTrials();
  });
  render();
}

init().catch((error) => {
  document.getElementById("subtitle").textContent = `Failed to load data.json: ${error}`;
});
