"use strict";

(() => {
  const source = document.getElementById("file-preview-source");
  if (!source) return;

  const status = document.getElementById("file-preview-status");
  const renderStatus = document.getElementById("file-render-status");
  const plain = document.getElementById("file-preview-plain");
  const rendered = document.getElementById("file-preview-rendered");
  const previewTab = document.getElementById("file-preview-tab");
  const plainTab = document.getElementById("file-plain-tab");
  const rawText = source.textContent;
  const layout = document.querySelector("[data-file-name]");

  function addLineNumbers(code) {
    const lines = code.textContent.replace(/\r\n?/g, "\n").split("\n");
    // A final newline terminates the last line; it does not add a visible row.
    if (lines.length > 1 && lines.at(-1) === "") lines.pop();
    const numbers = document.createElement("span");
    numbers.className = "file-preview-line-numbers";
    numbers.setAttribute("aria-hidden", "true");
    numbers.textContent = Array.from({ length: lines.length }, (_, index) => index + 1).join("\n");
    code.parentElement.classList.add("file-preview-numbered");
    code.before(numbers);
  }

  function rewriteMarkdownLinks() {
    const previewURL = new URL(layout.dataset.previewUrl, window.location.href);
    const filesRoot = new URL("./", previewURL);
    const currentFile = new URL(layout.dataset.filePath.split("/").map(encodeURIComponent).join("/"), filesRoot);
    for (const link of rendered.querySelectorAll("a[href]")) {
      const href = link.getAttribute("href").trim();
      // Keep external URLs, other protocols, and links within this page intact.
      if (!href || /^(?:[a-z][a-z0-9+.-]*:|\/\/|[?#])/i.test(href)) continue;
      try {
        const target = new URL(href, currentFile);
        if (target.origin !== filesRoot.origin || !target.pathname.startsWith(filesRoot.pathname)) continue;
        const destination = new URL(previewURL);
        destination.searchParams.set("path", decodeURIComponent(target.pathname.slice(filesRoot.pathname.length)));
        destination.hash = target.hash;
        link.setAttribute("href", destination.pathname + destination.search + destination.hash);
      } catch {
        // Leave malformed links readable without interrupting the preview.
      }
    }
  }

  addLineNumbers(source);

  function loadLibrary(name, src, integrity) {
    if (window[name]) return Promise.resolve(window[name]);
    return new Promise((resolve, reject) => {
      const script = document.createElement("script");
      const timeout = setTimeout(() => finish(new Error(`${name} loading timed out`)), 15000);
      function finish(error) {
        clearTimeout(timeout);
        script.onload = script.onerror = null;
        if (error) {
          script.remove();
          reject(error);
        } else {
          resolve(window[name]);
        }
      }
      script.src = src;
      script.integrity = integrity;
      script.crossOrigin = "anonymous";
      script.async = true;
      script.onload = () => finish(window[name] ? null : new Error(`${name} unavailable`));
      script.onerror = () => finish(new Error(`${name} could not be loaded`));
      document.head.append(script);
    });
  }

  function highlight(code, language) {
    try {
      if (!language || !window.hljs?.getLanguage(language)) return;
      // Highlight only text, never file-provided markup or automatically guessed prose.
      code.innerHTML = window.hljs.highlight(code.textContent, { language, ignoreIllegals: true }).value;
      code.classList.add("hljs");
    } catch {
      // A highlighter failure leaves the original source readable.
    }
  }

  function selectTab(tab) {
    const showPreview = tab === previewTab;
    rendered.hidden = !showPreview;
    plain.hidden = showPreview;
    for (const item of [previewTab, plainTab]) {
      item.setAttribute("aria-selected", String(item === tab));
      item.tabIndex = item === tab ? 0 : -1;
    }
  }

  async function renderMermaid() {
    const blocks = rendered.querySelectorAll("pre > code.language-mermaid");
    if (!blocks.length) return;
    const notices = Array.from(blocks, (code) => {
      const notice = document.createElement("p");
      notice.className = "mermaid-error";
      notice.textContent = "Loading diagram…";
      code.parentElement.before(notice);
      return notice;
    });
    let ready = false;
    try {
      if (!window.mermaid) {
        await loadLibrary("mermaid", "/static/vendor/mermaid-11.12.0.min.js",
          "sha384-o+g/BxPwhi0C3RK7oQBxQuNimeafQ3GE/ST4iT2BxVI4Wzt60SH4pq9iXVYujjaS");
      }
      window.mermaid.initialize({
        startOnLoad: false,
        securityLevel: "sandbox",
        htmlLabels: false,
        theme: "dark",
        suppressErrorRendering: true,
      });
      ready = Boolean(window.mermaid);
    } catch {
      // Keep the source readable if the renderer cannot initialize.
    }
    // Mermaid uses shared rendering state, so process diagrams one at a time.
    for (const [index, code] of Array.from(blocks).entries()) {
      const pre = code.parentElement;
      try {
        if (!ready) throw new Error("Mermaid unavailable");
        const { svg } = await window.mermaid.render(`file-preview-mermaid-${index}`, code.textContent);
        const diagram = document.createElement("div");
        diagram.className = "mermaid-diagram";
        // Sandbox mode returns an isolated iframe, matching the timeline renderer.
        diagram.innerHTML = svg;
        const frame = diagram.querySelector("iframe");
        if (frame) frame.title = `Mermaid diagram ${index + 1}`;
        pre.replaceWith(diagram);
        notices[index].remove();
      } catch {
        notices[index].textContent = ready
          ? "Unable to render Mermaid diagram. Source shown below."
          : "Diagram renderer could not be loaded. Source shown below. Reload the page to try again.";
      }
    }
  }

  if (rendered) {
    try {
      if (!window.marked || !window.DOMPurify) throw new Error("Markdown libraries could not be loaded. Reload the page to try again.");
      rendered.innerHTML = window.DOMPurify.sanitize(window.marked.parse(rawText, { async: false, gfm: true }), {
        USE_PROFILES: { html: true },
        FORBID_TAGS: ["style", "form", "input", "button", "textarea", "select"],
        FORBID_ATTR: ["style"],
        SANITIZE_NAMED_PROPS: true,
      });
      rewriteMarkdownLinks();
      for (const code of rendered.querySelectorAll("pre > code")) addLineNumbers(code);
      previewTab.disabled = false;
      for (const tab of [previewTab, plainTab]) {
        tab.addEventListener("click", () => selectTab(tab));
        tab.addEventListener("keydown", (event) => {
          if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
          event.preventDefault();
          const target = event.key === "Home" ? previewTab : event.key === "End" ? plainTab
            : tab === previewTab ? plainTab : previewTab;
          selectTab(target);
          target.focus();
        });
      }
      selectTab(previewTab);
      renderStatus.textContent = "";
      void renderMermaid();
    } catch (error) {
      renderStatus.textContent = "Markdown preview is unavailable. Showing plain text. " + (error.message || "");
    }
  }

  function highlightPreview() {
    if (rendered) {
      for (const code of rendered.querySelectorAll("pre code")) {
        const language = Array.from(code.classList).find((name) => name.startsWith("language-"));
        highlight(code, language?.slice(9));
      }
      return;
    }
    const filename = layout.dataset.fileName.toLowerCase();
    const extension = filename.includes(".") ? filename.split(".").pop() : "";
    const aliases = {
      py: "python", pyw: "python", js: "javascript", mjs: "javascript", cjs: "javascript", jsx: "javascript",
      ts: "typescript", tsx: "typescript", yml: "yaml", toml: "ini", htm: "xml", html: "xml", svg: "xml",
      sh: "bash", zsh: "bash", rb: "ruby", rs: "rust", h: "c", cc: "cpp", hpp: "cpp", cs: "csharp",
      kt: "kotlin", kts: "kotlin", pl: "perl", gql: "graphql", jsonc: "json", patch: "diff",
    };
    const language = filename === "makefile" ? "makefile" : Object.hasOwn(aliases, extension) ? aliases[extension] : extension;
    highlight(source, language);
  }

  // Optional renderers must never delay enabling Markdown tabs or Copy raw.
  if (window.hljs) {
    highlightPreview();
  } else {
    void loadLibrary("hljs", "/static/vendor/highlight-11.11.1.min.js",
      "sha384-RH2xi4eIQ/gjtbs9fUXM68sLSi99C7ZWBRX1vDrVv6GQXRibxXLbwO2NGZB74MbU")
      .then(highlightPreview).catch(() => {});
  }

  function fallbackCopy(text) {
    const textarea = document.createElement("textarea");
    textarea.className = "file-preview-copy-source";
    textarea.value = text;
    textarea.setAttribute("readonly", "");
    document.body.append(textarea);
    try {
      textarea.select();
      if (!document.execCommand("copy")) throw new Error("Clipboard unavailable");
    } finally {
      textarea.remove();
      copyButton.focus();
    }
  }

  const copyButton = document.getElementById("file-copy-raw");
  copyButton.disabled = false;
  copyButton.addEventListener("click", async () => {
    if (busy || editing) return;
    setBusy(true);
    status.textContent = "Copying…";
    try {
      // Fetch the original file so truncated previews and CRLF retain their full raw text.
      const response = await fetch(document.getElementById("file-download").href, { credentials: "same-origin", cache: "no-store" });
      if (!response.ok) throw new Error("File could not be read");
      const text = await response.text();
      try {
        if (!navigator.clipboard?.writeText) throw new Error("Clipboard API unavailable");
        await navigator.clipboard.writeText(text);
      } catch {
        fallbackCopy(text);
      }
      status.textContent = "Raw content copied.";
    } catch {
      status.textContent = "Could not copy raw content. Try again or download the file.";
    } finally {
      setBusy(false);
    }
  });

  const editButton = document.getElementById("file-edit");
  const saveButton = document.getElementById("file-save");
  const cancelButton = document.getElementById("file-cancel-edit");
  const editor = document.getElementById("file-editor");
  const truncatedNotice = document.getElementById("file-preview-truncated");
  let editing = false;
  let busy = false;
  let originalText = "";
  let lineEnding = "\n";
  let hasBOM = false;
  let previewWasVisible = false;
  let previewWasEnabled = false;

  function setBusy(value) {
    busy = value;
    editButton.disabled = value;
    saveButton.disabled = value;
    cancelButton.disabled = value;
    editor.readOnly = value;
    copyButton.disabled = value || editing;
  }

  function showEditing(value) {
    editing = value;
    editor.hidden = !value;
    editButton.hidden = value;
    saveButton.hidden = !value;
    cancelButton.hidden = !value;
    plain.hidden = value || previewWasVisible;
    if (rendered) {
      rendered.hidden = value || !previewWasVisible;
      previewTab.disabled = value || !previewWasEnabled;
      plainTab.disabled = value;
    }
    if (truncatedNotice) truncatedNotice.hidden = value;
    setBusy(false);
  }

  editButton.disabled = false;
  editButton.addEventListener("click", async () => {
    if (busy || editing) return;
    setBusy(true);
    status.textContent = "Loading file for editing…";
    try {
      // Always read the entire file, including bytes beyond the preview limit.
      const response = await fetch(document.getElementById("file-download").href, { credentials: "same-origin", cache: "no-store" });
      if (!response.ok) throw new Error("The file could not be read. Try again.");
      const text = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(await response.arrayBuffer());
      if (/[\u0000-\u0008\u000b\u000e-\u001f]/.test(text)) throw new Error("This file contains binary content.");
      hasBOM = text.startsWith("\uFEFF");
      const content = hasBOM ? text.slice(1) : text;
      lineEnding = content.match(/\r\n|\r|\n/)?.[0] || "\n";
      originalText = content.replace(/\r\n?/g, "\n");
      editor.value = originalText;
      previewWasVisible = Boolean(rendered && !rendered.hidden);
      previewWasEnabled = Boolean(previewTab && !previewTab.disabled);
      showEditing(true);
      status.textContent = "Editing. Save to update the file.";
      editor.focus();
    } catch (error) {
      status.textContent = "Could not open file for editing. " + (error.message || "Try again.");
    } finally {
      setBusy(false);
    }
  });

  cancelButton.addEventListener("click", () => {
    if (busy) return;
    showEditing(false);
    editor.value = "";
    status.textContent = "Editing cancelled.";
    editButton.focus();
  });

  saveButton.addEventListener("click", async () => {
    if (busy || !editing) return;
    const text = editor.value.replace(/\r\n?/g, "\n");
    if (text === originalText) {
      showEditing(false);
      status.textContent = "No changes to save.";
      editButton.focus();
      return;
    }
    setBusy(true);
    status.textContent = "Saving…";
    try {
      const response = await fetch(saveButton.dataset.saveUrl, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/octet-stream" },
        body: new TextEncoder().encode((hasBOM ? "\uFEFF" : "") + text.replace(/\n/g, lineEnding)),
      });
      if (!response.ok) {
        const data = await response.json().catch(() => null);
        throw new Error(data?.error?.message || "Try again.");
      }
      editing = false;
      status.textContent = "File saved.";
      window.location.reload();
    } catch (error) {
      status.textContent = "Could not save file. " + (error.message || "Try again.");
      setBusy(false);
    }
  });

  window.addEventListener("beforeunload", (event) => {
    if (!editing || (!busy && editor.value === originalText)) return;
    event.preventDefault();
    event.returnValue = "";
  });
})();
