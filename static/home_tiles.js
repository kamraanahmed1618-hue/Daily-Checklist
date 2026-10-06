"use strict";

document.querySelectorAll(".stat-tile-trigger").forEach((trigger) => {
  trigger.addEventListener("click", () => {
    document.getElementById(trigger.dataset.dialog)?.showModal();
  });
});

document.querySelectorAll(".preview-dialog").forEach((dialog) => {
  // A click that lands on the <dialog> element itself (not its content box) is a
  // backdrop click, since the content box doesn't fill the dialog's own box.
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });
  dialog.querySelector(".dialog-close-button")?.addEventListener("click", () => dialog.close());
});
