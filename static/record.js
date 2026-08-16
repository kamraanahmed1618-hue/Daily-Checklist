"use strict";
document.getElementById("print-record")?.addEventListener("click", () => window.print());
document.querySelector(".delete-form")?.addEventListener("submit", (event) => {
  if (!window.confirm("Delete this record permanently? This cannot be undone.")) {
    event.preventDefault();
  }
});
