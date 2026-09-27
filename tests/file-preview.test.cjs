const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { test } = require("node:test");
const vm = require("node:vm");

function element(extra = {}) {
  return {
    textContent: "", innerHTML: "", hidden: false, disabled: false, attrs: {}, events: {},
    classList: { add() {} },
    setAttribute(key, value) { this.attrs[key] = value; },
    addEventListener(name, callback) { this.events[name] = callback; },
    focus() { this.focused = true; },
    remove() { this.removed = true; },
    ...extra,
  };
}

function setup({ markdown = false, filename = "app.py", renderer = true, clipboard = true, fetchOK = true, copyOK = true, mermaid, diagrams = [], highlighter = true, loadScript = (script) => queueMicrotask(() => script.onerror()), timers = { setTimeout, clearTimeout } } = {}) {
  const nodes = Object.fromEntries(["source", "plain", "status"].map((id) => [`file-preview-${id}`, element()]));
  nodes["file-preview-source"].textContent = "<script>raw</script>\n";
  nodes["file-copy-raw"] = element();
  nodes["file-download"] = element({ href: "https://example.test/file/download" });
  nodes["file-render-status"] = element();
  if (markdown) {
    nodes["file-preview-rendered"] = element({ hidden: true, querySelectorAll: () => diagrams });
    nodes["file-preview-tab"] = element({ disabled: true });
    nodes["file-plain-tab"] = element();
  }
  const highlighted = [], copied = [], fetched = [], sanitized = [], scripts = [];
  const rawDownload = "# Full raw file\r\nwith unseen content\r\n";
  const textarea = element({ select() {}, remove() { this.removed = true; } });
  const window = {
    mermaid,
    hljs: highlighter ? {
      getLanguage: (language) => ["python", "javascript", "yaml", "ini"].includes(language),
      highlight: (text, options) => { highlighted.push({ text, ...options }); return { value: "escaped-highlight" }; },
    } : undefined,
    ...(renderer ? {
      marked: { parse: (text) => `<h1>${text}</h1>` },
      DOMPurify: { sanitize: (html, options) => { sanitized.push({ html, options }); return "clean-markdown"; } },
    } : {}),
  };
  vm.runInNewContext(readFileSync("static/js/file-preview.js", "utf8"), {
    window,
    ...timers,
    navigator: { clipboard: clipboard ? { writeText: async (text) => copied.push(text) } : undefined },
    fetch: async (...args) => { fetched.push(args); return { ok: fetchOK, text: async () => rawDownload }; },
    document: {
      getElementById: (id) => nodes[id] || null,
      querySelector: () => ({ dataset: { fileName: filename } }),
      createElement: (tag) => tag === "textarea" ? textarea : element({ querySelector: () => ({}) }),
      body: { append() {} },
      head: { append(script) { scripts.push(script); loadScript(script); } },
      execCommand: () => { if (copyOK) copied.push(textarea.value); return copyOK; },
    },
  });
  return { nodes, highlighted, copied, fetched, sanitized, textarea, rawDownload, scripts, window };
}

function diagramCode(textContent) {
  const replacements = [], notices = [];
  return {
    textContent, classList: ["language-mermaid"], replacements, notices,
    parentElement: {
      replaceWith: (node) => replacements.push(node),
      before: (node) => notices.push(node),
    },
  };
}

const flush = () => new Promise((resolve) => setImmediate(resolve));

test("Mermaid renders sequentially in a sandbox and keeps Plain text available while pending", async () => {
  const diagrams = [diagramCode("flowchart TD\nA --> B"), diagramCode("sequenceDiagram\nA->>B: Hello")];
  const calls = [], finishes = [];
  let config;
  const mermaid = {
    initialize(options) { config = options; },
    render(id, source) {
      calls.push({ id, source });
      return new Promise((resolve) => finishes.push(resolve));
    },
  };
  const { nodes } = setup({ markdown: true, mermaid, diagrams });
  assert.equal(config.securityLevel, "sandbox");
  assert.equal(config.htmlLabels, false);
  assert.equal(config.startOnLoad, false);
  assert.equal(calls.length, 1);
  nodes["file-plain-tab"].events.click();
  assert.equal(nodes["file-preview-plain"].hidden, false);
  finishes[0]({ svg: "<iframe>first diagram</iframe>" });
  await flush();
  assert.equal(calls.length, 2);
  assert.equal(calls[0].source, diagrams[0].textContent);
  assert.notEqual(calls[0].id, calls[1].id);
  assert.equal(diagrams[0].replacements[0].innerHTML, "<iframe>first diagram</iframe>");
  finishes[1]({ svg: "<iframe>second diagram</iframe>" });
  await flush();
  assert.equal(diagrams[1].replacements.length, 1);
  assert.equal(nodes["file-preview-rendered"].hidden, true);
  nodes["file-preview-tab"].events.click();
  assert.equal(calls.length, 2);
  assert.equal(nodes["file-preview-source"].textContent, "<script>raw</script>\n");
});

test("invalid Mermaid retains its source while the next diagram still renders", async () => {
  const diagrams = [diagramCode("invalid diagram"), diagramCode("flowchart LR\nA --> B")];
  setup({ markdown: true, diagrams, mermaid: {
    initialize() {},
    async render(_id, source) {
      if (source === "invalid diagram") throw new Error("Parse error");
      return { svg: "<iframe>valid diagram</iframe>" };
    },
  } });
  await flush();
  assert.equal(diagrams[0].replacements.length, 0);
  assert.match(diagrams[0].notices[0].textContent, /Source shown below/);
  assert.equal(diagrams[1].replacements.length, 1);
});

test("missing Mermaid library keeps Markdown and diagram source readable", async () => {
  const diagram = diagramCode("flowchart TD\nA --> B");
  const { nodes } = setup({ markdown: true, diagrams: [diagram] });
  await flush();
  assert.equal(diagram.replacements.length, 0);
  assert.equal(diagram.notices.length, 1);
  assert.match(diagram.notices[0].textContent, /renderer could not be loaded/);
  assert.equal(nodes["file-preview-rendered"].hidden, false);
  assert.equal(nodes["file-copy-raw"].disabled, false);
});

test("a stalled Mermaid download does not block Preview, Plain text, or Copy raw", async () => {
  const diagram = diagramCode("flowchart LR\nA --> B");
  const { nodes, scripts, window, copied } = setup({ markdown: true, diagrams: [diagram], loadScript() {} });
  assert.equal(nodes["file-preview-tab"].disabled, false);
  assert.equal(nodes["file-preview-rendered"].hidden, false);
  assert.match(diagram.notices[0].textContent, /Loading diagram/);
  nodes["file-plain-tab"].events.click();
  assert.equal(nodes["file-preview-plain"].hidden, false);
  await nodes["file-copy-raw"].events.click();
  assert.equal(copied.length, 1);
  window.mermaid = { initialize() {}, render: async () => ({ svg: "<iframe>diagram</iframe>" }) };
  scripts[0].onload();
  await flush();
  assert.equal(diagram.replacements.length, 1);
  assert.equal(diagram.notices[0].removed, true);
  assert.equal(nodes["file-preview-rendered"].hidden, true);
});

test("a timed-out diagram download reports its failure without disabling Preview", async () => {
  let timeout;
  const diagram = diagramCode("flowchart LR\nA --> B");
  const { nodes, scripts } = setup({
    markdown: true, diagrams: [diagram], loadScript() {},
    timers: { setTimeout(callback) { timeout = callback; }, clearTimeout() {} },
  });
  timeout();
  await flush();
  assert.match(diagram.notices[0].textContent, /renderer could not be loaded/);
  assert.equal(nodes["file-preview-tab"].disabled, false);
  assert.equal(nodes["file-copy-raw"].disabled, false);
  assert.equal(scripts[0].removed, true);
});

test("a failed highlighter download leaves Markdown controls usable", async () => {
  const { nodes } = setup({ markdown: true, highlighter: false });
  await flush();
  assert.equal(nodes["file-preview-tab"].disabled, false);
  assert.equal(nodes["file-preview-rendered"].hidden, false);
  assert.equal(nodes["file-copy-raw"].disabled, false);
});

test("Markdown defaults to sanitized preview and supports click and keyboard switching", () => {
  const { nodes, sanitized, highlighted } = setup({ markdown: true });
  const preview = nodes["file-preview-tab"], plain = nodes["file-plain-tab"];
  assert.equal(nodes["file-preview-rendered"].innerHTML, "clean-markdown");
  assert.equal(nodes["file-preview-rendered"].hidden, false);
  assert.equal(nodes["file-preview-plain"].hidden, true);
  assert.equal(preview.attrs["aria-selected"], "true");
  assert.equal(sanitized[0].options.FORBID_TAGS.includes("style"), true);
  assert.equal(highlighted.length, 0);
  plain.events.click();
  assert.equal(nodes["file-preview-plain"].hidden, false);
  assert.equal(nodes["file-preview-source"].textContent, "<script>raw</script>\n");
  plain.events.keydown({ key: "ArrowLeft", preventDefault() {} });
  assert.equal(preview.focused, true);
  assert.equal(nodes["file-preview-plain"].hidden, true);
});

test("missing Markdown libraries keep readable plain text and copying available", () => {
  const { nodes } = setup({ markdown: true, renderer: false });
  assert.equal(nodes["file-preview-rendered"].hidden, true);
  assert.equal(nodes["file-preview-plain"].hidden, false);
  assert.equal(nodes["file-preview-tab"].disabled, true);
  assert.match(nodes["file-render-status"].textContent, /libraries could not be loaded/);
  assert.equal(nodes["file-copy-raw"].disabled, false);
});

for (const [filename, language] of Object.entries({ "APP.PY": "python", "app.js": "javascript", "config.yaml": "yaml", "config.yml": "yaml", "pyproject.toml": "ini" })) {
  test(`${filename} highlights source as ${language}`, () => {
    const { nodes, highlighted } = setup({ filename });
    assert.equal(highlighted[0].language, language);
    assert.equal(highlighted[0].text, "<script>raw</script>\n");
    assert.equal(nodes["file-preview-source"].innerHTML, "escaped-highlight");
  });
}

test("unknown files stay as plain text", () => {
  assert.equal(setup({ filename: "notes.unknown" }).highlighted.length, 0);
  assert.equal(setup({ filename: "notes.constructor" }).highlighted.length, 0);
  assert.equal(setup({ filename: "notes.__proto__" }).highlighted.length, 0);
});

test("Copy raw reads the full file, preserving CRLF regardless of the active view", async () => {
  const { nodes, copied, fetched, rawDownload } = setup({ markdown: true });
  await nodes["file-copy-raw"].events.click();
  assert.deepEqual(copied, [rawDownload]);
  assert.equal(fetched[0][0], nodes["file-download"].href);
  assert.equal(fetched[0][1].credentials, "same-origin");
  assert.equal(nodes["file-copy-raw"].disabled, false);
  assert.equal(nodes["file-preview-status"].textContent, "Raw content copied.");
});

test("Copy raw falls back when the Clipboard API is unavailable", async () => {
  const { nodes, copied, textarea, rawDownload } = setup({ clipboard: false });
  await nodes["file-copy-raw"].events.click();
  assert.deepEqual(copied, [rawDownload]);
  assert.equal(textarea.removed, true);
});

for (const options of [{ fetchOK: false }, { clipboard: false, copyOK: false }]) {
  test(`copy failures report an error and allow retry: ${JSON.stringify(options)}`, async () => {
    const { nodes, copied } = setup(options);
    await nodes["file-copy-raw"].events.click();
    assert.deepEqual(copied, []);
    assert.match(nodes["file-preview-status"].textContent, /Could not copy/);
    assert.equal(nodes["file-copy-raw"].disabled, false);
  });
}
