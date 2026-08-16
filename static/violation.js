"use strict";

const form = document.getElementById("violation-form");
const errorBox = document.getElementById("form-error");
const successPanel = document.getElementById("success-panel");
const successCopy = document.getElementById("success-copy");
const photoUpload = initPhotoUpload("violation-photo-upload");

function field(name) {
  return form.elements.namedItem(name);
}

function checkedValues(name) {
  return [...form.querySelectorAll(`input[name="${name}"]:checked`)].map((input) => input.value);
}

function showError(message) {
  errorBox.textContent = message;
  errorBox.classList.remove("hidden");
  errorBox.scrollIntoView({ behavior: "smooth", block: "center" });
}

async function submitNotice(event) {
  event.preventDefault();
  errorBox.classList.add("hidden");
  if (!form.reportValidity()) return;

  const payload = {
    projectName: field("projectName").value,
    violationDate: field("violationDate").value,
    employeeName: field("employeeName").value,
    employeeId: field("employeeId").value,
    companyContractor: field("companyContractor").value,
    jobTitle: field("jobTitle").value,
    violationLocation: field("violationLocation").value,
    violationType: field("violationType").value,
    violationDescription: field("violationDescription").value,
    actions: checkedValues("actions"),
    deductionAmount: field("deductionAmount").value,
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
