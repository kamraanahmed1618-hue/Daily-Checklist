(() => {
  "use strict";

  const root = document.getElementById("hseDashboardRoot");
  if (!root) return;

  const API_URL = root.dataset.apiUrl;
  const POLL_MS = 45000;

  const COLORS = {
    navy: "#0a1f8f", navy2: "#1456b8", navyLight: "#7fa8f5",
    green: "#1d5a46", red: "#ad3328", amber: "#8a5b12", muted: "#5c6578",
  };
  const GRID = "#e1e0d9", AXIS = "#c3c2b7", MUTED_TEXT = "#898781";

  let DATA = null;      // last successful API payload
  let allRecords = [];  // DATA.records, sorted by date
  let pollTimer = null;

  /* ---------- formatting helpers ---------- */
  function fmtInt(v) { return (v === null || v === undefined || isNaN(v)) ? "–" : Math.round(v).toLocaleString("en-US"); }
  function fmtPct(v) { return (v === null || v === undefined || isNaN(v)) ? "–" : (Math.round(v * 1000) / 10) + "%"; }
  function fmtDec(v, d) { return (v === null || v === undefined || isNaN(v)) ? "–" : v.toFixed(d); }
  function shortDate(iso) {
    const d = new Date(iso + "T00:00:00");
    return d.toLocaleDateString("en-GB", { day: "2-digit", month: "short" });
  }
  function within(iso, start, end) { return (!start || iso >= start) && (!end || iso <= end); }
  function esc(s) { return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;"); }

  function aggregate(rows, header) {
    const kind = (DATA.agg && DATA.agg[header]) || "sum";
    const vals = rows.map(r => r[header]).filter(v => v !== null && v !== undefined);
    if (!vals.length) return null;
    if (kind === "sum") return vals.reduce((a, b) => a + b, 0);
    if (kind === "avg") return vals.reduce((a, b) => a + b, 0) / vals.length;
    return vals[vals.length - 1];
  }

  /* ---------- SVG chart engine ---------- */
  const CW = 640, CH = 210, PL = 40, PR = 14, PT = 16, PB = 26;
  const PW = CW - PL - PR, PH = CH - PT - PB;

  function niceMax(v) {
    if (!v || v <= 0) return 1;
    const mag = Math.pow(10, Math.floor(Math.log10(v)));
    const steps = [1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10];
    for (const s of steps) { if (s * mag >= v * 1.08) return s * mag; }
    return 10 * mag;
  }
  function xPos(i, count) { return count <= 1 ? PL + PW / 2 : PL + (PW * i / (count - 1)); }
  function yPos(v, yMax) { const r = yMax === 0 ? 0 : Math.max(0, Math.min(1, v / yMax)); return PT + PH * (1 - r); }

  function gridlines(yMax, suffix, steps) {
    steps = steps || 4;
    let out = "";
    for (let s = 0; s <= steps; s++) {
      const val = yMax * s / steps;
      const y = yPos(val, yMax);
      out += `<line x1="${PL}" y1="${y.toFixed(1)}" x2="${CW - PR}" y2="${y.toFixed(1)}" stroke="${GRID}" stroke-width="1"/>`;
      const label = val >= 1000 ? Math.round(val / 100) / 10 + "k" : Math.round(val * 10) / 10;
      out += `<text x="${PL - 6}" y="${(y + 3).toFixed(1)}" font-size="9" fill="${MUTED_TEXT}" text-anchor="end">${label}${suffix}</text>`;
    }
    return out;
  }
  function xLabels(labels) {
    const count = labels.length;
    const every = Math.max(1, Math.ceil(count / 8));
    const indices = [];
    for (let i = 0; i < count; i += every) indices.push(i);
    if (indices[indices.length - 1] !== count - 1) {
      if (count - 1 - indices[indices.length - 1] < every * 0.6) indices.pop();
      indices.push(count - 1);
    }
    let out = "";
    indices.forEach(i => {
      const x = xPos(i, count);
      const last = i === count - 1;
      const anchor = last ? "end" : "middle";
      const lx = last ? x - 2 : x;
      out += `<text x="${lx.toFixed(1)}" y="${CH - 6}" font-size="9" fill="${MUTED_TEXT}" text-anchor="${anchor}">${labels[i]}</text>`;
    });
    return out;
  }
  function svgWrap(inner, label) {
    return `<svg viewBox="0 0 ${CW} ${CH}" role="img" aria-label="${esc(label || "chart")}" class="hse-chart-svg" preserveAspectRatio="xMidYMid meet">${inner}</svg>`;
  }

  function lineChart(el, labels, series, opts) {
    opts = opts || {};
    if (!labels.length) { el.innerHTML = `<div class="hse-chart-empty">No data in the selected range.</div>`; return; }
    const allVals = series.flatMap(s => s.values.filter(v => v !== null && v !== undefined));
    const yMax = opts.yMax !== undefined ? opts.yMax : niceMax(Math.max(0, ...allVals));
    let body = gridlines(yMax, opts.suffix || "");
    body += `<line x1="${PL}" y1="${CH - PB}" x2="${CW - PR}" y2="${CH - PB}" stroke="${AXIS}" stroke-width="1"/>`;
    series.forEach(s => {
      const pts = s.values.map((v, i) => [i, v]).filter(p => p[1] !== null && p[1] !== undefined);
      if (!pts.length) return;
      let path = "";
      pts.forEach((p, k) => { path += (k === 0 ? "M" : "L") + xPos(p[0], labels.length).toFixed(1) + "," + yPos(p[1], yMax).toFixed(1) + " "; });
      body += `<path d="${path}" fill="none" stroke="${s.color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>`;
      const dotEvery = pts.length > 40 ? Math.ceil(pts.length / 40) : 1;
      pts.forEach((p, k) => {
        if (k % dotEvery !== 0 && k !== pts.length - 1) return;
        const x = xPos(p[0], labels.length), y = yPos(p[1], yMax);
        body += `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="2.6" fill="${s.color}"><title>${esc(labels[p[0]])} · ${esc(s.name)}: ${esc(fmtDec(p[1], 1))}${opts.suffix || ""}</title></circle>`;
      });
    });
    body += xLabels(labels);
    el.innerHTML = svgWrap(body, opts.title);
  }

  function barChart(el, labels, series, opts) {
    opts = opts || {};
    if (!labels.length) { el.innerHTML = `<div class="hse-chart-empty">No data in the selected range.</div>`; return; }
    const allVals = series.flatMap(s => s.values.map(v => v || 0));
    const yMax = opts.yMax !== undefined ? opts.yMax : niceMax(Math.max(0, ...allVals));
    let body = gridlines(yMax, opts.suffix || "");
    body += `<line x1="${PL}" y1="${CH - PB}" x2="${CW - PR}" y2="${CH - PB}" stroke="${AXIS}" stroke-width="1"/>`;
    const band = PW / labels.length;
    const groupW = band * (opts.groupWidth || 0.62);
    const barW = groupW / series.length;
    labels.forEach((label, i) => {
      const groupX = PL + band * i + (band - groupW) / 2;
      series.forEach((s, si) => {
        const v = s.values[i] || 0;
        const x = groupX + barW * si;
        const y = yPos(v, yMax);
        const h = (CH - PB) - y;
        body += `<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${Math.max(1, barW - 1.2).toFixed(1)}" height="${Math.max(0, h).toFixed(1)}" rx="1.5" fill="${s.color}"><title>${esc(label)} · ${esc(s.name)}: ${esc(fmtDec(v, 1))}${opts.suffix || ""}</title></rect>`;
      });
    });
    body += xLabels(labels);
    el.innerHTML = svgWrap(body, opts.title);
  }

  function categoryBarChart(el, categories, opts) {
    opts = opts || {};
    if (categories.every(c => !c.value)) {
      el.innerHTML = `<div class="hse-chart-empty">No occurrences recorded in the selected range — all categories at zero.</div>`;
      return;
    }
    const labels = categories.map(c => c.label);
    const yMax = niceMax(Math.max(0, ...categories.map(c => c.value)));
    let body = gridlines(yMax, opts.suffix || "", 4);
    body += `<line x1="${PL}" y1="${CH - PB}" x2="${CW - PR}" y2="${CH - PB}" stroke="${AXIS}" stroke-width="1"/>`;
    const band = PW / labels.length;
    const barW = band * 0.55;
    categories.forEach((c, i) => {
      const x = PL + band * i + (band - barW) / 2;
      const y = yPos(c.value, yMax);
      const h = (CH - PB) - y;
      body += `<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${barW.toFixed(1)}" height="${Math.max(0, h).toFixed(1)}" rx="2" fill="${c.color || COLORS.navy2}"><title>${esc(c.label)}: ${esc(fmtDec(c.value, 1))}</title></rect>`;
      if (c.value) body += `<text x="${(x + barW / 2).toFixed(1)}" y="${(y - 4).toFixed(1)}" font-size="9" font-weight="700" fill="${c.color || COLORS.navy2}" text-anchor="middle">${Math.round(c.value * 10) / 10}</text>`;
    });
    labels.forEach((label, i) => {
      const x = PL + band * i + band / 2;
      body += `<text x="${x.toFixed(1)}" y="${CH - 6}" font-size="8" fill="${MUTED_TEXT}" text-anchor="middle">${esc(label.length > 14 ? label.slice(0, 13) + "…" : label)}<title>${esc(label)}</title></text>`;
    });
    el.innerHTML = svgWrap(body, opts.title);
  }

  function kpiCard(value, label, sub) {
    return `<div class="hse-kpi"><strong>${value}</strong><span>${label}</span>${sub ? `<small>${sub}</small>` : ""}</div>`;
  }

  /* ---------- sync status ---------- */
  const syncDot = document.getElementById("hseSyncDot");
  const syncText = document.getElementById("hseSyncText");
  const errorNotice = document.getElementById("hseErrorNotice");

  function setSyncStatus(state, text) {
    syncDot.className = "hse-sync-dot" + (state === "error" ? " error" : state === "stale" ? " stale" : "");
    syncText.textContent = text;
  }

  function relativeTime(iso) {
    const diffMs = Date.now() - new Date(iso).getTime();
    const mins = Math.round(diffMs / 60000);
    if (mins < 1) return "just now";
    if (mins === 1) return "1 minute ago";
    if (mins < 60) return `${mins} minutes ago`;
    const hours = Math.round(mins / 60);
    return hours === 1 ? "1 hour ago" : `${hours} hours ago`;
  }

  /* ---------- filters ---------- */
  const startInput = document.getElementById("hseStartDate");
  const endInput = document.getElementById("hseEndDate");
  let fullMin = "", fullMax = "";

  function setPresetActive(name) {
    document.querySelectorAll("#hsePresetGroup button").forEach(b => b.classList.toggle("active", b.dataset.preset === name));
  }
  function applyPreset(preset) {
    if (preset === "all") { startInput.value = fullMin; endInput.value = fullMax; }
    else {
      const days = parseInt(preset, 10);
      const endDate = new Date(fullMax + "T00:00:00");
      const startDate = new Date(endDate); startDate.setDate(startDate.getDate() - (days - 1));
      const iso = d => d.toISOString().slice(0, 10);
      startInput.value = iso(startDate) < fullMin ? fullMin : iso(startDate);
      endInput.value = fullMax;
    }
    setPresetActive(preset);
    renderAll();
  }
  document.getElementById("hsePresetGroup").addEventListener("click", e => {
    const btn = e.target.closest("button"); if (!btn) return;
    applyPreset(btn.dataset.preset);
  });
  startInput.addEventListener("change", () => { setPresetActive("custom"); renderAll(); });
  endInput.addEventListener("change", () => { setPresetActive("custom"); renderAll(); });

  function currentRows() {
    const s = startInput.value, e = endInput.value;
    return allRecords.filter(r => within(r.date, s, e));
  }

  /* ---------- rendering ---------- */
  function renderAll() {
    const rows = currentRows();
    const labels = rows.map(r => shortDate(r.date));

    const totalManHours = aggregate(rows, "Total Man Hours");
    const avgManpower = aggregate(rows, "Total Man Power");
    const peakManpower = Math.max(0, ...rows.map(r => r["Total Man Power"] || 0));
    const fatalities = aggregate(rows, "Number of Fatalities") || 0;
    const ltis = aggregate(rows, "Number of Lost Time Injuries (LTI)") || 0;
    const ltifr = aggregate(rows, "Lost Time Injury Rate (LTIFR)");
    const trir = aggregate(rows, "Total Recordable Incidents Rate (TRIR)");

    document.getElementById("hseOverviewKpis").innerHTML = [
      kpiCard(fmtInt(totalManHours), "Total man-hours", "Sum over range"),
      kpiCard(fmtInt(avgManpower), "Average daily manpower", "BEC + sub-contractor"),
      kpiCard(fmtInt(peakManpower), "Peak daily manpower", "Highest single day"),
      kpiCard(rows.length, "Days recorded", `${allRecords.length} total in database`),
      kpiCard(fmtDec(ltifr, 2), "LTIFR", "Last record in range"),
      kpiCard(fmtDec(trir, 2), "TRIR", "Last record in range"),
    ].join("");

    const zeroHarm = fatalities === 0 && ltis === 0;
    document.getElementById("hseZeroHarmBanner").innerHTML = zeroHarm
      ? `<div class="notice success">✓ <strong>Zero Harm maintained</strong> — no fatalities or lost-time injuries recorded in the selected period.</div>`
      : `<div class="notice warn">⚠ ${fmtInt(fatalities)} fatalit${fatalities === 1 ? "y" : "ies"} and ${fmtInt(ltis)} lost-time injur${ltis === 1 ? "y" : "ies"} recorded in this period.</div>`;

    document.getElementById("hseWorkforceKpis").innerHTML = [
      kpiCard(fmtInt(aggregate(rows, "Total BEC Manpower")), "Avg BEC manpower/day"),
      kpiCard(fmtInt(aggregate(rows, "Total Sub. Con Manpower")), "Avg sub-con manpower/day"),
      kpiCard(fmtDec(aggregate(rows, "Working Hours / Day"), 1), "Avg working hours/day"),
      kpiCard(fmtInt(aggregate(rows, "Hours Worked since last LTI (safe Man Hours)")), "Safe man-hours (last record)"),
      kpiCard(fmtInt(aggregate(rows, "Safety Population Required")), "Avg safety pop. required"),
      kpiCard(fmtInt(aggregate(rows, "Safety Population Available")), "Avg safety pop. available"),
    ].join("");

    lineChart(document.getElementById("hseChartManpower"), labels, [
      { name: "BEC", color: COLORS.navy, values: rows.map(r => r["Total BEC Manpower"]) },
      { name: "Sub-contractor", color: COLORS.navy2, values: rows.map(r => r["Total Sub. Con Manpower"]) },
      { name: "Total", color: COLORS.navyLight, values: rows.map(r => r["Total Man Power"]) },
    ], { title: "Daily manpower" });

    barChart(document.getElementById("hseChartManHours"), labels, [
      { name: "Man-hours", color: COLORS.navy2, values: rows.map(r => r["Total Man Hours"]) },
    ], { title: "Man-hours per day" });

    lineChart(document.getElementById("hseChartSafeHours"), labels, [
      { name: "Safe man-hours since last LTI", color: COLORS.green, values: rows.map(r => r["Hours Worked since last LTI (safe Man Hours)"]) },
    ], { title: "Safe man-hours" });

    document.getElementById("hseLaggingKpis").innerHTML = [
      kpiCard(fmtInt(fatalities), "Fatalities", "Sum over range"),
      kpiCard(fmtInt(ltis), "Lost time injuries", "Sum over range"),
      kpiCard(fmtInt(aggregate(rows, "Total Recordable Incidents (LTI+Fire+MTC+PD+MVI)")), "Total recordable incidents"),
      kpiCard(fmtInt(aggregate(rows, "First Aid Cases (FAC)")), "First aid cases"),
      kpiCard(fmtInt(aggregate(rows, "Medical Treatment Cases (MTC)")), "Medical treatment cases"),
      kpiCard(fmtInt(aggregate(rows, "Property Damages (PD)")), "Property damages"),
    ].join("");

    categoryBarChart(document.getElementById("hseChartIncidentSummary"), [
      { label: "Fatalities", value: aggregate(rows, "Number of Fatalities") || 0, color: COLORS.red },
      { label: "LTI", value: aggregate(rows, "Number of Lost Time Injuries (LTI)") || 0, color: COLORS.red },
      { label: "First Aid", value: aggregate(rows, "First Aid Cases (FAC)") || 0, color: COLORS.amber },
      { label: "Medical Tx", value: aggregate(rows, "Medical Treatment Cases (MTC)") || 0, color: COLORS.amber },
      { label: "Property Dmg", value: aggregate(rows, "Property Damages (PD)") || 0, color: COLORS.navy2 },
      { label: "Fire I/II", value: aggregate(rows, "Fire Incident Level I & ii") || 0, color: COLORS.navy2 },
      { label: "Fire III", value: aggregate(rows, "Fire Incidents Level iii") || 0, color: COLORS.navy2 },
      { label: "Security", value: aggregate(rows, "Security Incidents (SIC)") || 0, color: COLORS.muted },
      { label: "Vehicle", value: aggregate(rows, "Motor Vehicle Incidents (MVI)") || 0, color: COLORS.muted },
    ], { title: "Incidents by category" });

    document.getElementById("hseLeadingKpis").innerHTML = [
      kpiCard(fmtInt(aggregate(rows, "Toolbox Talks Sessions(TBT)")), "Toolbox talks"),
      kpiCard(fmtInt(aggregate(rows, "Near Miss (NM)")), "Near misses reported"),
      kpiCard(fmtInt(aggregate(rows, "HSE NCRs Issued")), "NCRs issued"),
      kpiCard(fmtInt(aggregate(rows, "HSE NCRs Closed")), "NCRs closed"),
      kpiCard(fmtPct(aggregate(rows, "NCRs Closure Rate")), "NCR closure rate", "Last record"),
      kpiCard(fmtInt(aggregate(rows, "Health Survalience Conducted (# of People)")), "Health surveillance (people)"),
    ].join("");

    barChart(document.getElementById("hseChartTbt"), labels, [
      { name: "TBT sessions", color: COLORS.navy, values: rows.map(r => r["Toolbox Talks Sessions(TBT)"]) },
    ], { title: "Toolbox talks" });

    barChart(document.getElementById("hseChartNearMiss"), labels, [
      { name: "Near misses", color: COLORS.amber, values: rows.map(r => r["Near Miss (NM)"]) },
    ], { title: "Near misses" });

    barChart(document.getElementById("hseChartNcr"), labels, [
      { name: "Issued", color: COLORS.red, values: rows.map(r => r["HSE NCRs Issued"]) },
      { name: "Closed", color: COLORS.green, values: rows.map(r => r["HSE NCRs Closed"]) },
    ], { title: "NCRs" });

    categoryBarChart(document.getElementById("hseChartLeadingSummary"), [
      { label: "HSE Campaigns", value: aggregate(rows, "HSE Campaigns") || 0 },
      { label: "Drills", value: aggregate(rows, "Drills") || 0 },
      { label: "Mass TBTs", value: aggregate(rows, "Mass TBTs") || 0 },
      { label: "Stop Work", value: aggregate(rows, "Stop Work Notice Issued") || 0 },
      { label: "HSE Rewards", value: aggregate(rows, "HSE Rewards (# of People)") || 0 },
      { label: "SubCon Mtgs", value: aggregate(rows, "No. SubCon Safety meeting with CM") || 0 },
      { label: "Violations", value: aggregate(rows, "Voilations Issued") || 0, color: COLORS.red },
    ], { title: "Leading activity totals" });

    document.getElementById("hseInspectionsKpis").innerHTML = [
      kpiCard(fmtInt(aggregate(rows, "Internal HSE Audits")), "Internal HSE audits"),
      kpiCard(fmtInt(aggregate(rows, "External Audits")), "External audits"),
      kpiCard(fmtInt(aggregate(rows, "Safety Management Walkthroughs")), "Safety mgmt walkthroughs"),
      kpiCard(fmtInt(aggregate(rows, "No. of Site Walkthrough (CM/Site Eng/HSE Team)")), "Site walkthroughs"),
      kpiCard(fmtInt(aggregate(rows, "Project Safety Committee Meeting")), "Safety committee meetings"),
      kpiCard(fmtInt(aggregate(rows, "Safety Observations Issued(SOR)")), "SORs issued"),
      kpiCard(fmtPct(aggregate(rows, "SOR Closeout Ratio")), "SOR closeout ratio", "Last record"),
    ].join("");

    categoryBarChart(document.getElementById("hseChartAudits"), [
      { label: "Internal Audits", value: aggregate(rows, "Internal HSE Audits") || 0 },
      { label: "External Audits", value: aggregate(rows, "External Audits") || 0 },
      { label: "Safety Walkthroughs", value: aggregate(rows, "Safety Management Walkthroughs") || 0 },
      { label: "Site Walkthroughs", value: aggregate(rows, "No. of Site Walkthrough (CM/Site Eng/HSE Team)") || 0 },
      { label: "Committee Mtgs", value: aggregate(rows, "Project Safety Committee Meeting") || 0 },
    ], { title: "Audits & walkthroughs" });

    barChart(document.getElementById("hseChartSor"), labels, [
      { name: "Issued", color: COLORS.amber, values: rows.map(r => r["Safety Observations Issued(SOR)"]) },
      { name: "Closed", color: COLORS.green, values: rows.map(r => r["Safety Observations Clossed(SOR)"]) },
    ], { title: "SORs" });

    document.getElementById("hseTrainingsKpis").innerHTML = [
      kpiCard(fmtInt(aggregate(rows, "HSE Inductions(NO of People)")), "People inducted"),
      kpiCard(fmtPct(aggregate(rows, "HSE Inductions Compliance Percentage")), "Induction compliance", "Avg over range"),
      kpiCard(fmtInt(aggregate(rows, "Internal / Third Party HSE Trainings (Sessions)")), "Training sessions"),
      kpiCard(fmtInt(aggregate(rows, "Training Manhours")), "Training man-hours"),
    ].join("");

    barChart(document.getElementById("hseChartInductions"), labels, [
      { name: "Inductions", color: COLORS.navy, values: rows.map(r => r["HSE Inductions(NO of People)"]) },
    ], { title: "Inductions" });

    barChart(document.getElementById("hseChartTrainingSessions"), labels, [
      { name: "Sessions", color: COLORS.navy2, values: rows.map(r => r["Internal / Third Party HSE Trainings (Sessions)"]) },
    ], { title: "Training sessions" });

    document.getElementById("hsePermitsKpis").innerHTML = [
      kpiCard(fmtInt(aggregate(rows, "Permit to Work Issued (PTW)")), "PTWs issued"),
      kpiCard(fmtPct(aggregate(rows, "PTW Compliance Percentage")), "PTW compliance", "Avg over range"),
    ].join("");

    barChart(document.getElementById("hseChartPtw"), labels, [
      { name: "PTWs issued", color: COLORS.navy, values: rows.map(r => r["Permit to Work Issued (PTW)"]) },
    ], { title: "PTWs issued" });
  }

  /* ---------- data loading ---------- */
  function applyData(payload) {
    DATA = payload;
    allRecords = (payload.records || []).slice().sort((a, b) => (a.date < b.date ? -1 : 1));

    const emptyState = document.getElementById("hseEmptyState");
    const content = document.getElementById("hseDashboardContent");
    if (!allRecords.length) {
      emptyState.classList.remove("hidden");
      content.classList.add("hidden");
      setSyncStatus("stale", "No data imported yet");
      return;
    }
    emptyState.classList.add("hidden");
    content.classList.remove("hidden");

    fullMin = allRecords[0].date;
    fullMax = allRecords[allRecords.length - 1].date;
    if (!startInput.value || startInput.value < fullMin || startInput.value > fullMax) startInput.value = fullMin;
    if (!endInput.value || endInput.value > fullMax || endInput.value < startInput.value) endInput.value = fullMax;
    startInput.min = fullMin; startInput.max = fullMax;
    endInput.min = fullMin; endInput.max = fullMax;

    renderAll();

    if (payload.lastSync && payload.lastSync.uploaded_at) {
      setSyncStatus("ok", `Synced ${relativeTime(payload.lastSync.uploaded_at)} · ${payload.totalDays} day(s) in database`);
    } else {
      setSyncStatus("ok", `${payload.totalDays} day(s) in database`);
    }
    errorNotice.classList.add("hidden");
  }

  async function loadData({ silent } = {}) {
    if (!silent) setSyncStatus("stale", "Refreshing…");
    try {
      const response = await fetch(API_URL, { headers: { Accept: "application/json" } });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const payload = await response.json();
      applyData(payload);
    } catch (error) {
      setSyncStatus("error", "Could not refresh — showing last known data");
      if (DATA) {
        errorNotice.classList.add("hidden");
      } else {
        errorNotice.classList.remove("hidden");
        errorNotice.textContent = "Could not load the HSE dashboard data. Check your connection and try again.";
      }
    }
  }

  document.getElementById("hseRefreshBtn").addEventListener("click", () => loadData());

  function startPolling() {
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(() => {
      if (document.visibilityState === "visible") loadData({ silent: true });
    }, POLL_MS);
  }
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") loadData({ silent: true });
  });

  loadData();
  startPolling();
})();
