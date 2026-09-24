const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { test } = require("node:test");
const vm = require("node:vm");

function consoleView() {
  const context = { window: {}, localStorage: { getItem: () => null } };
  vm.runInNewContext(readFileSync("static/js/codex-console.js", "utf8"), context);
  return context.window.codexConsole();
}

test("HTTP panel errors read the HTMX 4 response context", () => {
  const view = consoleView();
  for (const status of [400, 401, 403, 404, 422, 500, 503]) {
    view.handleHtmxError({
      type: "htmx:response:error",
      detail: { ctx: { response: { status } } },
    });
    assert.equal(view.errorMessage, `A console panel failed to refresh (HTTP ${status}).`);
  }
});

test("network failures and timeouts have no HTTP response", () => {
  const view = consoleView();
  for (const error of [new TypeError("Failed to fetch"), new DOMException("Aborted", "AbortError")]) {
    view.handleHtmxError({ type: "htmx:error", detail: { ctx: {}, error } });
    assert.equal(view.errorMessage, "A console panel failed to refresh. Please try again.");
  }
});

test("swap failures do not report a successful HTTP status as an HTTP error", () => {
  const view = consoleView();
  view.handleHtmxError({
    type: "htmx:error",
    detail: { ctx: { response: { status: 200 } }, error: new Error("Swap failed") },
  });
  assert.equal(view.errorMessage, "A console panel failed to refresh. Please try again.");
});
