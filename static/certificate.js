"use strict";

const form = document.getElementById("certificate-form");
const errorBox = document.getElementById("form-error");
const successPanel = document.getElementById("success-panel");
const successCopy = document.getElementById("success-copy");
const fileUpload = initCertificateUpload("certificate-file-upload");
const typeSelect = document.getElementById("certificate-type");
const holderRoleField = document.getElementById("holder-role-field");
const subjectNameLabel = document.getElementById("subject-name-label");

function field(name) {
  return form.elements.namedItem(name);
}

function syncCertificateTypeFields() {
  const isPersonnel = typeSelect.value === "personnel";
  holderRoleField.classList.toggle("hidden", !isPersonnel);
  field("holderRole").required = isPersonnel;
  if (!isPersonnel) field("holderRole").value = "";
  subjectNameLabel.innerHTML = isPersonnel
    ? 'Person’s name <b>*</b>'
    : 'Equipment / Asset name <b>*</b>';
}

typeSelect?.addEventListener("change", syncCertificateTypeFields);
syncCertificateTypeFields();

function showError(message) {
  errorBox.textContent = message;
  errorBox.classList.remove("hidden");
  errorBox.scrollIntoView({ behavior: "smooth", block: "center" });
}

async function submitCertificate(event) {
  event.preventDefault();
  errorBox.classList.add("hidden");
  if (!form.reportValidity()) return;

  const fileKey = fileUpload.getKey();
  if (!fileKey) {
    showError("Attach the certificate file before submitting.");
    return;
  }

  const payload = {
    certificateType: field("certificateType").value,
    holderRole: field("holderRole").value,
    subjectName: field("subjectName").value,
    certifyingBody: field("certifyingBody").value,
    certificateNumber: field("certificateNumber").value,
    uploadedBy: field("uploadedBy").value,
    issueDate: field("issueDate").value,
    expiryDate: field("expiryDate").value,
    notes: field("notes").value,
    fileKey,
  };

  const button = form.querySelector("button[type=submit]");
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "Saving certificate…";
  try {
    const response = await fetch("/api/certificates", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "The certificate could not be saved.");
    form.classList.add("hidden");
    successCopy.textContent = `${result.certNo} was saved.`;
    successPanel.classList.remove("hidden");
    successPanel.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    showError(error.message || "The certificate could not be saved. Check your connection and try again.");
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

form.addEventListener("submit", submitCertificate);
document.getElementById("new-report")?.addEventListener("click", () => window.location.reload());
