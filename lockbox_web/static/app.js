// Lockbox web page: one WebSocket to the service, three tabs, shared state.
//
// Messages from the service (see ../README.md):
//   hello, status, reading, log, scan_progress, scan, scan_failed, ok, error
// Commands to the service: {cmd: "run_scan" | "lock" | "release" | "set_output"
//   | "apply_settings" | "engage" | "autolock_now" | "set_name", ...}

const $ = (id) => document.getElementById(id);

let socket = null;
let page = null;          // static description: signals, settings form
let status = null;        // latest status from the service
let scan = null;          // latest scan result
let readings = [];        // [{t, values}]
const dirtyFields = new Set();   // settings edited here but not applied yet
let scanInputsDirty = false;
let monitorRedrawQueued = false;

const plots = {
  scanErr: new LinePlot($("scan-err-plot"), {xLabel: "Control out, physical (V)", yLabel: "Error (V)"}),
  scanOut: new LinePlot($("scan-out-plot"), {xLabel: "Control out, physical (V)", yLabel: "Outputs (V)"}),
  monitorOut: new LinePlot($("monitor-out-plot"), {xLabel: "Time (s)", yLabel: "Voltage (V)"}),
  monitorErr: new LinePlot($("monitor-err-plot"), {xLabel: "Time (s)", yLabel: "Error (V)"}),
};

// ---------------------------------------------------------------- connection
function connect() {
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  socket = new WebSocket(`${scheme}://${location.host}/ws`);
  socket.onopen = () => setConnection(true);
  socket.onclose = () => { setConnection(false); setTimeout(connect, 2000); };
  socket.onmessage = (event) => handleMessage(JSON.parse(event.data));
}

function setConnection(connected) {
  const pill = $("connection");
  pill.textContent = connected ? "Connected" : "Disconnected - retrying";
  pill.className = "pill " + (connected ? "good" : "bad");
}

function send(command) {
  if (!socket || socket.readyState !== WebSocket.OPEN) {
    showError("Not connected to the lockbox service.");
    return;
  }
  showError(null);
  socket.send(JSON.stringify(command));
}

function handleMessage(message) {
  switch (message.type) {
    case "hello":
      page = message.page;
      readings = message.readings;
      buildSettingsForm();
      $("log").textContent = "";
      message.log.forEach(appendLog);
      applyStatus(message.status);
      scan = message.scan;
      drawScan();
      queueMonitorRedraw();
      break;
    case "status":
      applyStatus(message.status);
      break;
    case "reading":
      readings.push(message);
      queueMonitorRedraw();
      break;
    case "log":
      appendLog(message);
      break;
    case "scan_progress":
      $("scan-progress").value = message.fraction;
      break;
    case "scan":
      scan = message.scan;
      $("scan-progress").value = 1;
      drawScan();
      break;
    case "scan_failed":
      $("scan-progress").value = 0;
      break;
    case "ok":
      // The service accepted the settings this browser sent, so nothing here is unapplied any more.
      if (message.cmd === "apply_settings") {
        clearDirty();
        if (status) applyStatus(status);
      }
      break;
    case "error":
      showError(message.message);
      break;
  }
}

function showError(text) {
  const banner = $("error-banner");
  banner.textContent = text || "";
  banner.classList.toggle("hidden", !text);
}

function appendLog(entry) {
  const log = $("log");
  const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 4;
  const time = new Date(entry.t * 1000).toLocaleTimeString();
  log.textContent += `[${time}] ${entry.message}\n`;
  if (atBottom) log.scrollTop = log.scrollHeight;
}

// -------------------------------------------------------------------- status
function applyStatus(newStatus) {
  status = newStatus;
  $("hostname").textContent = status.hostname;
  document.title = `Lockbox - ${status.name || status.hostname}`;
  $("mqtt-topic").textContent = status.mqtt_topic;
  if (document.activeElement !== $("name-input")) $("name-input").value = status.name;

  // PID: on when B/C/D are LOW; off (caps shorted, PID blocked) when they're HIGH.
  const pid = $("pid-status");
  if (status.pid_running === true) {
    pid.textContent = "PID: on";
    pid.className = "pill good";
  } else if (status.pid_running === false) {
    pid.textContent = "PID: off (B/C/D high)";
    pid.className = "pill warn";
  } else {
    pid.textContent = "PID: mixed (B/C/D disagree)";
    pid.className = "pill bad";
  }
  pid.title = "DAC channels B, C, D: low = PID running, high = caps shorted / PID blocked";

  const autolock = $("autolock-status");
  autolock.textContent = status.engaged ? `Autolock: engaged (${status.mode_name})` : "Autolock: disengaged";
  autolock.className = "pill " + (status.engaged ? "good" : "");
  $("toggle-engage").textContent = status.engaged ? "Disengage Autolock" : "Engage Autolock";
  const signs = {positive: "positive", negative: "negative", both: "either"};
  $("autolock-uses").textContent = `Uses the saved settings: ${status.mode_name}`
    + (status.settings.AUTOLOCK_MODE === "zero_crossing" ? `, ${signs[status.settings.CROSSING_SIGN]} crossing` : "")
    + " (change on the Settings tab)";

  const busy = $("busy-status");
  const working = status.scanning ? "Manual scan running"
    : status.autolock_running ? "Autolock scan running"
    : status.lock_acquiring ? "Locking (following the crossing)" : null;
  busy.textContent = working || "";
  busy.className = "pill warn" + (working ? "" : " hidden");
  // Stop buttons: next to Run Scan, and in the header so a scan can be stopped from any tab.
  $("stop-scan").disabled = !working;
  $("stop-scan-header").classList.toggle("hidden", !working);

  // Controls that would fight a running scan or an engaged autolock.
  const blocked = Boolean(status.busy);
  for (const id of ["run-scan", "lock-selected", "set-output", "autolock-now"]) {
    $(id).disabled = blocked;
    $(id).title = blocked ? status.busy : "";
  }
  $("toggle-engage").disabled = !status.engaged && blocked;

  // Fill in settings the user isn't in the middle of editing.
  for (const [name, value] of Object.entries(status.settings)) {
    const input = document.querySelector(`[data-setting="${name}"]`);
    if (input && !dirtyFields.has(name)) setInputValue(input, value);
  }
  if (!scanInputsDirty) {
    $("scan-min").value = status.settings.SCAN_MIN_VOLTAGE;
    $("scan-max").value = status.settings.SCAN_MAX_VOLTAGE;
    $("scan-points").value = status.settings.MANUAL_SCAN_POINTS;
    $("scan-adaptive").checked = status.settings.ADAPTIVE_SCAN;
    $("scan-points").disabled = $("scan-adaptive").checked;
    // Last-used lock mode and crossing sign; until a scan has been run, the Settings tab's.
    setRadio("scan-mode", status.settings.MANUAL_SCAN_MODE || status.settings.AUTOLOCK_MODE);
    setRadio("scan-sign", status.settings.MANUAL_SCAN_SIGN || status.settings.CROSSING_SIGN);
  }
  if (document.activeElement !== $("output-voltage")) {
    $("output-voltage").value = status.settings.CONSTANT_OUTPUT_VOLTAGE;
  }
  drawScan();          // range bars follow the settings
  queueMonitorRedraw();
}

// ------------------------------------------------------------- settings form
function setInputValue(input, value) {
  if (input.type === "checkbox") input.checked = Boolean(value);
  else input.value = value;
}

function getInputValue(input) {
  return input.type === "checkbox" ? input.checked : input.value;
}

function buildSettingsForm() {
  const container = $("settings-groups");
  container.textContent = "";
  for (const group of page.settings_groups) {
    const fieldset = document.createElement("fieldset");
    const legend = document.createElement("legend");
    legend.textContent = group.title;
    fieldset.appendChild(legend);
    for (const field of group.fields) {
      const row = document.createElement("div");
      row.className = "field";
      const label = document.createElement("label");
      label.textContent = field.label;
      let input;
      if (field.kind === "choice") {
        input = document.createElement("select");
        for (const choice of field.choices) {
          const option = document.createElement("option");
          option.value = choice.value;
          option.textContent = choice.label;
          input.appendChild(option);
        }
      } else {
        input = document.createElement("input");
        input.type = field.kind === "bool" ? "checkbox" : field.kind === "text" ? "text" : "number";
        if (input.type === "number") input.step = field.kind === "int" ? "1" : "any";
      }
      input.dataset.setting = field.name;
      input.id = `setting-${field.name}`;
      label.htmlFor = input.id;
      input.addEventListener("input", () => markDirty(field.name, row));
      input.addEventListener("change", () => markDirty(field.name, row));
      row.append(label, input);
      fieldset.appendChild(row);
    }
    container.appendChild(fieldset);
  }
}

function markDirty(name, row) {
  dirtyFields.add(name);
  row.classList.add("dirty");
  $("settings-dirty").classList.remove("hidden");
}

function clearDirty() {
  dirtyFields.clear();
  document.querySelectorAll(".field.dirty").forEach((row) => row.classList.remove("dirty"));
  $("settings-dirty").classList.add("hidden");
}

function collectSettings() {
  const values = {};
  document.querySelectorAll("[data-setting]").forEach((input) => {
    values[input.dataset.setting] = getInputValue(input);
  });
  return values;
}

// --------------------------------------------------------------------- plots
function rangeBar(name, lo, hi) {
  const signal = page.signals[name];
  return {lo, hi, color: signal.color, label: `${signal.label} ${signal.range_name}`};
}

// DC error is plotted on its own right-hand axis, so a ~10 mV error signal on
// the left axis isn't flattened by a DC error of several volts.
function dcErrorSeries(series) {
  return {...series, alpha: 0.6, axis: "right", label: `${page.signals.dc_err.label} (right axis)`};
}

function dcErrorAxis() {
  return {label: `${page.signals.dc_err.label} (V)`, color: page.signals.dc_err.color};
}

function dcErrorLockRangeBar(s) {
  return {...rangeBar("dc_err", s.DC_ERR_SAFE_MIN, s.DC_ERR_SAFE_MAX), axis: "right"};
}

function drawScan() {
  if (!page || !status) return;
  const s = status.settings;
  const outBars = [
    rangeBar("slow_output", s.SLOW_OUTPUT_SAFE_MIN, s.SLOW_OUTPUT_SAFE_MAX),
    rangeBar("fast_output", s.FAST_OUTPUT_SAFE_MIN, s.FAST_OUTPUT_SAFE_MAX),
  ];
  // Same right margin on both scan plots (room for 2 range bars + the error plot's
  // right axis), so their control-out axes line up.
  const rightMargin = 16 + 2 * 11 + 54;
  if (!scan) {
    plots.scanErr.draw({series: [], rightMargin});
    plots.scanOut.draw({series: [], rangeBars: outBars, rightMargin});
    $("candidates").textContent = "";
    return;
  }
  const zeroCrossing = scan.mode === "zero_crossing";
  const searched = zeroCrossing ? "error" : "dc_err";
  const markerY = zeroCrossing ? 0 : (s.DC_ERR_SAFE_MIN + s.DC_ERR_SAFE_MAX) / 2;
  const line = (name) => ({
    label: page.signals[name].label, color: page.signals[name].color,
    x: scan.voltages, y: scan.trace[name], markers: true,
  });
  // A fast sweep measures only the searched signal; the others aren't in the trace.
  const measured = (name) => name in scan.trace;

  // Rebuild the candidate list only when the scan changes, so a selection survives status updates.
  const list = $("candidates");
  if (list.dataset.scanTime !== String(scan.t)) {
    list.dataset.scanTime = String(scan.t);
    list.textContent = "";
    scan.candidates.forEach((candidate, i) => {
      const option = document.createElement("option");
      option.value = i;
      option.textContent = `${i + 1}. ${candidate.label}`;
      list.appendChild(option);
    });
  }
  const selected = list.value === "" ? -1 : Number(list.value);   // highlighted on the plot

  // Error on the left axis, DC error on its own right axis: a ~10 mV error
  // signal would otherwise be flattened by a DC error of several volts.
  // Candidates (and the smoothed curve) go on the axis of the signal searched.
  // "Show DC error in plots" off hides DC error's trace, axis and lock-range bar;
  // in DC error mode the candidate shading and numbers stay.
  const showDc = s.SHOW_DC_ERROR;
  const searchedAxis = zeroCrossing ? "left" : "right";
  const smoothed = {label: `${page.signals[searched].label} (smoothed)`, color: "#000", x: scan.voltages,
                    y: scan.smoothed, width: 1.2, axis: searchedAxis};
  // Both plots span the scan's range even when they have nothing to draw.
  const xRange = [Math.min(...scan.voltages), Math.max(...scan.voltages)];
  const errSeries = [
    ...(measured("error") ? [{...line("error"), alpha: 0.6}] : []),
    ...(showDc && measured("dc_err") ? [dcErrorSeries(line("dc_err"))] : []),
    ...(zeroCrossing || showDc ? [smoothed] : []),
  ];
  plots.scanErr.draw({
    series: errSeries,
    xRange,
    message: errSeries.length ? null
      : "DC error was the only signal measured, and it's hidden (Settings: Show DC error in plots).",
    rightAxis: dcErrorAxis(),
    hLines: zeroCrossing ? [0] : [],
    vLines: scan.candidates.map((c, i) => ({
      ...(zeroCrossing || showDc
        ? {x: c.lock_voltage, y: markerY, axis: searchedAxis}
        : {x: c.lock_voltage}),                 // no DC scale shown: number at the top
      label: String(i + 1), highlight: i === selected,
    })),
    vSpans: zeroCrossing ? []
      : scan.candidates.map((c, i) => ({start: c.start, end: c.end, highlight: i === selected})),
    rangeBars: !zeroCrossing && showDc ? [dcErrorLockRangeBar(s)] : [],
    rightMargin,
  });
  const outputs = page.output_signals.filter(measured);
  plots.scanOut.draw({
    series: outputs.map(line), rangeBars: outBars, rightMargin, xRange,
    message: outputs.length ? null
      : `Outputs not measured: this fast sweep read only ${page.signals[searched].label}.`,
  });
}

function queueMonitorRedraw() {
  if (monitorRedrawQueued) return;
  monitorRedrawQueued = true;
  setTimeout(() => { monitorRedrawQueued = false; drawMonitor(); }, 200);  // at most ~5 redraws a second
}

function drawMonitor() {
  if (!page || !status || readings.length === 0) return;
  const s = status.settings;
  const newest = readings[readings.length - 1].t;
  const cutoff = newest - s.LIVE_MONITOR_TIME_SPAN;
  while (readings.length && readings[0].t < cutoff) readings.shift();

  const last = readings[readings.length - 1];
  $("latest-reading").textContent = Object.entries(last.values)
    .map(([name, value]) => `${page.signals[name].label} = ${value.toFixed(4)} V`).join("     ");
  if (!$("tab-monitor").classList.contains("active")) return;  // nothing to draw on a hidden tab

  const x = readings.map((r) => r.t - newest);   // seconds before now
  const line = (name) => ({
    label: page.signals[name].label, color: page.signals[name].color,
    x, y: readings.map((r) => r.values[name]),
  });
  const xRange = [-s.LIVE_MONITOR_TIME_SPAN, 0];
  plots.monitorOut.draw({
    series: page.output_signals.map(line), xRange,
    rangeBars: [
      rangeBar("slow_output", s.SLOW_OUTPUT_SAFE_MIN, s.SLOW_OUTPUT_SAFE_MAX),
      rangeBar("fast_output", s.FAST_OUTPUT_SAFE_MIN, s.FAST_OUTPUT_SAFE_MAX),
    ],
  });
  // Error on the left axis, DC error (if shown) on its own right axis.
  const showDc = s.SHOW_DC_ERROR;
  plots.monitorErr.draw({
    series: [line("error"), ...(showDc ? [dcErrorSeries(line("dc_err"))] : [])],
    rightAxis: dcErrorAxis(),
    xRange,
    rangeBars: showDc && s.AUTOLOCK_MODE === "dc_err_range" ? [dcErrorLockRangeBar(s)] : [],
  });
}

// ------------------------------------------------------------------ controls
function setRadio(name, value) {
  const radio = document.querySelector(`input[name="${name}"][value="${value}"]`);
  if (radio) radio.checked = true;
}

function radioValue(name) {
  return document.querySelector(`input[name="${name}"]:checked`).value;
}

document.querySelectorAll("#tabs button").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("active", b === button));
    document.querySelectorAll(".tab").forEach((tab) => {
      tab.classList.toggle("active", tab.id === `tab-${button.dataset.tab}`);
    });
    drawScan();
    drawMonitor();
  });
});

for (const id of ["scan-min", "scan-max", "scan-points", "scan-adaptive"]) {
  $(id).addEventListener("input", () => { scanInputsDirty = true; });
}
document.querySelectorAll('input[name="scan-mode"], input[name="scan-sign"]').forEach((radio) => {
  radio.addEventListener("change", () => { scanInputsDirty = true; });
});
$("scan-adaptive").addEventListener("change", () => {
  $("scan-points").disabled = $("scan-adaptive").checked;
});

$("run-scan").addEventListener("click", () => {
  $("scan-progress").value = 0;
  scanInputsDirty = false;
  send({
    cmd: "run_scan",
    v_min: $("scan-min").value, v_max: $("scan-max").value, points: $("scan-points").value,
    adaptive: $("scan-adaptive").checked, mode: radioValue("scan-mode"), sign: radioValue("scan-sign"),
  });
});
$("candidates").addEventListener("change", drawScan);   // highlight the selected candidate
$("lock-selected").addEventListener("click", () => {
  if ($("candidates").value === "") { showError("Select a lock candidate from the list first."); return; }
  send({cmd: "lock", index: Number($("candidates").value)});
});
$("release").addEventListener("click", () => send({cmd: "release"}));
for (const id of ["stop-scan", "stop-scan-header"]) {
  $(id).addEventListener("click", () => send({cmd: "stop_scan"}));
}
$("set-output").addEventListener("click", () => send({cmd: "set_output", voltage: $("output-voltage").value}));

$("apply-settings").addEventListener("click", () => send({cmd: "apply_settings", values: collectSettings()}));

// Engage / Run Autolock Now (on the Scan & Lock tab) use the saved settings; they
// don't apply the Settings form. Point out unapplied edits so nobody is surprised.
function warnIfSettingsUnapplied() {
  if (dirtyFields.size) {
    showError("Note: the Settings tab has unapplied changes - autolock is using the saved settings. "
              + "Click Apply & Save Settings to use them.");
  }
}
$("toggle-engage").addEventListener("click", () => {
  if (status.engaged) { send({cmd: "engage", on: false}); return; }
  send({cmd: "engage", on: true});
  warnIfSettingsUnapplied();
});
$("autolock-now").addEventListener("click", () => {
  send({cmd: "autolock_now"});
  warnIfSettingsUnapplied();
});

function saveName() { send({cmd: "set_name", name: $("name-input").value}); $("name-input").blur(); }
$("save-name").addEventListener("click", saveName);
$("name-input").addEventListener("keydown", (event) => { if (event.key === "Enter") saveName(); });

// ------------------------------------------------------------------ resizing
// Each .resize-handle resizes the element named by data-target: dragging down
// makes it taller, or - with data-grow="up", for the log under its handle -
// dragging up does. Sizes are remembered per browser; double-click resets.
const MIN_RESIZE_HEIGHT = 80;

function makeResizable(handle) {
  const target = $(handle.dataset.target);
  const growsUp = handle.dataset.grow === "up";
  const key = `lockbox.height.${handle.dataset.target}`;
  try {
    const saved = Number(localStorage.getItem(key));
    if (saved) target.style.height = `${saved}px`;
  } catch (_) { /* storage unavailable: use the default size */ }

  handle.addEventListener("pointerdown", (down) => {
    down.preventDefault();
    handle.setPointerCapture(down.pointerId);
    handle.classList.add("dragging");
    const startY = down.clientY;
    const startHeight = target.getBoundingClientRect().height;
    const move = (event) => {
      const dy = event.clientY - startY;
      const height = startHeight + (growsUp ? -dy : dy);
      const limit = window.innerHeight * 0.85;
      target.style.height = `${Math.round(Math.max(MIN_RESIZE_HEIGHT, Math.min(limit, height)))}px`;
    };
    const up = () => {
      handle.classList.remove("dragging");
      handle.removeEventListener("pointermove", move);
      handle.removeEventListener("pointerup", up);
      try { localStorage.setItem(key, parseInt(target.style.height, 10)); } catch (_) { /* not remembered */ }
    };
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", up);
  });

  handle.addEventListener("dblclick", () => {
    target.style.height = "";
    try { localStorage.removeItem(key); } catch (_) { /* nothing saved */ }
  });
}
document.querySelectorAll(".resize-handle").forEach(makeResizable);

connect();
