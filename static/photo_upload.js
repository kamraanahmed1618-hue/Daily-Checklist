"use strict";

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
