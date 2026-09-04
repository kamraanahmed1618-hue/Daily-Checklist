"use strict";

const form = document.getElementById("training-import-form");

if (form) {
  const status = document.getElementById("training-import-status");
  const fileInput = document.getElementById("training-import-file");

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!fileInput.files.length) {
      status.textContent = "Choose a file first.";
      return;
    }
    const button = form.querySelector("button[type=submit]");
    const original = button.textContent;
    button.disabled = true;
    button.textContent = "Importing…";
    status.textContent = "";
    try {
      const formData = new FormData();
      formData.append("file", fileInput.files[0]);
      const response = await fetch(form.action, { method: "POST", body: formData });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "Import failed.");
      status.textContent = `Imported ${result.imported} entr${result.imported === 1 ? "y" : "ies"}.`;
      window.location.reload();
    } catch (error) {
      status.textContent = error.message || "Import failed.";
    } finally {
      button.disabled = false;
      button.textContent = original;
    }
  });
}
