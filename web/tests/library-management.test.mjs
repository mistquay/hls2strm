import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import {
  canonicalRoute, cleanupDefaults, fileScenarios, libraryOperations,
  managementLibraryIds, managementParams, managementTabs, managementView,
} from "../src/lib/library-management.ts";

test("management defaults to guide without losing existing film/job deep links", () => {
  assert.equal(managementView(new URLSearchParams()), "center");
  assert.equal(managementView(new URLSearchParams("library=2")), "center");
  assert.equal(managementView(new URLSearchParams("ids=7,9&library=2")), "assets");
  assert.equal(managementView(new URLSearchParams("filter={}")), "assets");
  assert.equal(managementView(new URLSearchParams("job=3")), "history");
  assert.equal(managementView(new URLSearchParams("tab=metadata&job=3")), "metadata");
});

test("old STRM links open the unified workspace and preserve library scope", () => {
  const route = canonicalRoute("strm", new URLSearchParams("library=2"));
  assert.equal(route.page, "maintenance");
  assert.equal(route.params.get("tab"), "strm");
  assert.deepEqual(managementLibraryIds(route.params), [2]);
});

test("navigation retains scope but does not reuse a previous operation's job or action", () => {
  const original = new URLSearchParams("tab=cleanup&library=2&ids=8,9&job=3&action=remove");
  const next = managementParams(original, "differences");
  assert.equal(next.get("tab"), "differences");
  assert.equal(next.get("library"), "2");
  assert.equal(next.get("ids"), "8,9");
  assert.equal(next.has("job"), false);
  assert.equal(next.has("action"), false);
  assert.equal(original.get("job"), "3");
  assert.equal(managementParams(next, "cleanup", "remove").get("action"), "remove");
});

test("direct manual-deletion links start with record reconciliation, not resource deletion", () => {
  assert.deepEqual(cleanupDefaults("differences", null), {action: "reconcile", disposition: "trash"});
  assert.deepEqual(cleanupDefaults("differences", "remove"), {action: "reconcile", disposition: "trash"});
  assert.deepEqual(cleanupDefaults("cleanup", "remove"), {action: "remove", disposition: "trash"});
  assert.deepEqual(cleanupDefaults("cleanup", null), {action: "resources", disposition: "permanent"});
});

test("single and multiple library scopes survive navigation without becoming all-library", () => {
  assert.deepEqual(managementLibraryIds(new URLSearchParams("library=2")), [2]);
  assert.deepEqual(managementLibraryIds(new URLSearchParams("libraries=2,3,2")), [2,3]);
  const next = managementParams(new URLSearchParams("libraries=2,3"), "cleanup");
  assert.deepEqual(managementLibraryIds(next), [2,3]);
  assert.deepEqual(managementLibraryIds(new URLSearchParams("libraries=NaN,-1,2")), [2]);
});

test("each scenario points to an existing operation with explicit filesystem effects", () => {
  assert.equal(new Set(libraryOperations.map(op => op.id)).size, libraryOperations.length);
  for (const op of libraryOperations) {
    assert.ok(op.effects.length && op.boundary.length && op.impacts.length, op.id);
    if (op.view) assert.ok(managementTabs.some(([view]) => view === op.view));
    if (op.externalOnly) assert.equal(op.oneLibrary, true);
  }
  for (const scenario of fileScenarios) assert.ok(libraryOperations.some(op => op.id === scenario.operation));
  const locate = libraryOperations.find(op => op.id === "locate");
  assert.match(locate.boundary, /新增、改写或删除/);
  assert.match(libraryOperations.find(op => op.id === "reclassify").effects, /全局/);
  assert.match(libraryOperations.find(op => op.id === "resources").effects, /默认.*永久删除/);
});

test("verification defaults to no repair and inventory hides destructive controls", () => {
  const options = readFileSync(new URL("../src/components/verify-options.tsx", import.meta.url), "utf8");
  assert.match(options, /defaultVerifyOptions = \{ repair: false/);
  const workspace = readFileSync(new URL("../src/pages/maintenance.tsx", import.meta.url), "utf8");
  assert.match(workspace, /view === "cleanup" \|\| view === "differences"/);
  assert.match(workspace, /此工具不按影片筛选执行/);
  const libraries = readFileSync(new URL("../src/pages/libraries.tsx", import.meta.url), "utf8");
  assert.ok(!libraries.includes("/reclassify`"));
  assert.ok(!libraries.includes("/tidy?apply=false"));
});
