"use strict";

const form = document.getElementById("site-request-form");
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

async function submitRequest(event) {
  event.preventDefault();
  errorBox.classList.add("hidden");
  if (!form.reportValidity()) return;

  const payload = {
    department: field("department").value,
    requestedBy: field("requestedBy").value,
    item: field("item").value,
    quantity: field("quantity").value,
    urgency: field("urgency").value,
    notes: field("notes").value,
  };

  const button = form.querySelector("button[type=submit]");
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "Saving request…";
  try {
    const response = await fetch("/api/site-requests", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "The request could not be saved.");
    form.classList.add("hidden");
    successCopy.textContent = `${result.requestNo} was saved.`;
    successPanel.classList.remove("hidden");
    successPanel.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    showError(error.message || "The request could not be saved. Check your connection and try again.");
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

form.addEventListener("submit", submitRequest);
document.getElementById("new-report")?.addEventListener("click", () => window.location.reload());
