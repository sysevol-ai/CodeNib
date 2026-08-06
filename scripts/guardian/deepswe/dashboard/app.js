const state = {
  data: null,
  metric: "pass_rate",
  trialFilter: "all",
};

const metricLabels = {
  pass_rate: "Pass rate",
  avg_f2p: "Avg F2P",
  avg_p2p: "Avg P2P",
  avg_partial: "Avg partial",
  avg_total_cost_usd: "Avg cost",
  sum_total_cost_usd: "Total cost",
  avg_main_input_tokens: "Avg input tokens",
  avg_main_output_tokens: "Avg output tokens",
  n_trials: "Valid trials",
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
  if (["pass_rate", "avg_f2p", "avg_p2p", "avg_partial"].includes(metric)) {
    return "pct";
  }
  if (metric.includes("cost")) return "cost";
  return "number";
}

function heatColor(value, metric) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "#f3f4f6";
  const n = Number(value);
  if (["pass_rate", "avg_f2p", "avg_p2p", "avg_partial"].includes(metric)) {
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
  return ["solo", "guardian"];
}

function settings() {
  const seen = new Set();
  const rows = [];
  for (const row of state.data.summary) {
    const key = `${row.task}:::${row.model}:::${row.reasoning_effort}`;
    if (!seen.has(key)) {
      seen.add(key);
      rows.push({
        key,
        task: row.task,
        model: row.model,
        reasoning_effort: row.reasoning_effort,
      });
    }
  }
  return rows.sort((a, b) => a.key.localeCompare(b.key));
}

function settingLabel(row) {
  return `${row.task} · ${row.model} · ${row.reasoning_effort}`;
}

function renderHeatmap() {
  const host = document.getElementById("heatmap");
  const cols = columns();
  const rows = settings();
  const metric = state.metric;
  const lookup = new Map(
    state.data.summary.map((r) => [
      `${r.task}:::${r.model}:::${r.reasoning_effort}:::${r.baseline}`,
      r,
    ]),
  );

  host.innerHTML = "";
  const grid = document.createElement("div");
  grid.className = "heat-grid";
  grid.style.gridTemplateColumns = `minmax(420px, 1.6fr) repeat(${cols.length}, minmax(160px, 1fr))`;

  grid.appendChild(cell("Task / Model / Effort", "heat-head"));
  for (const col of cols) grid.appendChild(cell(col, "heat-head"));

  for (const rowInfo of rows) {
    grid.appendChild(cell(settingLabel(rowInfo), "heat-task"));
    for (const col of cols) {
      const row = lookup.get(`${rowInfo.key}:::${col}`);
      const value = row ? row[metric] : null;
      const el = document.createElement("div");
      el.className = "heat-cell";
      el.style.background = heatColor(value, metric);
      el.innerHTML = `<span class="cell-main">${fmt(value, metricKind(metric))}</span><span class="cell-sub">${row ? `${row.n_trials}/${row.n_recorded_trials} valid` : ""}</span>`;
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
    [
      "Task",
      "Model",
      "Effort",
      "Solo",
      "Guardian",
      "Delta",
      "Solo avg F2P",
      "Guardian avg F2P",
      "Solo avg P2P",
      "Guardian avg P2P",
      "Solo avg partial",
      "Guardian avg partial",
      "Solo valid/recorded",
      "Guardian valid/recorded",
      "Guardian avg cost",
    ],
    state.data.tasks.map((r) => [
      r.task,
      r.model,
      r.reasoning_effort,
      fmt(r.solo_pass_rate, "pct"),
      fmt(r.guardian_pass_rate, "pct"),
      fmt(r.delta_pass_rate, "pct"),
      fmt(r.solo_avg_f2p, "pct"),
      fmt(r.guardian_avg_f2p, "pct"),
      fmt(r.solo_avg_p2p, "pct"),
      fmt(r.guardian_avg_p2p, "pct"),
      fmt(r.solo_avg_partial, "pct"),
      fmt(r.guardian_avg_partial, "pct"),
      `${fmt(r.solo_trials)}/${fmt(r.solo_recorded_trials)}`,
      `${fmt(r.guardian_trials)}/${fmt(r.guardian_recorded_trials)}`,
      fmt(r.guardian_avg_cost_usd, "cost"),
    ]),
    [
      false,
      false,
      false,
      true,
      true,
      true,
      true,
      true,
      true,
      true,
      true,
      true,
      true,
      true,
      true,
    ],
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
      "Model",
      "Effort",
      "Baseline",
      "Job",
      "Reward",
      "F2P",
      "P2P",
      "Cost",
      "Main tokens",
      "Guardian tokens",
    ],
    rows.map((r) => [
      r.task,
      r.model,
      r.reasoning_effort,
      r.baseline,
      r.job_id,
      `<span class="${Number(r.reward) === 1 ? "status-pass" : "status-fail"}">${fmt(r.reward)}</span>`,
      `${fmt(r.f2p, "pct")} (${fmt(r.f2p_passed)}/${fmt(r.f2p_total)})`,
      `${fmt(r.p2p, "pct")} (${fmt(r.p2p_passed)}/${fmt(r.p2p_total)})`,
      fmt(r.total_cost_usd, "cost"),
      fmt(r.main_input_tokens),
      fmt((Number(r.guardian_prompt_tokens) || 0) + (Number(r.guardian_completion_tokens) || 0)),
    ]),
    [false, false, false, false, false, true, true, true, true, true],
  );
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
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 8000);
  const response = await fetch(`data.json?v=${Date.now()}`, {
    cache: "no-store",
    signal: controller.signal,
  });
  clearTimeout(timeout);
  if (!response.ok) {
    throw new Error(`data.json returned HTTP ${response.status}`);
  }
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
