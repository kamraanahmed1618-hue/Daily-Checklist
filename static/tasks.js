"use strict";

document.querySelectorAll(".task-checkbox").forEach((checkbox) => {
  checkbox.addEventListener("change", async () => {
    const taskId = checkbox.dataset.taskId;
    const tile = checkbox.closest(".task-tile");
    checkbox.disabled = true;
    try {
      const response = await fetch(`/api/tasks/${taskId}/toggle`, { method: "POST" });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "Could not update this task.");
      checkbox.checked = result.completed;
      tile.classList.toggle("done", result.completed);
    } catch (error) {
      checkbox.checked = !checkbox.checked;
      alert(error.message || "Could not update this task. Check your connection and try again.");
    } finally {
      checkbox.disabled = false;
    }
  });
});
