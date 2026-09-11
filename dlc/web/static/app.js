  /*# ───────────────────────────────────────────────────────────────────
  *#  Digital Lab Coach front end. Loads Cytoscape, handles multi-file
  *#  uploads, renders signal-flow graph, drives summary + hover popup.  
  *# ──────────────────────────────────────────────────────────────────#*/

const GRAPH_LIBS_OK =
  typeof cytoscape !== "undefined" && typeof window.cytoscapeDagre !== "undefined";
if (GRAPH_LIBS_OK) {
  cytoscape.use(window.cytoscapeDagre);
} else {
  console.error(
    "DLC: graph libraries (cytoscape/dagre) failed to load from the CDN. " +
    "The signal-flow graph is disabled, but the rest of the UI still works.",
  );
}

const FAMILY_COLORS = {
  "io-in":      "#cfe5ff",
  "io-out":     "#ffdcb3",
  "gate":       "#b9e4c1",
  "arith":      "#f4b9b9",
  "mux":        "#d8c4ef",
  "splitter":   "#f1ea9a",
  "storage":    "#d3d3d3",
  "tunnel":     "#f7d7e8",
  "subcircuit": "#ffc1c1",
  "const":      "#dfdfdf",
  "clock":      "#dfdfdf",
  "switch":     "#c9e8e4",
  "other":      "#e9ecef",
};

const CY_STYLE = [
  {
    selector: "node",
    style: {
      "shape": "round-rectangle",
      "background-color": (n) => FAMILY_COLORS[n.data("family")] || "#e9ecef",
      "border-color": "#444",
      "border-width": 1,
      "label": "data(label)",
      "color": "#1f2933",
      "font-size": 10,
      "text-wrap": "wrap",
      "text-max-width": 90,
      "text-valign": "center",
      "text-halign": "center",
      "width": 70,
      "height": 38,
      "padding": "4px",
    },
  },
    // Nodes that carry a parametric glyph (gate/mux/decoder/splitter/…): draw
  // the inline-SVG as the node body, sized to it, with the label underneath.
  {
    selector: "node[shape_svg]",
    style: {
      "shape": "rectangle",
      "background-image": "data(shape_svg)",
      "background-fit": "contain",
      "background-opacity": 0,
      "border-width": 0,
      "width": "data(shape_w)",
      "height": "data(shape_h)",
      "text-valign": "bottom",
      "text-halign": "center",
      "text-margin-y": 2,
      "font-size": 8,
      "text-max-width": 80,
    },
  },
  { selector: "node.faded", style: { "opacity": 0.22 } },
  {
    selector: "edge",
    style: {
      "width": 1.5,
      "line-color": "#9aa1ab",
      "target-arrow-color": "#9aa1ab",
      "target-arrow-shape": "triangle",
      "curve-style": "bezier",
      "arrow-scale": 0.9,
    },
  },
    // Anchor an edge to the exact port stub on a glyph node (when graph_export
  // provided one), so arrows meet the ports instead of the bounding box.
  { selector: "edge[se]", style: { "source-endpoint": "data(se)" } },
  { selector: "edge[te]", style: { "target-endpoint": "data(te)" } },
  // Switch-level net wiring: plain wires with no arrowhead, like Digital
  { selector: "edge[wire]", style: { "target-arrow-shape": "none" } },
  { selector: "edge.faded", style: { "opacity": 0.1 } },
  {
    selector: "edge.highlight",
    style: {
      "line-color": "#2563eb",
      "target-arrow-color": "#2563eb",
      "width": 2.5,
    },
  },
  // Net-id overlay (the "net IDs" toggle on both graph panes): every wire
  // shows the id of the net it belongs to — the same ids the hover popups
  // and Mode A's hint evidence speak. Label pixels are part of the edge,
  // so hovering the number highlights/pops exactly like the wire itself.
  {
    selector: "edge.show-netid[net_id]",
    style: {
      "label": "data(net_id)",
      "font-size": 10,
      "font-weight": 700,
      "color": "#334155",
      "text-background-color": "#ffffff",
      "text-background-opacity": 0.85,
      "text-background-padding": 2,
      "text-background-shape": "roundrectangle",
      "text-border-color": "#9aa1ab",
      "text-border-width": 1,
      "text-border-opacity": 0.6,
    },
  },
  {
    selector: "node.issue-target",
    style: {
      "border-color": "#dc2626",
      "border-width": 4,
      "background-color": "#fee2e2",
    },
  },
  /*# ───────────────────────────────────────────────────────────────────
  *#  Signal-flow coloring
  *# ──────────────────────────────────────────────────────────────────#*/
  // 1-bit wires: bright green = 1, dark green = 0 (no number, like Digital).
  {
    selector: "edge.sig-hi",
    style: {
      "line-color": "#22c55e", "target-arrow-color": "#22c55e", "width": 3,
      "opacity": 1,
    },
  },
  {
    selector: "edge.sig-lo",
    style: {
      "line-color": "#166534", "target-arrow-color": "#166534", "width": 2,
      "opacity": 1,
    },
  },
  // multi-bit bus: blue + the hex value shown on the wire.
  {
    selector: "edge.sig-bus",
    style: {
      "line-color": "#2563eb", "target-arrow-color": "#2563eb", "width": 3,
      "label": "data(sigLabel)", "font-size": 9, "color": "#1e3a8a",
      "text-background-color": "#ffffff", "text-background-opacity": 0.9,
      "text-background-padding": 2, "text-rotation": "autorotate",
      "opacity": 1,
    },
  },
  // signal-carrying wire the evaluator could not resolve (e.g. clock, or a
  // register cone with no clock stepped yet): gray, no value.
  { selector: "edge.sig-none", style: { "line-color": "#cbd5e1", "target-arrow-color": "#cbd5e1", "width": 1 } },
  // dim everything not touched by the active row while a row is selected.
  { selector: "edge.sig-dim", style: { "opacity": 0.15 } },
  // Blue blink for a clicked net-id reference in Mode A (l3FlashNet) —
  // LAST on purpose: cytoscape's cascade is stylesheet order, and the
  // flash must win over the green/gray signal-flow classes too.
  {
    selector: "edge.netid-flash",
    style: {
      "line-color": "#2563eb",
      "target-arrow-color": "#2563eb",
      "width": 3.5,
      "opacity": 1,
      "color": "#1d4ed8",
      "text-background-color": "#dbeafe",
    },
  },
  {
    // fix walkthrough: yellow marks laid by the animation player
    selector: "node.l3-fix-mark",
    style: {
      "border-width": 4,
      "border-color": "#eab308",
      "background-color": "#fef9c3",
      "background-opacity": 0.9,
    },
  },
  {
    selector: "edge.l3-fix-mark-edge",
    style: {
      "line-color": "#eab308",
      "target-arrow-color": "#eab308",
      "width": 5,
      "opacity": 1,
    },
  },
  { selector: "node.sig-dim", style: { "opacity": 0.35 } },
  // failed-row output: red ring + expected/found chip in the label.
  {
    selector: "node.sig-mismatch",
    style: {
      "border-color": "#dc2626", "border-width": 4,
      "background-color": "#fee2e2", "label": "data(label)",
    },
  },
  {
    selector: "node.walk-done",
    style: { "border-color": "#c4b5fd", "border-width": 3 },
  },
  {
    selector: "node.walk-focus",
    style: {
      "border-color": "#a855f7", "border-width": 5,
      "background-color": "#f5d0fe", "background-opacity": 0.95,
    },
  },
  {
    selector: "edge.walk-edge",
    style: {
      "line-color": "#d946ef", "target-arrow-color": "#d946ef",
      "width": 5, "opacity": 1,
    },
  },
  /*# ───────────────────────────────────────────────────────────────────
  *#  Wire hover focus
  *# ──────────────────────────────────────────────────────────────────#*/
  // Defined LAST so it wins over the signal-flow opacities above. Hovering a
  // wire fades everything else so an overlapping value stays readable.
  { selector: "edge.hover-dim", style: { "opacity": 0.06 } },
  { selector: "node.hover-dim", style: { "opacity": 0.18 } },
  {
    selector: "edge.wire-focus",
    style: {
      "opacity": 1, "width": 4, "z-index": 9999,
      "font-size": 12, "color": "#0f172a", "font-weight": "bold",
      "text-background-color": "#ffffff", "text-background-opacity": 1,
      "text-background-padding": 3,
    },
  },
    // the Clock glyph is clickable to tick through rows — flag it while hinting
  {
    selector: "node.clock-hint",
    style: { "border-color": "#f59e0b", "border-width": 3, "border-opacity": 1 },
  },
];

//DOM 

const MAX_FILES = 16;

const fileInput     = document.getElementById("file-input");
const fileSelect    = document.getElementById("file-select");
const prevBtn       = document.getElementById("prev-btn");
const nextBtn       = document.getElementById("next-btn");
const clearBtn      = document.getElementById("clear-btn");
const placeholder   = document.getElementById("placeholder");
const summaryEl     = document.getElementById("summary");
const issuesListEl  = document.getElementById("issues-list");
const issuesCountsEl= document.getElementById("issues-counts");
const testsStatusEl = document.getElementById("tests-status");
const testsResultsEl= document.getElementById("tests-results");
const testsProgressEl     = document.getElementById("tests-progress");
const testsProgressTextEl = document.getElementById("tests-progress-text");
const perRowToggle  = document.getElementById("perrow-toggle");
const runTestsBtn   = document.getElementById("run-tests-btn");
const testAllBtn    = document.getElementById("test-all-btn");
const testAllPanel  = document.getElementById("test-all-panel");
const testAllHeadEl = document.getElementById("test-all-headline");
const testAllListEl = document.getElementById("test-all-list");
const testAllClose  = document.getElementById("test-all-close");
const muteToggle    = document.getElementById("mute-toggle");
const popupEl       = document.getElementById("hover-popup");
const popupTitle    = document.getElementById("hover-popup-title");
const popupBody     = document.getElementById("hover-popup-body");
const jarChipBtn    = document.getElementById("jar-chip");
const jarStateEl    = document.getElementById("jar-state");
const jarModal      = document.getElementById("jar-modal");
const jarPathInput  = document.getElementById("jar-path-input");
const jarBrowseBtn  = document.getElementById("jar-browse-btn");
const jarSaveBtn    = document.getElementById("jar-save-btn");
const jarCancelBtn  = document.getElementById("jar-cancel-btn");
const jarModalMsg   = document.getElementById("jar-modal-msg");
const keyChipBtn    = document.getElementById("key-chip");
const keyStateEl    = document.getElementById("key-state");
const keyModal      = document.getElementById("key-modal");
const keyCancelBtn  = document.getElementById("key-cancel-btn");


const KEY_PROVIDERS = ["anthropic", "openai"];
const keyEls = Object.fromEntries(KEY_PROVIDERS.map((p) => [p, {
  status: document.getElementById(`key-status-${p}`),
  input:  document.getElementById(`key-input-${p}`),
  msg:    document.getElementById(`key-msg-${p}`),
}]));

const l2ModelSelect = document.getElementById("l2-model-select");
const libraryGridEl = document.getElementById("library-grid");
const cardOverlay   = document.getElementById("card-overlay");
const cardDetail    = document.getElementById("card-detail");
const goalTextarea  = document.getElementById("goal-textarea");
const goalCountEl   = document.getElementById("goal-count");
const l2LlmBtn      = document.getElementById("l2-llm-btn");
const l2StopBtn     = document.getElementById("l2-stop-btn");
const l2LlmStatus   = document.getElementById("l2-llm-status");
const l2LlmOutput   = document.getElementById("l2-llm-output");
const graderSelect  = document.getElementById("grader-model-select");
const gradeBody     = document.getElementById("grade-body");
let lastGradedSummary = null;
// Below this total, the grade panel suggests (but never auto-runs) a
// fresh Summarize attempt. The old silent auto-retry was removed: it
// re-spent tokens without consent and could replace a summary with a
// worse one.
const GRADE_HINT_THRESHOLD = 90;

let sessionId = null;

const eventLog = [];
function logEvent(kind, details = {}) {
  eventLog.push({ ts: Date.now(), kind, ...details });
}
window.dlcEventLog = eventLog;

  /*# ───────────────────────────────────────────────────────────────────
  *#  telemetry sink flush
  *# ──────────────────────────────────────────────────────────────────#*/
// Batch-persist dlcEventLog to /api/telemetry (local SQLite; see
// dlc/telemetry/sink.py) every 15s and on page hide. Fire-and-forget:
// telemetry must never affect the app, so failures are swallowed and a
// batch is marked sent when handed off (at-most-once, no duplicates).
let telemetrySentIdx = 0;
function flushTelemetry(useBeacon = false) {
  if (telemetrySentIdx >= eventLog.length) return;
  const payload = JSON.stringify({
    session_id: sessionId,
    events: eventLog.slice(telemetrySentIdx),
  });
  telemetrySentIdx = eventLog.length;
  try {
    if (useBeacon && navigator.sendBeacon) {
      navigator.sendBeacon(
        "/api/telemetry", new Blob([payload], { type: "application/json" }),
      );
    } else {
      fetch("/api/telemetry", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: payload,
        keepalive: useBeacon,
      }).catch(() => {});
    }
  } catch { /* never let telemetry break the app */ }
}
setInterval(flushTelemetry, 15000);
window.addEventListener("pagehide", () => flushTelemetry(true));

const MUTE_THRESHOLD = 3;
let mutedByUser = new Set();   
let activeIssueIdx = null;     
const testState = {};  

//Session state 
let fileObjects = [];  
let loaded      = [];
let currentIdx  = 0;
let cy          = null;
let sigActive   = null;   // {specIdx, rowIdx} of the row driving the overlay
let clockTimer  = null;   // set while the clock is ticking through rows
let clockDone   = false;  // true once ticking reached the last row (offer restart)
let drillCy     = null;   // Cytoscape instance inside the drill-in overlay
let drillPath   = [];     // component indices from the top circuit to the view


fileInput.addEventListener("change", async () => {
  if (!fileInput.files || fileInput.files.length === 0) return;
  if (!l3ConfirmNav()) { fileInput.value = ""; return; }

  const incomingNames = new Set();
  for (const f of fileInput.files) incomingNames.add(f.name);
  const keptExisting = fileObjects.filter((f) => !incomingNames.has(f.name));
  const projectedTotal = keptExisting.length + fileInput.files.length;
  if (projectedTotal > MAX_FILES) {
    alert(
      `File limit is ${MAX_FILES}. This upload would bring you to ` +
      `${projectedTotal}. Use "Clear all" to reset, or upload fewer ` +
      `files at once.`
    );
    fileInput.value = "";
    return;
  }

  for (const f of fileInput.files) {
    fileObjects = fileObjects.filter((existing) => existing.name !== f.name);
    fileObjects.push(f);
  }
  fileInput.value = "";
  returnToMain();
  await postAll();
});

clearBtn.addEventListener("click", () => {
  if (loaded.length === 0) return;
  if (!l3ConfirmNav()) return;
  if (!confirm("Clear all uploaded files and return to the dashboard?")) return;
  playClearWipe();     // decorative right-to-left "garble" wipe back to Layer 1
  returnToMain();
  resetDashboard();
});

// Decorative ~1s "digital snow storm" blowing right-to-left when Clear all
// returns the user to the Layer-1 dashboard: big/small green snowflakes mixed
// with digital glyphs, plus a few wind streaks. Purely cosmetic.
function playClearWipe() {
  const el = document.createElement("div");
  el.className = "snow-overlay";
  const glyphs = ["❄", "❅", "❆", "✳", "❄", "❄", "0", "1", "0", "＊", "＊"];
  const rand = (a, b) => a + Math.random() * (b - a);
  let html = "";
  for (let k = 0; k < 90; k++) {                    // snowflakes
    const ch = glyphs[(Math.random() * glyphs.length) | 0];
    html += `<span class="flake" style="top:${rand(-8, 100)}vh;` +
      `font-size:${rand(10, 34) | 0}px;animation-duration:${rand(0.7, 1.25).toFixed(2)}s;` +
      `animation-delay:${rand(0, 0.4).toFixed(2)}s;` +
      `--drift:${(rand(-24, 24)) | 0}px;--rot:${(rand(-540, 540)) | 0}deg">${ch}</span>`;
  }
  for (let k = 0; k < 7; k++) {                      // wind streaks
    html += `<span class="wind" style="top:${rand(0, 100)}vh;` +
      `animation-duration:${rand(0.55, 0.95).toFixed(2)}s;` +
      `animation-delay:${rand(0, 0.3).toFixed(2)}s"></span>`;
  }
  el.innerHTML = html;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 1500);
}


function resetDashboard() {
  fileObjects = [];
  loaded = [];
  currentIdx = 0;
  mutedByUser = new Set();
  activeIssueIdx = null;
  sessionId = null;
  for (const k of Object.keys(testState)) delete testState[k];
  // Tear down the signal-flow session UI too: the "click the Clock to tick"
  // HUD and any open subcircuit drill-in would otherwise linger after a clear.
  sigActive = null;
  clockDone = false;
  stopClockTick();
  hideClockHud();
  closeDrill();
  if (cy) { cy.destroy(); cy = null; }
  l3ExpireAll("clear");
  l3ResetDom();
  fileSelect.innerHTML = "<option>(no file)</option>";
  fileSelect.disabled = true;
  prevBtn.disabled = true;
  nextBtn.disabled = true;
  clearBtn.disabled = true;
  runTestsBtn.disabled = true;
  testAllBtn.disabled = true;
  testAllBtn.textContent = "Test all";
  testAllPanel.classList.add("hidden");
  placeholder.classList.remove("hidden");
  placeholder.innerHTML =
    `No circuit loaded. Add a <code>.dig</code> file from the toolbar ` +
    `above &mdash; multiple files (parent + subcircuits) supported.`;
  summaryEl.innerHTML = `<span class="muted">No file loaded.</span>`;
  issuesListEl.innerHTML = `<span class="muted">No file loaded.</span>`;
  issuesCountsEl.innerHTML = `<span class="muted">&mdash;</span>`;
  testsStatusEl.textContent = "No file loaded.";
  testsStatusEl.className = "tests-status muted";
  testsResultsEl.innerHTML = "";
  testsResultsEl.classList.add("empty");
  libraryGridEl.innerHTML = `<div class="muted">Load a circuit on the Dashboard tab to populate the library.</div>`;
  l2LibraryFilename = null;
  goalTextarea.value = "";
  goalCountEl.textContent = "0 / 500 characters";
  l2LlmStatus.textContent = "";
  l2LlmOutput.innerHTML = "";
  l2LlmOutput.classList.add("empty");
  _resetGrade();
  hidePopup();
}

muteToggle.addEventListener("change", () => {
  if (loaded.length > 0) renderIssues(loaded[currentIdx]);
});

prevBtn.addEventListener("click", () => {
  if (loaded.length === 0) return;
  if (!l3ConfirmNav()) return;
  currentIdx = (currentIdx - 1 + loaded.length) % loaded.length;
  returnToMain();
  renderCurrent();
});

nextBtn.addEventListener("click", () => {
  if (loaded.length === 0) return;
  if (!l3ConfirmNav()) return;
  currentIdx = (currentIdx + 1) % loaded.length;
  returnToMain();
  renderCurrent();
});

fileSelect.addEventListener("change", () => {
  if (!l3ConfirmNav()) { fileSelect.value = String(currentIdx); return; }
  currentIdx = parseInt(fileSelect.value, 10) || 0;
  returnToMain();
  renderCurrent();
});

async function postAll() {
  summaryEl.innerHTML = `<span class="muted">Uploading ${fileObjects.length} file(s)...</span>`;

  const fd = new FormData();
  for (const f of fileObjects) fd.append("files", f);

  let res;
  try {
    res = await fetch("/api/circuit", { method: "POST", body: fd });
  } catch (err) {
    summaryEl.innerHTML =
      `<span style="color:#991b1b">Upload failed: ${escapeHtml(String(err))}.</span> ` +
      `<span class="muted">If this keeps happening (many/large files, or tests still ` +
      `running), click "Clear all" and re-upload.</span>`;
    return;
  }
  if (!res.ok) {
    const text = await res.text();
    summaryEl.innerHTML =
      `<span style="color:#991b1b">${escapeHtml(text)}</span> ` +
      `<span class="muted">If this keeps happening, click "Clear all" and re-upload.</span>`;
    return;
  }
  const data = await res.json();
  loaded = data.files || [];
  sessionId = data.session_id || null;
  logEvent("upload", { session_id: sessionId, count: loaded.length });
  l3ExpireAll("re-upload");   // hypothesis cards die on re-upload (l3.debug.v1.1 §7)
  if (loaded.length === 0) {
    summaryEl.innerHTML = `<span style="color:#991b1b">No .dig files were processed.</span>`;
    return;
  }
  if (currentIdx >= loaded.length) currentIdx = 0;

  fileSelect.innerHTML = loaded
    .map((f, i) => `<option value="${i}">${escapeHtml(f.filename)}</option>`)
    .join("");
  fileSelect.value = String(currentIdx);
  fileSelect.disabled = false;
  prevBtn.disabled = loaded.length < 2;
  nextBtn.disabled = loaded.length < 2;
  clearBtn.disabled = false;
  testAllBtn.disabled = false;

  renderCurrent();
}

function renderCurrent() {
  if (loaded.length === 0) return;
  const f = loaded[currentIdx];
  fileSelect.value = String(currentIdx);
  activeIssueIdx = null;

  if (f.error) {
    placeholder.classList.remove("hidden");
    placeholder.innerHTML = `<span style="color:#991b1b">${escapeHtml(f.filename)}: ${escapeHtml(f.error)}</span>`;
    if (cy) { cy.destroy(); cy = null; }
    summaryEl.innerHTML = `<span style="color:#991b1b">Parse error.</span>`;
    issuesListEl.innerHTML = `<span style="color:#991b1b">Could not parse; no issues to show.</span>`;
    issuesCountsEl.innerHTML = `<span class="muted">&mdash;</span>`;
    return;
  }

  renderGraph(f.graph);
  renderSummary(f.summary, f.issues || []);
  renderIssues(f);
  renderTestsForFile(f);
  if (l3PageVisible()) renderL3Tab();   // e.g. file picked from the Test-all panel
  l2LibraryFilename = null;
  l2LlmStatus.textContent = "";
  l2LlmStatus.className = "l2-llm-status";
  l2LlmOutput.innerHTML = "";
  l2LlmOutput.classList.add("empty");
  _resetGrade();
}

function renderGraph(graph) {
  placeholder.classList.add("hidden");

  if (!GRAPH_LIBS_OK) {
    const box = document.getElementById("cy");
    if (box) {
      box.innerHTML =
        `<div class="muted" style="padding:24px">Graph unavailable: the ` +
        `cytoscape/dagre libraries did not load (network or CDN blocked). ` +
        `Structural issues, tests, library, and the Layer 2 coach still work.</div>`;
    }
    return;
  }

  // switching circuits ends any active tick / overlay
  stopClockTick();
  sigActive = null;
  hideClockHud();

  
  if (cy) cy.destroy();

  cy = cytoscape({
    container: document.getElementById("cy"),
    elements: { nodes: graph.nodes, edges: graph.edges },
    style: CY_STYLE,
    layout: {
      name: "dagre",
      rankDir: "LR",
      nodeSep: 30,
      rankSep: 60,
      edgeSep: 10,
      animate: false,
    },
    wheelSensitivity: 0.2,
    minZoom: 0.15,
    maxZoom: 3,
  });

  applyNetIdsL1();               // keep the net-id toggle across rebuilds

  // If the container was still settling when dagre ran (e.g. graph
  // built while another tab was active), re-measure and re-fit once
  // so the tree always starts centered.
  const inst = cy;
  inst.once("layoutstop", () => {
    setTimeout(() => { try { inst.resize(); inst.fit(undefined, 40); } catch {} }, 0);
  });

  cy.on("mouseover", "node", (evt) => {
    const node = evt.target;
    cy.elements().addClass("faded");
    const nb = node.closedNeighborhood();
    nb.removeClass("faded");
    nb.edges().addClass("highlight");
    // Subcircuits get a one-line "click to view inside" affordance instead of
    // the detail popup, and only while a test row is selected.
    if (isSubNode(node)) {
      if (sigActive) showSubHint(node, cy);
    } else {
      showNodePopup(node);
    }
  });
  cy.on("mouseout", "node", () => {
    cy.elements().removeClass("faded");
    cy.edges().removeClass("highlight");
    hidePopup();
    hideSubHint();
  });

  cy.on("mouseover", "edge", (evt) => {
    const edge = evt.target;
    // Isolate this wire: fade everything except it and its two endpoints, so
    // its value stays readable even where wires (and their labels) overlap.
    const keep = edge.union(edge.connectedNodes());
    cy.elements().not(keep).addClass("hover-dim");
    edge.addClass("wire-focus");
    showEdgePopup(edge);
  });
  cy.on("mouseout", "edge", (evt) => {
    cy.elements().removeClass("hover-dim");
    evt.target.removeClass("wire-focus");
    hidePopup();
  });
  // Click the Clock glyph to tick the signal flow through the rest of the rows;
  // click a subcircuit (row selected, clock stopped) to view its inner flow.
  cy.on("tap", "node", (evt) => {
    const node = evt.target;
    if (node.data("element_name") === "Clock") { toggleClock(); return; }
    if (isSubNode(node)) tryOpenDrill(node, [nodeIndex(node)]);
  });
}


  /*# ───────────────────────────────────────────────────────────────────
  *#  Subcircuit drill-in overlay 
  *# ──────────────────────────────────────────────────────────────────#*/

function isSubNode(node) {
  const en = node && node.data && node.data("element_name");
  return typeof en === "string" && en.endsWith(".dig");
}

function nodeIndex(node) {
  return parseInt(node.id(), 10);
}

// The single hint element is fixed-positioned, so it serves both the main graph
// and the drill-in graph. Place it just above the hovered subcircuit rectangle.
function showSubHint(node, inst) {
  const hint = document.getElementById("sub-hint");
  if (!hint) return;
  const ticking = !!clockTimer;
  hint.textContent = ticking
    ? "⏸ stop the clock to view inside"
    : "▸ click to view this row inside";
  hint.classList.toggle("blocked", ticking);
  const rect = inst.container().getBoundingClientRect();
  const bb = node.renderedBoundingBox();
  hint.style.left = (rect.left + (bb.x1 + bb.x2) / 2) + "px";
  hint.style.top = (rect.top + bb.y1 - 6) + "px";
  hint.classList.remove("hidden");
}

function hideSubHint() {
  const hint = document.getElementById("sub-hint");
  if (hint) hint.classList.add("hidden");
}

// Guarded entry: only drill when a row is selected and the clock is stopped.
function tryOpenDrill(node, path) {
  if (!sigActive) return;                 // no row -> nothing to show
  if (clockTimer) { showSubHint(node, node.cy()); return; }  // stop clock first
  openDrill(path);
}

function openDrill(path) {
  drillPath = path.slice();
  hideSubHint();
  hidePopup();
  if (cy) { cy.elements().removeClass("faded"); cy.edges().removeClass("highlight"); }
  const overlay = document.getElementById("drill-overlay");
  if (overlay) overlay.classList.remove("hidden");
  loadDrill();
}

async function loadDrill() {
  if (!sessionId || !loaded[currentIdx] || !sigActive || !drillPath.length) return;
  const filename = loaded[currentIdx].filename;
  const noteEl = document.getElementById("drill-note");
  if (noteEl) noteEl.textContent = "loading…";
  let data;
  try {
    const res = await fetch("/api/subcircuit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        session_id: sessionId, filename,
        spec_index: sigActive.specIdx, row_index: sigActive.rowIdx,
        path: drillPath,
      }),
    });
    data = await res.json();
  } catch (err) {
    if (noteEl) noteEl.textContent = "could not load subcircuit";
    return;
  }
  if (!data || data.ok === false) {
    if (noteEl) noteEl.textContent = (data && data.warning) || "unavailable";
    return;
  }
  renderDrillGraph(data);
}

function renderDrillGraph(data) {
  const box = document.getElementById("drill-cy");
  if (!box) return;
  if (drillCy) { try { drillCy.destroy(); } catch {} drillCy = null; }

  drillCy = cytoscape({
    container: box,
    elements: { nodes: data.graph.nodes, edges: data.graph.edges },
    style: CY_STYLE,
    layout: {
      name: "dagre", rankDir: "LR",
      nodeSep: 30, rankSep: 60, edgeSep: 10, animate: false,
    },
    wheelSensitivity: 0.2,
    minZoom: 0.1, maxZoom: 3,
    // View-only: no dragging, no selection — the only controls are the deeper
    // drill (on a nested subcircuit) and Go back.
    autoungrabify: true,
    boxSelectionEnabled: false,
    autounselectify: true,
  });

  const inst = drillCy;
  inst.once("layoutstop", () => {
    setTimeout(() => { try { inst.resize(); inst.fit(undefined, 40); } catch {} }, 0);
  });

  // Nested subcircuits are themselves drillable; everything else is inert.
  inst.on("mouseover", "node", (evt) => {
    if (isSubNode(evt.target)) showSubHint(evt.target, inst);
  });
  inst.on("mouseout", "node", hideSubHint);
  inst.on("tap", "node", (evt) => {
    const node = evt.target;
    if (isSubNode(node)) {
      hideSubHint();
      openDrill(drillPath.concat(nodeIndex(node)));
    }
  });

  applySignalFlow(data, inst);
  renderDrillCrumb(data);
}

function renderDrillCrumb(data) {
  const crumbEl = document.getElementById("drill-crumb");
  const noteEl = document.getElementById("drill-note");
  if (crumbEl) {
    const master = (loaded[currentIdx] && loaded[currentIdx].filename) || "circuit";
    const parts = [master].concat(data.breadcrumb || []);
    crumbEl.innerHTML =
      parts.map((p) => escapeHtml(p)).join('<span class="crumb-sep">&#9656;</span>') +
      `<span class="crumb-row">row ${sigActive ? sigActive.rowIdx : "?"}</span>`;
  }
  // Convey what this subcircuit *produced* for the row at a glance: its output
  // pin values (the inner wires show the rest of the flow). Falls back to the
  // server note when nothing resolved.
  if (noteEl) {
    const outs = drillOutputSummary(data);
    const shown = outs.slice(0, 6);
    if (outs.length > 6) shown.push(`+${outs.length - 6} more`);
    noteEl.textContent = outs.length
      ? "produces  " + shown.join("   ")
      : (data.note || "");
    noteEl.title = outs.length ? "produces  " + outs.join("   ") : "";
  }
  const back = document.getElementById("drill-back");
  if (back) back.textContent = drillPath.length > 1 ? "◂ Go back" : "◂ Close";
}

// Read each Out node's incoming wire value from the captured row so the header
// can show "produces  Out=0xEDD  Sign=0". Bus values in hex, 1-bit in decimal.
function drillOutputSummary(data) {
  const nv = data.net_values || {};
  const edges = (data.graph && data.graph.edges) || [];
  const nodes = (data.graph && data.graph.nodes) || [];
  const out = [];
  nodes.forEach((n) => {
    const d = n.data;
    if (d.element_name !== "Out") return;
    const e = edges.find((e) => e.data.target === d.id);
    const info = e ? nv[String(e.data.net_id)] : null;
    if (!info) return;
    const val = (info.bits || 1) > 1 ? "0x" + (info.hex || "0") : String(info.value);
    out.push(`${d.comp_label || "out"}=${val}`);
  });
  return out;
}


function drillBack() {
  drillPath.pop();
  hideSubHint();
  if (!drillPath.length) { closeDrill(); return; }
  loadDrill();
}

function closeDrill() {
  drillPath = [];
  hideSubHint();
  if (drillCy) { try { drillCy.destroy(); } catch {} drillCy = null; }
  const overlay = document.getElementById("drill-overlay");
  if (overlay) overlay.classList.add("hidden");
}

(function wireDrillControls() {
  const back = document.getElementById("drill-back");
  if (back) back.addEventListener("click", drillBack);
})();


function showNodePopup(node) {
  const d = node.data();
  const title = d.comp_label ? `${d.element_name} - ${d.comp_label}` : d.element_name;
  popupTitle.textContent = title;

  const bits = (d.attributes && d.attributes.Bits !== undefined)
    ? d.attributes.Bits : null;
  const bitsRow = bits !== null
    ? `<tr><td class="k">bits</td><td class="v">${escapeHtml(String(bits))}</td></tr>`
    : "";
  const attrRows = Object.entries(d.attributes || {})
    .filter(([k]) => k !== "Label" && k !== "Bits")
    .map(([k, v]) =>
      `<tr><td class="k">${escapeHtml(k)}</td><td class="v">${escapeHtml(String(v))}</td></tr>`
    ).join("");

  const incoming = node.incomers("edge");
  const outgoing = node.outgoers("edge");

  const inputsBySinkPin = groupBy(incoming, (e) => e.data("sink_pin") || "?");
  const outputsByDriverPin = groupBy(outgoing, (e) => e.data("driver_pin") || "?");

  const inputsHtml = renderPinList(inputsBySinkPin, "input");
  const outputsHtml = renderPinList(outputsByDriverPin, "output");

  popupBody.innerHTML = `
    <table>
      <tr><td class="k">family</td><td class="v">${escapeHtml(d.family_display || d.family)}</td></tr>
      <tr><td class="k">index</td><td class="v">${escapeHtml(d.id)}</td></tr>
      ${bitsRow}
      <tr><td class="k">.dig pos</td><td class="v">(${d.x_dig}, ${d.y_dig})</td></tr>
      ${attrRows}
    </table>
    ${inputsHtml}
    ${outputsHtml}
  `;

  popupEl.classList.remove("hidden");
}

function showEdgePopup(edge) {
  const d = edge.data();
  popupTitle.textContent = `Net ${d.net_id ?? "?"}`;

  const sourceNode = cy.getElementById(d.source);
  const targetNode = cy.getElementById(d.target);
  const sourceLabel = sourceNode.data("comp_label") || sourceNode.data("element_name");
  const targetLabel = targetNode.data("comp_label") || targetNode.data("element_name");

  popupBody.innerHTML = `
    <table>
      <tr><td class="k">net id</td><td class="v">${escapeHtml(d.net_id ?? "?")}</td></tr>
      <tr><td class="k">bits</td><td class="v">${escapeHtml(d.bits ?? "?")}</td></tr>
      <tr><td class="k">from</td><td class="v">${escapeHtml(sourceLabel)} [${escapeHtml(d.source)}] . ${escapeHtml(d.driver_pin || "?")}</td></tr>
      <tr><td class="k">to</td><td class="v">${escapeHtml(targetLabel)} [${escapeHtml(d.target)}] . ${escapeHtml(d.sink_pin || "?")}</td></tr>
    </table>
  `;
  popupEl.classList.remove("hidden");
}

function hidePopup() {
  popupEl.classList.add("hidden");
}

window.addEventListener("keydown", (e) => {
  if (popupEl.classList.contains("hidden")) return;
  if (e.key === "ArrowDown") {
    popupEl.scrollTop += 40;
    e.preventDefault();
  } else if (e.key === "ArrowUp") {
    popupEl.scrollTop -= 40;
    e.preventDefault();
  } else if (e.key === "PageDown") {
    popupEl.scrollTop += popupEl.clientHeight - 20;
    e.preventDefault();
  } else if (e.key === "PageUp") {
    popupEl.scrollTop -= popupEl.clientHeight - 20;
    e.preventDefault();
  }
});

function renderPinList(byPin, kind) {
  const keys = Object.keys(byPin).sort();
  if (keys.length === 0) return "";

  const sectionTitle = kind === "input" ? "INPUTS" : "OUTPUTS";

  const items = keys.map((pinName) => {
    const edges = byPin[pinName];
    const peers = edges.map((e) => {
      const d = e.data();
      const otherId = kind === "input" ? d.source : d.target;
      const otherPin = kind === "input" ? d.driver_pin : d.sink_pin;
      const otherNode = cy.getElementById(otherId);
      const otherLabel = otherNode.data("comp_label") || otherNode.data("element_name");
      const arrow = kind === "input" ? "&larr;" : "&rarr;";
      return `${arrow} ${escapeHtml(otherLabel)}[${escapeHtml(otherId)}].${escapeHtml(otherPin || "?")} (net ${escapeHtml(d.net_id ?? "?")})`;
    }).join("<br>");

    return `<li><strong>${escapeHtml(pinName)}</strong> ${peers}</li>`;
  }).join("");

  return `
    <div class="hover-popup-section">
      <div class="hover-popup-section-title">${sectionTitle}</div>
      <ul>${items}</ul>
    </div>
  `;
}

function groupBy(collection, keyFn) {
  const out = {};
  collection.forEach((item) => {
    const k = keyFn(item);
    (out[k] = out[k] || []).push(item);
  });
  return out;
}

function renderSummary(s, issues) {
  const stats = s.net_stats || {};
  const undrivenBadge = stats.undriven_with_pins
    ? `<span class="badge warn">${stats.undriven_with_pins} undriven</span>`
    : "";
  const multiBadge = stats.multi_driver
    ? `<span class="badge err">${stats.multi_driver} multi-driver</span>`
    : "";
  const widthKinds = new Set(["width_mismatch", "width_conflict"]);
  const widthCount = (issues || []).filter((i) => widthKinds.has(i.kind)).length;
  const widthBadge = widthCount
    ? `<span class="badge widx">${widthCount} width mismatch${widthCount === 1 ? "" : "es"}</span>`
    : "";
  const hasAny = undrivenBadge || multiBadge || widthBadge;

  const inventoryRows = Object.entries(s.inventory || {})
    .sort(([, a], [, b]) => b - a)
    .map(([name, count]) =>
      `<tr><td class="k">${escapeHtml(name)}</td><td class="v">${count}</td></tr>`
    ).join("");
  const inventoryTotal = Object.values(s.inventory || {})
    .reduce((a, b) => a + b, 0);

  const inputsList = (s.inputs || [])
    .map((p) => `<li>${escapeHtml(p.label)} <span class="muted">${p.bits} bit${p.bits === 1 ? "" : "s"}</span></li>`)
    .join("");
  const outputsList = (s.outputs || [])
    .map((p) => `<li>${escapeHtml(p.label)} <span class="muted">${p.bits} bit${p.bits === 1 ? "" : "s"}</span></li>`)
    .join("");
  const subsList = (s.subcircuits || [])
    .map((sub) => {
      const badge = sub.resolved ? "" : `<span class="badge err">missing</span>`;
      return `<li>${escapeHtml(sub.reference)} ${badge}</li>`;
    })
    .join("");

  summaryEl.innerHTML = `
    <table>
      <tr><td class="k">nets</td><td class="v">${stats.total ?? 0}</td></tr>
      <tr><td class="k">driven</td><td class="v">${stats.driven ?? 0}</td></tr>
          <tr><td class="k">structural issues</td><td class="v">${undrivenBadge}${multiBadge}${widthBadge}${hasAny ? "" : '<span class="ok">none</span>'}</td></tr>
    </table>

    <h2 style="margin-top:14px">Inputs (${(s.inputs || []).length})</h2>
    ${inputsList ? `<ul>${inputsList}</ul>` : `<div class="muted">(none)</div>`}

    <h2>Outputs (${(s.outputs || []).length})</h2>
    ${outputsList ? `<ul>${outputsList}</ul>` : `<div class="muted">(none)</div>`}

    <h2>Subcircuits (${(s.subcircuits || []).length})</h2>
    ${subsList ? `<ul>${subsList}</ul>` : `<div class="muted">(none)</div>`}

    <h2>Inventory (${inventoryTotal})</h2>
    <table>${inventoryRows || '<tr><td class="muted">(empty)</td></tr>'}</table>
  `;
}

function escapeHtml(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function fileL1Errors(file) {
  // Blocking is on ERRORS only (any nesting depth): warnings stay
  // testable so the "mute when tests pass" flow keeps working.
  return ((file && file.issues) || []).filter((i) => i.severity === "error");
}

function getFileTestsPassed(filename) {
  const st = testState[filename];
  if (!st || st.status !== "done" || !st.payload) return null;
  if (st.payload.all_passed === true)  return true;
  if (st.payload.all_passed === false) return false;
  return null;
}

// "Mute when tests pass" is HIT for this file: toggle on, the last run
// (per-row or general — both set all_passed) fully passed, and the
// issue count is within the mute threshold. While hit, test runs stay
// available in BOTH modes — a muted note must never dead-end the Run
// button (the run that proved "tests pass" must always be repeatable).
function fileMuteActive(file) {
  if (!file || !muteToggle.checked) return false;
  const issues = file.issues || [];
  return getFileTestsPassed(file.filename) === true
    && issues.length > 0
    && issues.length <= MUTE_THRESHOLD;
}

function countsBadge(issues) {
  if (!issues || issues.length === 0) {
    return `<span class="ok">clean</span>`;
  }
  const nErr  = issues.filter((i) => i.severity === "error").length;
  const nWarn = issues.filter((i) => i.severity === "warning").length;
  const nInfo = issues.filter((i) => i.severity === "info").length;
  const parts = [];
  if (nErr)  parts.push(`<span class="err">${nErr} err</span>`);
  if (nWarn) parts.push(`<span class="warn">${nWarn} warn</span>`);
  if (nInfo) parts.push(`<span class="info">${nInfo} info</span>`);
  return parts.join(" &middot; ");
}

function renderIssues(file) {
  const issues = file.issues || [];
  issuesCountsEl.innerHTML = countsBadge(issues);

  if (file.issues_error) {
    issuesListEl.innerHTML =
      `<span style="color:#991b1b">L1 check failed: ${escapeHtml(file.issues_error)}</span>`;
    return;
  }

  if (issues.length === 0) {
    issuesListEl.innerHTML =
      `<div class="muted-banner" style="cursor:default">No Layer 1 issues detected.</div>`;
    return;
  }
  const fileTestsPassed = getFileTestsPassed(file.filename);
  const shouldMute =
    muteToggle.checked
    && fileTestsPassed === true
    && issues.length <= MUTE_THRESHOLD
    && !mutedByUser.has(currentIdx);
  if (shouldMute) {
    issuesListEl.innerHTML =
      `<div class="muted-banner" id="mute-banner">
         Tests pass; ${issues.length} minor note${issues.length === 1 ? "" : "s"} muted. Click to show.
       </div>`;
    document.getElementById("mute-banner").addEventListener("click", () => {
      mutedByUser.add(currentIdx);
      renderIssues(file);
    });
    return;
  }

  issuesListEl.innerHTML = issues.map((iss, idx) => {
    const fixHtml = iss.suggested_fix
      ? `<div class="issue-fix">${escapeHtml(iss.suggested_fix)}</div>`
      : "";
    const kindCls = iss.kind ? ` kind-${escapeHtml(iss.kind)}` : "";
    return `
      <div class="issue-card sev-${escapeHtml(iss.severity)}${kindCls}" data-issue-idx="${idx}">
        <span class="sev-badge">${escapeHtml(iss.severity)}</span>
        <span class="issue-title">${escapeHtml(iss.title)}</span>
        <div class="issue-msg">${escapeHtml(iss.message)}</div>
        ${fixHtml}
      </div>
    `;
  }).join("");

  issuesListEl.querySelectorAll(".issue-card").forEach((card) => {
    card.addEventListener("click", () => {
      const idx = parseInt(card.dataset.issueIdx, 10);
      const iss = issues[idx];
      highlightIssueComponents(iss, card);
    });
  });
}

function highlightIssueComponents(issue, card) {
  if (!cy) return;
  const comps = (issue.component_indices || []).map(String);
  if (comps.length === 0) return;

  if (activeIssueIdx === card) {
    clearIssueHighlight();
    return;
  }

  clearIssueHighlight();
  activeIssueIdx = card;
  card.classList.add("active");

  cy.elements().addClass("faded");
  comps.forEach((id) => {
    const n = cy.getElementById(id);
    if (n && n.nonempty && n.nonempty()) {
      n.removeClass("faded");
      n.addClass("issue-target");
      n.closedNeighborhood().removeClass("faded");
    }
  });

  const targets = cy.collection(
    comps.map((id) => cy.getElementById(id)).filter((n) => n && n.nonempty && n.nonempty())
  );
  if (targets.length > 0) {
    cy.animate({ fit: { eles: targets, padding: 80 } }, { duration: 250 });
  }
}

function clearIssueHighlight() {
  if (!cy) return;
  cy.elements().removeClass("faded");
  cy.nodes().removeClass("issue-target");
  if (activeIssueIdx && activeIssueIdx.classList) {
    activeIssueIdx.classList.remove("active");
  }
  activeIssueIdx = null;
}

// Tests panel 

function setTestSlot(filename, patch) {
  const prev = testState[filename] || { status: "idle", progress: "", payload: null, mode: null, jobId: null, message: null };
  testState[filename] = { ...prev, ...patch };
  if (loaded[currentIdx] && loaded[currentIdx].filename === filename) {
    renderTestsForFile(loaded[currentIdx]);
    if (testState[filename].status === "done") {
      renderIssues(loaded[currentIdx]);
    }
    // Mode A readiness depends on per-row results — keep the L3 boards live
    // (a per-row job can finish while the user is on the L3 tab).
    if (l3PageVisible()) renderL3Boards(loaded[currentIdx]);
  }
}

function renderTestsForFile(file) {
  testsResultsEl.innerHTML = "";
  testsResultsEl.classList.add("empty");

  const hasTests = !!(file.summary && file.summary.has_testcases);
  const officialAvailable = !!(file.summary && file.summary.official_test_available);
  if (!hasTests && !officialAvailable) {
    runTestsBtn.disabled = true;
    runTestsBtn.classList.remove("running");
    runTestsBtn.textContent = "Run tests";
    testsStatusEl.textContent = "No test data found in this file.";
    testsStatusEl.className = "tests-status muted";
    hideProgress();
    return;
  }

  const l1Errors = fileL1Errors(file);
  if (l1Errors.length > 0 && !fileMuteActive(file)) {
    runTestsBtn.disabled = true;
    runTestsBtn.classList.remove("running");
    runTestsBtn.textContent = "Run tests";
    testsStatusEl.textContent =
      `Blocked: ${l1Errors.length} Layer 1 error${l1Errors.length === 1 ? "" : "s"} unresolved. ` +
      `Fix the structural errors above first - they make test results unreliable.`;
    testsStatusEl.className = "tests-status warning";
    hideProgress();
    return;
  }

  const slot = testState[file.filename];

  if (!slot || slot.status === "idle") {
    runTestsBtn.disabled = false;
    runTestsBtn.classList.remove("running");
    runTestsBtn.textContent = "Run tests";
    const officialStatus = file.summary && file.summary.official_test_status;
    if (officialStatus === "missing") {
      testsStatusEl.textContent =
        `Ready: this file has no test rows, but an official test set exists for "${file.filename}" — runs use the official tests (Gradescope-style).`;
    } else if (officialStatus === "modified") {
      testsStatusEl.textContent =
        `Ready: this file's testcase differs from the official set for "${file.filename}" — runs use the official tests (Gradescope-style).`;
    } else if (hasTests) {
      testsStatusEl.textContent =
        `Ready: ${file.summary.testcase_count} testcase${file.summary.testcase_count === 1 ? "" : "s"} found. Click "Run tests" to execute.`;
    } else {
      testsStatusEl.textContent =
        `Ready: an official test set exists for "${file.filename}" — runs use the official tests (Gradescope-style).`;
    }
    testsStatusEl.className = "tests-status muted";
    hideProgress();
    return;
  }

  if (slot.status === "running") {
    runTestsBtn.disabled = true;
    runTestsBtn.classList.add("running");
    runTestsBtn.textContent = "Running...";
    testsStatusEl.textContent =
      slot.mode === "per_row" ? "Running per-row..." : "Running general...";
    testsStatusEl.className = "tests-status muted";
    showProgress(slot.progress || "starting...");
    return;
  }

  if (slot.status === "warning") {
    runTestsBtn.disabled = false;
    runTestsBtn.classList.remove("running");
    runTestsBtn.textContent = "Run tests";
    testsStatusEl.textContent = `Warning: ${slot.message || "Test runner error"}`;
    testsStatusEl.className = "tests-status warning";
    hideProgress();
    return;
  }

  runTestsBtn.disabled = false;
  runTestsBtn.classList.remove("running");
  runTestsBtn.textContent = "Run tests";
  hideProgress();

  const payload = slot.payload;
  if (!payload) return;

  const injectedNote = (payload.injected || []).length
    ? " This run used official course content (missing/modified testcase or empty ROM substituted — Gradescope-style)."
    : "";
  if (payload.warning) {
    testsStatusEl.textContent = `Warning: ${payload.warning}`;
    testsStatusEl.className = "tests-status warning";
  } else if ((payload.specs || []).length === 0) {
    testsStatusEl.textContent = "No Testcase elements were found.";
    testsStatusEl.className = "tests-status muted";
  } else {
    const allPassed = payload.all_passed === true;
    testsStatusEl.textContent =
      (allPassed ? "All rows passed." : "Some rows did not pass.") + injectedNote;
    testsStatusEl.className =
      allPassed ? "tests-status passed" : "tests-status failed";
  }

  if (slot.mode === "general") {
    renderGeneralResults(payload);
  } else {
    renderTestResults(payload);
  }
}

runTestsBtn.addEventListener("click", async () => {
  if (!sessionId || loaded.length === 0) return;
  const file = loaded[currentIdx];
  if (!file || !file.summary) return;
  // Runnable with the file's own tests OR via official-test injection
  // (official_test_status "missing"/"modified") — the render path enables
  // the button for both, so the click gate must agree with it.
  const s = file.summary;
  const injectable = s.official_test_status === "missing"
    || s.official_test_status === "modified";
  if (!s.has_testcases && !injectable) return;
  // blocked: fix L1 errors first — unless "mute when tests pass" is hit,
  // where a proven-passing file stays re-runnable in both modes
  if (fileL1Errors(file).length > 0 && !fileMuteActive(file)) return;
  const filename = file.filename;
  const mode = perRowToggle.checked ? "per_row" : "general";

  setTestSlot(filename, { status: "running", progress: "starting...", mode, jobId: null, payload: null, message: null });
  logEvent("tests_run_started", { filename, mode });

  let res;
  try {
    res = await fetch("/api/tests/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId, filename, mode }),
    });
  } catch (err) {
    setTestSlot(filename, { status: "warning", message: `Network error: ${err}`, mode });
    return;
  }
  if (!res.ok) {
    const text = await res.text();
    setTestSlot(filename, { status: "warning", message: `Server error ${res.status}: ${text}`, mode });
    return;
  }
  const startResp = await res.json();

  if (startResp.mode === "general") {
    finalizeSlot(filename, startResp, "general");
    return;
  }

  setTestSlot(filename, { status: "running", progress: "starting...", jobId: startResp.job_id, mode: "per_row" });
  pollFor(filename, startResp.job_id);
});

async function pollFor(filename, jobId) {
  while (true) {
    await new Promise((r) => setTimeout(r, 400));
    const slot = testState[filename];
    if (!slot || slot.jobId !== jobId) return;

    let snap;
    try {
      const res = await fetch(`/api/tests/progress/${jobId}`);
      if (!res.ok) {
        const t = await res.text();
        setTestSlot(filename, { status: "warning", message: `Job lookup failed: ${t}`, mode: "per_row" });
        return;
      }
      snap = await res.json();
    } catch (err) {
      setTestSlot(filename, { status: "warning", message: `Polling error: ${err}`, mode: "per_row" });
      return;
    }

    const pct = snap.total_rows
      ? Math.floor((snap.done_rows * 100) / snap.total_rows)
      : 0;
    const progress = snap.total_rows
      ? `${pct}% (${snap.done_rows}/${snap.total_rows} rows)`
      : "starting...";

    if (snap.finished) {
      finalizeSlot(filename, snap, "per_row");
      return;
    }
    setTestSlot(filename, { status: "running", progress, jobId, mode: "per_row" });
  }
}

function finalizeSlot(filename, payload, mode) {
  logEvent("tests_run_complete", {
    filename, mode, ok: payload.ok, all_passed: payload.all_passed,
  });
  if (!payload.ok) {
    setTestSlot(filename, { status: "warning", message: payload.warning || "Test runner reported an error.", mode });
  } else {
    setTestSlot(filename, { status: "done", payload, mode });
  }
}

// "Test all" — one fast pass over every uploaded file.

testAllBtn.addEventListener("click", async () => {
  if (!sessionId || loaded.length === 0) return;
  testAllBtn.disabled = true;
  testAllBtn.textContent = "Testing...";
  testAllPanel.classList.remove("hidden");
  testAllHeadEl.innerHTML =
    `<span class="l3-spinner"></span> Testing all files...`;
  testAllListEl.innerHTML = "";
  logEvent("tests_run_all_started", { count: loaded.length });

  let data;
  try {
    const res = await fetch("/api/tests/all", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId }),
    });
    if (!res.ok) throw new Error(`server ${res.status}: ${await res.text()}`);
    data = await res.json();
  } catch (err) {
    testAllHeadEl.textContent = `Test all failed: ${err}`;
    testAllBtn.disabled = false;
    testAllBtn.textContent = "Test all";
    return;
  }
  testAllBtn.disabled = false;
  testAllBtn.textContent = "Test all";
  logEvent("tests_run_all_complete", data.summary || {});
  renderTestAllPanel(data);

  // Each tested file's payload is general-mode shaped — drop it into
  // testState so the per-file Tests panel and issue muting update too.
  for (const f of data.files || []) {
    if (f.status === "no_tests" || f.status === "parse_error" || f.status === "blocked") continue;
    setTestSlot(f.filename, {
      status: "done",
      payload: { ok: f.ok, warning: f.warning, specs: f.specs, all_passed: f.all_passed },
      mode: "general", jobId: null, message: null,
    });
  }
});

testAllClose.addEventListener("click", () => testAllPanel.classList.add("hidden"));

function renderTestAllPanel(data) {
  const s = data.summary || {};
  if (!s.files_with_tests) {
    testAllHeadEl.textContent = "No testcases found in any uploaded file.";
  } else {
    const head = `${s.passed}/${s.files_with_tests} circuit${s.files_with_tests === 1 ? "" : "s"} pass`;
    const extras = [];
    if (s.blocked) extras.push(`${s.blocked} blocked by L1 errors`);
    if (s.errors) extras.push(`${s.errors} error${s.errors === 1 ? "" : "s"}`);
    testAllHeadEl.textContent = head + (extras.length ? " · " + extras.join(" · ") : "");
  }
  testAllListEl.innerHTML = (data.files || []).map((f) => {
    const chip = {
      passed: `<span class="ta-chip ta-pass">pass</span>`,
      failed: `<span class="ta-chip ta-fail">fail</span>`,
      blocked: `<span class="ta-chip ta-blocked">blocked</span>`,
      error: `<span class="ta-chip ta-err">error</span>`,
      parse_error: `<span class="ta-chip ta-err">parse error</span>`,
      no_tests: `<span class="ta-chip ta-none">no tests</span>`,
    }[f.status] || `<span class="ta-chip ta-none">?</span>`;
    const detail = (f.specs || [])
      .filter((sp) => sp.status === "failed" && sp.failing_rows != null)
      .map((sp) => `${sp.failing_rows} row${sp.failing_rows === 1 ? "" : "s"} failing`)
      .join(", ");
    const warn = f.warning ? ` · ${escapeHtml(f.warning)}` : "";
    return `<div class="test-all-row" data-fname="${escapeHtml(f.filename)}">
      ${chip}
      <span class="ta-name">${escapeHtml(f.filename)}</span>
      <span class="ta-detail">${escapeHtml(detail)}${warn}</span>
    </div>`;
  }).join("");
  testAllListEl.querySelectorAll(".test-all-row").forEach((rowEl) => {
    rowEl.addEventListener("click", () => {
      const idx = loaded.findIndex((x) => x.filename === rowEl.dataset.fname);
      if (idx < 0 || !l3ConfirmNav()) return;
      currentIdx = idx;
      renderCurrent();
    });
  });
}

function showProgress(text) {
  testsProgressTextEl.textContent = text;
  testsProgressEl.classList.remove("hidden");
}
function hideProgress() {
  testsProgressEl.classList.add("hidden");
}

function renderTestResults(payload) {
  testsResultsEl.classList.remove("empty");

  const html = payload.specs.map((spec, specIdx) => {
    const headers = spec.headers || [];
    const headerCells =
      `<td class="row-idx">idx</td>` +
      headers.map((h) => `<td>${escapeHtml(h)}</td>`).join("") +
      `<td class="row-status">status</td>`;

    const rowsHtml = spec.rows.map((row) => {
      const idxCell = `<td class="row-idx">${row.index}</td>`;
      if (row.error_message) {
        const span = headers.length + 1;
        return `<tr class="${escapeHtml(row.status)}">
          ${idxCell}
          <td class="row-err" colspan="${span}">${escapeHtml(row.error_message)}</td>
        </tr>`;
      }
      const tokens = (row.raw || "").split(/\s+/).filter(Boolean);
      const tokenCells = headers.map((_, i) =>
        `<td>${escapeHtml(tokens[i] ?? "")}</td>`
      ).join("");
      let mismatchHtml = "";
      if (row.status === "failed" && Array.isArray(row.mismatches) && row.mismatches.length) {
        const parts = row.mismatches.map((m) =>
          `${escapeHtml(m.column ?? "?")}: expected ${escapeHtml(m.expected)}, got ${escapeHtml(m.found)}`
        );
        mismatchHtml = `<tr class="mismatch-row">
          <td></td>
          <td colspan="${headers.length + 1}">${parts.join(" &middot; ")}</td>
        </tr>`;
      }
      // Clicking the row lights up the signal-flow graph for that vector.
      return `<tr class="${escapeHtml(row.status)} sig-clickable" data-sig-row="1" data-spec="${specIdx}" data-row="${row.index}" title="Click to show signal flow for this row">
        ${idxCell}
        ${tokenCells}
        <td class="row-status">${escapeHtml(row.status)}</td>
      </tr>${mismatchHtml}`;
    }).join("");

    return `
      <div class="spec-title">${escapeHtml(spec.name)} &middot; ${spec.rows.length} row${spec.rows.length === 1 ? "" : "s"}</div>
      <table>
        <thead><tr>${headerCells}</tr></thead>
        <tbody>${rowsHtml}</tbody>
      </table>
    `;
  }).join("");
  testsResultsEl.innerHTML = html;
}


  /*# ───────────────────────────────────────────────────────────────────
  *#  Signal-flow overlay: click a test row to color wires by their value
  *# ──────────────────────────────────────────────────────────────────#*/

async function showSignalFlowForRow(specIdx, rowIdx, trEl) {
  if (!sessionId || !loaded[currentIdx]) return;
  const filename = loaded[currentIdx].filename;

  document.querySelectorAll("tr.sig-selected")
    .forEach((t) => t.classList.remove("sig-selected"));
  if (trEl) trEl.classList.add("sig-selected");

  let sim;
  try {
    const res = await fetch("/api/simulate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        session_id: sessionId, filename,
        spec_index: specIdx, row_index: rowIdx,
      }),
    });
    if (!res.ok) { console.warn("simulate failed", res.status); return; }
    sim = await res.json();
  } catch (err) {
    console.warn("simulate error", err);
    return;
  }
  if (!sim || sim.ok === false) return;
  applySignalFlow(sim);
  sigActive = { specIdx, rowIdx };
  updateClockHint();
}

  /*# ───────────────────────────────────────────────────────────────────
  *#  Clock tick: step the signal flow through the remaining rows
  *# ──────────────────────────────────────────────────────────────────#*/

function circuitHasClock() {
  return !!(cy && cy.nodes('[element_name = "Clock"]').nonempty());
}

function ensureClockHud() {
  let hud = document.getElementById("clock-hud");
  if (!hud) {
    hud = document.createElement("div");
    hud.id = "clock-hud";
    hud.className = "clock-hud";
    const box = document.getElementById("cy");
    if (box && box.parentElement) {
      if (getComputedStyle(box.parentElement).position === "static") {
        box.parentElement.style.position = "relative";
      }
      box.parentElement.appendChild(hud);
    }
    hud.addEventListener("click", toggleClock);
  }
  return hud;
}

// One entry point for the clock affordance (HUD click or the Clock glyph):
// stop while running, restart from the top when finished, else start.
function toggleClock() {
  if (clockTimer) stopClockTick();
  else if (clockDone) restartClockTick();
  else startClockTick();
}

function restartClockTick() {
  if (!sigActive) return;
  const rows = rowsForSpec(sigActive.specIdx);
  if (!rows.length) return;
  sigActive = { specIdx: sigActive.specIdx, rowIdx: rows[0] };
  clockDone = false;
  startClockTick();
}

function hideClockHud() {
  const hud = document.getElementById("clock-hud");
  if (hud) hud.style.display = "none";
  if (cy) cy.nodes().removeClass("clock-hint");
}

// Idle hint shown once a row is active on a clocked circuit.
function updateClockHint() {
  if (!sigActive || !circuitHasClock()) { hideClockHud(); return; }
  if (clockTimer) return;               // ticking shows its own text
  const hud = ensureClockHud();
  hud.className = "clock-hud";
  hud.textContent = "⏱ Click the Clock to tick through the rows";
  hud.style.display = "block";
  cy.nodes('[element_name = "Clock"]').addClass("clock-hint");
}

function rowsForSpec(specIdx) {
  return Array.from(
    document.querySelectorAll(`tr[data-sig-row][data-spec="${specIdx}"]`),
  ).map((tr) => parseInt(tr.getAttribute("data-row"), 10))
    .filter((n) => !Number.isNaN(n));
}

function stopClockTick() {
  if (clockTimer) { clearTimeout(clockTimer); clockTimer = null; }
  updateClockHint();
}

// From the active row, advance through the rest of the testcase, re-rendering
// the signal flow for each and counting ticks. Click again (clock or HUD) to stop.
function startClockTick() {
  if (!sigActive) return;
  const specIdx = sigActive.specIdx;
  const rows = rowsForSpec(specIdx);
  if (!rows.length) return;
  let i = Math.max(0, rows.indexOf(sigActive.rowIdx));
  const hud = ensureClockHud();
  hud.className = "clock-hud running";
  if (cy) cy.nodes().removeClass("clock-hint");

  const step = async () => {
    if (clockTimer === null) return;                 // stopped
    const rowIdx = rows[i];
    const tr = document.querySelector(
      `tr[data-sig-row][data-spec="${specIdx}"][data-row="${rowIdx}"]`);
    await showSignalFlowForRow(specIdx, rowIdx, tr);
    if (clockTimer === null) return;                 // stopped mid-await
    hud.className = "clock-hud running";
    hud.textContent = `⏱ tick ${i + 1} / ${rows.length}  ·  row ${rowIdx}  (click to stop)`;
    hud.style.display = "block";
    if (i + 1 < rows.length) {
      i += 1;
      clockTimer = setTimeout(step, 1500);
    } else {
      clockTimer = null;
      clockDone = true;
      hud.className = "clock-hud restart";
      hud.textContent = `⟲ restart — ${rows.length} rows ticked`;
      hud.style.display = "block";
    }
  };
  clockDone = false;
  clockTimer = true;   // mark running before the first (async) step
  step();
}

function clearSignalFlow(inst = cy) {
  if (!inst) return;
  inst.edges().removeClass("sig-hi sig-lo sig-bus sig-none sig-dim");
  inst.nodes().removeClass("sig-mismatch sig-dim");
  inst.batch(() => {
    inst.edges().forEach((e) => e.data("sigLabel", ""));
    inst.nodes().forEach((n) => {
      const base = n.data("baseLabel");
      if (base != null) n.data("label", base);
      const baseShape = n.data("baseShape");   // restore reacted glyph
      if (baseShape != null) n.data("shape_svg", baseShape);
    });
  });
}

// Paint every edge by the value its net carries this row: 1-bit -> green
// (bright 1 / dark 0), multi-bit -> blue + hex on the wire, unresolved ->
// gray. Failed-row outputs get a red ring + expected/found chip. Works on any
// Cytoscape instance so the drill-in graph reuses the exact same coloring.
function applySignalFlow(sim, inst = cy) {
  if (!inst) return;
  clearSignalFlow(inst);
  const nv = sim.net_values || {};
  inst.batch(() => {
    inst.edges().forEach((e) => {
      const nid = e.data("net_id");
      const info = (nid !== null && nid !== undefined) ? nv[String(nid)] : null;
      if (!info) { e.addClass("sig-none"); return; }
      const bits = info.bits || 1;
      if (bits <= 1) {
        e.addClass(info.value ? "sig-hi" : "sig-lo");
      } else {
        e.addClass("sig-bus");
        e.data("sigLabel", "0x" + (info.hex || "0"));
      }
    });
    (sim.outputs || []).forEach((o) => {
      if (o.ok !== false) return;
       inst.nodes().forEach((n) => {
        if (n.data("element_name") !== "Out") return;
        if (n.data("comp_label") !== o.label) return;
        if (n.data("baseLabel") == null) n.data("baseLabel", n.data("label"));
        n.addClass("sig-mismatch");
        n.data("label", n.data("baseLabel") + "\n⚠ exp " + o.expected + " / got " + o.found);
      });
    });
        // determined per-component reactions: swap in the reacted glyph
    // (7-seg lit segments, mux/decoder selected-port ring).
    const svgs = sim.node_svgs || {};
    Object.keys(svgs).forEach((id) => {
      const n = inst.getElementById(id);
      if (!n || n.empty()) return;
      if (n.data("baseShape") == null) n.data("baseShape", n.data("shape_svg"));
      n.data("shape_svg", svgs[id]);
    });
  });
}

testsResultsEl.addEventListener("click", (evt) => {
  const tr = evt.target.closest("tr[data-sig-row]");
  if (!tr) return;
  const specIdx = parseInt(tr.getAttribute("data-spec"), 10);
  const rowIdx = parseInt(tr.getAttribute("data-row"), 10);
  if (Number.isNaN(specIdx) || Number.isNaN(rowIdx)) return;
  stopClockTick();          // a manual pick ends any tick run / restart state
  clockDone = false;
  showSignalFlowForRow(specIdx, rowIdx, tr);
});

function renderGeneralResults(payload) {
  testsResultsEl.classList.remove("empty");
  const html = payload.specs.map((spec) => {
    const headline = renderGeneralHeadline(spec);
    return `<div class="spec-title">${escapeHtml(spec.name)} &middot; ${spec.row_count} row${spec.row_count === 1 ? "" : "s"}</div>
            <div class="spec-headline">${headline}</div>`;
  }).join("");
  testsResultsEl.innerHTML = html;
}

function renderGeneralHeadline(spec) {
  if (spec.status === "error") {
    return `<span class="neutral">runner could not match this testcase</span>`;
  }
  const pp = spec.pass_pct ?? 0;
  const fp = spec.fail_pct ?? 0;
  const parts = [];
  if (pp > 0) parts.push(`<span class="pct-pass">${pp}% passed</span>`);
  if (fp > 0) parts.push(`<span class="pct-fail">${fp}% failed</span>`);
  if (parts.length === 0) parts.push(`<span class="neutral">no rows reported</span>`);
  return parts.join(" &middot; ");
}

async function refreshJarChip() {
  let info;
  try {
    const res = await fetch("/api/config/jar");
    info = await res.json();
  } catch {
    jarStateEl.innerHTML = `<span class="jar-state-unknown">unknown</span>`;
    return;
  }
  if (info.exists) {
    jarStateEl.innerHTML = `<span class="jar-state-good">found</span>`;
    jarChipBtn.title = `Configured: ${info.path}`;
  } else {
    jarStateEl.innerHTML = `<span class="jar-state-missing">not set</span>`;
    jarChipBtn.title = "Click to set Digital.jar location";
  }
}

jarChipBtn.addEventListener("click", async () => {
  let info;
  try {
    const r = await fetch("/api/config/jar");
    info = await r.json();
  } catch {
    info = {};
  }
  jarPathInput.value = info.path || "";
  jarModalMsg.textContent = "";
  jarModalMsg.className = "modal-msg";
  jarModal.classList.remove("hidden");
});

jarCancelBtn.addEventListener("click", () => jarModal.classList.add("hidden"));

jarBrowseBtn.addEventListener("click", async () => {
  jarModalMsg.textContent = "Opening native file picker on the server...";
  jarModalMsg.className = "modal-msg";
  let info;
  try {
    const r = await fetch("/api/config/jar/browse");
    info = await r.json();
  } catch (err) {
    jarModalMsg.textContent = `Browse failed: ${err}`;
    jarModalMsg.className = "modal-msg err";
    return;
  }
  if (info.ok) {
    jarPathInput.value = info.path;
    jarModalMsg.textContent = `Selected: ${info.path}. Click Save to persist.`;
    jarModalMsg.className = "modal-msg ok";
    return;
  }
  const reason = (info.reason || "").toLowerCase();
  if (reason.includes("cancel")) {
    jarModalMsg.textContent = "";
    jarModalMsg.className = "modal-msg";
  } else {
    jarModalMsg.textContent = `Browse unavailable (${info.reason || "no reason"}).`;
    jarModalMsg.className = "modal-msg warn";
  }
});

jarSaveBtn.addEventListener("click", async () => {
  const path = jarPathInput.value.trim();
  if (!path) {
    jarModalMsg.textContent = "Path is empty.";
    jarModalMsg.className = "modal-msg err";
    return;
  }
  let res;
  try {
    res = await fetch("/api/config/jar", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    });
  } catch (err) {
    jarModalMsg.textContent = `Save failed: ${err}`;
    jarModalMsg.className = "modal-msg err";
    return;
  }
  if (!res.ok) {
    const text = await res.text();
    jarModalMsg.textContent = `Server rejected: ${text}`;
    jarModalMsg.className = "modal-msg err";
    return;
  }
  jarModalMsg.textContent = "Saved.";
  jarModalMsg.className = "modal-msg ok";
  await refreshJarChip();
  setTimeout(() => jarModal.classList.add("hidden"), 600);
});

refreshJarChip();

// Cached model catalog from /api/llm/models. Re-fetched on every key
let modelCatalog = [];

function _setKeyRowStatus(provider, configured) {
  const el = keyEls[provider].status;
  if (configured) {
    el.textContent = "set";
    el.className = "key-row-status set";
  } else {
    el.textContent = "missing";
    el.className = "key-row-status missing";
  }
}

async function refreshKeyChip() {
  if (!keyChipBtn || !keyStateEl) {
    await refreshModelCatalog();
    return;
  }
  try {
    let proxied = false;
    try {
      const pr = await fetch("/api/config/proxy");
      proxied = !!(await pr.json()).configured;
    } catch {}
    const r = await fetch("/api/config/api_key");
    const info = await r.json();
    const per = info.providers || {};
    for (const p of KEY_PROVIDERS) _setKeyRowStatus(p, per[p]);
    const set = KEY_PROVIDERS.filter((p) => per[p]);
    if (proxied) {
      keyStateEl.innerHTML = `<span class="jar-state-good">course ✓</span>`;
      keyChipBtn.title = "Connected to the course server — no personal " +
        "API key needed. (A personal key, if set, is only used when the " +
        "course server is unreachable.)";
    } else if (set.length === 0) {
      keyStateEl.innerHTML = `<span class="jar-state-missing">missing</span>`;
      keyChipBtn.title = "No LLM API keys configured. Click to add — or " +
        "paste your course server URL + token under Settings.";
    } else if (set.length === KEY_PROVIDERS.length) {
      keyStateEl.innerHTML = `<span class="jar-state-good">${set.length}/${KEY_PROVIDERS.length}</span>`;
      keyChipBtn.title = "All providers configured.";
    } else {
      keyStateEl.innerHTML = `<span class="jar-state-good">${set.length}/${KEY_PROVIDERS.length}</span>`;
      keyChipBtn.title = `Configured: ${set.join(", ")}`;
    }
  } catch (err) {
    keyStateEl.innerHTML = `<span class="jar-state-unknown">unknown</span>`;
    keyChipBtn.title = "Could not read API key status. Click to add a key.";
    console.error("DLC: refreshKeyChip failed:", err);
  }
  await refreshModelCatalog();
}

async function refreshModelCatalog() {
  try {
    const r = await fetch("/api/llm/models");
    const d = await r.json();
    modelCatalog = d.models || [];
    populateModelSelect(d.default);
    populateGraderSelect();
  } catch {
  }
}

const PRODUCTION_MODELS = ["claude-sonnet-4-6", "claude-haiku-4-5-20251001"];
function populateModelSelect(defaultModel) {
  const offered = modelCatalog.filter((m) => PRODUCTION_MODELS.includes(m.id));
  const byProvider = {};
  for (const m of offered) {
    (byProvider[m.provider] = byProvider[m.provider] || []).push(m);
  }
  const previous = l2ModelSelect.value;
  let html = "";
  for (const provider of ["anthropic", "openai"]) {
    const arr = byProvider[provider] || [];
    if (arr.length === 0) continue;
    html += `<optgroup label="${provider}">`;
    for (const m of arr) {
      const tag = m.key_configured ? "" : " (no key)";
      html += `<option value="${m.id}" ${m.key_configured ? "" : "disabled"}>${m.label}${tag}</option>`;
    }
    html += `</optgroup>`;
  }
  l2ModelSelect.innerHTML = html;
  const enabled = offered.filter((m) => m.key_configured).map((m) => m.id);
  if (enabled.includes(previous)) {
    l2ModelSelect.value = previous;
  } else if (defaultModel && enabled.includes(defaultModel)) {
    l2ModelSelect.value = defaultModel;
  } else if (enabled.length > 0) {
    l2ModelSelect.value = enabled[0];
  }
}

const GRADER_DEFAULT = "claude-sonnet-4-6";
function populateGraderSelect() {
  if (!graderSelect) return;
  const offered = modelCatalog.filter((m) => PRODUCTION_MODELS.includes(m.id));
  const byProvider = {};
  for (const m of offered) {
    (byProvider[m.provider] = byProvider[m.provider] || []).push(m);
  }
  const previous = graderSelect.value;
  let html = "";
  for (const provider of ["anthropic", "openai"]) {
    const arr = byProvider[provider] || [];
    if (arr.length === 0) continue;
    html += `<optgroup label="${provider}">`;
    for (const m of arr) {
      const tag = m.key_configured ? "" : " (no key)";
      html += `<option value="${m.id}" ${m.key_configured ? "" : "disabled"}>${m.label}${tag}</option>`;
    }
    html += `</optgroup>`;
  }
  graderSelect.innerHTML = html;
  const enabled = offered.filter((m) => m.key_configured).map((m) => m.id);
  if (enabled.includes(previous)) graderSelect.value = previous;
  else if (enabled.includes(GRADER_DEFAULT)) graderSelect.value = GRADER_DEFAULT;
  else if (enabled.length > 0) graderSelect.value = enabled[0];
}

if (graderSelect) {
  graderSelect.addEventListener("change", () => {
    if (lastGradedSummary) gradeCurrentSummary(lastGradedSummary);
  });
}

refreshKeyChip();

if (keyChipBtn) keyChipBtn.addEventListener("click", () => {
  for (const p of KEY_PROVIDERS) {
    keyEls[p].input.value = "";
    keyEls[p].msg.textContent = "";
    keyEls[p].msg.className = "modal-msg key-row-msg";
  }
  keyModal.classList.remove("hidden");
});
keyCancelBtn.addEventListener("click", () => keyModal.classList.add("hidden"));

function _showKeyRowMsg(provider, text, cls) {
  const el = keyEls[provider].msg;
  el.textContent = text;
  el.className = `modal-msg key-row-msg ${cls}`;
}

document.querySelectorAll(".key-save-btn").forEach((btn) => {
  btn.addEventListener("click", async () => {
    const provider = btn.dataset.provider;
    const key = keyEls[provider].input.value.trim();
    if (!key) {
      _showKeyRowMsg(provider, "Empty.", "err");
      return;
    }
    let res;
    try {
      res = await fetch("/api/config/api_key", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ provider, key }),
      });
    } catch (err) {
      _showKeyRowMsg(provider, `Save failed: ${err}`, "err");
      return;
    }
    if (!res.ok) {
      const t = await res.text();
      _showKeyRowMsg(provider, `Rejected: ${t}`, "err");
      return;
    }
    _showKeyRowMsg(provider, "Saved.", "ok");
    keyEls[provider].input.value = "";
    await refreshKeyChip();
  });
});

document.querySelectorAll(".key-clear-btn").forEach((btn) => {
  btn.addEventListener("click", async () => {
    const provider = btn.dataset.provider;
    if (!confirm(`Clear the saved ${provider} key from ~/.dlc/config.json?`)) return;
    let res;
    try {
      res = await fetch(`/api/config/api_key?provider=${provider}`, { method: "DELETE" });
    } catch (err) {
      _showKeyRowMsg(provider, `Clear failed: ${err}`, "err");
      return;
    }
    if (!res.ok) {
      const t = await res.text();
      _showKeyRowMsg(provider, `Clear failed: ${t}`, "err");
      return;
    }
    const d = await res.json();
    _showKeyRowMsg(
      provider,
      d.configured ? "Cleared from config (env var still set)." : "Cleared.",
      "ok",
    );
    keyEls[provider].input.value = "";
    await refreshKeyChip();
  });
});

let l2LibraryFilename = null;  

async function refreshLibrary() {
  if (!sessionId || loaded.length === 0) {
    libraryGridEl.innerHTML = `<div class="muted">Load a circuit on the Dashboard tab to populate the library.</div>`;
    l2LibraryFilename = null;
    return;
  }
  const file = loaded[currentIdx];
  if (file.error) {
    libraryGridEl.innerHTML = `<div class="muted">Could not parse this file; no library to show.</div>`;
    return;
  }
  if (l2LibraryFilename === file.filename) return;

  libraryGridEl.innerHTML = `<div class="muted">Loading library...</div>`;
  let res;
  try {
    res = await fetch(`/api/library?session_id=${encodeURIComponent(sessionId)}&filename=${encodeURIComponent(file.filename)}`);
  } catch (err) {
    libraryGridEl.innerHTML = `<div style="color:#991b1b">Library fetch failed: ${escapeHtml(String(err))}</div>`;
    return;
  }
  if (!res.ok) {
    libraryGridEl.innerHTML = `<div style="color:#991b1b">Library error: ${res.status}</div>`;
    return;
  }
  const data = await res.json();
  renderLibrary(data.cards || []);
  l2LibraryFilename = file.filename;
}

function renderLibrary(cards) {
  if (cards.length === 0) {
    libraryGridEl.innerHTML = `<div class="muted">No components in this circuit.</div>`;
    return;
  }
  libraryGridEl.innerHTML = cards.map((c, i) => `
    <div class="library-card" data-card-idx="${i}">
      ${c.count > 1 ? `<span class="count">${c.count}</span>` : ""}
      <img src="/static/images/components/${escapeHtml(c.image)}"
           alt="${escapeHtml(c.display_name)}"
           onerror="this.onerror=null;this.src='/static/images/components/placeholder.png';" />
      <div class="name">${escapeHtml(c.display_name)}</div>
    </div>
  `).join("");
  libraryGridEl.querySelectorAll(".library-card").forEach((el) => {
    const card = cards[parseInt(el.dataset.cardIdx, 10)];
    el.addEventListener("click", () => openCardDetail(card, { pinned: true }));
    if (CARD_HOVER_CAPABLE) {
      el.addEventListener("mouseenter", () => onCardHover(card));
      el.addEventListener("mouseleave", scheduleCardClose);
    }
  });
}

const CARD_HOVER_CAPABLE =
  !!(window.matchMedia && window.matchMedia("(hover: hover) and (pointer: fine)").matches);
const CARD_OPEN_DELAY = 180;   
const CARD_CLOSE_DELAY = 240; 
let cardPinned = false;
let cardOpenTimer = null;
let cardCloseTimer = null;

function _clearCardTimers() {
  if (cardOpenTimer) { clearTimeout(cardOpenTimer); cardOpenTimer = null; }
  if (cardCloseTimer) { clearTimeout(cardCloseTimer); cardCloseTimer = null; }
}

function onCardHover(card) {
  if (cardPinned) return;                     
  _clearCardTimers();
  if (!cardOverlay.classList.contains("hidden")) {
    openCardDetail(card, { pinned: false });    
  } else {
    cardOpenTimer = setTimeout(() => openCardDetail(card, { pinned: false }), CARD_OPEN_DELAY);
  }
}

function scheduleCardClose() {
  if (cardPinned) return;
  _clearCardTimers();
  cardCloseTimer = setTimeout(closeCardDetail, CARD_CLOSE_DELAY);
}

function openCardDetail(card, { pinned = false } = {}) {
  if (!card) return;
  _clearCardTimers();
  cardPinned = pinned;
  cardDetail.innerHTML =
    `<button class="card-close" type="button" aria-label="Close">&times;</button>` +
    renderCardDetail(card);
  const flip = cardDetail.querySelector(".cardflip");
  if (flip) {
    const real = flip.querySelector(".cf-realimg");
    const noReal = () => flip.classList.add("no-real");
    if (!real) {
      noReal();
    } else {
      if (real.complete && real.naturalWidth === 0) noReal();
      real.addEventListener("error", noReal);
      real.addEventListener("load", () => { if (real.naturalWidth === 0) noReal(); });
    }
    flip.addEventListener("click", (ev) => {
      ev.stopPropagation();
      if (!flip.classList.contains("no-real")) flip.classList.toggle("flipped");
    });
  }
  cardOverlay.classList.remove("hidden");
  cardOverlay.classList.toggle("preview", !pinned); 
  if (pinned) cardDetail.focus({ preventScroll: true });
  cardDetail.scrollTop = 0;
}

function closeCardDetail() {
  _clearCardTimers();
  cardPinned = false;
  cardOverlay.classList.add("hidden");
  cardOverlay.classList.remove("preview");
  cardDetail.innerHTML = "";
}

cardDetail.addEventListener("mouseenter", () => { if (!cardPinned) _clearCardTimers(); });
cardDetail.addEventListener("mouseleave", scheduleCardClose);

cardDetail.addEventListener("click", (e) => {
  if (e.target.closest(".card-close")) closeCardDetail();
});

cardOverlay.addEventListener("click", (e) => {
  if (e.target === cardOverlay) closeCardDetail();
});

window.addEventListener("keydown", (e) => {
  if (cardOverlay.classList.contains("hidden")) return;
  if (e.key === "ArrowDown") {
    cardDetail.scrollTop += 40; e.preventDefault();
  } else if (e.key === "ArrowUp") {
    cardDetail.scrollTop -= 40; e.preventDefault();
  } else if (e.key === "PageDown") {
    cardDetail.scrollTop += cardDetail.clientHeight - 30; e.preventDefault();
  } else if (e.key === "PageUp") {
    cardDetail.scrollTop -= cardDetail.clientHeight - 30; e.preventDefault();
  } else if (e.key === "Escape") {
    closeCardDetail(); e.preventDefault();
  }
});

// Real-world analog shown on the flipped (back) side of each component card.
// Keyed by the Digital image filename. Edit the text freely; the real-world
// photo must live at /static/images/components_real/<same filename>.
// Components with no real-world analog: show only the Digital glyph —
// no flip, no "try hover/tap", no back face.
const NO_REAL_IMAGE = new Set([
  "bit_extender.png", "decoder.png", "in.png", "out.png", "register.png",
  "seven_seg.png", "splitter.png", "subcircuit.png", "tunnel.png",
  "nmos.png", "pmos.png", "pullup.png", "pulldown.png",
]);

const REAL_CAPTIONS = {
  "adder.png": "Outputs the sum of two binary numbers",
  "and.png": "Two switches in series — on only when both are closed",
  "barrel_shifter.png": "Shifts bits left/right by a chosen amount at once (logical/arithmetic)",
  "clock.png": "Electronic logic signal (voltage or current) which oscillates between a high and a low state at a constant frequency",
  "comparator.png": "Says whether A is less than, equal to, or greater than B",
  "const.png": "A hard-wired number",
  "ground.png": "Logic-0 reference",
  "mux.png": "Routes one of several inputs out, chosen by a signal.",
  "nand.png": "(4049 CMOS)The universal gate — AND then NOT; any logic can be built from these",
  "nor.png": "(4049 CMOS) OR then NOT — on only when every input is off",
  "not.png": "Outputs the opposite of its input",
  "or.png": "Two switches in parallel, on if either is closed",
  "priority_encoder.png": "Outputs the index of the highest active input",
  "rom.png": "A printed lookup table / Read-Only-Memory — returns a fixed stored word for each address",
  "vdd.png": "The logic-1 reference",
  "xnor.png": "On when both inputs match (2)",
  "xor.png": "On when inputs differ (2)",
};


function renderCardDetail(card) {
  const extra = card.extra || {};
  const truth2 = (extra.truth_table_2 || []).length
    ? `<h4>Truth table (2 inputs)</h4>` + renderTruthTable(extra.truth_table_2)
    : "";
  const truth3 = (extra.truth_table_3 || []).length
    ? `<h4>Truth table (3 inputs)</h4>` + renderTruthTable(extra.truth_table_3)
    : "";
  const behaviour = extra.behavior_example
    ? `<h4>Example behavior</h4><div class="behavior">${escapeHtml(extra.behavior_example)}</div>`
    : "";
  const note = (card.transistor_note && !String(card.transistor_note).startsWith("N/A"))
    ? `<p class="muted" style="font-size:11.5px;">${escapeHtml(card.transistor_note)}</p>`
    : "";
  const tcount = (card.transistor_count && !String(card.transistor_count).startsWith("N/A"))
    ? `<span>transistors: ${escapeHtml(card.transistor_count)}</span>`
    : "";

  const frontImg =
    `<img class="detail-img" src="/static/images/components/${escapeHtml(card.image)}"
          alt="${escapeHtml(card.display_name)}"
          onerror="this.onerror=null;this.src='/static/images/components/placeholder.png';" />`;

  const imgBlock = NO_REAL_IMAGE.has(card.image)
    ? `<div class="cardflip no-real"><div class="cardflip-inner">
         <div class="cardflip-face cardflip-front">${frontImg}</div>
       </div></div>`
    : `<div class="cardflip">
      <div class="cardflip-inner">
        <div class="cardflip-face cardflip-front">
          ${frontImg}
          <div class="cf-hint">Try hover or tap</div>
        </div>
        <div class="cardflip-face cardflip-back">
          <img class="detail-img cf-realimg"
               src="/static/images/components_real/${escapeHtml(card.image)}"
               alt="${escapeHtml(card.display_name)} in the real world" />
          <div class="cf-callout"><span class="cf-dot"></span><span class="cf-line"></span><span class="cf-text">${escapeHtml(REAL_CAPTIONS[card.image] || "")}</span></div>
        </div>
      </div>
    </div>`;

  return `
    ${imgBlock}
    <div>
      <div class="detail-head">
        <div class="detail-name">${escapeHtml(card.display_name)}</div>
        <div class="detail-meta">
          ${card.port_summary ? `<span class="pill-small">${escapeHtml(card.port_summary)}</span>` : ""}
          ${tcount}
        </div>
      </div>
      <div class="detail-body">
        <p>${escapeHtml(card.description || "")}</p>
        ${note}
        ${truth2}
        ${truth3}
        ${behaviour}
      </div>
    </div>
  `;
}

function renderTruthTable(rows) {
  if (rows.length === 0) return "";
  const inLen = rows[0].in.length;
  const headers = [];
  for (let i = 0; i < inLen; i++) headers.push(`<td>in${i}</td>`);
  headers.push(`<td>out</td>`);
  const body = rows.map((r) => {
    const cls = r.out ? "true" : "false";
    const cells = r.in.map((v) => `<td>${v}</td>`).join("");
    return `<tr class="${cls}">${cells}<td class="out">${r.out}</td></tr>`;
  }).join("");
  return `<table class="truth-table"><thead><tr>${headers.join("")}</tr></thead><tbody>${body}</tbody></table>`;
}

goalTextarea.addEventListener("input", () => {
  const chars = goalTextarea.value.length;
  goalCountEl.textContent = `${chars} / 500 characters`;
  goalCountEl.style.color = chars >= 500 ? "#b91c1c" : "";
});

let l2Abort = null;
function l2BeginAbortable() {
  l2Abort = new AbortController();
  if (l2StopBtn) l2StopBtn.disabled = false;
  return l2Abort.signal;
}
function l2EndAbortable() {
  l2Abort = null;
  if (l2StopBtn) l2StopBtn.disabled = true;
}
if (l2StopBtn) {
  l2StopBtn.addEventListener("click", () => { if (l2Abort) l2Abort.abort(); });
}

l2LlmBtn.addEventListener("click", async () => {
  if (!sessionId || loaded.length === 0) {
    l2LlmStatus.textContent = "Load a circuit first.";
    l2LlmStatus.className = "l2-llm-status error";
    return;
  }
  const file = loaded[currentIdx];
  if (file.error) {
    l2LlmStatus.textContent = "Current file failed to parse.";
    l2LlmStatus.className = "l2-llm-status error";
    return;
  }

  const goal = goalTextarea.value.trim();
  if (goal.length > 500) {
    l2LlmStatus.textContent = "Goal too long (500 character max).";
    l2LlmStatus.className = "l2-llm-status error";
    return;
  }

  let testSummary = null;
  const slot = testState[file.filename];
  if (slot && slot.status === "done" && slot.payload) {
    if (slot.payload.all_passed === true) testSummary = "All rows passed.";
    else if (slot.payload.all_passed === false) testSummary = "Some rows failed.";
  }

  const selectedModel = l2ModelSelect.value || null;
  const selectedInfo = modelCatalog.find((m) => m.id === selectedModel);
  if (selectedInfo && !selectedInfo.key_configured) {
    l2LlmStatus.textContent =
      `No AI connection for ${selectedInfo.provider} — open Settings and connect to your course server.`;
    l2LlmStatus.className = "l2-llm-status error";
    return;
  }

  const signal = l2BeginAbortable();
  l2LlmBtn.disabled = true;
  l2LlmStatus.innerHTML =
    `Talking to ${escapeHtml(selectedInfo ? selectedInfo.label : "the model")}` +
    `<span class="llm-dots" aria-hidden="true"><i></i><i></i><i></i></span>`;
  l2LlmStatus.className = "l2-llm-status running";
  l2LlmOutput.innerHTML = "";
  l2LlmOutput.classList.add("empty");
  logEvent("l2_llm_started", {
    filename: file.filename, has_goal: goal.length > 0, model: selectedModel,
  });

  let res;
  try {
    res = await fetch("/api/llm/explain", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        session_id: sessionId,
        filename: file.filename,
        student_goal: goal || null,
        test_summary: testSummary,
        model: selectedModel,
      }),
      signal,
    });
  } catch (err) {
    l2LlmBtn.disabled = false;
    l2EndAbortable();
    if (err.name === "AbortError") {
      // Stopped during summarization -> grader was never triggered, so the
      // grade panel stays untouched; show a red "Stopped" on the summary.
      l2LlmStatus.textContent = "";
      l2LlmStatus.className = "l2-llm-status";
      l2LlmOutput.classList.remove("empty");
      l2LlmOutput.innerHTML = `<div style="color:#dc2626;font-weight:600;">Stopped.</div>`;
      return;
    }
    l2LlmStatus.textContent = `Network error: ${err}`;
    l2LlmStatus.className = "l2-llm-status error";
    return;
  }
  l2LlmBtn.disabled = false;

  if (!res.ok) {
    const t = await res.text();
    l2LlmStatus.textContent = `Server error ${res.status}: ${t}`;
    l2LlmStatus.className = "l2-llm-status error";
    l2EndAbortable();
    return;
  }
  const payload = await res.json();
  logEvent("l2_llm_complete", { filename: file.filename, ok: payload.ok, gated: !!payload.gate_message });

  if (!payload.ok) {
    l2LlmStatus.textContent = `Error: ${payload.error || "unknown"}`;
    l2LlmStatus.className = "l2-llm-status error";
    l2EndAbortable();
    return;
  }

  if (payload.gate_message) {
    l2LlmStatus.textContent = "Precheck blocked the summary.";
    l2LlmStatus.className = "l2-llm-status gated";
    l2LlmOutput.classList.remove("empty");
    l2LlmOutput.textContent = payload.gate_message;
    l2EndAbortable();
    return;
  }

  l2LlmStatus.textContent = "Done.";
  l2LlmStatus.className = "l2-llm-status done";
  l2LlmOutput.classList.remove("empty");
  l2Extras = {
    filename: file.filename,
    exampleRow: payload.example_row || null,
    roles: payload.subcircuit_roles || [],
    walk: null, walkPending: !!payload.example_row, walkError: null,
  };
  l2LlmOutput.innerHTML = renderL2ParagraphCards(payload.text || "(empty response)", l2Extras);
  wireL2CardEvents();
  // The walkthrough is deterministic (no model call): fetch it alongside
  // the grade so the flow card fills in without waiting for anything.
  if (l2Extras.exampleRow) l2FetchWalkthrough(file.filename, l2Extras.exampleRow);

  // Grade the summary just shown, keeping the same abort scope so Stop also
  // cancels grading; if there's nothing to grade, close the scope here.
  if (payload.text) gradeCurrentSummary(payload.text);
  else l2EndAbortable();
});

  /*# ───────────────────────────────────────────────────────────────────
  *#  L2 signal-flow walkthrough: expression in the flow card + a player
  *#  that walks the example row through the Dashboard graph
  *# ──────────────────────────────────────────────────────────────────#*/
let l2Extras = { filename: null, exampleRow: null, roles: [], walk: null,
                 walkPending: false, walkError: null };
let l2WalkState = null;

async function l2FetchWalkthrough(filename, exampleRow) {
  let body = null;
  try {
    const res = await fetch("/api/l2/walkthrough", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        session_id: sessionId, filename,
        spec_index: exampleRow.spec_index, row_index: exampleRow.row_index,
      }),
    });
    body = res.ok ? await res.json() : { ok: false, warning: `Server error ${res.status}` };
  } catch (err) {
    body = { ok: false, warning: `Network error: ${err}` };
  }
  if (l2Extras.filename !== filename) return;      // a newer summary replaced this one
  l2Extras.walkPending = false;
  if (body && body.ok) l2Extras.walk = body;
  else l2Extras.walkError = (body && body.warning) || "walkthrough unavailable";
  l2RefreshFlowCard();
}

function l2RefreshFlowCard() {
  const body = l2LlmOutput.querySelector(".l2-card-flow .l2-card-body");
  if (!body) return;
  body.innerHTML = l2FlowBodyHtml(l2Extras.flowText || "", l2Extras);
}

// Sentences of a paragraph; filenames such as alu.dig keep their dot.
function l2Sentences(text) {
  const guarded = text.replace(/\.dig\b/g, "․dig");
  const parts = guarded.match(/[^.!?]+[.!?]+(?:\s+|$)|[^.!?]+$/g) || [guarded];
  return parts.map((s) => s.replace(/․dig/g, ".dig").trim()).filter(Boolean);
}

function l2SubsBodyHtml(text, roles) {
  if (!roles || !roles.length) return escapeHtml(text);
  const sentences = l2Sentences(text);
  const used = new Set();
  const items = roles.map((r) => {
    const ref = String(r.reference || "");
    const stem = ref.replace(/\.dig$/i, "");
    const mine = [];
    sentences.forEach((s, i) => {
      if (used.has(i)) return;
      const low = s.toLowerCase();
      if (low.includes(ref.toLowerCase()) || (stem && low.includes(stem.toLowerCase() + "."))) {
        used.add(i); mine.push(s);
      }
    });
    return `<li><span class="l2-sub-name">${escapeHtml(ref)}</span> ` +
      `<span class="l2-sub-role">${escapeHtml(r.role || "")}</span>` +
      (mine.length ? `<div class="l2-sub-model">${escapeHtml(mine.join(" "))}</div>` : "") +
      `</li>`;
  });
  const rest = sentences.filter((_, i) => !used.has(i));
  return `<div class="l2-rich"><ol class="l2-sub-list">${items.join("")}</ol>` +
    (rest.length ? `<div class="l2-sub-rest">${escapeHtml(rest.join(" "))}</div>` : "") +
    `</div>`;
}

function l2FlowBodyHtml(text, extras) {
  let html = `<div class="l2-flow-prose">${escapeHtml(text)}</div>`;
  const ex = extras && extras.exampleRow;
  if (!ex) {
    return html + `<div class="l2-rich"><div class="l2-walk-hint">No test row to replay for this file.</div></div>`;
  }
  const walk = extras.walk;
  let block = "";
  if (walk) {
    // the row the walkthrough replays (the same row the model was told to trace)
    block = `<div class="l2-walk-hint">Row ${walk.row_index} of the tests: ` +
      `<code>${escapeHtml(walk.raw || "")}</code></div>`;
    const unknown = (walk.outputs || []).filter((o) => o.found == null).map((o) => o.label);
    const assumed = walk.assumptions || [];
    if (assumed.length) {
      block += `<div class="l2-walk-hint">Where the evaluator was stuck, values marked * were assumed: ` +
        `${escapeHtml(assumed.slice(0, 3).join("; "))}${assumed.length > 3 ? `; and ${assumed.length - 3} more` : ""}.</div>`;
      const off = (walk.outputs || []).filter((o) => o.ok === false && o.assumed_upstream)
        .map((o) => `${o.label} gives ${o.found} where the row expects ${o.expected}`);
      if (off.length) {
        block += `<div class="l2-walk-hint">With those assumed values ${escapeHtml(off.join("; "))}; ` +
          `the walkthrough still plays, marked values included.</div>`;
      }
    }
    if (unknown.length) {
      // the evaluator could not compute the row (memory, clocked state it
      // does not model): not the circuit's fault, so no "invalid" verdict
      block += `<div class="l2-walk-invalid">No walkthrough for this row: the built-in evaluator cannot compute ` +
        `${unknown.length ? escapeHtml(unknown.join(", ")) + " " : "it "}(${walk.unresolved || 0} net value${walk.unresolved === 1 ? "" : "s"} ` +
        `stay unknown). The Dashboard row view shows the same; the official tests need Digital.jar.</div>`;
    } else if (walk.valid === false) {
      const bad = (walk.outputs || []).filter((o) => o.ok === false)
        .map((o) => `${o.label} gives ${o.found == null ? "?" : o.found} instead of ${o.expected}`).join("; ");
      const unk = walk.unresolved ? ` ${walk.unresolved} net value${walk.unresolved === 1 ? "" : "s"} stay unknown to the built-in evaluator, which may be the cause.` : "";
      block += `<div class="l2-walk-invalid">Flow example invalid: on this row your circuit does not produce ` +
        `the expected outputs (${escapeHtml(bad)}).${unk} Run the tests on the Dashboard and use Mode A on the L3 Coach tab first.</div>`;
    } else if (!(walk.steps || []).length) {
      block += `<div class="l2-walk-hint">Nothing to walk through on this row.</div>`;
    } else {
      const n = (walk.steps || []).length;
      block += `<div class="l2-walk-row"><button type="button" class="l2-walk-btn" data-l2-walk="1">` +
        `&#9654; Play the walkthrough on the circuit</button>` +
        `<span class="l2-walk-hint">${n} component${n === 1 ? "" : "s"} in ${walk.waves || 1} wave${walk.waves === 1 ? "" : "s"}, ` +
        `one Next click each, on the Dashboard graph</span></div>`;
    }
    if (walk.valid !== false && walk.unresolved) {
      block += `<div class="l2-walk-hint">${walk.unresolved} net value${walk.unresolved === 1 ? "" : "s"} stay unknown to the built-in evaluator and show as ?.</div>`;
    }
    if ((walk.notes || []).length) {
      block += `<div class="l2-walk-hint">${escapeHtml(walk.notes.join(" "))}</div>`;
    }
  } else if (extras.walkPending) {
    block = `<div class="l2-walk-hint">Preparing the walkthrough of row ${ex.row_index}<span class="llm-dots" aria-hidden="true"><i></i><i></i><i></i></span></div>`;
  } else {
    block = `<div class="l2-walk-hint">Walkthrough unavailable: ${escapeHtml(extras.walkError || "")}</div>`;
  }
  return html + `<div class="l2-rich">${block}</div>`;
}

l2LlmOutput.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-l2-walk]");
  if (!btn) return;
  e.stopPropagation();
  l2PlayWalkthrough();
});

function l2WalkShield(on) {
  let el = document.getElementById("l2-walk-shield");
  if (on) {
    if (!el) {
      el = document.createElement("div");
      el.id = "l2-walk-shield";
      document.body.appendChild(el);
    }
    el.classList.remove("hidden");
  } else if (el) {
    el.classList.add("hidden");
  }
}

function l2WalkBoard() {
  let el = document.getElementById("l2-walk-board");
  if (!el) {
    el = document.createElement("div");
    el.id = "l2-walk-board";
    el.innerHTML =
      `<div class="l2-walk-head"><span class="l2-walk-title">Signal-flow walkthrough</span>` +
      `<span class="l2-walk-where"></span></div>` +
      `<div class="l2-walk-text"></div>` +
      `<div class="l2-walk-bar"><i></i></div>` +
      `<div class="l2-walk-ctl">` +
      `<button class="l2-walk-next" title="light the next wave (→ or Enter)">Next &#9654;</button>` +
      `<button class="l2-walk-replay hidden" title="start over">&#8635; Replay</button>` +
      `<button class="l2-walk-finish hidden" title="back to the summary">Finish</button>` +
      `<span class="l2-walk-count"></span></div>`;
    el.querySelector(".l2-walk-next").onclick = l2WalkNext;
    el.querySelector(".l2-walk-replay").onclick = () => l2WalkStart();
    el.querySelector(".l2-walk-finish").onclick = l2WalkFinish;
    document.body.appendChild(el);
  }
  el.classList.remove("hidden");
  return el;
}

function l2WalkPointer() {
  const pane = document.getElementById("graph-pane");
  if (!pane) return null;
  let el = document.getElementById("l2-walk-pointer");
  if (!el) {
    el = document.createElement("div");
    el.id = "l2-walk-pointer";
    el.textContent = "➤";
    pane.appendChild(el);
  }
  el.classList.remove("hidden");
  return el;
}

function l2WalkPlacePointer() {
  const st = l2WalkState;
  if (!st || !cy) return;
  const wave = st.waves[st.k - 1];
  const step = wave && wave[0];
  const n = step ? cy.getElementById(String(step.component_index)) : null;
  const el = document.getElementById("l2-walk-pointer");
  const box = document.getElementById("cy");
  if (!el || !box || !n || n.empty()) return;
  const p = n.renderedPosition();
  el.style.left = `${box.offsetLeft + p.x + 6}px`;
  el.style.top = `${box.offsetTop + p.y + 6}px`;
}

function l2LightEdge(e, nv) {
  const info = nv[String(e.data("net_id"))];
  e.removeClass("sig-dim sig-none sig-hi sig-lo sig-bus");
  if (!info) { e.addClass("sig-none"); return; }
  const bits = info.bits || 1;
  const star = info.assumed ? "*" : "";
  if (bits <= 1) {
    e.addClass(info.value ? "sig-hi" : "sig-lo");
    if (star) e.data("sigLabel", String(info.value) + star);
  } else {
    e.addClass("sig-bus");
    e.data("sigLabel", (bits <= 8 && info.value != null ? String(info.value) : "0x" + (info.hex || "0")) + star);
  }
}

function l2WalkLightOutputs(st, lit) {
  const nv = st.walk.net_values || {};
  const found = {};
  (st.walk.outputs || []).forEach((o) => { found[o.label] = o.found; });
  cy.nodes(".sig-dim").forEach((n) => {
    if (n.data("element_name") !== "Out") return;
    const inc = n.incomers("edge");
    if (inc.empty() || inc.sources().some((s) => s.hasClass("sig-dim"))) return;
    n.removeClass("sig-dim").addClass("walk-focus");
    lit.merge(n);
    inc.forEach((e) => { l2LightEdge(e, nv); e.addClass("walk-edge"); });
    const v = found[n.data("comp_label")];
    if (v != null) {
      if (n.data("baseLabel") == null) n.data("baseLabel", n.data("label"));
      n.data("label", n.data("baseLabel") + "\n= " + v);
    }
  });
}

function l2WalkLightRemainingOutputs(st, lit) {
  const found = {};
  (st.walk.outputs || []).forEach((o) => { found[o.label] = o.found; });
  cy.nodes(".sig-dim").forEach((n) => {
    if (n.data("element_name") !== "Out") return;
    const v = found[n.data("comp_label")];
    if (v == null) return;
    n.removeClass("sig-dim").addClass("walk-focus");
    lit.merge(n);
    if (n.data("baseLabel") == null) n.data("baseLabel", n.data("label"));
    n.data("label", n.data("baseLabel") + "\n= " + v);
  });
}

function l2WalkKeepVisible(lit) {
  if (lit.empty()) return;
  const bb = lit.renderedBoundingBox();
  const w = cy.width(), h = cy.height(), m = 12;
  if (bb.x1 >= m && bb.y1 >= m && bb.x2 <= w - m && bb.y2 <= h - m) return;
  try { cy.animate({ center: { eles: lit }, duration: 350 }); } catch {}
}

async function l2PlayWalkthrough() {
  const walk = l2Extras.walk;
  const file = loaded.length > 0 ? loaded[currentIdx] : null;
  if (!walk || walk.valid === false || !(walk.steps || []).length ||
      !file || file.filename !== l2Extras.filename) return;
  showTab("main");
  if (!cy) return;
  logEvent("l2_walkthrough_played", { filename: file.filename, waves: walk.waves });
  l2WalkStart();
}

function l2WalkStart() {
  const walk = l2Extras.walk;
  if (!walk || !cy) return;
  stopClockTick();
  clockDone = false;
  hideClockHud();
  sigActive = null;
  clearSignalFlow(cy);
  const waves = [];
  walk.steps.forEach((s) => {
    const w = Math.max(1, s.wave || 1);
    while (waves.length < w) waves.push([]);
    waves[w - 1].push(s);
  });
  l2WalkState = { walk, waves, k: 0 };
  cy.batch(() => {
    cy.elements().addClass("sig-dim");
    cy.nodes().removeClass("walk-focus walk-done");
    cy.edges().removeClass("walk-edge");
    (walk.sources || []).forEach((idx) => {
      const n = cy.getElementById(String(idx));
      if (n && n.nonempty()) n.removeClass("sig-dim");
    });
  });
  try { cy.fit(undefined, 60); } catch {}
  l2WalkShield(true);
  const board = l2WalkBoard();
  board.querySelector(".l2-walk-where").textContent =
    `row ${walk.row_index} of '${walk.spec_name || ""}' · ${waves.length} wave${waves.length === 1 ? "" : "s"}`;
  const ins = (walk.inputs || []).map((i) => `${i.label} = ${i.text}`).join(", ");
  board.querySelector(".l2-walk-text").innerHTML =
    `<b>Ready.</b> The inputs and the registers' current values are lit. ` +
    `Each <b>Next</b> lets the signal reach the next group of components; ` +
    `a component waits until all of its inputs have arrived.` +
    ((walk.assumptions || []).length ? ` Values marked <b>*</b> were assumed where the evaluator was stuck.` : "") +
    (ins ? `<div class="l2-walk-final">Inputs: ${escapeHtml(ins)}.</div>` : "");
  board.querySelector(".l2-walk-bar > i").style.width = "0%";
  board.querySelector(".l2-walk-count").textContent = `0 / ${waves.length}`;
  board.querySelector(".l2-walk-next").classList.remove("hidden");
  board.querySelector(".l2-walk-next").disabled = false;
  board.querySelector(".l2-walk-replay").classList.add("hidden");
  board.querySelector(".l2-walk-finish").classList.add("hidden");
  const ptr = document.getElementById("l2-walk-pointer");
  if (ptr) ptr.classList.add("hidden");
  cy.off("pan zoom resize", l2WalkPlacePointer);
  cy.on("pan zoom resize", l2WalkPlacePointer);
}

const L2_WALK_MAX_LINES = 8;   // sentences told per wave; the rest are counted

function l2WalkNext() {
  const st = l2WalkState;
  if (!st || !cy || st.k >= st.waves.length) return;
  const nv = st.walk.net_values || {};
  const svgs = st.walk.node_svgs || {};
  const wave = st.waves[st.k];
  st.k += 1;
  const lit = cy.collection();
  cy.batch(() => {
    cy.nodes(".walk-focus").removeClass("walk-focus").addClass("walk-done");
    cy.edges(".walk-edge").removeClass("walk-edge");
    wave.forEach((s) => {
      const n = cy.getElementById(String(s.component_index));
      if (!n || n.empty()) return;
      n.removeClass("sig-dim").addClass("walk-focus");
      lit.merge(n);
      const nets = new Set((s.inputs || []).map((e) => e.net_id));
      n.incomers("edge").forEach((e) => {
        if (!nets.has(e.data("net_id"))) return;
        l2LightEdge(e, nv);
        e.addClass("walk-edge");
        e.source().removeClass("sig-dim");
      });
      const svg = svgs[String(s.component_index)];
      if (svg) {
        if (n.data("baseShape") == null) n.data("baseShape", n.data("shape_svg"));
        n.data("shape_svg", svg);
      }
    });
    l2WalkLightOutputs(st, lit);
    if (st.k >= st.waves.length) l2WalkLightRemainingOutputs(st, lit);
  });
  const board = l2WalkBoard();
  const last = st.k >= st.waves.length;
  const active = wave.filter((s) => s.active !== false);
  const quiet = wave.filter((s) => s.active === false);
  const told = active.concat(quiet).slice(0, L2_WALK_MAX_LINES);
  const rest = wave.length - told.length;
  const restQuiet = quiet.length - Math.max(0, told.length - active.length);
  const more = rest > 0
    ? `<li class="l2-walk-more">… and ${rest} more component${rest === 1 ? "" : "s"}` +
      (restQuiet === rest ? ` whose outputs stay 0` : "") + `</li>` : "";
  board.querySelector(".l2-walk-text").innerHTML =
    `<b>Wave ${st.k}.</b> ${wave.length} component${wave.length === 1 ? "" : "s"}<ul class="l2-walk-list">` +
    told.map((s) => `<li>${escapeHtml(s.text)}</li>`).join("") + more + `</ul>` +
    (last ? `<div class="l2-walk-final">Outputs: ${escapeHtml((st.walk.outputs || []).map((o) =>
        `${o.label} = ${o.found == null ? "?" : o.found}`).join(", "))}. Done: ` +
        `Finish returns to the summary, Replay starts over.</div>` : "");
  board.querySelector(".l2-walk-count").textContent = `${st.k} / ${st.waves.length}`;
  board.querySelector(".l2-walk-bar > i").style.width = `${Math.round(100 * st.k / st.waves.length)}%`;
  if (last) {
    board.querySelector(".l2-walk-next").classList.add("hidden");
    board.querySelector(".l2-walk-replay").classList.remove("hidden");
    board.querySelector(".l2-walk-finish").classList.remove("hidden");
  }
  if (lit.nonempty()) {
    l2WalkKeepVisible(lit);
    l2WalkPointer();
    setTimeout(l2WalkPlacePointer, 380);
  }
}

function l2WalkFinish() {
  const had = !!l2WalkState;
  l2WalkState = null;
  if (cy) {
    cy.off("pan zoom resize", l2WalkPlacePointer);
    cy.nodes().removeClass("walk-focus walk-done");
    cy.edges().removeClass("walk-edge");
    clearSignalFlow(cy);
  }
  l2WalkShield(false);
  const board = document.getElementById("l2-walk-board");
  if (board) board.classList.add("hidden");
  const ptr = document.getElementById("l2-walk-pointer");
  if (ptr) ptr.classList.add("hidden");
  if (had) showTab("l2");
}

window.addEventListener("keydown", (e) => {
  if (!l2WalkState) return;
  if (e.key === "ArrowRight" || e.key === "Enter") {
    if (l2WalkState.k < l2WalkState.waves.length) l2WalkNext();
    e.preventDefault();
  }
});

  /*# ───────────────────────────────────────────────────────────────────
  *#  L2 summary grading and donut chart
  *# ──────────────────────────────────────────────────────────────────#*/
const GRADE_COLORS = ["#3b82f6", "#10b981", "#8b5cf6", "#f59e0b", "#ec4899", "#06b6d4", "#f97316"];

function _bandColor(band) {
  return band === "green" ? "#16a34a" : band === "yellow" ? "#d97706" : "#dc2626";
}
function _polar(cx, cy, r, deg) {
  const a = (deg - 90) * Math.PI / 180;
  return [cx + r * Math.cos(a), cy + r * Math.sin(a)];
}
function _arcPath(cx, cy, r, start, end) {
  if (end - start >= 359.999) end = start + 359.999;
  const [x1, y1] = _polar(cx, cy, r, start);
  const [x2, y2] = _polar(cx, cy, r, end);
  const large = end - start > 180 ? 1 : 0;
  return `M ${x1.toFixed(2)} ${y1.toFixed(2)} A ${r} ${r} 0 ${large} 1 ${x2.toFixed(2)} ${y2.toFixed(2)}`;
}

function _resetGrade() {
  lastGradedSummary = null;
  if (gradeBody) {
    gradeBody.innerHTML = `<div class="muted">Summarize a circuit to see its credibility grade out of 100.</div>`;
  }
}

async function gradeCurrentSummary(summaryText) {
  if (!gradeBody || !sessionId || loaded.length === 0) return;
  const file = loaded[currentIdx];
  if (!file || file.error) return;
  lastGradedSummary = summaryText;
  const graderModel = graderSelect ? graderSelect.value || null : null;
  // Reuse the summarize flow's abort scope if present; a standalone re-grade
  // (grader-dropdown change) opens its own so Stop works there too.
  const signal = l2Abort ? l2Abort.signal : l2BeginAbortable();

  gradeBody.innerHTML =
    `<span class="muted">Grading with ${escapeHtml(graderModel || "default")}` +
    `<span class="llm-dots" aria-hidden="true"><i></i><i></i><i></i></span></span>`;

  let res;
  try {
    res = await fetch("/api/llm/grade", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        session_id: sessionId,
        filename: file.filename,
        summary_text: summaryText,
        student_goal: goalTextarea.value.trim() || null,
        grader_model: graderModel,
      }),
      signal,
    });
  } catch (err) {
    l2EndAbortable();
    if (err.name === "AbortError") {
      gradeBody.innerHTML = `<span style="color:#dc2626;font-weight:600;">Grading stopped.</span>`;
      return;
    }
    gradeBody.innerHTML = `<span style="color:#b91c1c">Grade request failed: ${escapeHtml(String(err))}</span>`;
    return;
  }
  let g;
  try { g = await res.json(); } catch { g = null; }
  if (!g || !g.ok) {
    gradeBody.innerHTML = `<span style="color:#b91c1c">${escapeHtml((g && g.error) || ("Grader error " + res.status))}</span>`;
    l2EndAbortable();
    return;
  }
  renderGradeDonut(g);
  l2EndAbortable();

  // One summary -> one grade. A low score only SUGGESTS a re-run; the
  // user decides (the old auto-retry silently re-clicked Summarize).
  if (typeof g.total === "number" && g.total < GRADE_HINT_THRESHOLD) {
    const host = gradeBody.querySelector(".grade-info") || gradeBody;
    const n = document.createElement("div");
    n.className = "grade-note";
    n.textContent =
      `Score ${g.total} is below ${GRADE_HINT_THRESHOLD} - click ` +
      `"Summarize circuit" again if you want a fresh attempt.`;
    host.appendChild(n);
  }
}

function renderGradeDonut(g) {
  const subs = g.sub_scores || [];
  const cx = 80, cy = 80, r = 62, sw = 22, gap = 3;
  let cursor = 0, arcs = "";
  for (let i = 0; i < subs.length; i++) {
    const s = subs[i];
    const span = (s.max / 100) * 360;
    const start = cursor + gap / 2;
    const end = cursor + span - gap / 2;
    const valEnd = start + (end - start) * (s.max ? s.score / s.max : 0);
    const color = GRADE_COLORS[i % GRADE_COLORS.length];
    arcs += `<path d="${_arcPath(cx, cy, r, start, end)}" stroke="${color}" stroke-opacity="0.18" stroke-width="${sw}" fill="none"></path>`;
    if (valEnd > start + 0.2) {
      arcs += `<path class="grade-val" data-i="${i}" d="${_arcPath(cx, cy, r, start, valEnd)}" stroke="${color}" stroke-width="${sw}" fill="none"></path>`;
    }
    arcs += `<path class="grade-seg-hit" data-i="${i}" d="${_arcPath(cx, cy, r, start, end)}" stroke="transparent" stroke-width="${sw + 6}" fill="none"></path>`;
    cursor += span;
  }
  const svg =
    `<svg class="grade-donut" viewBox="0 0 160 160" role="img" aria-label="grade ${g.total} of 100">` +
    arcs +
    `<text class="grade-total" x="80" y="86" fill="${_bandColor(g.band)}">${g.total}</text>` +
    `<text class="grade-outof" x="80" y="104">/ 100</text></svg>`;

  let legend = `<div class="grade-legend">`;
  for (let i = 0; i < subs.length; i++) {
    const s = subs[i];
    legend +=
      `<div class="grade-legend-row" data-i="${i}">` +
      `<span class="grade-swatch" style="background:${GRADE_COLORS[i % GRADE_COLORS.length]}"></span>` +
      `<span class="lg-label">${escapeHtml(s.label)}</span>` +
      `<span class="lg-src">${escapeHtml(s.source)}</span>` +
      `<span class="lg-score">${s.score}/${s.max}</span></div>`;
  }
  legend += `</div>`;

  const note = g.capped
    ? `<div class="grade-note capped">Capped at ${g.total} - hallucinated: ${escapeHtml((g.hallucinated_items || []).join(", ") || "yes")}.</div>`
    : "";
  // Flags = problems the grader caught in the SUMMARY's text (not in
  // the circuit) that the sub-scores don't already express.
  let flags = "";
  if (g.flags && g.flags.length) {
    const items = g.flags.map((f) => {
      if (typeof f === "string") return `<li>${escapeHtml(f)}</li>`;
      const para = f.paragraph ? `<span class="flag-para">P${f.paragraph}</span> ` : "";
      const quote = f.quote ? `<span class="flag-quote">"${escapeHtml(f.quote)}"</span> — ` : "";
      return `<li>${para}${quote}${escapeHtml(f.issue || "")}</li>`;
    }).join("");
    flags =
      `<div class="grade-flags" id="grade-flags">` +
      `<div class="grade-flags-title">Grader feedback: ${g.flags.length} issue${g.flags.length === 1 ? "" : "s"} ` +
      `in this summary's wording</div>` +
      `<ul>${items}</ul></div>`;
  }

  gradeBody.innerHTML =
    `<div class="grade-card">${svg}<div class="grade-info">${legend}` +
    `<div class="grade-detail muted"></div>${note}</div></div>${flags}`;
  const detail = gradeBody.querySelector(".grade-detail");
  const card = gradeBody.querySelector(".grade-card");
  const valArcs = gradeBody.querySelectorAll(".grade-val");
  const legRows = gradeBody.querySelectorAll(".grade-legend-row");
  const show = (i) => {
    const s = subs[i];
    if (!s) return;
    detail.classList.remove("muted");
    const full = s.score >= s.max;
    detail.innerHTML =
      `<div class="gd-title">${escapeHtml(s.label)} - ${s.score}/${s.max} <span class="lg-src">${escapeHtml(s.source)}</span></div>` +
      (full ? "" :
        `<div class="gd-how">${escapeHtml(s.description || "")}</div>` +
        (s.rationale ? `<div class="gd-why">"${escapeHtml(s.rationale)}"</div>` : ""));
  };
  // Highlight + "pop" the slice WITHOUT moving the hit geometry, so the
  // pointer never bounces in/out near a slice edge. Hovering a slice OR its
  // legend row highlights the same slice; cleared only on leaving the block.
  const highlight = (i) => {
    valArcs.forEach((p) => {
      const on = parseInt(p.dataset.i, 10) === i;
      p.setAttribute("stroke-width", on ? (sw + 6) : sw);
      p.style.opacity = on ? "1" : "0.5";
    });
    legRows.forEach((row) => row.classList.toggle("active", parseInt(row.dataset.i, 10) === i));
    show(i);
  };
  const clearHi = () => {
    valArcs.forEach((p) => { p.setAttribute("stroke-width", sw); p.style.opacity = "1"; });
    legRows.forEach((row) => row.classList.remove("active"));
    detail.classList.add("muted");
    detail.textContent = "";
  };
  gradeBody.querySelectorAll(".grade-seg-hit").forEach((el) =>
    el.addEventListener("mouseenter", () => highlight(parseInt(el.dataset.i, 10))));
  legRows.forEach((row) =>
    row.addEventListener("mouseenter", () => highlight(parseInt(row.dataset.i, 10))));
  if (card) card.addEventListener("mouseleave", clearHi);
}

const L2_CARD_TYPES = [
  { key: "purpose", name: "Overall purpose",     hint: "What this circuit does." },
  { key: "subs",    name: "Subcircuits",         hint: "Role of each child .dig." },
  { key: "flow",    name: "Signal flow example", hint: "One row traced end to end." },
  { key: "goal",    name: "Goal comparison",     hint: "Versus what you asked for." },
  { key: "topo",    name: "Topology",            hint: "Architectural pattern." },
  { key: "lect",    name: "Course concepts",     hint: "Most relevant lectures." },
];

function _splitL2Paragraphs(text) {
  const chunks = text
    .split(/\n\s*\n+/)
    .map((s) => s.trim())
    .filter(Boolean);
  const cleaned = chunks.map((c) =>
    c.replace(/^\s*[\(\[]?\s*\d+\s*[\)\]\.\:]\s*/, "").trim()
  );
  if (cleaned.length > 6) {
    const head = cleaned.slice(0, 5);
    const tail = cleaned.slice(5).join("\n\n");
    return [...head, tail];
  }
  return cleaned;
}

function renderL2ParagraphCards(rawText, extras) {
  const paras = _splitL2Paragraphs(rawText);
  if (paras.length === 0) {
    return `<div class="muted">(empty response)</div>`;
  }
  if (extras) extras.flowText = paras[2] || "";
  const cards = L2_CARD_TYPES.map((type, idx) => {
    const body = paras[idx] || "";
    const empty = body.length === 0;
    let inner;
    if (empty) inner = `<span class="muted">(no content for this section)</span>`;
    else if (type.key === "subs" && extras) inner = l2SubsBodyHtml(body, extras.roles);
    else if (type.key === "flow" && extras) inner = l2FlowBodyHtml(body, extras);
    else inner = escapeHtml(body);
    const play = (type.key === "flow" && extras && extras.exampleRow)
      ? `<span class="l2-card-play" title="open to play the walkthrough">&#9654;</span>` : "";
    return `
      <div class="l2-card l2-card-${type.key} ${empty ? "l2-card-empty" : ""}" data-card-idx="${idx}">
        <div class="l2-card-head" role="button" tabindex="0">
          <span class="l2-card-num">${idx + 1}</span>
          <span class="l2-card-name">${type.name}</span>
          <span class="l2-card-hint">${type.hint}</span>${play}
          <span class="l2-card-toggle">+</span>
        </div>
        <div class="l2-card-body">${inner}</div>
      </div>
    `;
  }).join("");
  return `<div class="l2-card-grid">${cards}</div>`;
}

function wireL2CardEvents() {
  const cards = l2LlmOutput.querySelectorAll(".l2-card");
  cards.forEach((card) => {
    const head = card.querySelector(".l2-card-head");
    const toggle = () => card.classList.toggle("expanded");
    head.addEventListener("click", toggle);
    head.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        toggle();
      }
    });
  });
}

window.addEventListener("keydown", (e) => {
  const t = e.target;
  if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA")) return;
  const l2page = document.querySelector('.page[data-page="l2"]');
  if (!l2page || l2page.hasAttribute("hidden")) return;
  const n = parseInt(e.key, 10);
  if (!Number.isFinite(n) || n < 1 || n > 6) return;
  const card = l2LlmOutput.querySelector(`.l2-card[data-card-idx="${n - 1}"]`);
  if (card) {
    card.classList.toggle("expanded");
    e.preventDefault();
  }
});


  /*# ───────────────────────────────────────────────────────────────────
  *#  Layer 3 coach tab: moved to l3.js (loaded right after this file)
  *# ──────────────────────────────────────────────────────────────────#*/

const tabButtons = document.querySelectorAll(".tabs .tab");
const pages = document.querySelectorAll(".page");

function showTab(name) {
  tabButtons.forEach((b) => {
    b.classList.toggle("active", b.dataset.tab === name);
  });
  pages.forEach((p) => {
    if (p.dataset.page === name) {
      p.removeAttribute("hidden");
    } else {
      p.setAttribute("hidden", "");
    }
  });
  if (name === "main" && cy) {
    setTimeout(() => { try { cy.resize(); cy.fit(undefined, 60); } catch {} }, 0);
  }
  if (name === "l2") {
    refreshLibrary();
  }
  if (name === "l3") {
    renderL3Tab();
  }
  logEvent("tab_switch", { tab: name });
}

tabButtons.forEach((b) => {
  b.addEventListener("click", () => showTab(b.dataset.tab));
});

function returnToMain() { showTab("main"); }

  /*# ───────────────────────────────────────────────────────────────────
  *#  Net-id overlay toggle (Layer 1 graph)
  *# ──────────────────────────────────────────────────────────────────#*/

let netIdsOnL1 = false;

function applyNetIdsL1() {
  if (!cy) return;
  cy.edges()[netIdsOnL1 ? "addClass" : "removeClass"]("show-netid");
  const btn = document.getElementById("netid-toggle");
  if (btn) btn.classList.toggle("active", netIdsOnL1);
}

(function wireNetIdToggleL1() {
  const btn = document.getElementById("netid-toggle");
  if (!btn) return;
  btn.addEventListener("click", () => {
    netIdsOnL1 = !netIdsOnL1;
    applyNetIdsL1();
    logEvent("netids_toggled", { on: netIdsOnL1 });
  });
})();
