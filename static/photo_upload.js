"use strict";

const PHOTO_MAX_BYTES = 8 * 1024 * 1024;
const ALLOWED_PHOTO_TYPES = new Set(["image/jpeg", "image/png", "image/webp"]);
const EXTENSION_CONTENT_TYPES = { jpg: "image/jpeg", jpeg: "image/jpeg", png: "image/png", webp: "image/webp" };

// Some phones/browsers don't report a MIME type for certain extensions (.jpeg is a common
// gap), leaving file.type empty — fall back to the filename extension in that case.
function resolveContentType(file) {
  if (ALLOWED_PHOTO_TYPES.has(file.type)) return file.type;
  const extension = file.name.split(".").pop().toLowerCase();
  return EXTENSION_CONTENT_TYPES[extension] || null;
}

function initPhotoUpload(rootId, maxPhotos = 8) {
  const root = document.getElementById(rootId);
  if (!root) return { getKeys: () => [] };
  const input = root.querySelector('input[type="file"]');
  const addButton = root.querySelector(".photo-add-button");
  const grid = root.querySelector(".photo-grid");
  const token = crypto.randomUUID().replace(/-/g, "");
  const keys = [];

  addButton.addEventListener("click", () => input.click());

  function makeTile(file) {
    const tile = document.createElement("div");
    tile.className = "photo-tile uploading";
    const preview = document.createElement("img");
    preview.src = URL.createObjectURL(file);
    preview.alt = file.name;
    tile.appendChild(preview);
    const label = document.createElement("span");
    label.className = "photo-tile-status";
    label.textContent = "Uploading…";
    tile.appendChild(label);
    return { tile, label };
  }

  async function uploadFile(file) {
    const { tile, label } = makeTile(file);
    grid.appendChild(tile);

    const contentType = resolveContentType(file);
    if (!contentType) {
      tile.classList.remove("uploading");
      tile.classList.add("error");
      label.textContent = "Only JPEG, PNG, or WEBP photos are supported.";
      return;
    }
    if (file.size > PHOTO_MAX_BYTES) {
      tile.classList.remove("uploading");
      tile.classList.add("error");
      label.textContent = "Photo is too large (max 8 MB).";
      return;
    }

    try {
      const presignResponse = await fetch(`/api/uploads/${token}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ contentType }),
      });
      const presigned = await presignResponse.json();
      if (!presignResponse.ok) throw new Error(presigned.error || "Upload failed");

      const uploadResponse = await fetch(presigned.uploadUrl, {
        method: "PUT",
        headers: { "Content-Type": contentType },
        body: file,
      });
      if (!uploadResponse.ok) throw new Error("Upload failed");

      const result = { key: presigned.key };
      keys.push(result.key);
      tile.classList.remove("uploading");
      tile.dataset.key = result.key;
      label.remove();
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "photo-tile-remove";
      remove.setAttribute("aria-label", "Remove photo");
      remove.textContent = "✕";
      remove.addEventListener("click", () => {
        const index = keys.indexOf(result.key);
        if (index >= 0) keys.splice(index, 1);
        tile.remove();
      });
      tile.appendChild(remove);
    } catch (error) {
      tile.classList.remove("uploading");
      tile.classList.add("error");
      label.textContent = error.message || "Upload failed";
    }
  }

  input.addEventListener("change", () => {
    const remaining = Math.max(0, maxPhotos - keys.length);
    [...input.files].slice(0, remaining).forEach(uploadFile);
    input.value = "";
  });

  return { getKeys: () => keys.slice() };
}
