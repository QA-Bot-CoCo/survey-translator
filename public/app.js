import { loadGlossary, saveGlossary, removeGlossary, totalSteps, translateNext } from "/state.mjs";

const $ = (id) => document.getElementById(id);
const names = {
  deDE: "German", csCZ: "Czech", frFR: "French", huHU: "Hungarian", itIT: "Italian",
  ruRU: "Russian", esES: "Spanish", roRO: "Romanian", enGB: "English (UK)",
};
let brands;
let glossary = null;
let glossaryReady = false;
let job = null;
let busy = false;
let pauseRequested = false;
let downloadUrl = null;

function showError(error) {
  $("error").textContent = error ? error.message : "";
  $("error").hidden = !error;
}

function controls() {
  const complete = job && job.cursor === totalSteps(job);
  $("settings").disabled = busy || Boolean(job) || !brands;
  $("start").disabled = busy || !brands || !glossaryReady;
  $("start").hidden = Boolean(complete);
  $("start").textContent = job ? "Resume" : "Translate";
  $("pause").hidden = !busy || !job || Boolean(complete);
  $("pause").disabled = pauseRequested;
  $("new-survey").hidden = !job;
  $("new-survey").disabled = busy;
  $("download").hidden = !complete;
  $("download").disabled = busy;
}

async function api(path, body) {
  const multipart = body instanceof FormData;
  let response;
  try {
    response = await fetch(path, {
      method: "POST",
      headers: { "X-Survey-Request": "1", ...(multipart ? {} : { "Content-Type": "application/json" }) },
      body: multipart ? body : JSON.stringify(body),
      signal: AbortSignal.timeout(110000),
    });
  } catch (error) {
    if (error.name === "TimeoutError" || error instanceof TypeError) {
      throw new Error("The request did not finish. Check your connection and retry. Retrying a translation may repeat a provider charge.");
    }
    throw error;
  }
  if (!response.ok) {
    if (response.headers.get("Content-Type")?.includes("application/json")) {
      const detail = await response.json();
      throw new Error(detail.error || `Request failed (${response.status}).`);
    }
    throw new Error(`Request failed (${response.status}). Please retry.`);
  }
  return path === "/api/zip" ? response.blob() : response.json();
}

function glossaryStatus() {
  $("glossary-status").textContent = glossary
    ? `${glossary.name} saved in this browser. Glossary languages: ${glossary.languages.join(", ")}. Other languages use the brand prompt without glossary terms.`
    : "Using the bundled brand glossary.";
}

function readSavedGlossary() {
  glossaryReady = false;
  glossary = null;
  try {
    glossary = loadGlossary(window.localStorage, $("brand").value);
    glossaryReady = true;
    glossaryStatus();
  } catch (error) {
    $("glossary-status").textContent = "The saved glossary could not be loaded.";
    showError(new Error(`${error.message} Browser storage must be available to save glossary updates.`));
  }
  controls();
}

function selectBrand() {
  showError(null);
  const brand = brands[$("brand").value];
  $("targets").replaceChildren();
  for (const target of brand.targets) {
    const label = document.createElement("label");
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.name = "targets";
    checkbox.value = target;
    checkbox.checked = true;
    label.append(checkbox, document.createTextNode(names[target] || target));
    $("targets").append(label);
  }
  $("glossary").value = "";
  $("glossary-help").textContent = `UTF-8 CSV, up to 256 KiB. Use language-code headers, including ${brand.source} and a target such as ${brand.targets[0]}. Semicolons, commas and tabs are supported.`;
  readSavedGlossary();
}

$("brand").addEventListener("change", selectBrand);

$("glossary").addEventListener("change", async () => {
  const file = $("glossary").files[0];
  if (!file) return;
  busy = true;
  controls();
  showError(null);
  try {
    if (!file.name.toLowerCase().endsWith(".csv") || file.size > 256 * 1024) {
      throw new Error("Choose a CSV no larger than 256 KiB.");
    }
    let csv;
    try {
      csv = new TextDecoder("utf-8", { fatal: true }).decode(await file.arrayBuffer());
    } catch (error) {
      if (error instanceof TypeError) throw new Error("Save the glossary as UTF-8 CSV and upload it again.");
      throw error;
    }
    const result = await api("/api/glossary", { brand: $("brand").value, csv });
    const record = { name: file.name, csv, languages: result.languages };
    try {
      saveGlossary(window.localStorage, $("brand").value, record);
    } catch {
      throw new Error("The glossary could not be saved. Enable browser storage or free up space, then try again. Your previous glossary is still selected.");
    }
    glossary = record;
    glossaryReady = true;
    glossaryStatus();
  } catch (error) {
    showError(error);
  } finally {
    $("glossary").value = "";
    busy = false;
    controls();
  }
});

$("reset-glossary").addEventListener("click", () => {
  showError(null);
  try {
    removeGlossary(window.localStorage, $("brand").value);
    glossary = null;
    glossaryReady = true;
    glossaryStatus();
    controls();
  } catch {
    showError(new Error("The saved glossary could not be removed. Enable browser storage and try again."));
  }
});

function formData(current) {
  const data = new FormData();
  data.append("file", current.file);
  data.append("brand", current.brand);
  for (const target of current.targets) data.append("targets", target);
  return data;
}

function progress(message) {
  $("progress-section").hidden = false;
  $("progress").max = totalSteps(job);
  $("progress").value = job.cursor;
  $("status").textContent = message;
}

$("survey-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (busy) return;
  busy = true;
  pauseRequested = false;
  showError(null);
  controls();
  try {
    if (!job) {
      const file = $("file").files[0];
      const targets = [...document.querySelectorAll('input[name="targets"]:checked')].map((input) => input.value);
      if (!file || !file.name.toLowerCase().endsWith(".xlsx") || file.size > 2 * 1024 * 1024) {
        throw new Error("Choose an XLSX workbook no larger than 2 MiB.");
      }
      if (!targets.length) throw new Error("Select at least one target language.");
      const current = { file, brand: $("brand").value, targets, glossary: glossary?.csv ?? null };
      const prepared = await api("/api/prepare", formData(current));
      job = { ...current, filename: prepared.filename, batches: prepared.batches, cursor: 0, translations: {} };
      controls();
    }
    while (!pauseRequested && job.cursor < totalSteps(job)) {
      const target = job.targets[Math.floor(job.cursor / job.batches.length)];
      progress(`Translating ${names[target] || target}. ${job.cursor} of ${totalSteps(job)} batches complete.`);
      await translateNext(job, (body) => api("/api/translate", body));
    }
    progress(job.cursor === totalSteps(job)
      ? "Translations complete. Download your ZIP."
      : `Paused. ${job.cursor} of ${totalSteps(job)} batches complete.`);
  } catch (error) {
    showError(error);
    if (job) progress(`Stopped. ${job.cursor} of ${totalSteps(job)} batches complete. Resume to retry.`);
  } finally {
    busy = false;
    controls();
  }
});

$("pause").addEventListener("click", () => {
  pauseRequested = true;
  controls();
});

$("download").addEventListener("click", async () => {
  if (busy || !job) return;
  busy = true;
  controls();
  showError(null);
  try {
    progress("Preparing ZIP...");
    const data = formData(job);
    const translations = JSON.stringify(job.translations);
    if (new Blob([translations]).size > 2 * 1024 * 1024) {
      throw new Error("Translations exceed the ZIP request limit. Translate fewer languages at a time.");
    }
    data.append("translations", translations);
    const blob = await api("/api/zip", data);
    if (downloadUrl) URL.revokeObjectURL(downloadUrl);
    downloadUrl = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = downloadUrl;
    link.download = `${job.filename.slice(0, -5)}-translations.zip`;
    document.body.append(link);
    link.click();
    link.remove();
    progress("ZIP ready. You can download it again or start a new survey.");
  } catch (error) {
    showError(error);
    progress("ZIP could not be prepared. Your translations are still in this tab; retry the download.");
  } finally {
    busy = false;
    controls();
  }
});

$("new-survey").addEventListener("click", () => {
  job = null;
  if (downloadUrl) URL.revokeObjectURL(downloadUrl);
  downloadUrl = null;
  $("file").value = "";
  $("progress-section").hidden = true;
  showError(null);
  readSavedGlossary();
});

window.addEventListener("beforeunload", (event) => {
  if (job || busy) {
    event.preventDefault();
    event.returnValue = "";
  }
});

window.addEventListener("storage", () => {
  if (brands && !job && !busy) readSavedGlossary();
});

async function init() {
  try {
    const response = await fetch("/brands.json");
    if (!response.ok) throw new Error("Brand configuration could not be loaded. Reload to retry.");
    brands = await response.json();
    for (const [id, brand] of Object.entries(brands)) {
      const option = document.createElement("option");
      option.value = id;
      option.textContent = brand.name;
      $("brand").append(option);
    }
    selectBrand();
  } catch (error) {
    showError(error);
  }
}

init();
