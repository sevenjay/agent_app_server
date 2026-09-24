const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { test } = require("node:test");
const vm = require("node:vm");

function consoleView(globals = {}) {
  const context = {
    window: {},
    localStorage: { getItem: () => null },
    URLSearchParams,
    ...globals,
  };
  vm.runInNewContext(readFileSync("static/js/codex-console.js", "utf8"), context);
  return context.window.codexConsole();
}

// Evaluate the row's actual directives against the persistent parent state.
function rowDirective(view, threadId, attribute) {
  const template = readFileSync("templates/_thread_list.html", "utf8");
  const directive = template.split(`${attribute}='`).slice(1)
    .map(value => value.split("'")[0])
    .find(value => value.includes("sessionActionsThreadId"));
  assert.ok(directive, `Missing session menu directive: ${attribute}`);
  const expression = directive.replaceAll("{{ thread.id|tojson }}", JSON.stringify(threadId));
  return vm.runInNewContext(`with (view) { ${expression} }`, { view });
}

test("session menu stays open when its list is refreshed and rows are recreated", async () => {
  let refreshes = 0;
  const view = consoleView({ htmx: { ajax: async () => { refreshes++; } } });
  view.projectKey = "project";
  rowDirective(view, "thread-one", "@click.stop");
  for (let pass = 0; pass < 3; pass++) {
    await view.refreshThreads();
    assert.equal(rowDirective(view, "thread-one", "x-show"), true);
    assert.equal(rowDirective(view, "thread-two", "x-show"), false);
    assert.equal(rowDirective(view, "thread-one", ":aria-expanded"), true);
  }
  assert.equal(refreshes, 3);
});

test("session menus toggle, switch rows, and close on outside clicks", () => {
  const view = consoleView();
  rowDirective(view, "thread-one", "@click.stop");
  rowDirective(view, "thread-two", "@click.stop");
  // A stale outside listener on another row must not close the active menu.
  rowDirective(view, "thread-one", "@click.outside");
  assert.equal(rowDirective(view, "thread-two", "x-show"), true);
  rowDirective(view, "thread-two", "@click.outside");
  assert.equal(rowDirective(view, "thread-two", "x-show"), false);
  rowDirective(view, "thread-two", "@click.stop");
  rowDirective(view, "thread-two", "@click.stop");
  assert.equal(rowDirective(view, "thread-two", "x-show"), false);
});

const validationFailure = async () => ({
  ok: false,
  status: 422,
  json: async () => ({ error: { code: "invalid_request", message: "Check the submitted values." } }),
});

test("failed draft submission restores unsaved text and allows retry", async () => {
  const timeline = { textContent: "" };
  const view = consoleView({
    fetch: validationFailure,
    document: { querySelector: () => timeline },
  });
  view.projectKey = "project";
  view.draftSession = true;
  view.prompt = "An unsaved draft";
  await view.submitPrompt();
  assert.equal(view.prompt, "An unsaved draft");
  assert.equal(view.draftSession, true);
  assert.equal(view.busy, false);
  assert.equal(view.errorMessage, "Check the submitted values.");
});

test("goal validation failure retains the editor and its unsaved fields", async () => {
  const view = consoleView({ fetch: validationFailure });
  view.threadId = "thread-one";
  view.goalEditorOpen = true;
  view.goalObjective = "An unsaved objective";
  view.goalTokenBudget = "12345";
  await view.startGoalFromEditor();
  assert.equal(view.goalObjective, "An unsaved objective");
  assert.equal(view.goalTokenBudget, "12345");
  assert.equal(view.goalEditorOpen, true);
  assert.equal(view.active, false);
  assert.equal(view.busy, false);
  assert.equal(view.errorMessage, "Check the submitted values.");
});
