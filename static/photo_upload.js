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
  if (!root) return { getKeys: () => [], waitForUploads: async () => true };
  const input = root.querySelector('input[type="file"]');
  const addButton = root.querySelector(".photo-add-button");
  const grid = root.querySelector(".photo-grid");
  const token = crypto.randomUUID().replace(/-/g, "");
  const keys = [];
  // Every upload runs in the background while the rest of the form stays usable, so a
  // quick tap on Submit can land before one finishes — without tracking these, the
  // submit handler would read getKeys() too early and silently drop that photo from
  // the record with no error shown anywhere. waitForUploads() lets the caller block
  // until every upload now in flight has actually settled.
  const pending = new Set();

  addButton.addEventListener("click", () => input.click());

  function addRemoveButton(tile, onRemove) {
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "photo-tile-remove";
    remove.setAttribute("aria-label", "Remove photo");
    remove.textContent = "✕";
    remove.addEventListener("click", () => {
      onRemove();
      tile.remove();
    });
    tile.appendChild(remove);
  }

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

  function uploadFile(file) {
    const { tile, label } = makeTile(file);
    grid.appendChild(tile);

    const promise = (async () => {
      const contentType = resolveContentType(file);
      if (!contentType) {
        tile.classList.remove("uploading");
        tile.classList.add("error");
        label.textContent = "Only JPEG, PNG, or WEBP photos are supported.";
        addRemoveButton(tile, () => {});
        return;
      }
      if (file.size > PHOTO_MAX_BYTES) {
        tile.classList.remove("uploading");
        tile.classList.add("error");
        label.textContent = "Photo is too large (max 8 MB).";
        addRemoveButton(tile, () => {});
        return;
      }

      const formData = new FormData();
      formData.append("photo", file);
      try {
        const response = await fetch(`/api/uploads/${token}`, { method: "POST", body: formData });
        const result = await response.json();
        if (!response.ok) throw new Error(result.error || "Upload failed");
        keys.push(result.key);
        tile.classList.remove("uploading");
        tile.dataset.key = result.key;
        label.remove();
        addRemoveButton(tile, () => {
          const index = keys.indexOf(result.key);
          if (index >= 0) keys.splice(index, 1);
        });
      } catch (error) {
        tile.classList.remove("uploading");
        tile.classList.add("error");
        label.textContent = error.message || "Upload failed";
        addRemoveButton(tile, () => {});
      }
    })();

    pending.add(promise);
    promise.finally(() => pending.delete(promise));
  }

  input.addEventListener("change", () => {
    const remaining = Math.max(0, maxPhotos - keys.length);
    [...input.files].slice(0, remaining).forEach(uploadFile);
    input.value = "";
  });

  async function waitForUploads() {
    await Promise.allSettled([...pending]);
    // A failed tile now has its own remove button, so anything still showing .error
    // at this point is a failure the person hasn't dismissed or retried yet.
    return !root.querySelector(".photo-tile.error");
  }

  return { getKeys: () => keys.slice(), waitForUploads };
}
