"use strict";

const CERTIFICATE_FILE_MAX_BYTES = 15 * 1024 * 1024;
const ALLOWED_CERTIFICATE_TYPES = new Set(["image/jpeg", "image/png", "image/webp", "application/pdf"]);
const CERTIFICATE_EXTENSION_CONTENT_TYPES = {
  jpg: "image/jpeg", jpeg: "image/jpeg", png: "image/png", webp: "image/webp", pdf: "application/pdf",
};

// Some phones/browsers don't report a MIME type for certain extensions, leaving
// file.type empty — fall back to the filename extension in that case.
function resolveCertificateContentType(file) {
  if (ALLOWED_CERTIFICATE_TYPES.has(file.type)) return file.type;
  const extension = file.name.split(".").pop().toLowerCase();
  return CERTIFICATE_EXTENSION_CONTENT_TYPES[extension] || null;
}

function initCertificateUpload(rootId) {
  const root = document.getElementById(rootId);
  if (!root) return { getKey: () => "" };
  const input = root.querySelector('input[type="file"]');
  const addButton = root.querySelector(".photo-add-button");
  const status = root.querySelector(".certificate-upload-status");
  const token = crypto.randomUUID().replace(/-/g, "");
  let key = "";

  addButton.addEventListener("click", () => input.click());

  async function uploadFile(file) {
    const contentType = resolveCertificateContentType(file);
    if (!contentType) {
      status.classList.add("error");
      status.textContent = "Only PDF, JPEG, PNG, or WEBP files are supported.";
      return;
    }
    if (file.size > CERTIFICATE_FILE_MAX_BYTES) {
      status.classList.add("error");
      status.textContent = "File is too large (max 15 MB).";
      return;
    }
    status.classList.remove("error");
    status.textContent = `Uploading ${file.name}…`;

    const formData = new FormData();
    formData.append("file", file);
    try {
      const response = await fetch(`/api/certificate-uploads/${token}`, { method: "POST", body: formData });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "Upload failed");
      key = result.key;
      status.classList.remove("error");
      status.textContent = `Attached: ${file.name}`;
    } catch (error) {
      key = "";
      status.classList.add("error");
      status.textContent = error.message || "Upload failed";
    }
  }

  input.addEventListener("change", () => {
    if (input.files[0]) uploadFile(input.files[0]);
    input.value = "";
  });

  return { getKey: () => key };
}
