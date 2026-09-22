"use strict";

const form = document.getElementById("violation-form");
const errorBox = document.getElementById("form-error");
const successPanel = document.getElementById("success-panel");
const successCopy = document.getElementById("success-copy");
const photoUpload = initPhotoUpload("violation-photo-upload");

const DESCRIPTIONS = JSON.parse(document.getElementById("hse-descriptions-data").textContent);
const ROLE_COMPANY_NAMES = JSON.parse(document.getElementById("hse-role-company-data").textContent);
const AMBIGUOUS_PENALTY = JSON.parse(document.getElementById("hse-ambiguous-penalty-data").textContent);

const roleSelect = document.getElementById("violator-role");
const companyNameField = document.getElementById("company-name");
const idLabel = document.getElementById("id-field-label");
const idField = document.getElementById("employee-id");
const freeTextBox = document.getElementById("free-text-description");
const interpretButton = document.getElementById("interpret-button");
const interpretStatus = document.getElementById("interpret-status");
const multiCategoryWarning = document.getElementById("multi-category-warning");
const suggestionsBox = document.getElementById("suggestions");
const subTypeSelect = document.getElementById("sub-type-select");
const descriptionSelect = document.getElementById("description-select");
const numberOfViolationSelect = document.getElementById("number-of-violation");
const historyNote = document.getElementById("history-note");
const penaltySelect = document.getElementById("penalty-select");
const subcontractorAmountField = document.getElementById("subcontractor-amount-field");
const subcontractorAmountInput = document.getElementById("subcontractor-amount");

function field(name) {
  return form.elements.namedItem(name);
}

function showError(message) {
  errorBox.textContent = message;
  errorBox.classList.remove("hidden");
  errorBox.scrollIntoView({ behavior: "smooth", block: "center" });
}

// --- Role-driven Company Name + ID label -----------------------------------

roleSelect.addEventListener("change", () => {
  const role = roleSelect.value;
  const lockedName = ROLE_COMPANY_NAMES[role];
  if (lockedName) {
    companyNameField.value = lockedName;
    companyNameField.readOnly = true;
  } else {
    companyNameField.value = "";
    companyNameField.readOnly = false;
  }
  idLabel.innerHTML = role === "BEC Staff"
    ? "Employee ID Number <b>*</b>"
    : "Iqama Number <b>*</b>";
});

let numberOfViolationTouched = false;
numberOfViolationSelect.addEventListener("change", () => {
  numberOfViolationTouched = true;
});

idField.addEventListener("blur", async () => {
  const employeeId = idField.value.replace(/\D/g, "");
  if (!employeeId) return;
  try {
    const response = await fetch(`/api/violations/next-level?employeeId=${encodeURIComponent(employeeId)}`);
    if (!response.ok) return;
    const result = await response.json();
    if (result.count > 0) {
      historyNote.textContent = `This ID has ${result.count} prior violation${result.count === 1 ? "" : "s"} on record.`;
    } else {
      historyNote.textContent = "";
    }
    if (!numberOfViolationTouched) {
      numberOfViolationSelect.value = result.suggested;
    }
  } catch (error) {
    // Best-effort only — never blocks submission.
  }
});

// --- Sub-Type -> Description cascade ----------------------------------------

function populateDescriptions(subType, selected) {
  descriptionSelect.innerHTML = "";
  const matches = DESCRIPTIONS.filter((entry) => entry.sub_type === subType);
  if (!matches.length) {
    descriptionSelect.disabled = true;
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "Choose a Sub-Type first…";
    option.disabled = true;
    option.selected = true;
    descriptionSelect.appendChild(option);
    return;
  }
  descriptionSelect.disabled = false;
  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = "Select…";
  placeholder.disabled = true;
  placeholder.selected = !selected;
  descriptionSelect.appendChild(placeholder);
  for (const entry of matches) {
    const option = document.createElement("option");
    option.value = entry.description;
    option.textContent = entry.description;
    if (entry.description === selected) option.selected = true;
    descriptionSelect.appendChild(option);
  }
}

subTypeSelect.addEventListener("change", () => populateDescriptions(subTypeSelect.value));
populateDescriptions(subTypeSelect.value);

function applyMatch(subType, description) {
  subTypeSelect.value = subType;
  populateDescriptions(subType, description);
  subTypeSelect.scrollIntoView({ behavior: "smooth", block: "center" });
}

// --- Free-text interpreter ---------------------------------------------------

interpretButton.addEventListener("click", async () => {
  const text = freeTextBox.value.trim();
  suggestionsBox.innerHTML = "";
  suggestionsBox.classList.add("hidden");
  multiCategoryWarning.classList.add("hidden");
  interpretStatus.textContent = "";
  if (!text) {
    interpretStatus.textContent = "Describe what happened first.";
    return;
  }
  const original = interpretButton.textContent;
  interpretButton.disabled = true;
  interpretButton.textContent = "Matching…";
  try {
    const response = await fetch("/api/violations/interpret", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Could not match a category.");
    if (result.multiCategory) {
      multiCategoryWarning.textContent = "This sounds like more than one violation. Aconex only accepts one Sub-Type per notice — please submit a separate notice for each finding. " + (result.multiCategoryNote || "");
      multiCategoryWarning.classList.remove("hidden");
    }
    if (!result.matches.length) {
      interpretStatus.textContent = "No close match found — pick from the dropdowns below instead.";
      return;
    }
    for (const match of result.matches) {
      const label = document.createElement("label");
      label.className = "checkbox-tile";
      label.innerHTML = `<input type="radio" name="suggestion">
        <span><strong>[${match.confidence}]</strong> ${match.subType} — ${match.description}<br><small>${match.reason}</small></span>`;
      label.querySelector("input").addEventListener("change", () => applyMatch(match.subType, match.description));
      suggestionsBox.appendChild(label);
    }
    suggestionsBox.classList.remove("hidden");
    interpretStatus.textContent = "Pick the closest match below, or use the dropdowns if none fit.";
  } catch (error) {
    interpretStatus.textContent = error.message || "Could not match a category — pick from the dropdowns below instead.";
  } finally {
    interpretButton.disabled = false;
    interpretButton.textContent = original;
  }
});

// --- Penalty -> conditional SAR amount --------------------------------------

penaltySelect.addEventListener("change", () => {
  const isAmbiguous = penaltySelect.value === AMBIGUOUS_PENALTY;
  subcontractorAmountField.classList.toggle("hidden", !isAmbiguous);
  subcontractorAmountInput.required = isAmbiguous;
  if (!isAmbiguous) subcontractorAmountInput.value = "";
});

// --- Submit -------------------------------------------------------------------

async function submitNotice(event) {
  event.preventDefault();
  errorBox.classList.add("hidden");
  if (!form.reportValidity()) return;
  if (!descriptionSelect.value) {
    showError("Choose a violation description from the approved list — use the matcher above or pick manually.");
    return;
  }
  if (!photoUpload.getKeys().length) {
    showError("Attach at least one photo before submitting — photographs are the primary evidence and are required.");
    return;
  }

  const payload = {
    projectName: field("projectName").value,
    violationDate: field("violationDate").value,
    violatorRole: field("violatorRole").value,
    companyContractor: field("companyContractor").value,
    employeeId: field("employeeId").value,
    employeeName: field("employeeName").value,
    jobTitle: field("jobTitle").value,
    violationLocation: field("violationLocation").value,
    subType: field("subType").value,
    violationDescription: field("violationDescription").value,
    numberOfViolation: field("numberOfViolation").value,
    relDepartment: field("relDepartment").value,
    penalty: field("penalty").value,
    subcontractorDiscountValue: field("subcontractorDiscountValue") ? field("subcontractorDiscountValue").value : "",
    photoKeys: photoUpload.getKeys(),
    documentsAttached: field("documentsAttached").checked,
    issuedByName: field("issuedByName").value,
    issuedByPosition: field("issuedByPosition").value,
  };

  const button = form.querySelector("button[type=submit]");
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "Saving notice…";
  try {
    const response = await fetch("/api/violations", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "The violation notice could not be saved.");
    form.classList.add("hidden");
    successCopy.textContent = `${result.violationNo} was saved.`;
    successPanel.classList.remove("hidden");
    successPanel.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    showError(error.message || "The violation notice could not be saved. Check your connection and try again.");
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

form.addEventListener("submit", submitNotice);
document.getElementById("new-report")?.addEventListener("click", () => window.location.reload());
