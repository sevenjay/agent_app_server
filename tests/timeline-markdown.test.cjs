const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { test } = require("node:test");
const vm = require("node:vm");

function setup(render, { links = [], projectKey = "agent_app_server" } = {}) {
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
    closest: (selector) => selector === ".timeline-root" ? { dataset: { projectKey } } : null,
    querySelectorAll: (selector) => selector === "a[href]" ? links : [{ textContent: "flowchart TD\nA --> B", parentElement: pre }],
  };
  const mermaid = render && { initialize() {}, render };
  const context = {
    window: { mermaid, renderMarkdown: (source) => source, location: { href: "https://example.test/" } },
    URL,
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

function link(href) {
  return {
    href,
    getAttribute: function () { return this.href; },
    setAttribute: function (_name, value) { this.href = value; },
  };
}

test("Timeline relative links open the session project's file preview", () => {
  const links = ["docs/api.md", "./docs/api.md#examples", "src/app.py", "docs/API%20%E8%B3%87%E6%96%99%231.md"].map(link);
  const view = setup(null, { links });
  view.update({ text: "[API 文件](docs/api.md)" });
  assert.equal(links[0].href, "/api/projects/agent_app_server/files/preview?path=docs%2Fapi.md");
  assert.equal(links[1].href, "/api/projects/agent_app_server/files/preview?path=docs%2Fapi.md#examples");
  assert.equal(links[2].href, "/api/projects/agent_app_server/files/preview?path=src%2Fapp.py");
  assert.equal(new URL(links[3].href, "https://example.test").searchParams.get("path"), "docs/API 資料#1.md");
});

test("streaming Timeline messages rewrite links before Mermaid rendering is enabled", () => {
  const links = [link("docs/../README.md")];
  const view = setup(null, { links, projectKey: "another_project" });
  view.update({ text: "[Home](docs/../README.md)", streaming: true });
  assert.equal(links[0].href, "/api/projects/another_project/files/preview?path=README.md");
});

test("each Timeline uses its own project for file links", () => {
  for (const projectKey of ["agent_app_server", "another_project"]) {
    const links = [link("docs/api.md")];
    const view = setup(null, { links, projectKey });
    view.update({ text: "[API 文件](docs/api.md)" });
    assert.equal(links[0].href, `/api/projects/${projectKey}/files/preview?path=docs%2Fapi.md`);
  }
});

test("Timeline preserves external, page, unrelated site, and malformed links", () => {
  const hrefs = ["https://example.test/docs/api.md", "http://example.test/docs/api.md", "//example.test/docs/api.md", "mailto:dev@example.test", "tel:+123456789", "#section", "?tab=timeline", "/settings", "../outside.md", "invalid%zz.md", ""];
  const links = hrefs.map(link);
  const view = setup(null, { links });
  view.update({ text: "message" });
  assert.deepEqual(links.map((link) => link.href), hrefs);
  assert.equal(view.el.innerHTML, "message");
});

test("messages without a project keep their original links", () => {
  const links = [link("docs/api.md")];
  const view = setup(null, { links, projectKey: "" });
  view.update({ text: "message" });
  assert.equal(links[0].href, "docs/api.md");
});

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
