"use strict";

// Own the rendered subtree so message HTML is never interpreted as Alpine code.
document.addEventListener("alpine:init", () => {
  let diagramId = 0;
  let renderQueue = Promise.resolve();

  function rewriteFileLinks(el) {
    const projectKey = el.closest(".timeline-root")?.dataset.projectKey;
    if (!projectKey) return;
    const filesRoot = new URL(`/api/projects/${encodeURIComponent(projectKey)}/files/`, window.location.href);
    for (const link of el.querySelectorAll("a[href]")) {
      const href = link.getAttribute("href").trim();
      // Resolve relative file links from the session's project root.
      if (!href || /^(?:[a-z][a-z0-9+.-]*:|\/\/|[?#])/i.test(href)) continue;
      try {
        const target = new URL(href, filesRoot);
        if (target.origin !== filesRoot.origin || !target.pathname.startsWith(filesRoot.pathname)) continue;
        const destination = new URL("preview", filesRoot);
        destination.searchParams.set("path", decodeURIComponent(target.pathname.slice(filesRoot.pathname.length)));
        destination.hash = target.hash;
        link.setAttribute("href", destination.pathname + destination.search + destination.hash);
      } catch {
        // Leave malformed links readable without interrupting the message.
      }
    }
  }

  window.mermaid?.initialize({
    startOnLoad: false,
    securityLevel: "sandbox",
    theme: "dark",
    suppressErrorRendering: true,
  });

  Alpine.directive("markdown", (el, { expression }, { evaluateLater, effect, cleanup }) => {
    const evaluate = evaluateLater(expression);
    let revision = 0;
    cleanup(() => { revision += 1; });

    effect(() => evaluate((value) => {
      const current = ++revision;
      const { text = "", streaming = false } = value || {};
      Alpine.mutateDom(() => {
        el.innerHTML = window.renderMarkdown(text);
        rewriteFileLinks(el);
      });
      if (streaming || !window.mermaid) return;

      for (const code of el.querySelectorAll("pre > code.language-mermaid")) {
        const source = code.textContent;
        const pre = code.parentElement;
        // Serialize work and discard results from replaced messages or threads.
        renderQueue = renderQueue.then(async () => {
          if (current !== revision || !el.isConnected) return;
          const timeline = el.closest("#timeline");
          try {
            const { svg } = await window.mermaid.render(`timeline-mermaid-${++diagramId}`, source);
            if (current !== revision || !el.isConnected) return;
            const pin = timeline && timeline.clientHeight > 0 &&
              timeline.scrollHeight - timeline.scrollTop - timeline.clientHeight < 120;
            Alpine.mutateDom(() => {
              const diagram = document.createElement("div");
              diagram.className = "mermaid-diagram";
              // Mermaid's sandbox mode returns an isolated iframe, not live SVG.
              diagram.innerHTML = svg;
              const frame = diagram.querySelector("iframe");
              if (frame) frame.title = "Mermaid diagram";
              pre.replaceWith(diagram);
            });
            if (pin) timeline.scrollTop = timeline.scrollHeight;
          } catch {
            if (current !== revision || !el.isConnected) return;
            Alpine.mutateDom(() => {
              const notice = document.createElement("p");
              notice.className = "mermaid-error";
              notice.textContent = "Unable to render Mermaid diagram. Source shown below.";
              pre.before(notice);
            });
          }
        });
      }
    }));
  });
});
