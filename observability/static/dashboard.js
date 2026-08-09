"use strict";

const POLL_INTERVAL_MS = 10000;
const API_ROOT = "/api/v1/kpi/";
let activeController = null;
let refreshTimer = null;

function element(id) {
  return document.getElementById(id);
}

function setText(id, value) {
  const target = element(id);
  if (target) {
    target.textContent = value === null || value === undefined || value === "" ? "—" : String(value);
  }
}

function pathValue(source, paths) {
  for (const path of paths) {
    let current = source;
    for (const part of path.split(".")) {
      current = current && typeof current === "object" ? current[part] : undefined;
    }
    if (current !== undefined && current !== null) {
      return current;
    }
  }
  return null;
}

function formatNumber(value, suffix = "", digits = 1) {
  if (value === null || value === undefined || value === "") {
    return "—";
  }
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) {
    return "—";
  }
  return `${numeric.toFixed(digits)}${suffix}`;
}

function formatInteger(value) {
  if (value === null || value === undefined || value === "") {
    return "—";
  }
  const numeric = Number(value);
  return Number.isFinite(numeric) ? Math.round(numeric).toLocaleString() : "—";
}

function formatRate(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) {
    return "—";
  }
  const percent = Math.abs(numeric) <= 1 ? numeric * 100 : numeric;
  return `${percent.toFixed(1)}%`;
}

function formatLatencyPercentiles(source, metrics, percentiles) {
  const values = percentiles.map((percentile) =>
    pathValue(
      source,
      metrics.flatMap((metric) => [
        `${metric}.${percentile}`,
        `${metric}.${percentile}_ms`,
        `${metric}_${percentile}`,
        `${metric}_${percentile}_ms`,
      ]),
    ),
  );
  if (values.every((value) => value === null)) {
    return "—";
  }
  return values.map((value) => formatNumber(value, " ms", 0)).join(" / ");
}

function buildFilterQuery() {
  const query = new URLSearchParams();
  const fields = [
    ["window", "filter-window"],
    ["mode", "filter-mode"],
    ["locality", "filter-locality"],
    ["provider", "filter-provider"],
    ["model", "filter-model"],
    ["route", "filter-route"],
    ["outcome", "filter-outcome"],
    ["network_tier", "filter-network-tier"],
  ];
  for (const [name, id] of fields) {
    const control = element(id);
    const value = control && "value" in control ? control.value.trim() : "";
    if (value) {
      query.set(name, value);
    }
  }
  return query;
}

async function request(resource, baseQuery, extra, signal) {
  const query = new URLSearchParams(baseQuery);
  for (const [name, value] of Object.entries(extra || {})) {
    query.set(name, String(value));
  }
  const suffix = query.size ? `?${query.toString()}` : "";
  const response = await fetch(`${API_ROOT}${resource}${suffix}`, {
    headers: { Accept: "application/json" },
    cache: "no-store",
    signal,
  });
  if (!response.ok) {
    throw new Error(`KPI endpoint returned ${response.status}`);
  }
  return response.json();
}

function flatten(source, prefix = "", rows = [], depth = 0) {
  if (rows.length >= 80 || depth > 5) {
    return rows;
  }
  if (Array.isArray(source)) {
    source.slice(0, 30).forEach((value, index) => {
      flatten(value, prefix ? `${prefix}.${index + 1}` : String(index + 1), rows, depth + 1);
    });
    return rows;
  }
  if (source && typeof source === "object") {
    for (const [name, value] of Object.entries(source)) {
      flatten(value, prefix ? `${prefix}.${name}` : name, rows, depth + 1);
      if (rows.length >= 80) {
        break;
      }
    }
    return rows;
  }
  if (source !== null && source !== undefined) {
    rows.push([prefix || "value", source]);
  }
  return rows;
}

function displayLabel(value) {
  return String(value)
    .replaceAll("_", " ")
    .replaceAll(".", " · ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase())
    .replace(/^Rejection Reasons/, "Rejected Candidates");
}

function displayValue(value, name = "") {
  if (typeof value === "boolean") {
    return value ? "Yes" : "No";
  }
  if (typeof value === "number") {
    if (/(^|\.)timestamp(_ms)?$/.test(name)) {
      const timestamp = name.endsWith("_ms") ? value : value * 1000;
      return new Date(timestamp).toLocaleString();
    }
    if (/(^|\.)(success_rate|probe_success_ratio)$/.test(name)) {
      return formatRate(value);
    }
    return Number.isInteger(value)
      ? value.toLocaleString("en-US")
      : value.toLocaleString("en-US", { maximumFractionDigits: 2 });
  }
  return String(value);
}

function statisticsWithSamples(source) {
  if (!source || typeof source !== "object" || Array.isArray(source)) {
    return {};
  }
  return Object.fromEntries(
    Object.entries(source).filter(([, statistics]) => {
      if (!statistics || typeof statistics !== "object" || Array.isArray(statistics)) {
        return false;
      }
      return Number(statistics.count) > 0;
    }),
  );
}

function groupedStatisticsWithSamples(source) {
  if (!source || typeof source !== "object" || Array.isArray(source)) {
    return {};
  }
  return Object.fromEntries(
    Object.entries(source)
      .map(([name, statistics]) => [name, statisticsWithSamples(statistics)])
      .filter(([, statistics]) => Object.keys(statistics).length > 0),
  );
}

function renderTable(id, sources, emptyId) {
  const body = element(id);
  if (!body) {
    return;
  }
  body.replaceChildren();
  const rows = [];
  for (const source of sources) {
    flatten(source, "", rows);
  }
  for (const [name, value] of rows) {
    const row = document.createElement("tr");
    const label = document.createElement("td");
    const measurement = document.createElement("td");
    label.textContent = displayLabel(name);
    measurement.textContent = displayValue(value, name);
    row.append(label, measurement);
    body.append(row);
  }
  const empty = element(emptyId);
  if (empty) {
    empty.hidden = rows.length > 0;
  }
}

function orderSeries(points) {
  return [...points].sort((left, right) => {
    const leftTimestamp = pathValue(left, ["timestamp_ms", "timestamp"]);
    const rightTimestamp = pathValue(right, ["timestamp_ms", "timestamp"]);
    const leftValue = typeof leftTimestamp === "number" ? leftTimestamp : Date.parse(leftTimestamp);
    const rightValue =
      typeof rightTimestamp === "number" ? rightTimestamp : Date.parse(rightTimestamp);
    return (Number.isFinite(leftValue) ? leftValue : 0) -
      (Number.isFinite(rightValue) ? rightValue : 0);
  });
}

function seriesCandidates(payload) {
  if (Array.isArray(payload)) {
    return orderSeries(payload);
  }
  if (!payload || typeof payload !== "object") {
    return [];
  }
  for (const key of ["points", "series", "data", "samples", "timeline", "buckets"]) {
    if (Array.isArray(payload[key])) {
      return orderSeries(payload[key]);
    }
    if (payload[key] && typeof payload[key] === "object") {
      const nested = Object.values(payload[key]).find((value) => Array.isArray(value));
      if (nested) {
        return orderSeries(nested);
      }
    }
  }
  return [];
}

function numericValue(point, paths) {
  if (typeof point === "number") {
    return point;
  }
  if (!point || typeof point !== "object") {
    return null;
  }
  for (const key of [
    ...paths,
    "value",
    "avg",
    "average",
    "mean",
    "p50",
    "p50_ms",
  ]) {
    const raw = pathValue(point, [key]);
    if (raw === null) {
      continue;
    }
    const candidate = Number(raw);
    if (Number.isFinite(candidate)) {
      return candidate;
    }
  }
  return null;
}

function prepareCanvas(canvas) {
  const ratio = Math.max(1, window.devicePixelRatio || 1);
  const width = Math.max(320, canvas.clientWidth || 700);
  const height = 250;
  canvas.width = Math.round(width * ratio);
  canvas.height = Math.round(height * ratio);
  const context = canvas.getContext("2d");
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  context.clearRect(0, 0, width, height);
  return { context, width, height };
}

function drawLineChart(canvasId, emptyId, payload, color, valuePaths) {
  const canvas = element(canvasId);
  const empty = element(emptyId);
  if (!(canvas instanceof HTMLCanvasElement)) {
    return;
  }
  const values = seriesCandidates(payload)
    .map((point) => numericValue(point, valuePaths))
    .filter(Number.isFinite);
  const { context, width, height } = prepareCanvas(canvas);
  const padding = 34;
  context.strokeStyle = "rgba(157, 176, 170, 0.18)";
  context.lineWidth = 1;
  for (let row = 0; row <= 4; row += 1) {
    const y = padding + ((height - padding * 2) * row) / 4;
    context.beginPath();
    context.moveTo(padding, y);
    context.lineTo(width - padding, y);
    context.stroke();
  }
  if (!values.length) {
    if (empty) {
      empty.hidden = false;
    }
    return;
  }
  if (empty) {
    empty.hidden = true;
  }
  const minimum = Math.min(...values);
  const maximum = Math.max(...values);
  const range = maximum - minimum || 1;
  context.strokeStyle = color;
  context.lineWidth = 2.5;
  context.lineJoin = "round";
  context.beginPath();
  values.forEach((value, index) => {
    const x = padding + ((width - padding * 2) * index) / Math.max(1, values.length - 1);
    const y = height - padding - ((value - minimum) / range) * (height - padding * 2);
    if (index === 0) {
      context.moveTo(x, y);
    } else {
      context.lineTo(x, y);
    }
  });
  context.stroke();
  context.fillStyle = color;
  context.font = "12px ui-monospace, monospace";
  context.fillText(maximum.toFixed(1), 4, padding + 4);
  context.fillText(minimum.toFixed(1), 4, height - padding + 4);
}

function drawMultiLineChart(canvasId, emptyId, definitions) {
  const canvas = element(canvasId);
  const empty = element(emptyId);
  if (!(canvas instanceof HTMLCanvasElement)) {
    return;
  }
  const lines = definitions.map((definition) => ({
    ...definition,
    values: seriesCandidates(definition.payload)
      .map((point) => numericValue(point, definition.valuePaths))
      .filter(Number.isFinite),
  }));
  const allValues = lines.flatMap((line) => line.values);
  const { context, width, height } = prepareCanvas(canvas);
  const padding = 34;
  context.strokeStyle = "rgba(157, 176, 170, 0.18)";
  context.lineWidth = 1;
  for (let row = 0; row <= 4; row += 1) {
    const y = padding + ((height - padding * 2) * row) / 4;
    context.beginPath();
    context.moveTo(padding, y);
    context.lineTo(width - padding, y);
    context.stroke();
  }
  if (!allValues.length) {
    if (empty) {
      empty.hidden = false;
    }
    return;
  }
  if (empty) {
    empty.hidden = true;
  }
  const minimum = Math.min(...allValues);
  const maximum = Math.max(...allValues);
  const range = maximum - minimum || 1;
  lines.forEach((line, lineIndex) => {
    if (!line.values.length) {
      return;
    }
    context.strokeStyle = line.color;
    context.lineWidth = 2.5;
    context.lineJoin = "round";
    context.beginPath();
    line.values.forEach((value, index) => {
      const x = padding +
        ((width - padding * 2) * index) / Math.max(1, line.values.length - 1);
      const y = height - padding - ((value - minimum) / range) * (height - padding * 2);
      if (index === 0) {
        context.moveTo(x, y);
      } else {
        context.lineTo(x, y);
      }
    });
    context.stroke();
    context.fillStyle = line.color;
    context.font = "11px ui-monospace, monospace";
    const legendWidth = (width - padding * 2) / Math.max(1, lines.length);
    context.fillText(line.label, padding + lineIndex * legendWidth, 16);
  });
  context.fillStyle = "#9db0aa";
  context.font = "12px ui-monospace, monospace";
  context.fillText(maximum.toFixed(1), 4, padding + 4);
  context.fillText(minimum.toFixed(1), 4, height - padding + 4);
}

function drawRoutingChart(payload) {
  const canvas = element("routing-chart");
  const empty = element("routing-empty");
  if (!(canvas instanceof HTMLCanvasElement)) {
    return;
  }
  const candidates = flatten(payload).filter((row) => typeof row[1] === "number").slice(0, 10);
  const { context, width, height } = prepareCanvas(canvas);
  if (!candidates.length) {
    if (empty) {
      empty.hidden = false;
    }
    return;
  }
  if (empty) {
    empty.hidden = true;
  }
  const maximum = Math.max(...candidates.map((row) => Number(row[1])), 1);
  const left = Math.min(190, width * 0.38);
  const rowHeight = (height - 25) / candidates.length;
  candidates.forEach(([name, value], index) => {
    const y = index * rowHeight + 7;
    const barWidth = ((width - left - 24) * Number(value)) / maximum;
    context.fillStyle = index % 2 ? "#4dd8c5" : "#ffc857";
    context.fillRect(left, y, Math.max(2, barWidth), Math.max(4, rowHeight - 9));
    context.fillStyle = "#9db0aa";
    context.font = "11px ui-monospace, monospace";
    const label = displayLabel(name).slice(0, 25);
    context.fillText(label, 5, y + Math.max(12, rowHeight - 12));
  });
}

function renderOverview(health, summary, network, resources, providers) {
  const aggregateProviders = Array.isArray(providers.providers) ? providers.providers : [];
  const observedAvailability = (locality) => {
    const rows = aggregateProviders.filter((item) => item.locality === locality);
    if (!rows.length) {
      return null;
    }
    return rows.some((item) => Number(item.success_count) > 0);
  };
  const reportedHealth = pathValue(health, ["status", "assistant.status", "health"]);
  const hasRuntimeHealth = reportedHealth
    && String(reportedHealth).toLowerCase() !== "unknown";
  const local = hasRuntimeHealth
    ? pathValue(health, ["local.available", "local_available"])
    : observedAvailability("local");
  const remote = hasRuntimeHealth
    ? pathValue(health, ["remote.available", "remote_available"])
    : observedAvailability("remote");
  const observedAttempts = aggregateProviders.reduce(
    (total, item) => total + (Number(item.attempt_count) || 0),
    0,
  );
  const observedSuccesses = aggregateProviders.reduce(
    (total, item) => total + (Number(item.success_count) || 0),
    0,
  );
  const healthValue = (hasRuntimeHealth ? reportedHealth : null) || (
    observedAttempts > 0
      ? (observedSuccesses === observedAttempts ? "healthy" : "degraded")
      : null
  );
  const networkState = pathValue(network, [
    "current.network_state",
    "network_state",
    "current.connectivity",
    "connectivity",
  ]);
  const networkScore = pathValue(network, [
    "current.network_quality_score",
    "network_quality_score",
    "current.quality_score",
    "quality_score",
  ]);
  const runtimeProviders = Array.isArray(health.providers) ? health.providers : [];
  const providerRows = runtimeProviders.length ? runtimeProviders : aggregateProviders;
  const providerState = providerRows
    .slice(0, 3)
    .map((item) => {
      const provider = pathValue(item, ["provider"]);
      const model = pathValue(item, ["model"]);
      let state = pathValue(item, ["circuit_state", "circuit.state"]);
      if (!state) {
        const attempts = Number(pathValue(item, ["attempt_count"]));
        const failures = Number(pathValue(item, ["failure_count"]));
        state = Number.isFinite(attempts) && attempts > 0
          ? (failures === 0 ? "successful" : "failures observed")
          : "not observed";
      }
      const identity = [provider, model].filter(Boolean).join("/") || "provider";
      return `${identity} · ${state}`;
    })
    .join("; ");
  setText("card-health", healthValue || "Unknown");
  setText(
    "card-availability",
    local === null && remote === null
      ? "—"
      : `Local ${local ? "ready" : "unavailable"} · Remote ${remote ? "ready" : "unavailable"}`,
  );
  setText(
    "card-network",
    networkState === null
      ? "Unknown"
      : `${networkState}${networkScore === null ? "" : ` · ${formatNumber(networkScore, "", 2)}`}`,
  );
  setText("card-provider", providerState || "—");
  setText("card-requests", formatInteger(pathValue(summary, ["requests", "request_count", "totals.requests"])));
  setText("card-success", formatRate(pathValue(summary, ["success_rate", "rates.success"])));
  setText("card-fallback", formatRate(pathValue(summary, ["fallback_rate", "rates.fallback"])));
  setText("card-errors", formatRate(pathValue(summary, ["error_rate", "rates.error"])));
  setText(
    "card-e2e",
    formatLatencyPercentiles(summary, ["end_to_end_ms", "end_to_end"], ["p50", "p95", "p99"]),
  );
  setText(
    "card-first-token",
    formatLatencyPercentiles(summary, ["first_token_ms", "first_token"], ["p50", "p95"]),
  );
  setText(
    "card-first-audio",
    formatLatencyPercentiles(
      summary,
      ["actual_first_audio_ms", "first_audio_ms", "actual_first_audio", "first_audio"],
      ["p50", "p95"],
    ),
  );
  const cpu = pathValue(resources, ["current.cpu_percent", "cpu_percent"]);
  const gpu = pathValue(resources, ["current.gpu_percent", "gpu_percent"]);
  const memory = pathValue(resources, ["current.memory_percent", "memory_percent"]);
  const temperature = pathValue(resources, [
    "current.cpu_temperature_c",
    "current.gpu_temperature_c",
    "cpu_temperature_c",
    "gpu_temperature_c",
    "current.temperature_c",
    "temperature_c",
  ]);
  const power = pathValue(resources, ["current.power_w", "power_w"]);
  const resourceText = [
    `CPU ${formatNumber(cpu, "%")}`,
    `GPU ${formatNumber(gpu, "%")}`,
    `RAM ${formatNumber(memory, "%")}`,
    `Temp ${formatNumber(temperature, " °C")}`,
    `Power ${formatNumber(power, " W")}`,
  ].join(" · ");
  setText("card-resources", resourceText);
  const healthy = String(healthValue || "").toLowerCase();
  const indicator = element("live-indicator");
  if (indicator) {
    indicator.className = `status-dot ${healthy === "healthy" || healthy === "ok" ? "status-good" : "status-unknown"}`;
  }
}

function payloadHasMeasurements(payload) {
  if (Array.isArray(payload)) {
    return payload.length > 0;
  }
  if (!payload || typeof payload !== "object") {
    return false;
  }
  const requests = pathValue(payload, ["requests", "request_count", "totals.requests"]);
  if (Number(requests) > 0) {
    return true;
  }
  return Object.values(payload).some((value) => Array.isArray(value) && value.length > 0);
}

function render(data) {
  const health = data.health || {};
  const summary = data.summary || {};
  const routing = data.routing || {};
  const latency = data.latency || {};
  const providers = data.providers || {};
  const network = data.network || {};
  const resources = data.resources || {};
  const timeseries = data.timeseries || {};
  const firstTokenTimeseries = data.firstTokenTimeseries || {};
  const firstAudioTimeseries = data.firstAudioTimeseries || {};

  renderOverview(health, summary, network, resources, providers);
  drawRoutingChart(routing);
  drawMultiLineChart(
    "latency-chart",
    "latency-chart-empty",
    [
      {
        label: "End to end",
        color: "#ffc857",
        payload: timeseries,
        valuePaths: ["metrics.end_to_end_ms.mean"],
      },
      {
        label: "First token",
        color: "#4dd8c5",
        payload: firstTokenTimeseries,
        valuePaths: ["metrics.first_token_ms.mean"],
      },
      {
        label: "First audio",
        color: "#d8a7ff",
        payload: firstAudioTimeseries,
        valuePaths: ["metrics.actual_first_audio_ms.mean"],
      },
    ],
  );
  drawLineChart("network-chart", "network-empty", network, "#4dd8c5", [
    "network_quality_score",
  ]);
  drawLineChart("resource-chart", "resource-empty", resources, "#71db8b", [
    "cpu_percent",
  ]);
  renderTable("routing-distribution", [
    { model_tiers: pathValue(routing, ["model_tiers"]) || {} },
    { complexity_scores: pathValue(routing, ["complexity_scores"]) || {} },
  ]);
  renderTable("routing-table", [routing, providers], "routing-table-empty");
  renderTable("latency-table", [
    { statistics: pathValue(latency, ["statistics"]) || {} },
    { histogram: pathValue(latency, ["histogram"]) || [] },
  ]);
  renderTable("latency-breakdown", [
    statisticsWithSamples(pathValue(latency, ["breakdown"]) || {}),
  ]);
  renderTable("network-table", [network]);
  renderTable("resource-table", [
    { current: pathValue(resources, ["current"]) || {} },
    { metrics: statisticsWithSamples(pathValue(resources, ["metrics"]) || {}) },
    {
      by_locality: groupedStatisticsWithSamples(
        pathValue(resources, ["by_locality"]) || {},
      ),
    },
    {
      sample_count: pathValue(resources, ["sample_count"]),
      throttled_sample_count: pathValue(resources, ["throttled_sample_count"]),
    },
  ]);

  const hasData = [
    summary,
    routing,
    latency,
    network,
    resources,
    timeseries,
    firstTokenTimeseries,
    firstAudioTimeseries,
  ].some(payloadHasMeasurements);
  const message = element("dashboard-message");
  if (message) {
    message.textContent = hasData
      ? "Live content-free metrics for the selected window."
      : "No metrics have been recorded yet. The dashboard will update automatically.";
  }
}

async function refresh() {
  if (activeController) {
    activeController.abort();
  }
  activeController = new AbortController();
  const filters = buildFilterQuery();
  const calls = {
    health: request("health", new URLSearchParams(), {}, activeController.signal),
    summary: request("summary", filters, {}, activeController.signal),
    routing: request("routing", filters, {}, activeController.signal),
    latency: request("latency", filters, {}, activeController.signal),
    providers: request("providers", filters, {}, activeController.signal),
    network: request("network", filters, {}, activeController.signal),
    resources: request("resources", filters, {}, activeController.signal),
    timeseries: request(
      "timeseries",
      filters,
      { metric: "end_to_end_ms", points: 180 },
      activeController.signal,
    ),
    firstTokenTimeseries: request(
      "timeseries",
      filters,
      { metric: "first_token_ms", points: 180 },
      activeController.signal,
    ),
    firstAudioTimeseries: request(
      "timeseries",
      filters,
      { metric: "actual_first_audio_ms", points: 180 },
      activeController.signal,
    ),
  };
  const names = Object.keys(calls);
  const results = await Promise.allSettled(Object.values(calls));
  const data = {};
  const failures = [];
  results.forEach((result, index) => {
    if (result.status === "fulfilled") {
      data[names[index]] = result.value;
    } else if (result.reason && result.reason.name !== "AbortError") {
      failures.push(names[index]);
    }
  });
  if (Object.keys(data).length) {
    render(data);
    const timestamp = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    setText(
      "last-updated",
      failures.length
        ? `Updated ${timestamp} · unavailable: ${failures.join(", ")}`
        : `Updated ${timestamp}`,
    );
  } else if (failures.length) {
    setText("last-updated", "Dashboard data is temporarily unavailable");
    const indicator = element("live-indicator");
    if (indicator) {
      indicator.className = "status-dot status-bad";
    }
  }
}

function scheduleRefresh() {
  if (refreshTimer) {
    window.clearInterval(refreshTimer);
  }
  refreshTimer = window.setInterval(refresh, POLL_INTERVAL_MS);
}

document.addEventListener("DOMContentLoaded", () => {
  const form = element("filters");
  if (form) {
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      refresh();
      scheduleRefresh();
    });
  }
  window.addEventListener("resize", () => {
    window.clearTimeout(window.__heliosResizeTimer);
    window.__heliosResizeTimer = window.setTimeout(refresh, 180);
  });
  refresh();
  scheduleRefresh();
});
