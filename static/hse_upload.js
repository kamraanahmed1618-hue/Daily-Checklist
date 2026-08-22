(() => {
  "use strict";

  const zone = document.getElementById("hseUploadZone");
  const fileInput = document.getElementById("hseFileInput");
  const filenameLabel = document.getElementById("hseUploadFilename");
  const submitBtn = document.getElementById("hseUploadSubmit");
  const form = document.getElementById("hseUploadForm");
  const statusHost = document.getElementById("hseUploadStatus");

  if (!form) return;

  function updateSelectedFile() {
    const file = fileInput.files[0];
    filenameLabel.textContent = file ? file.name : "";
    submitBtn.disabled = !file;
  }
  fileInput.addEventListener("change", updateSelectedFile);

  ["dragenter", "dragover"].forEach(evt => {
    zone.addEventListener(evt, e => {
      e.preventDefault();
      zone.classList.add("dragover");
    });
  });
  ["dragleave", "drop"].forEach(evt => {
    zone.addEventListener(evt, e => {
      e.preventDefault();
      zone.classList.remove("dragover");
    });
  });
  zone.addEventListener("drop", e => {
    const file = e.dataTransfer.files[0];
    if (file) {
      fileInput.files = e.dataTransfer.files;
      updateSelectedFile();
    }
  });

  function esc(s) {
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  function renderResult(payload, ok) {
    const statusClass = payload.status || (ok ? "success" : "failed");
    let html = `<div class="notice ${statusClass === "failed" ? "danger" : statusClass === "partial" ? "warn" : "success"}" role="status">`;
    if (payload.error) {
      html += `<strong>Import failed:</strong> ${esc(payload.error)}`;
    } else {
      html += `<strong>Import ${statusClass}.</strong> ${esc(payload.filename || "")} `;
      html += `(sheet "${esc(payload.sheetName || "")}")${payload.projectName ? " for " + esc(payload.projectName) : ""}.`;
    }
    html += `</div>`;

    if (!payload.error) {
      html += `<div class="hse-upload-result"><dl>
        <div><dt>Processed</dt><dd>${payload.recordsProcessed ?? 0}</dd></div>
        <div><dt>Inserted</dt><dd>${payload.recordsInserted ?? 0}</dd></div>
        <div><dt>Updated</dt><dd>${payload.recordsUpdated ?? 0}</dd></div>
        <div><dt>Rejected</dt><dd>${payload.recordsRejected ?? 0}</dd></div>
      </dl></div>`;
    }

    if (payload.rejectedSample && payload.rejectedSample.length) {
      html += `<div class="hse-upload-errors"><strong>Rows skipped:</strong><ul>`;
      payload.rejectedSample.forEach(reason => { html += `<li>${esc(reason)}</li>`; });
      html += `</ul></div>`;
    }

    statusHost.innerHTML = html;
  }

  form.addEventListener("submit", async e => {
    e.preventDefault();
    const file = fileInput.files[0];
    if (!file) return;

    submitBtn.disabled = true;
    submitBtn.textContent = "Uploading…";
    statusHost.innerHTML = `<div class="notice">Validating and importing "${esc(file.name)}"…</div>`;

    const body = new FormData();
    body.append("workbook", file);

    try {
      const response = await fetch(form.getAttribute("action") || "/admin/hse/upload", {
        method: "POST",
        body,
      });
      let payload;
      try {
        payload = await response.json();
      } catch (parseError) {
        payload = { status: "failed", error: `Unexpected server response (HTTP ${response.status}).` };
      }
      renderResult(payload, response.ok);
      if (response.ok && payload.status !== "failed") {
        // Reload so the import-history table and tab counts (rendered
        // server-side) pick up the new state without duplicating that
        // rendering logic here in JS.
        setTimeout(() => window.location.reload(), 1800);
      } else {
        submitBtn.disabled = false;
        submitBtn.textContent = "Upload & sync";
      }
    } catch (networkError) {
      statusHost.innerHTML = `<div class="notice danger">Upload failed: could not reach the server. Check your connection and try again.</div>`;
      submitBtn.disabled = false;
      submitBtn.textContent = "Upload & sync";
    }
  });

  const statusBtn = document.getElementById("hseStatusCheckBtn");
  const statusResult = document.getElementById("hseStatusCheckResult");
  if (statusBtn) {
    statusBtn.addEventListener("click", async () => {
      statusBtn.disabled = true;
      statusResult.innerHTML = `<div class="notice">Checking&hellip;</div>`;
      try {
        const response = await fetch("/api/hse/status");
        const payload = await response.json();
        if (payload.databaseOk) {
          statusResult.innerHTML = `<div class="notice success">Database reachable. ${payload.recordCount} day(s) stored. Server time: ${esc(payload.serverTime)}.</div>`;
        } else {
          statusResult.innerHTML = `<div class="notice danger">Database is not reachable right now.</div>`;
        }
      } catch (error) {
        statusResult.innerHTML = `<div class="notice danger">Could not reach the API.</div>`;
      } finally {
        statusBtn.disabled = false;
      }
    });
  }
})();
