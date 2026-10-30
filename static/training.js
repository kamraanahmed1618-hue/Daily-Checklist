"use strict";

const form = document.getElementById("training-form");
const errorBox = document.getElementById("form-error");
const successPanel = document.getElementById("success-panel");
const successCopy = document.getElementById("success-copy");
// TBT/Induction sessions can run large — many attendees, multi-page attendance sheets —
// so these get a higher cap than the 8-photo default used elsewhere.
const attendanceUpload = initPhotoUpload("attendance-photo-upload", 20);
const photoUpload = initPhotoUpload("training-photo-upload", 20);

function field(name) {
  return form.elements.namedItem(name);
}

function listValues(prefix, count) {
  const values = [];
  for (let i = 1; i <= count; i += 1) {
    const value = field(`${prefix}${i}`)?.value.trim();
    if (value) values.push(value);
  }
  return values;
}

function showError(message) {
  errorBox.textContent = message;
  errorBox.classList.remove("hidden");
  errorBox.scrollIntoView({ behavior: "smooth", block: "center" });
}

async function submitEntry(event) {
  event.preventDefault();
  errorBox.classList.add("hidden");
  if (!form.reportValidity()) return;
  const [attendanceOk, photosOk] = await Promise.all([attendanceUpload.waitForUploads(), photoUpload.waitForUploads()]);
  if (!attendanceOk || !photosOk) {
    showError("A photo failed to upload — remove it or wait for uploads to finish, then try again.");
    return;
  }

  const payload = {
    sessionType: field("sessionType").value,
    topic: field("topic").value,
    sessionDate: field("sessionDate").value,
    trainer: field("trainer").value,
    location: field("location").value,
    duration: field("duration").value,
    attendeesCount: field("attendeesCount").value,
    objective: field("objective").value,
    summary: field("summary").value,
    keyLessons: listValues("keyLesson", 8),
    remarks: field("remarks").value,
    photoKeys: photoUpload.getKeys(),
    attendancePhotoKeys: attendanceUpload.getKeys(),
  };

  const button = form.querySelector("button[type=submit]");
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "Saving entry…";
  try {
    const response = await fetch("/api/training", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "The training log entry could not be saved.");
    form.classList.add("hidden");
    successCopy.textContent = `${result.topic} was logged.`;
    successPanel.classList.remove("hidden");
    successPanel.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    showError(error.message || "The training log entry could not be saved. Check your connection and try again.");
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

form.addEventListener("submit", submitEntry);
document.getElementById("new-report")?.addEventListener("click", () => window.location.reload());
