const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { test } = require("node:test");
const vm = require("node:vm");

function setup(render) {
  let update, dispose, directive;
  const replacements = [];
  const notices = [];
  const pre = {
    replaceWith: (node) => replacements.push(node),
    before: (node) => notices.push(node),
  };
  const el = {
    isConnected: true,
    innerHTML: "",
    closest: () => null,
    querySelectorAll: () => [{ textContent: "flowchart TD\nA --> B", parentElement: pre }],
  };
  const mermaid = render && { initialize() {}, render };
  const context = {
    window: { mermaid, renderMarkdown: (source) => source },
    document: {
      addEventListener: (_, callback) => callback(),
      createElement: () => ({ querySelector: () => null }),
    },
    Alpine: {
      directive: (_, callback) => { directive = callback; },
      mutateDom: (callback) => callback(),
    },
  };
  vm.runInNewContext(readFileSync("static/js/timeline-markdown.js", "utf8"), context);
  directive(el, { expression: "message" }, {
    evaluateLater: () => (callback) => { update = callback; },
    effect: (callback) => callback(),
    cleanup: (callback) => { dispose = callback; },
  });
  return { el, update: (value) => update(value), dispose: () => dispose(), replacements, notices };
}

const flush = () => new Promise((resolve) => setImmediate(resolve));

test("streaming waits for completion before rendering", async () => {
  let calls = 0;
  const view = setup(async () => { calls++; return { svg: "diagram" }; });
  view.update({ text: "source", streaming: true });
  await flush();
  assert.equal(calls, 0);
  assert.equal(view.el.innerHTML, "source");
  view.update({ text: "source", streaming: false });
  await flush();
  assert.equal(calls, 1);
  assert.equal(view.replacements.length, 1);
});

test("replaced message discards an in-flight diagram", async () => {
  let finish;
  const view = setup(() => new Promise((resolve) => { finish = resolve; }));
  view.update({ text: "old" });
  await flush();
  view.update({ text: "new", streaming: true });
  finish({ svg: "obsolete" });
  await flush();
  assert.equal(view.replacements.length, 0);
  assert.equal(view.el.innerHTML, "new");
});

test("cleanup discards an in-flight diagram", async () => {
  let finish;
  const view = setup(() => new Promise((resolve) => { finish = resolve; }));
  view.update({ text: "old" });
  await flush();
  view.dispose();
  finish({ svg: "obsolete" });
  await flush();
  assert.equal(view.replacements.length, 0);
});

test("invalid syntax preserves source and does not block the next render", async () => {
  let calls = 0;
  const view = setup(async () => {
    if (++calls === 1) throw new Error("Invalid diagram");
    return { svg: "valid" };
  });
  view.update({ text: "invalid" });
  await flush();
  assert.equal(view.replacements.length, 0);
  assert.equal(view.el.innerHTML, "invalid");
  assert.equal(view.notices.length, 1);
  view.update({ text: "valid" });
  await flush();
  assert.equal(view.replacements.length, 1);
});

test("missing Mermaid preserves rendered Markdown", async () => {
  const view = setup(null);
  view.update({ text: "markdown" });
  await flush();
  assert.equal(view.el.innerHTML, "markdown");
  assert.equal(view.replacements.length, 0);
});
