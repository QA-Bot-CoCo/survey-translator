const storageKey = (brand) => `survey.glossary.v1.${brand}`;

export function loadGlossary(storage, brand) {
  const raw = storage.getItem(storageKey(brand));
  if (raw === null) return null;
  let record;
  try {
    record = JSON.parse(raw);
  } catch {
    throw new Error("The saved glossary is damaged. Upload it again or use the bundled glossary.");
  }
  if (!record || typeof record.csv !== "string" || !record.csv.trim() ||
      typeof record.name !== "string" || !Array.isArray(record.languages) ||
      !record.languages.every((language) => typeof language === "string")) {
    throw new Error("The saved glossary is damaged. Upload it again or use the bundled glossary.");
  }
  return record;
}

export function saveGlossary(storage, brand, record) {
  storage.setItem(storageKey(brand), JSON.stringify(record));
}

export function removeGlossary(storage, brand) {
  storage.removeItem(storageKey(brand));
}

export function totalSteps(job) {
  return job.targets.length * job.batches.length;
}

export async function translateNext(job, send) {
  if (job.cursor >= totalSteps(job)) return;
  const target = job.targets[Math.floor(job.cursor / job.batches.length)];
  const cells = job.batches[job.cursor % job.batches.length];
  const { translations } = await send({
    brand: job.brand, target, cells, glossary: job.glossary,
  });
  if (!translations || Object.keys(translations).length !== cells.length ||
      !cells.every(({ id }) => typeof translations[id] === "string" && translations[id].trim())) {
    throw new Error("The translation response was incomplete. Resume to retry this batch.");
  }
  job.translations[target] = { ...job.translations[target], ...translations };
  job.cursor += 1;
}
