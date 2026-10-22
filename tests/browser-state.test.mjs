import assert from "node:assert/strict";
import test from "node:test";
import { loadGlossary, saveGlossary, removeGlossary, totalSteps, translateNext } from "../public/state.mjs";

function storage() {
  const values = new Map();
  return {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: (key) => values.delete(key),
  };
}

test("glossary survives a new page instance, replaces per brand and resets independently", () => {
  const disk = storage();
  const first = { name: "first.csv", csv: "enUS;deDE\nHello;Hallo", languages: ["deDE"] };
  const updated = { ...first, name: "new.csv", csv: "enUS;deDE\nHello;Guten Tag" };
  saveGlossary(disk, "gaminator", first);
  saveGlossary(disk, "gametwist", first);
  assert.deepEqual(loadGlossary(disk, "gaminator"), first);
  saveGlossary(disk, "gaminator", updated);
  assert.deepEqual(loadGlossary(disk, "gaminator"), updated);
  assert.deepEqual(loadGlossary(disk, "gametwist"), first);
  removeGlossary(disk, "gaminator");
  assert.equal(loadGlossary(disk, "gaminator"), null);
  assert.deepEqual(loadGlossary(disk, "gametwist"), first);
});

test("corrupt data and storage quota failures are surfaced without replacing the old glossary", () => {
  const disk = storage();
  for (const value of ["bad json", "null", "{}", '{"csv":3}']) {
    disk.setItem("survey.glossary.v1.gaminator", value);
    assert.throws(() => loadGlossary(disk, "gaminator"), /damaged/);
  }
  const original = { name: "old.csv", csv: "a;b", languages: ["deDE"] };
  saveGlossary(disk, "gaminator", original);
  disk.setItem = () => { throw new Error("Quota exceeded"); };
  assert.throws(() => saveGlossary(disk, "gaminator", { ...original, name: "new.csv" }), /Quota/);
  assert.deepEqual(loadGlossary(disk, "gaminator"), original);
});

test("failed batch resumes without skipping or repeating completed translations", async () => {
  const job = {
    brand: "gametwist", glossary: "saved CSV", targets: ["deDE", "frFR"],
    batches: [[{ id: "item0", text: "Hello" }], [{ id: "item1", text: "Welcome" }]],
    cursor: 0, translations: {},
  };
  const requests = [];
  const send = async (body) => {
    requests.push(body);
    return { translations: Object.fromEntries(body.cells.map(({ id, text }) => [id, `${body.target}: ${text}`])) };
  };
  await translateNext(job, send);
  await assert.rejects(translateNext(job, async () => { throw new Error("offline"); }), /offline/);
  assert.equal(job.cursor, 1);
  assert.deepEqual(job.translations.deDE, { item0: "deDE: Hello" });
  while (job.cursor < totalSteps(job)) await translateNext(job, send);
  await translateNext(job, send);
  assert.equal(requests.length, 4);
  assert.deepEqual(requests.map(({ target }) => target), ["deDE", "deDE", "frFR", "frFR"]);
  assert.ok(requests.every(({ glossary }) => glossary === "saved CSV"));
  assert.deepEqual(job.translations.frFR, { item0: "frFR: Hello", item1: "frFR: Welcome" });
});

test("incomplete response leaves the current batch retryable", async () => {
  const job = { brand: "gaminator", targets: ["deDE"], batches: [[{ id: "item0", text: "Hello" }]], cursor: 0, translations: {} };
  await assert.rejects(translateNext(job, async () => ({ translations: {} })), /incomplete/);
  assert.equal(job.cursor, 0);
  assert.deepEqual(job.translations, {});
});
