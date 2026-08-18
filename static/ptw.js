"use strict";

const form = document.getElementById("ptw-form");
const errorBox = document.getElementById("form-error");
const successPanel = document.getElementById("success-panel");
const successCopy = document.getElementById("success-copy");

function field(name) {
  return form.elements.namedItem(name);
}

function showError(message) {
  errorBox.textContent = message;
  errorBox.classList.remove("hidden");
  errorBox.scrollIntoView({ behavior: "smooth", block: "center" });
}

async function submitEntry(event) {
  event.preventDefault();
  errorBox.classList.add("hidden");
  if (!form.reportValidity()) return;

  const payload = {
    ptwNumber: field("ptwNumber").value,
    ptwType: field("ptwType").value,
    issuer: field("issuer").value,
    receiver: field("receiver").value,
    company: field("company").value,
    location: field("location").value,
    areaHsePersonnel: field("areaHsePersonnel").value,
    shift: field("shift").value,
    workersCount: field("workersCount").value,
    reviewedBy: field("reviewedBy").value,
    workDescription: field("workDescription").value,
    startDate: field("startDate").value,
    startTime: field("startTime").value,
    endDate: field("endDate").value,
    endTime: field("endTime").value,
  };

  const button = form.querySelector("button[type=submit]");
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "Saving entry…";
  try {
    const response = await fetch("/api/ptw", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "The PTW log entry could not be saved.");
    form.classList.add("hidden");
    successCopy.textContent = `${result.ptwNumber} was logged.`;
    successPanel.classList.remove("hidden");
    successPanel.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    showError(error.message || "The PTW log entry could not be saved. Check your connection and try again.");
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

form.addEventListener("submit", submitEntry);
document.getElementById("new-report")?.addEventListener("click", () => window.location.reload());
