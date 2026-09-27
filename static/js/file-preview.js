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
    const filename = document.querySelector("[data-file-name]").dataset.fileName.toLowerCase();
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
    copyButton.disabled = true;
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
      copyButton.disabled = false;
    }
  });
})();
