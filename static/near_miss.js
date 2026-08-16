"use strict";

const form = document.getElementById("near-miss-form");
const errorBox = document.getElementById("form-error");
const successPanel = document.getElementById("success-panel");
const successCopy = document.getElementById("success-copy");
const photoUpload = initPhotoUpload("near-miss-photo-upload");

function field(name) {
  return form.elements.namedItem(name);
}

function checkedValues(name) {
  return [...form.querySelectorAll(`input[name="${name}"]:checked`)].map((input) => input.value);
}

function listValues(prefix, count) {
  const values = [];
  for (let i = 1; i <= count; i += 1) {
    const value = field(`${prefix}${i}`)?.value.trim();
    if (value) values.push(value);
  }
  return values;
}

function showError(message) {
  errorBox.textContent = message;
  errorBox.classList.remove("hidden");
  errorBox.scrollIntoView({ behavior: "smooth", block: "center" });
}

async function submitReport(event) {
  event.preventDefault();
  errorBox.classList.add("hidden");
  if (!form.reportValidity()) return;

  const nearMissTypes = checkedValues("nearMissTypes");
  if (nearMissTypes.length === 0) {
    showError("Select at least one type of near miss.");
    return;
  }

  const payload = {
    departmentProject: field("departmentProject").value,
    incidentDate: field("incidentDate").value,
    incidentTime: field("incidentTime").value,
    location: field("location").value,
    reportedBy: field("reportedBy").value,
    whatHappened: field("whatHappened").value,
    couldHaveHappened: field("couldHaveHappened").value,
    nearMissTypes,
    nearMissTypeOther: field("nearMissTypeOther").value,
    immediateActions: field("immediateActions").value,
    hazardEliminated: field("hazardEliminated").value,
    hazardActionsRequired: field("hazardActionsRequired").value,
    investigationLead: field("investigationLead").value,
    investigationDate: field("investigationDate").value,
    rootCauses: checkedValues("rootCauses"),
    rootCauseOther: field("rootCauseOther").value,
    rootCauseDetail: field("rootCauseDetail").value,
    correctiveActions: listValues("correctiveAction", 4),
    preventiveMeasures: listValues("preventiveMeasure", 4),
    personResponsible: field("personResponsible").value,
    targetCompletionDate: field("targetCompletionDate").value,
    reportedBySignoff: field("reportedBySignoff").value,
    hseManagerSignoff: field("hseManagerSignoff").value,
    followupBy: field("followupBy").value,
    followupDate: field("followupDate").value,
    status: field("status").value,
    statusReason: field("statusReason").value,
    photoKeys: photoUpload.getKeys(),
  };

  const button = form.querySelector("button[type=submit]");
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "Saving report…";
  try {
    const response = await fetch("/api/near-miss", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "The near-miss report could not be saved.");
    form.classList.add("hidden");
    successCopy.textContent = `${result.reportNo} was saved.`;
    successPanel.classList.remove("hidden");
    successPanel.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    showError(error.message || "The near-miss report could not be saved. Check your connection and try again.");
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

form.addEventListener("submit", submitReport);
document.getElementById("new-report")?.addEventListener("click", () => window.location.reload());
