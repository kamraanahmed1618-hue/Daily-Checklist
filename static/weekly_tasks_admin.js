"use strict";

document.querySelectorAll(".table-wrap .delete-form").forEach((form) => {
  form.addEventListener("submit", (event) => {
    if (!window.confirm("Delete this task permanently? This cannot be undone.")) {
      event.preventDefault();
    }
  });
});

document.getElementById("delete-all-tasks-form")?.addEventListener("submit", (event) => {
  if (!window.confirm("Delete ALL tasks for this week permanently? This cannot be undone.")) {
    event.preventDefault();
  }
});
