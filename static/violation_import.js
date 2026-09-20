"use strict";

const form = document.getElementById("violation-import-form");

if (form) {
  const status = document.getElementById("violation-import-status");
  const fileInput = document.getElementById("violation-import-files");

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!fileInput.files.length) {
      status.textContent = "Choose one or more files first.";
      return;
    }
    const button = form.querySelector("button[type=submit]");
    const original = button.textContent;
    button.disabled = true;
    button.textContent = "Importing…";
    status.textContent = "";
    try {
      const formData = new FormData();
      for (const file of fileInput.files) {
        formData.append("files", file);
      }
      const response = await fetch(form.action, { method: "POST", body: formData });
      const result = await response.json();
      if (!response.ok && !result.imported) throw new Error(result.error || "Import failed.");
      const imported = result.imported || [];
      const failed = result.failed || [];
      let message = imported.length
        ? `Imported ${imported.length} notice${imported.length === 1 ? "" : "s"}: ${imported.join(", ")}.`
        : "No notices were imported.";
      if (failed.length) {
        message += ` Failed: ${failed.join("; ")}`;
      }
      status.textContent = message;
      if (imported.length) window.location.reload();
    } catch (error) {
      status.textContent = error.message || "Import failed.";
    } finally {
      button.disabled = false;
      button.textContent = original;
    }
  });
}
