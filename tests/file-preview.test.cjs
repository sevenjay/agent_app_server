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

function setup({ markdown = false, filename = "app.py", renderer = true, clipboard = true, fetchOK = true, copyOK = true, mermaid, diagrams = [], highlighter = true, download = "# Full raw file\r\nwith unseen content\r\n", saveResponse = { ok: true }, loadScript = (script) => queueMicrotask(() => script.onerror()), timers = { setTimeout, clearTimeout } } = {}) {
  const nodes = Object.fromEntries(["source", "plain", "status"].map((id) => [`file-preview-${id}`, element()]));
  nodes["file-preview-source"].textContent = "<script>raw</script>\n";
  nodes["file-copy-raw"] = element();
  nodes["file-edit"] = element({ disabled: true });
  nodes["file-save"] = element({ hidden: true, dataset: { saveUrl: "/files/upload?path=docs&name=app.py&overwrite=true" } });
  nodes["file-cancel-edit"] = element({ hidden: true });
  nodes["file-editor"] = element({ hidden: true, value: "" });
  nodes["file-preview-truncated"] = element();
  nodes["file-download"] = element({ href: "https://example.test/file/download" });
  nodes["file-render-status"] = element();
  if (markdown) {
    nodes["file-preview-rendered"] = element({ hidden: true, querySelectorAll: () => diagrams });
    nodes["file-preview-tab"] = element({ disabled: true });
    nodes["file-plain-tab"] = element();
  }
  const highlighted = [], copied = [], fetched = [], sanitized = [], scripts = [];
  const rawDownload = download;
  const events = {};
  let reloads = 0;
  const textarea = element({ select() {}, remove() { this.removed = true; } });
  const window = {
    mermaid,
    addEventListener(name, callback) { events[name] = callback; },
    location: { reload() { reloads += 1; } },
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
    TextDecoder, TextEncoder,
    ...timers,
    navigator: { clipboard: clipboard ? { writeText: async (text) => copied.push(text) } : undefined },
    fetch: async (...args) => {
      fetched.push(args);
      if (args[1]?.method === "POST") return saveResponse;
      return {
        ok: fetchOK, text: async () => rawDownload,
        arrayBuffer: async () => typeof rawDownload === "string" ? new TextEncoder().encode(rawDownload) : rawDownload,
      };
    },
    document: {
      getElementById: (id) => nodes[id] || null,
      querySelector: () => ({ dataset: { fileName: filename } }),
      createElement: (tag) => tag === "textarea" ? textarea : element({ querySelector: () => ({}) }),
      body: { append() {} },
      head: { append(script) { scripts.push(script); loadScript(script); } },
      execCommand: () => { if (copyOK) copied.push(textarea.value); return copyOK; },
    },
  });
  return { nodes, highlighted, copied, fetched, sanitized, textarea, rawDownload, scripts, window, events, get reloads() { return reloads; } };
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

test("Edit reads the complete file and Cancel restores the selected Markdown view without saving", async () => {
  const { nodes, fetched, rawDownload } = setup({ markdown: true });
  assert.equal(nodes["file-edit"].disabled, false);
  await nodes["file-edit"].events.click();
  assert.equal(nodes["file-editor"].value, rawDownload.replace(/\r\n/g, "\n"));
  assert.equal(nodes["file-editor"].hidden, false);
  assert.equal(nodes["file-editor"].focused, true);
  assert.equal(nodes["file-preview-rendered"].hidden, true);
  assert.equal(nodes["file-preview-plain"].hidden, true);
  assert.equal(nodes["file-preview-tab"].disabled, true);
  assert.equal(nodes["file-copy-raw"].disabled, true);
  assert.equal(nodes["file-preview-truncated"].hidden, true);
  nodes["file-editor"].value = "Discard this change";
  nodes["file-cancel-edit"].events.click();
  assert.equal(fetched.length, 1);
  assert.equal(nodes["file-editor"].hidden, true);
  assert.equal(nodes["file-preview-rendered"].hidden, false);
  assert.equal(nodes["file-preview-plain"].hidden, true);
  assert.equal(nodes["file-preview-tab"].disabled, false);
  assert.equal(nodes["file-copy-raw"].disabled, false);
  assert.equal(nodes["file-save"].hidden, true);
});

for (const newline of ["\r\n", "\n", "\r"]) {
  test(`Save preserves UTF-8 BOM and ${JSON.stringify(newline)} line endings before reloading`, async () => {
    const app = setup({ download: `\uFEFFfirst${newline}last${newline}` });
    const { nodes, fetched } = app;
    await nodes["file-edit"].events.click();
    assert.equal(nodes["file-editor"].value, "first\nlast\n");
    nodes["file-editor"].value = "first\n修改內容\nlast\n";
    await nodes["file-save"].events.click();
    const [url, request] = fetched[1];
    assert.equal(url, nodes["file-save"].dataset.saveUrl);
    assert.equal(request.method, "POST");
    assert.equal(request.credentials, "same-origin");
    assert.equal(Buffer.from(request.body).toString("utf8"), `\uFEFFfirst${newline}修改內容${newline}last${newline}`);
    assert.equal(app.reloads, 1);
    assert.equal(nodes["file-preview-status"].textContent, "File saved.");
  });
}

test("saving an edit retains content beyond the 1 MiB preview limit", async () => {
  const content = "a".repeat(1024 * 1024) + "\nunseen tail\n";
  const { nodes, fetched } = setup({ download: content });
  await nodes["file-edit"].events.click();
  nodes["file-editor"].value = "updated\n" + nodes["file-editor"].value;
  await nodes["file-save"].events.click();
  assert.equal(Buffer.from(fetched[1][1].body).toString("utf8"), "updated\n" + content);
});

test("Save without edits leaves the original bytes untouched, and empty content can be saved", async () => {
  const app = setup({ download: "original\r\nmixed\n" });
  await app.nodes["file-edit"].events.click();
  await app.nodes["file-save"].events.click();
  assert.equal(app.fetched.length, 1);
  assert.equal(app.reloads, 0);
  assert.equal(app.nodes["file-editor"].hidden, true);
  await app.nodes["file-edit"].events.click();
  app.nodes["file-editor"].value = "";
  await app.nodes["file-save"].events.click();
  assert.equal(app.fetched[2][1].body.length, 0);
  assert.equal(app.reloads, 1);
});

test("a failed save retains edits and enables retry or cancellation", async () => {
  const saveResponse = { ok: false, json: async () => ({ error: { message: "Permission denied." } }) };
  const app = setup({ saveResponse });
  const { nodes } = app;
  await nodes["file-edit"].events.click();
  nodes["file-editor"].value = "keep this edit";
  await nodes["file-save"].events.click();
  assert.equal(nodes["file-editor"].hidden, false);
  assert.equal(nodes["file-editor"].value, "keep this edit");
  assert.equal(nodes["file-editor"].readOnly, false);
  assert.equal(nodes["file-save"].disabled, false);
  assert.equal(nodes["file-cancel-edit"].disabled, false);
  assert.match(nodes["file-preview-status"].textContent, /Could not save file.*Permission denied/);
  assert.equal(app.reloads, 0);
  saveResponse.ok = true;
  await nodes["file-save"].events.click();
  assert.equal(app.reloads, 1);
});

test("saving locks the editor and prevents duplicate writes or cancellation until completion", async () => {
  let finish;
  const saveResponse = new Promise((resolve) => { finish = resolve; });
  const app = setup({ saveResponse });
  const { nodes, fetched } = app;
  await nodes["file-edit"].events.click();
  nodes["file-editor"].value = "changed";
  const pending = nodes["file-save"].events.click();
  assert.equal(nodes["file-editor"].readOnly, true);
  assert.equal(nodes["file-save"].disabled, true);
  assert.equal(nodes["file-cancel-edit"].disabled, true);
  await nodes["file-save"].events.click();
  nodes["file-cancel-edit"].events.click();
  assert.equal(fetched.length, 2);
  assert.equal(nodes["file-editor"].hidden, false);
  finish({ ok: true });
  await pending;
  assert.equal(app.reloads, 1);
});

for (const options of [{ fetchOK: false }, { download: new Uint8Array([0xff]) }, { download: "text\u0000binary" }]) {
  test(`unreadable files cannot enter edit mode: ${JSON.stringify(options)}`, async () => {
    const { nodes, fetched } = setup(options);
    await nodes["file-edit"].events.click();
    assert.equal(nodes["file-editor"].hidden, true);
    assert.equal(nodes["file-preview-plain"].hidden, false);
    assert.equal(nodes["file-edit"].disabled, false);
    assert.equal(nodes["file-copy-raw"].disabled, false);
    assert.equal(fetched.length, 1);
    assert.match(nodes["file-preview-status"].textContent, /Could not open file for editing/);
  });
}

test("leaving with unsaved edits prompts until cancellation or successful saving", async () => {
  const { nodes, events } = setup();
  let prompts = 0;
  const event = { preventDefault() { prompts += 1; } };
  events.beforeunload(event);
  await nodes["file-edit"].events.click();
  events.beforeunload(event);
  assert.equal(prompts, 0);
  nodes["file-editor"].value = "changed";
  events.beforeunload(event);
  assert.equal(prompts, 1);
  assert.equal(event.returnValue, "");
  nodes["file-cancel-edit"].events.click();
  events.beforeunload(event);
  assert.equal(prompts, 1);
  await nodes["file-edit"].events.click();
  nodes["file-editor"].value = "saved";
  await nodes["file-save"].events.click();
  events.beforeunload(event);
  assert.equal(prompts, 1);
});
