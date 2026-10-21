"use strict";

const csrf = document.querySelector('meta[name="csrf-token"]').content;
const form = document.getElementById("survey-form");
const brand = document.getElementById("brand");
const fileInput = document.getElementById("file");
const errorBox = document.getElementById("error");
const resumeButton = document.getElementById("resume");
const pauseButton = document.getElementById("pause");
const deleteButton = document.getElementById("delete");
const submitButton = document.getElementById("translate");
let currentJob = null;
let running = false;
let pauseRequested = false;

function showError(message) {
  errorBox.textContent = message;
  errorBox.hidden = !message;
}
function selectBrand() {
  document.querySelectorAll("[data-brand]").forEach((group) => {
    group.hidden = group.dataset.brand !== brand.value;
    group.querySelectorAll("input").forEach((input) => { input.disabled = group.hidden; });
  });
}
function describeFile() {
  const file = fileInput.files[0];
  document.getElementById("file-description").textContent = file
    ? `${file.name} / ${(file.size / 1024).toFixed(0)} KiB`
    : "Drop a file here, or use the file picker. Up to 2 MiB.";
  if (file && file.size > 2 * 1024 * 1024) showError("Choose a workbook no larger than 2 MiB.");
}
async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options, credentials: "same-origin",
    headers: { "X-CSRF-Token": csrf, ...options.headers },
  });
  if (response.status === 204) return null;
  const data = await response.json().catch(() => ({
    error: "The request could not complete. Reload the page to resume your job.",
  }));
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status}).`);
  return data;
}
function renderJob(job) {
  currentJob = job;
  // Only an opaque ID is stored in this tab, never document text or credentials.
  sessionStorage.setItem("survey-job", job.id);
  document.getElementById("empty-state").hidden = true;
  document.getElementById("job").hidden = false;
  document.getElementById("job-brand").textContent = job.brand;
  document.getElementById("job-name").textContent = job.filename;
  const progress = document.getElementById("progress");
  progress.max = job.total;
  progress.value = job.completed;
  document.getElementById("job-status").textContent = job.done
    ? `Ready to review. ${job.targets.length} language(s) translated.`
    : `${job.completed} of ${job.total} batches complete. ${job.texts} survey texts.`;
  const downloads = document.getElementById("downloads");
  downloads.replaceChildren();
  job.downloads.forEach((download) => {
    const link = document.createElement("a");
    link.className = "download";
    link.href = download.url;
    link.textContent = `Download ${download.language} workbook`;
    downloads.appendChild(link);
  });
  resumeButton.hidden = running || job.done;
}
function busy(value) {
  running = value;
  submitButton.disabled = value || Boolean(currentJob);
  fileInput.disabled = value || Boolean(currentJob);
  brand.disabled = value || Boolean(currentJob);
  deleteButton.disabled = value;
  pauseButton.hidden = !value;
  resumeButton.hidden = value || !currentJob || currentJob.done;
}
async function runJob() {
  if (!currentJob || running || currentJob.done) return;
  showError("");
  pauseRequested = false;
  busy(true);
  try {
    while (!currentJob.done && !pauseRequested) {
      renderJob(await api(`/api/jobs/${currentJob.id}/step`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ cursor: currentJob.completed }),
      }));
    }
  } catch (error) {
    showError(error.message);
  } finally {
    busy(false);
    pauseButton.textContent = "Pause after this batch";
  }
}
form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (running || currentJob) return;
  showError("");
  const data = new FormData(form);
  if (!data.getAll("targets").length) { showError("Choose at least one language."); return; }
  if (!fileInput.files[0] || fileInput.files[0].size > 2 * 1024 * 1024) {
    showError("Choose a workbook no larger than 2 MiB."); return;
  }
  busy(true);
  try {
    renderJob(await api("/api/jobs", { method: "POST", body: data }));
  } catch (error) {
    showError(error.message);
  } finally {
    busy(false);
  }
  if (currentJob) await runJob();
});
resumeButton.addEventListener("click", runJob);
pauseButton.addEventListener("click", () => {
  pauseRequested = true;
  pauseButton.textContent = "Pausing after this batch...";
});
deleteButton.addEventListener("click", async () => {
  if (!currentJob || running) return;
  busy(true);
  try {
    await api(`/api/jobs/${currentJob.id}`, { method: "DELETE" });
    currentJob = null;
    sessionStorage.removeItem("survey-job");
    document.getElementById("job").hidden = true;
    document.getElementById("empty-state").hidden = false;
    showError("");
  } catch (error) {
    showError(error.message);
  } finally {
    busy(false);
  }
});
brand.addEventListener("change", selectBrand);
fileInput.addEventListener("change", describeFile);
const dropZone = document.getElementById("drop-zone");
dropZone.addEventListener("dragover", (event) => {
  event.preventDefault();
  if (!running && !currentJob) dropZone.classList.add("dragging");
});
dropZone.addEventListener("dragleave", () => dropZone.classList.remove("dragging"));
dropZone.addEventListener("drop", (event) => {
  event.preventDefault();
  dropZone.classList.remove("dragging");
  if (running || currentJob) return;
  if (event.dataTransfer.files.length !== 1) { showError("Choose one workbook at a time."); return; }
  fileInput.files = event.dataTransfer.files;
  describeFile();
});
selectBrand();
const previousJob = sessionStorage.getItem("survey-job");
if (previousJob && /^[a-f0-9]{32}$/.test(previousJob)) {
  busy(true);
  api(`/api/jobs/${previousJob}`).then(renderJob).catch((error) => {
    sessionStorage.removeItem("survey-job");
    showError(error.message);
  }).finally(() => busy(false));
}
