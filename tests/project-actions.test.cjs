const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { test } = require("node:test");
const vm = require("node:vm");

function viewForProject(window = {}) {
  const list = { textContent: "existing sessions", replaceChildren(child) { this.textContent = child.textContent; } };
  const context = {
    window,
    localStorage: { getItem: () => null },
    document: { getElementById: () => list, createElement: () => ({}) },
  };
  vm.runInNewContext(readFileSync("static/js/codex-console.js", "utf8"), context);
  const view = context.window.codexConsole();
  view.projects = [{ key: "demo", name: "Demo", path: "/projects/demo" }];
  view.projectKey = "demo";
  view.threadId = "thread-one";
  view.refreshProjects = async () => {};
  view.refreshThreads = async () => {};
  view.refreshThread = async () => {};
  view.resetProjectFiles = () => {};
  view.closeEvents = () => {};
  view.clearThreadPanels = () => {};
  return { view, list };
}

test("cancelled project deletion leaves files and the selected session alone", async () => {
  let confirmation;
  const { view, list } = viewForProject({ confirm: (message) => { confirmation = message; return false; } });
  view.api = async () => assert.fail("No deletion should be sent");
  await view.deleteProject("demo");
  assert.match(confirmation, /\/projects\/demo/);
  assert.match(confirmation, /all files/);
  assert.equal(view.threadId, "thread-one");
  assert.equal(list.textContent, "existing sessions");
});

test("failed project deletion retains selection and allows a retry", async () => {
  const { view, list } = viewForProject({ confirm: () => true });
  view.api = async () => { throw new Error("Project is in use"); };
  await view.deleteProject("demo");
  assert.equal(view.projectKey, "demo");
  assert.equal(view.threadId, "thread-one");
  assert.equal(view.projects.length, 1);
  assert.equal(view.errorMessage, "Project is in use");
  assert.equal(view.projectOperationBusy, false);
  assert.equal(list.textContent, "existing sessions");
});

test("deleting the selected project clears its sessions and event subscription", async () => {
  const { view, list } = viewForProject({ confirm: () => true });
  let eventsClosed = false;
  view.closeEvents = () => { eventsClosed = true; };
  view.api = async (url, options) => {
    assert.equal(url, "/api/projects/demo");
    assert.equal(options.method, "DELETE");
    return {};
  };
  await view.deleteProject("demo");
  assert.equal(view.projectKey, "");
  assert.equal(view.threadId, "");
  assert.equal(view.projects.length, 0);
  assert.equal(eventsClosed, true);
  assert.match(list.textContent, /Choose a project/);
});

test("renaming the selected project adopts its new key and keeps its session selected", async () => {
  const { view } = viewForProject({ prompt: () => "Renamed" });
  const renamed = { key: "renamed", name: "Renamed", path: "/projects/Renamed" };
  view.api = async (url, options) => {
    assert.equal(url, "/api/projects/demo");
    assert.equal(options.method, "PATCH");
    assert.deepEqual(JSON.parse(options.body), { name: "Renamed" });
    return renamed;
  };
  await view.renameProject("demo");
  assert.equal(view.projectKey, "renamed");
  assert.equal(view.threadId, "thread-one");
  assert.equal(view.projects[0].path, "/projects/Renamed");
  assert.equal(view.projectOperationBusy, false);
});
