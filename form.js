"use strict";

const form = document.getElementById("inspection-form");
const sectionsRoot = document.getElementById("checklist-sections");
const progressBar = document.getElementById("progress-bar");
const progressCopy = document.getElementById("progress-copy");
const scoreCopy = document.getElementById("score-copy");
const errorBox = document.getElementById("form-error");
const successPanel = document.getElementById("success-panel");
const successCopy = document.getElementById("success-copy");
const draftKey = "diriyah-ohs-inspection-draft-v1";
let checklist = [];
let totalItems = 0;

function escapeHtml(value) {
  return String(value).replace(/[&<>'"]/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
  })[character]);
}

function localDateAndTime() {
  const now = new Date();
  const offset = now.getTimezoneOffset() * 60000;
  const local = new Date(now.getTime() - offset).toISOString();
  return { date: local.slice(0, 10), time: local.slice(11, 16) };
}

function field(name) {
  return form.elements.namedItem(name);
}

function getDraft() {
  try { return JSON.parse(localStorage.getItem(draftKey) || "{}"); } catch { return {}; }
}

function collectState() {
  const fields = {};
  ["projectName", "workLocation", "contractor", "inspectedBy", "reportNo", "inspectionDate", "inspectionTime", "shift", "remarks", "signoffName"].forEach((name) => {
    fields[name] = field(name)?.value || "";
  });
  const responses = {};
  const responseNotes = {};
  document.querySelectorAll("input[data-item-id]:checked").forEach((input) => { responses[input.dataset.itemId] = input.value; });
  document.querySelectorAll("textarea[data-note-id]").forEach((input) => { if (input.value) responseNotes[input.dataset.noteId] = input.value; });
  return { fields, responses, responseNotes };
}

function saveDraft() {
  localStorage.setItem(draftKey, JSON.stringify(collectState()));
}

function renderChecklist() {
  let runningIndex = 0;
  sectionsRoot.innerHTML = checklist.map((section, sectionIndex) => {
    const rows = section.items.map((item) => {
      runningIndex += 1;
      const id = escapeHtml(item.id);
      return `<article class="requirement" data-requirement="${id}">
        <div class="requirement-copy"><span class="item-number">${String(runningIndex).padStart(3, "0")}</span><p>${escapeHtml(item.text)}</p></div>
        <fieldset class="choices" aria-label="Response for ${escapeHtml(item.text)}">
          <label><input type="radio" name="item-${id}" value="Y" data-item-id="${id}"><span>Yes</span></label>
          <label><input type="radio" name="item-${id}" value="N" data-item-id="${id}"><span>No</span></label>
          <label><input type="radio" name="item-${id}" value="NA" data-item-id="${id}"><span>N/A</span></label>
        </fieldset>
        <label class="observation hidden"><span>Observation / corrective action <b>*</b></span><textarea rows="2" maxlength="600" data-note-id="${id}" placeholder="Describe the finding and required action…"></textarea></label>
      </article>`;
    }).join("");
    return `<details class="checklist-section" data-section="${escapeHtml(section.id)}" ${sectionIndex === 0 ? "open" : ""}>
      <summary><span class="section-index">${String(sectionIndex + 1).padStart(2, "0")}</span><span class="section-name">${escapeHtml(section.title)}</span><span class="section-progress" data-section-progress>0 / ${section.items.length}</span><span class="chevron">⌄</span></summary>
      <div class="requirements">${rows}</div>
    </details>`;
  }).join("");
}

function applyDraft() {
  const draft = getDraft();
  const now = localDateAndTime();
  const values = { projectName: "1 Hotel Diriyah", inspectionDate: now.date, inspectionTime: now.time, ...(draft.fields || {}) };
  Object.entries(values).forEach(([name, value]) => { if (field(name) && value) field(name).value = value; });
  Object.entries(draft.responses || {}).forEach(([id, value]) => {
    const input = document.querySelector(`input[data-item-id="${CSS.escape(id)}"][value="${CSS.escape(value)}"]`);
    if (input) input.checked = true;
  });
  Object.entries(draft.responseNotes || {}).forEach(([id, value]) => {
    const input = document.querySelector(`textarea[data-note-id="${CSS.escape(id)}"]`);
    if (input) input.value = value;
  });
  updateProgress();
}

function updateProgress() {
  const responses = [...document.querySelectorAll("input[data-item-id]:checked")];
  const answered = responses.length;
  const yes = responses.filter((input) => input.value === "Y").length;
  const no = responses.filter((input) => input.value === "N").length;
  const score = yes + no ? Math.round((yes / (yes + no)) * 1000) / 10 : null;
  progressCopy.textContent = `${answered} of ${totalItems} answered`;
  progressBar.style.width = `${totalItems ? answered / totalItems * 100 : 0}%`;
  scoreCopy.textContent = score === null ? "Compliance —" : `Compliance ${score}%`;

  document.querySelectorAll(".requirement").forEach((row) => {
    const selected = row.querySelector("input[data-item-id]:checked");
    const observation = row.querySelector(".observation");
    const note = row.querySelector("textarea[data-note-id]");
    const requiresNote = selected?.value === "N";
    observation.classList.toggle("hidden", !requiresNote);
    note.required = requiresNote;
    row.dataset.result = selected?.value || "";
  });
  document.querySelectorAll(".checklist-section").forEach((section) => {
    const complete = section.querySelectorAll("input[data-item-id]:checked").length;
    const count = section.querySelectorAll(".requirement").length;
    section.querySelector("[data-section-progress]").textContent = `${complete} / ${count}`;
    section.classList.toggle("section-complete", complete === count);
  });
}

function showError(message, element) {
  errorBox.textContent = message;
  errorBox.classList.remove("hidden");
  if (element) {
    const section = element.closest("details");
    if (section) section.open = true;
    element.scrollIntoView({ behavior: "smooth", block: "center" });
    element.focus?.();
  } else {
    errorBox.scrollIntoView({ behavior: "smooth", block: "center" });
  }
}

async function submitInspection(event) {
  event.preventDefault();
  errorBox.classList.add("hidden");
  if (!form.reportValidity()) return;
  const state = collectState();
  if (Object.keys(state.responses).length !== totalItems) {
    const firstMissing = [...document.querySelectorAll(".requirement")].find((row) => !row.querySelector("input:checked"));
    showError(`Complete all ${totalItems} checklist requirements before submitting.`, firstMissing);
    return;
  }
  const missingNote = [...document.querySelectorAll(".requirement")].find((row) => row.querySelector("input:checked")?.value === "N" && !row.querySelector("textarea").value.trim());
  if (missingNote) {
    showError("Add an observation or corrective action for every non-compliant item.", missingNote.querySelector("textarea"));
    return;
  }

  const button = form.querySelector("button[type=submit]");
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "Saving inspection…";
  try {
    const response = await fetch("/api/inspections", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...state.fields, responses: state.responses, responseNotes: state.responseNotes, signed: field("signed").checked }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "The inspection could not be saved.");
    localStorage.removeItem(draftKey);
    form.classList.add("hidden");
    successCopy.textContent = `${result.reportNo} was saved with ${result.score}% compliance and ${result.nonCompliant} non-compliant finding${result.nonCompliant === 1 ? "" : "s"}.`;
    successPanel.classList.remove("hidden");
    successPanel.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    showError(error.message || "The inspection could not be saved. Check your connection and try again.");
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

async function start() {
  try {
    const response = await fetch("/api/checklist");
    if (!response.ok) throw new Error();
    const payload = await response.json();
    checklist = payload.sections;
    totalItems = payload.total;
    renderChecklist();
    applyDraft();
    form.addEventListener("input", () => { updateProgress(); saveDraft(); });
    form.addEventListener("change", () => { updateProgress(); saveDraft(); });
    form.addEventListener("submit", submitInspection);
  } catch {
    sectionsRoot.innerHTML = '<div class="notice danger">The inspection requirements could not be loaded. Refresh the page to try again.</div>';
  }
}

document.getElementById("new-inspection")?.addEventListener("click", () => window.location.reload());
start();
