"use strict";

const form = document.getElementById("good-practice-form");
const errorBox = document.getElementById("form-error");
const successPanel = document.getElementById("success-panel");
const successCopy = document.getElementById("success-copy");
const photoUpload = initPhotoUpload("good-practice-photo-upload");

function field(name) {
  return form.elements.namedItem(name);
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

  const payload = {
    projectName: field("projectName").value,
    location: field("location").value,
    practiceDate: field("practiceDate").value,
    observedBy: field("observedBy").value,
    category: field("category").value,
    categoryOther: field("categoryOther").value,
    description: field("description").value,
    photoKeys: photoUpload.getKeys(),
  };

  const button = form.querySelector("button[type=submit]");
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "Saving observation…";
  try {
    const response = await fetch("/api/good-practice", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "The good practice entry could not be saved.");
    form.classList.add("hidden");
    successCopy.textContent = `${result.reportNo} was saved.`;
    successPanel.classList.remove("hidden");
    successPanel.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    showError(error.message || "The good practice entry could not be saved. Check your connection and try again.");
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

form.addEventListener("submit", submitReport);
document.getElementById("new-report")?.addEventListener("click", () => window.location.reload());
