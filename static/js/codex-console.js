"use strict";

window.registerPwaServiceWorker = function registerPwaServiceWorker() {
  if (!("serviceWorker" in navigator)) return Promise.resolve(null);

  if (!window.pwaServiceWorkerRegistration) {
    window.pwaServiceWorkerRegistration = navigator.serviceWorker
      .register("/service-worker.js")
      .catch((error) => {
        window.pwaServiceWorkerRegistration = null;
        throw error;
      });
  }
  return window.pwaServiceWorkerRegistration;
};

if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    window.registerPwaServiceWorker().catch((error) => {
      console.warn("Service worker registration failed.", error);
    });
  });
}

window.pwaDiagnostics = function pwaDiagnostics() {
  const requested = new URLSearchParams(window.location.search).get("pwa-debug") === "1";
  if (requested) window.localStorage.setItem("pwa-debug", "1");

  return {
    visible: requested || window.localStorage.getItem("pwa-debug") === "1",
    url: window.location.href,
    secureContext: window.isSecureContext,
    serviceWorkerSupported: "serviceWorker" in navigator,
    serviceWorkerReady: false,
    serviceWorkerScope: "",
    displayMode: "browser",
    error: "",

    init() {
      this.refreshDisplayMode();
      if (!this.visible || !this.serviceWorkerSupported) return;
      this.refreshServiceWorker();
    },

    close() {
      this.visible = false;
      window.localStorage.removeItem("pwa-debug");
    },

    refreshDisplayMode() {
      if (window.matchMedia("(display-mode: fullscreen)").matches) {
        this.displayMode = "fullscreen";
      } else if (window.matchMedia("(display-mode: standalone)").matches) {
        this.displayMode = "standalone";
      } else {
        this.displayMode = "browser";
      }
    },

    async refreshServiceWorker() {
      this.secureContext = window.isSecureContext;
      this.serviceWorkerSupported = "serviceWorker" in navigator;
      this.refreshDisplayMode();
      this.error = "";
      if (!this.serviceWorkerSupported) return;

      try {
        const registration = await window.registerPwaServiceWorker();
        this.serviceWorkerReady = Boolean(registration);
        this.serviceWorkerScope = registration?.scope || "";
      } catch (error) {
        this.serviceWorkerReady = false;
        this.serviceWorkerScope = "";
        this.error = String(error?.message || error);
      }
    },
  };
};

window.renderMarkdown = function renderMarkdown(source) {
  const markdown = String(source || "");
  if (!window.marked || !window.DOMPurify) {
    return markdown
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;")
      .replaceAll("\n", "<br>");
  }

  const html = window.marked.parse(markdown, {
    async: false,
    breaks: true,
    gfm: true,
  });
  return window.DOMPurify.sanitize(html, {
    FORBID_ATTR: ["style"],
    FORBID_TAGS: ["style"],
    USE_PROFILES: { html: true },
  });
};

window.codexConsole = function codexConsole() {
  return {
    projectKey: "",
    threadId: "",
    appVersion: "",
    model: "",
    reasoningEffort: "",
    modelSettingsOpen: false,
    activeModel: null,
    activeReasoningEffort: null,
    activeKind: null,
    sessionStatus: { type: "idle", activeFlags: [] },
    models: [],
    archived: false,
    prompt: "",
    active: false,
    runningThreadIds: [],
    busy: false,
    streamReady: false,
    eventSource: null,
    lastEventSequences: Object.create(null),
    liveEvents: [],
    liveTimelineItems: [],
    livePlans: [],
    liveDiff: "",
    liveDiffSequence: null,
    liveDiffSource: null,
    liveUsage: null,
    liveUsageSequence: null,
    liveUsageSource: null,
    liveGoal: null,
    goalEditorOpen: false,
    goalObjective: "",
    goalTokenBudget: "",
    connectionState: "disconnected",
    errorMessage: "",
    mobileTab: "chat",
    conversationTab: "timeline",
    collapsibleToolCardCount: 0,
    allToolCardsExpanded: false,
    eventCounter: 0,
    pendingLiveEvents: [],
    liveFlushScheduled: false,
    pendingAgentMessageDeltas: [],
    agentMessageFlushFrame: null,
    pinTimelineAfterAgentFlush: false,
    liveResponseSegment: 0,
    composerResizeObserver: null,
    filesProjectKey: "",
    fileDirectories: {},
    fileExpandedPaths: [],
    fileLoadingPaths: [],
    fileCurrentPath: "",
    fileSelectedPath: "",
    fileTreeLoading: false,
    fileOperationBusy: false,
    fileOperationLabel: "",
    fileError: "",

    get connectionLabel() {
      return {
        connected: "Live stream connected",
        reconnecting: "Live stream reconnecting",
        disconnected: "No session selected",
      }[this.connectionState] || "Live stream error";
    },

    get currentModelId() {
      const requestedModel = this.active ? this.activeModel : this.model;
      if (requestedModel) return requestedModel;
      const defaultModel = this.models.find(
        (item) => item.is_default || item.isDefault,
      );
      return defaultModel?.model || defaultModel?.id || "";
    },

    get selectedModelLabel() {
      const selected = this.modelDetails(this.model);
      return selected?.display_name || selected?.displayName ||
        selected?.model || selected?.id || "Default model";
    },

    get reasoningEffortOptions() {
      const model = this.modelDetails(this.model);
      const options =
        model?.supported_reasoning_efforts ||
        model?.supportedReasoningEfforts ||
        [];
      return options
        .map((option) => {
          if (typeof option === "string") {
            return { value: option, description: "" };
          }
          return {
            value: option?.reasoning_effort || option?.reasoningEffort || "",
            description: option?.description || "",
          };
        })
        .filter((option) => option.value);
    },

    get defaultReasoningEffort() {
      const model = this.modelDetails(this.model);
      return model?.default_reasoning_effort || model?.defaultReasoningEffort || "";
    },

    get defaultReasoningEffortLabel() {
      const effort = this.defaultReasoningEffort;
      return effort
        ? `Default (${this.reasoningEffortLabel(effort)})`
        : "Model default";
    },

    get selectedReasoningEffortLabel() {
      const effort = this.reasoningEffort || this.defaultReasoningEffort;
      return effort ? this.reasoningEffortLabel(effort) : "Default";
    },

    get currentReasoningEffortLabel() {
      const requestedEffort = this.active
        ? this.activeReasoningEffort
        : this.reasoningEffort;
      if (requestedEffort) return this.reasoningEffortLabel(requestedEffort);
      const requestedModel = this.active ? this.activeModel : this.model;
      const model = this.modelDetails(requestedModel);
      const defaultEffort =
        model?.default_reasoning_effort || model?.defaultReasoningEffort || "";
      return defaultEffort
        ? `${this.reasoningEffortLabel(defaultEffort)} (default)`
        : "model default";
    },

    get reasoningEffortHint() {
      const selected = this.reasoningEffortOptions.find(
        (option) => option.value === this.reasoningEffort,
      );
      if (selected?.description) return selected.description;
      return ["max", "ultra"].includes(this.reasoningEffort)
        ? "Max and Ultra consume usage limits faster."
        : "Reasoning effort for the next turn.";
    },

    get sessionStatusLabel() {
      const waitingLabels = [];
      if (this.sessionStatus.activeFlags.includes("waitingOnApproval")) {
        waitingLabels.push("waiting for approval");
      }
      if (this.sessionStatus.activeFlags.includes("waitingOnUserInput")) {
        waitingLabels.push("waiting for input");
      }
      if (waitingLabels.length) return waitingLabels.join(" · ");

      const labels = {
        active: "running",
        error: "error",
        idle: "idle",
        notLoaded: "not loaded",
        running: "running",
        starting: "starting",
        stopping: "stopping",
        systemError: "system error",
      };
      const type = this.sessionStatus.type || "idle";
      return labels[type] || type
        .replace(/([a-z])([A-Z])/g, "$1 $2")
        .replaceAll("_", " ")
        .toLowerCase();
    },

    get goalStatusKey() {
      return String(this.liveGoal?.status || "unknown")
        .replace(/([a-z])([A-Z])/g, "$1-$2")
        .replaceAll("_", "-")
        .toLowerCase();
    },

    get goalStatusLabel() {
      const labels = {
        active: "Active",
        paused: "Paused",
        blocked: "Blocked",
        usageLimited: "Usage limited",
        budgetLimited: "Budget limited",
        complete: "Complete",
      };
      return labels[this.liveGoal?.status] || this.goalStatusKey.replaceAll("-", " ");
    },

    get goalCanResume() {
      return ["paused", "blocked", "usageLimited", "budgetLimited"]
        .includes(this.liveGoal?.status);
    },

    get goalBudgetPercent() {
      const budget = this.tokenNumber(this.liveGoal?.tokenBudget);
      if (!budget) return 0;
      return Math.min(100, (this.tokenNumber(this.liveGoal?.tokensUsed) / budget) * 100);
    },

    get visibleFileEntries() {
      const visible = [];
      const appendDirectory = (path, depth) => {
        const entries = this.fileDirectories[path] || [];
        for (const entry of entries) {
          visible.push({ ...entry, depth });
          if (
            entry.type === "directory" &&
            this.fileExpandedPaths.includes(entry.path)
          ) {
            appendDirectory(entry.path, depth + 1);
          }
        }
      };
      appendDirectory("", 0);
      return visible;
    },

    get selectedFileEntry() {
      return this.fileEntryForPath(this.fileSelectedPath);
    },

    async init() {
      try {
        const [projects, preferences, status] = await Promise.all([
          this.api("/api/projects"),
          this.api("/api/preferences"),
          this.api("/api/status"),
          this.loadModels(),
        ]);
        this.appVersion = String(status.version || "");
        const available = projects.data || [];
        const preferred = preferences.selected_project_key;
        this.projectKey = available.some((item) => item.key === preferred)
          ? preferred
          : "";
        if (this.projectKey) {
          await this.refreshThreads();
        }
        if (this.projectKey && preferences.selected_thread_id) {
          await this.restoreThread(preferences.selected_thread_id);
        } else if (preferred || preferences.selected_thread_id) {
          await this.savePreferences({
            selected_project_key: null,
            selected_thread_id: null,
          }).catch(() => {});
        }
      } catch (error) {
        this.showError(error);
      }
    },

    async api(url, options = {}) {
      const response = await fetch(url, {
        headers: { "Content-Type": "application/json", ...(options.headers || {}) },
        ...options,
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        const error = new Error(
          payload?.error?.message || `Request failed (HTTP ${response.status})`,
        );
        error.code = payload?.error?.code || "request_failed";
        error.status = response.status;
        throw error;
      }
      return payload;
    },

    async loadModels() {
      try {
        const payload = await this.api("/api/codex/models");
        this.models = payload.data || [];
        this.normalizeReasoningEffort();
      } catch (error) {
        this.models = [];
        if (!String(error.message).includes("unavailable")) {
          throw error;
        }
      }
    },

    modelDetails(modelId = "") {
      const requested = String(modelId || "");
      if (requested) {
        return this.models.find(
          (item) => (item.model || item.id) === requested,
        ) || null;
      }
      return this.models.find((item) => item.is_default || item.isDefault) || null;
    },

    normalizeReasoningEffort() {
      if (!this.reasoningEffort) return;
      const supported = this.reasoningEffortOptions.some(
        (option) => option.value === this.reasoningEffort,
      );
      if (!supported) this.reasoningEffort = "";
    },

    reasoningEffortLabel(value) {
      const normalized = String(value || "");
      const labels = {
        none: "None",
        minimal: "Minimal",
        low: "Low",
        medium: "Medium",
        high: "High",
        xhigh: "Extra high",
        max: "Max",
        ultra: "Ultra",
      };
      return labels[normalized] || normalized
        .replaceAll("_", " ")
        .replace(/^./, (character) => character.toUpperCase());
    },

    reasoningEffortOptionLabel(option) {
      const label = this.reasoningEffortLabel(option.value);
      return ["max", "ultra"].includes(option.value)
        ? `${label} · higher usage`
        : label;
    },

    async savePreferences(values) {
      await this.api("/api/preferences", {
        method: "PATCH",
        body: JSON.stringify(values),
      });
    },

    async selectProject(projectKey) {
      if (this.projectKey === projectKey) return;
      if (this.fileOperationBusy) {
        this.showFileError(new Error("Wait for the current file operation to finish."));
        return;
      }
      this.projectKey = projectKey;
      this.resetProjectFiles(projectKey);
      this.threadId = "";
      this.mobileTab = "sessions";
      this.closeEvents();
      this.clearThreadPanels();
      await this.savePreferences({
        selected_project_key: projectKey,
        selected_thread_id: null,
      }).catch(() => {});
      await this.refreshThreads();
    },

    async refreshProjects() {
      await htmx.ajax("GET", "/partials/projects", {
        target: "#project-selector",
        swap: "innerHTML",
      });
    },

    async refreshThreads() {
      if (!this.projectKey) return;
      const query = new URLSearchParams({
        project_key: this.projectKey,
        archived: String(this.archived),
      });
      query.set("cache_bust", String(Date.now()));
      await htmx.ajax("GET", `/partials/threads?${query}`, {
        target: "#thread-list",
        swap: "innerHTML",
      });
    },

    resetProjectFiles(projectKey = "") {
      this.filesProjectKey = projectKey;
      this.fileDirectories = {};
      this.fileExpandedPaths = [];
      this.fileLoadingPaths = [];
      this.fileCurrentPath = "";
      this.fileSelectedPath = "";
      this.fileTreeLoading = false;
      this.fileOperationBusy = false;
      this.fileOperationLabel = "";
      this.fileError = "";
    },

    async openFilesTab() {
      this.conversationTab = "files";
      if (!this.projectKey) {
        this.fileError = "Choose a project first.";
        return;
      }
      try {
        await this.ensureProjectFiles();
      } catch (error) {
        this.showFileError(error);
      }
    },

    async ensureProjectFiles() {
      if (!this.projectKey) return;
      if (this.filesProjectKey !== this.projectKey) {
        this.resetProjectFiles(this.projectKey);
      }
      if (!Object.hasOwn(this.fileDirectories, "")) {
        await this.refreshProjectFiles();
      }
    },

    projectFilesUrl(suffix = "", query = {}) {
      const project = encodeURIComponent(this.projectKey);
      const parameters = new URLSearchParams();
      for (const [key, value] of Object.entries(query)) {
        parameters.set(key, String(value));
      }
      const search = parameters.size ? `?${parameters}` : "";
      return `/api/projects/${project}/files${suffix}${search}`;
    },

    async loadFileDirectory(path) {
      const projectKey = this.projectKey;
      if (!projectKey || this.fileLoadingPaths.includes(path)) return false;
      this.fileLoadingPaths = [...this.fileLoadingPaths, path];
      try {
        const payload = await this.api(this.projectFilesUrl("", { path }));
        if (this.projectKey !== projectKey) return false;
        this.fileDirectories = {
          ...this.fileDirectories,
          [path]: payload.data || [],
        };
        return true;
      } finally {
        this.fileLoadingPaths = this.fileLoadingPaths.filter(
          (item) => item !== path,
        );
      }
    },

    async refreshProjectFiles(options = {}) {
      if (
        !this.projectKey ||
        (this.fileOperationBusy && !options.allowDuringOperation)
      ) return;
      const projectKey = this.projectKey;
      const requestedExpanded = [
        ...(options.expandedPaths || this.fileExpandedPaths),
      ].sort((left, right) => left.split("/").length - right.split("/").length);
      const requestedCurrent = options.currentPath ?? this.fileCurrentPath;
      const requestedSelected = options.selectedPath ?? this.fileSelectedPath;
      this.fileError = "";
      this.fileTreeLoading = true;
      this.fileDirectories = {};
      this.fileExpandedPaths = [];
      try {
        await this.loadFileDirectory("");
        const pathsToRestore = [
          ...new Set([
            ...requestedExpanded,
            requestedCurrent,
            this.fileParentPath(requestedSelected),
          ].filter(Boolean)),
        ];
        for (const path of pathsToRestore) {
          let current = "";
          for (const segment of path.split("/")) {
            current = this.fileJoinedPath(current, segment);
            if (
              this.projectKey !== projectKey ||
              !this.filePathIsListedDirectory(current)
            ) break;
            if (!Object.hasOwn(this.fileDirectories, current)) {
              await this.loadFileDirectory(current);
            }
          }
        }
        this.fileExpandedPaths = requestedExpanded.filter(
          (path) => Object.hasOwn(this.fileDirectories, path),
        );
        this.fileCurrentPath = this.filePathIsListedDirectory(requestedCurrent)
          ? requestedCurrent
          : "";
        this.fileSelectedPath = this.fileEntryForPath(requestedSelected)
          ? requestedSelected
          : "";
      } catch (error) {
        this.showFileError(error);
      } finally {
        this.fileTreeLoading = false;
      }
    },

    fileParentPath(path) {
      const separator = path.lastIndexOf("/");
      return separator < 0 ? "" : path.slice(0, separator);
    },

    fileJoinedPath(parent, name) {
      return parent ? `${parent}/${name}` : name;
    },

    filePathIsListedDirectory(path) {
      if (!path) return Object.hasOwn(this.fileDirectories, "");
      const parent = this.fileParentPath(path);
      return (this.fileDirectories[parent] || []).some(
        (entry) => entry.path === path && entry.type === "directory",
      );
    },

    fileEntryForPath(path) {
      if (!path) return null;
      for (const entries of Object.values(this.fileDirectories)) {
        const match = entries.find((entry) => entry.path === path);
        if (match) return match;
      }
      return null;
    },

    selectProjectFile(entry) {
      this.fileSelectedPath = entry?.path || "";
    },

    isFileFolderExpanded(path) {
      return this.fileExpandedPaths.includes(path);
    },

    async selectAndToggleFileFolder(entry) {
      if (entry.type !== "directory") return;
      if (this.fileLoadingPaths.includes(entry.path)) return;
      this.selectProjectFile(entry);
      this.fileError = "";
      if (this.isFileFolderExpanded(entry.path)) {
        this.fileCurrentPath = entry.path;
        this.fileExpandedPaths = this.fileExpandedPaths.filter(
          (path) => path !== entry.path,
        );
        return;
      }
      try {
        if (!Object.hasOwn(this.fileDirectories, entry.path)) {
          await this.loadFileDirectory(entry.path);
        }
        this.fileCurrentPath = entry.path;
        this.fileExpandedPaths = [...this.fileExpandedPaths, entry.path];
      } catch (error) {
        this.showFileError(error);
      }
    },

    selectFileRoot() {
      this.fileCurrentPath = "";
    },

    validateProjectFileName(value) {
      const name = String(value || "").trim();
      if (!name) return "A file or folder name is required.";
      if (
        name === "." ||
        name === ".." ||
        name.includes("/") ||
        name.includes("\\") ||
        [...name].some((character) => {
          const code = character.codePointAt(0);
          return code < 32 || code === 127;
        }) ||
        new TextEncoder().encode(name).length > 255
      ) {
        return "Use one name without slashes, control characters, or more than 255 bytes.";
      }
      return "";
    },

    async newProjectFolder() {
      if (!this.projectKey || this.fileOperationBusy) return;
      const requestedName = window.prompt("New folder name");
      if (requestedName === null) return;
      const name = requestedName.trim();
      const validationError = this.validateProjectFileName(name);
      if (validationError) {
        this.showFileError(new Error(validationError));
        return;
      }
      const parent = this.fileCurrentPath;
      this.fileOperationBusy = true;
      this.fileOperationLabel = `Creating ${name}…`;
      this.fileError = "";
      try {
        await this.api(this.projectFilesUrl("/directories"), {
          method: "POST",
          body: JSON.stringify({ path: parent, name }),
        });
        await this.loadFileDirectory(parent);
        this.fileOperationLabel = `Created folder ${name}.`;
      } catch (error) {
        this.fileOperationLabel = "Folder creation failed.";
        this.showFileError(error);
      } finally {
        this.fileOperationBusy = false;
      }
    },

    async uploadProjectFiles(event) {
      const input = event.currentTarget;
      const files = [...(input.files || [])];
      input.value = "";
      if (!files.length || !this.projectKey || this.fileOperationBusy) return;
      const parent = this.fileCurrentPath;
      this.fileOperationBusy = true;
      this.fileError = "";
      let completed = 0;
      try {
        for (const file of files) {
          const validationError = this.validateProjectFileName(file.name);
          if (validationError) throw new Error(`${file.name}: ${validationError}`);
          this.fileOperationLabel = `Uploading ${file.name}…`;
          const upload = async (overwrite) => this.api(
            this.projectFilesUrl("/upload", {
              path: parent,
              name: file.name,
              overwrite,
            }),
            {
              method: "POST",
              headers: { "Content-Type": file.type || "application/octet-stream" },
              body: file,
            },
          );
          try {
            await upload(false);
          } catch (error) {
            if (
              error.code !== "file_exists" ||
              !window.confirm(`${file.name} already exists. Replace it?`)
            ) {
              if (error.code === "file_exists") continue;
              throw error;
            }
            await upload(true);
          }
          completed += 1;
        }
        this.fileOperationLabel = `${completed} file${completed === 1 ? "" : "s"} uploaded.`;
      } catch (error) {
        this.fileOperationLabel = "Upload failed.";
        this.showFileError(error);
      } finally {
        try {
          await this.loadFileDirectory(parent);
        } catch (error) {
          this.showFileError(error);
        }
        this.fileOperationBusy = false;
      }
    },

    async downloadProjectFile(entry = this.selectedFileEntry) {
      if (
        !entry ||
        entry.type !== "file" ||
        !this.projectKey ||
        this.fileOperationBusy
      ) return;
      this.fileOperationBusy = true;
      this.fileOperationLabel = `Downloading ${entry.name}…`;
      this.fileError = "";
      try {
        const response = await fetch(
          this.projectFilesUrl("/download", { path: entry.path }),
        );
        if (!response.ok) {
          const payload = await response.json().catch(() => ({}));
          const error = new Error(
            payload?.error?.message ||
            `Download failed (HTTP ${response.status}).`,
          );
          error.code = payload?.error?.code || "download_failed";
          error.status = response.status;
          throw error;
        }
        const objectUrl = URL.createObjectURL(await response.blob());
        const link = document.createElement("a");
        link.href = objectUrl;
        link.download = entry.name;
        document.body.append(link);
        link.click();
        link.remove();
        URL.revokeObjectURL(objectUrl);
        this.fileOperationLabel = `Downloaded ${entry.name}.`;
      } catch (error) {
        this.fileOperationLabel = "Download failed.";
        this.showFileError(error);
      } finally {
        this.fileOperationBusy = false;
      }
    },

    remapFilePath(path, oldPath, newPath) {
      if (path === oldPath) return newPath;
      return path.startsWith(`${oldPath}/`)
        ? `${newPath}${path.slice(oldPath.length)}`
        : path;
    },

    async renameProjectFile(entry) {
      if (!entry || !this.projectKey || this.fileOperationBusy) return;
      const requestedName = window.prompt("New name", entry.name);
      if (requestedName === null) return;
      const name = requestedName.trim();
      const validationError = this.validateProjectFileName(name);
      if (validationError) {
        this.showFileError(new Error(validationError));
        return;
      }
      if (name === entry.name) return;
      const newPath = this.fileJoinedPath(this.fileParentPath(entry.path), name);
      const expandedPaths = this.fileExpandedPaths.map((path) =>
        this.remapFilePath(path, entry.path, newPath)
      );
      const currentPath = this.remapFilePath(
        this.fileCurrentPath,
        entry.path,
        newPath,
      );
      const selectedPath = this.remapFilePath(
        this.fileSelectedPath,
        entry.path,
        newPath,
      );
      this.fileOperationBusy = true;
      this.fileOperationLabel = `Renaming ${entry.name}…`;
      this.fileError = "";
      try {
        await this.api(this.projectFilesUrl(), {
          method: "PATCH",
          body: JSON.stringify({ path: entry.path, name }),
        });
        await this.refreshProjectFiles({
          expandedPaths,
          currentPath,
          selectedPath,
          allowDuringOperation: true,
        });
        this.fileOperationLabel = `Renamed ${entry.name} to ${name}.`;
      } catch (error) {
        this.fileOperationLabel = "Rename failed.";
        this.showFileError(error);
      } finally {
        this.fileOperationBusy = false;
      }
    },

    async deleteProjectFile(entry) {
      if (!entry || !this.projectKey || this.fileOperationBusy) return;
      const detail = entry.type === "directory"
        ? " and everything inside it"
        : "";
      if (!window.confirm(`Delete ${entry.name}${detail}? This cannot be undone.`)) {
        return;
      }
      const expandedPaths = this.fileExpandedPaths.filter(
        (path) => path !== entry.path && !path.startsWith(`${entry.path}/`),
      );
      const currentPath = (
        this.fileCurrentPath === entry.path ||
        this.fileCurrentPath.startsWith(`${entry.path}/`)
      ) ? this.fileParentPath(entry.path) : this.fileCurrentPath;
      this.fileOperationBusy = true;
      this.fileOperationLabel = `Deleting ${entry.name}…`;
      this.fileError = "";
      try {
        await this.api(this.projectFilesUrl("", { path: entry.path }), {
          method: "DELETE",
        });
        await this.refreshProjectFiles({
          expandedPaths,
          currentPath,
          selectedPath: "",
          allowDuringOperation: true,
        });
        this.fileOperationLabel = `Deleted ${entry.name}.`;
      } catch (error) {
        this.fileOperationLabel = "Delete failed.";
        this.showFileError(error);
      } finally {
        this.fileOperationBusy = false;
      }
    },

    showFileError(error) {
      this.fileError = error?.message || "The file operation failed.";
      this.showError(error);
    },

    async restoreThread(threadId) {
      try {
        const thread = await this.api(
          `/api/codex/threads/${encodeURIComponent(threadId)}`,
        );
        if (thread.project_key !== this.projectKey) {
          throw new Error("The saved session belongs to another project.");
        }
        await this.selectThread(threadId);
      } catch (_error) {
        this.threadId = "";
        this.closeEvents();
        this.clearThreadPanels();
        await this.savePreferences({ selected_thread_id: null }).catch(() => {});
      }
    },

    async selectThread(threadId) {
      this.modelSettingsOpen = false;
      this.errorMessage = "";
      this.threadId = threadId;
      this.mobileTab = "chat";
      this.conversationTab = "timeline";
      this.liveEvents = [];
      this.pendingLiveEvents = [];
      this.resetLiveTimeline();
      this.livePlans = [];
      this.liveDiff = "";
      this.liveDiffSequence = null;
      this.liveDiffSource = null;
      this.liveUsage = null;
      this.liveUsageSequence = null;
      this.liveUsageSource = null;
      this.liveGoal = null;
      this.goalEditorOpen = false;
      this.goalObjective = "";
      this.goalTokenBudget = "";
      this.activeModel = null;
      this.activeReasoningEffort = null;
      this.activeKind = null;
      this.active = this.isRunning(threadId);
      this.sessionStatus = this.normalizeSessionStatus(
        this.active ? "running" : "idle",
      );
      this.streamReady = false;
      await this.savePreferences({ selected_thread_id: threadId }).catch(() => {});
      await this.refreshThreadAndList();
      if (this.threadId === threadId) this.connectEvents(threadId);
    },

    async refreshThread() {
      if (!this.threadId) return;
      const encoded = encodeURIComponent(this.threadId);
      await htmx.ajax("GET", `/partials/threads/${encoded}/timeline`, {
        target: "#timeline",
        swap: "innerHTML",
      });
      this.convergeTimelineSnapshot();
      this.syncToolCardToggleState();
      this.scheduleToolCardToggleStateSync();
      await htmx.ajax("GET", `/partials/threads/${encoded}/inspector`, {
        target: "#inspector-content",
        swap: "innerHTML",
      });
      await htmx.ajax("GET", `/partials/threads/${encoded}/changes`, {
        target: "#latest-changes",
        swap: "innerHTML",
      });
      await htmx.ajax("GET", `/partials/threads/${encoded}/composer`, {
        target: "#composer",
        swap: "innerHTML",
      });
    },

    convergeTimelineSnapshot() {
      if (!this.threadId) return;
      const snapshot = document.querySelector(
        `#timeline [data-thread-id="${CSS.escape(this.threadId)}"]`,
      );
      if (!snapshot) return;
      const cursor = Number(snapshot.dataset.journalCursor);
      if (!Number.isSafeInteger(cursor) || cursor < 0) return;
      this.rememberEventSequence(this.threadId, cursor);
      this.liveTimelineItems = this.liveTimelineItems.filter((item) => {
        const sequence = Number(item.sequence);
        return !Number.isSafeInteger(sequence) || sequence > cursor;
      });
      this.pendingAgentMessageDeltas = this.pendingAgentMessageDeltas.filter(
        (segment) => {
          const sequence = Number(segment.sequence);
          return !Number.isSafeInteger(sequence) || sequence > cursor;
        },
      );
      this.livePlans = this.livePlans.filter((plan) => {
        const sequence = Number(plan.sequence);
        return !Number.isSafeInteger(sequence) || sequence > cursor;
      });
      if (
        Number.isSafeInteger(Number(this.liveDiffSequence)) &&
        Number(this.liveDiffSequence) <= cursor
      ) {
        this.liveDiff = "";
        this.liveDiffSequence = null;
        this.liveDiffSource = null;
      }
      if (
        Number.isSafeInteger(Number(this.liveUsageSequence)) &&
        Number(this.liveUsageSequence) <= cursor
      ) {
        this.liveUsage = null;
        this.liveUsageSequence = null;
        this.liveUsageSource = null;
      }
    },

    async refreshThreadAndList() {
      await this.refreshThread();
      await this.refreshThreads();
    },

    async newProject() {
      if (this.fileOperationBusy) {
        this.showFileError(new Error("Wait for the current file operation to finish."));
        return;
      }
      const requestedName = window.prompt("Project directory name");
      if (requestedName === null) return;
      const name = requestedName.trim();
      if (!name) return;
      try {
        this.busy = true;
        const project = await this.api("/api/projects", {
          method: "POST",
          body: JSON.stringify({ name }),
        });
        await this.refreshProjects();
        await this.selectProject(project.key);
        await this.createThread(null);
      } catch (error) {
        this.showError(error);
      } finally {
        this.busy = false;
      }
    },

    async newThread() {
      if (!this.projectKey) {
        this.errorMessage = "Choose a project first.";
        return;
      }
      const requestedName = window.prompt("Session name (optional)");
      if (requestedName === null) return;
      await this.createThread(requestedName.trim() || null);
    },

    async createThread(name) {
      try {
        this.busy = true;
        const thread = await this.api("/api/codex/threads", {
          method: "POST",
          body: JSON.stringify({
            project_key: this.projectKey,
            name,
            model: this.model || null,
          }),
        });
        await this.refreshThreads();
        await this.selectThread(thread.id);
      } catch (error) {
        this.showError(error);
      } finally {
        this.busy = false;
      }
    },

    connectEvents(threadId) {
      this.closeEvents();
      this.connectionState = "reconnecting";
      const encoded = encodeURIComponent(threadId);
      const params = new URLSearchParams();
      const afterSequence = this.lastEventSequence(threadId);
      if (afterSequence !== null) {
        params.set("after_sequence", String(afterSequence));
      }
      const encodedParams = params.toString();
      const query = encodedParams ? `?${encodedParams}` : "";
      const source = new EventSource(
        `/api/codex/threads/${encoded}/events${query}`,
      );
      this.eventSource = source;
      source.onopen = () => {
        if (this.threadId === threadId) this.connectionState = "connected";
      };
      source.onerror = () => {
        if (this.threadId === threadId) {
          this.connectionState = "reconnecting";
          this.streamReady = false;
        }
      };
      source.onmessage = (message) => {
        if (this.threadId !== threadId) return;
        try {
          const event = JSON.parse(message.data);
          if (event.thread_id !== threadId) return;
          this.handleEvent(event);
          this.rememberEventSequence(
            threadId,
            event.sequence,
            event.type === "console.stream.resync_required",
          );
        } catch (_error) {
          this.errorMessage = "A live event could not be decoded; refreshing history.";
          this.refreshThread().catch((error) => this.showError(error));
        }
      };
    },

    lastEventSequence(threadId) {
      if (!Object.hasOwn(this.lastEventSequences, threadId)) return null;
      const sequence = Number(this.lastEventSequences[threadId]);
      return Number.isSafeInteger(sequence) && sequence >= 0 ? sequence : null;
    },

    rememberEventSequence(threadId, value, replace = false) {
      const sequence = Number(value);
      if (!Number.isSafeInteger(sequence) || sequence < 0) return;
      const current = this.lastEventSequence(threadId);
      if (replace || current === null || sequence > current) {
        this.lastEventSequences[threadId] = sequence;
      }
    },

    forgetEventSequence(threadId) {
      delete this.lastEventSequences[threadId];
    },

    closeEvents() {
      if (this.eventSource) this.eventSource.close();
      this.eventSource = null;
      this.streamReady = false;
      this.connectionState = "disconnected";
    },

    handleEvent(event) {
      if (event.type === "console.stream.ready") {
        this.streamReady = true;
        return;
      }
      if (event.type === "console.stream.resync_required") {
        this.liveEvents = [];
        this.resetLiveTimeline();
        this.livePlans = [];
        this.liveDiff = "";
        this.liveDiffSequence = null;
        this.liveDiffSource = null;
        this.liveUsage = null;
        this.liveUsageSequence = null;
        this.liveUsageSource = null;
        this.liveGoal = null;
        this.refreshThread().catch((error) => this.showError(error));
        return;
      }
      if (event.method === "thread/goal/updated") {
        const goalThreadId =
          event.data?.thread_id || event.data?.threadId || event.thread_id;
        this.syncGoalSnapshot(goalThreadId, event.data?.goal);
      }
      if (event.method === "thread/goal/cleared") {
        const goalThreadId =
          event.data?.thread_id || event.data?.threadId || event.thread_id;
        this.syncGoalSnapshot(goalThreadId, null);
      }
      if (event.method === "turn/plan/updated") {
        this.recordPlanUpdate(event);
      }
      if (event.method === "item/agentMessage/delta") {
        this.queueAgentMessageDelta(event);
      }
      if (event.method === "item/completed") {
        this.recordCompletedAgentMessage(event);
        this.recordCompletedToolItem(event);
      }
      if (["turn/completed", "turn/error", "turn/interrupted"].includes(event.method)) {
        this.flushQueuedAgentMessages();
        this.finishStreamingAgentMessages(event.turn_id);
      }
      if (event.method === "turn/diff/updated") {
        this.liveDiff = event.data?.diff || "";
        this.liveDiffSequence = Number(event.sequence);
        this.liveDiffSource = "live";
      }
      if ((event.method || "").toLowerCase().includes("usage")) {
        this.liveUsage = this.normalizeUsage(event.data);
        this.liveUsageSequence = Number(event.sequence);
        this.liveUsageSource = "live";
      }
      if (event.method === "thread/status/changed") {
        const statusThreadId =
          event.data?.thread_id || event.data?.threadId || event.thread_id;
        this.syncSessionStatus(statusThreadId, event.data?.status);
        const sdkStatus = this.normalizeSessionStatus(event.data?.status);
        if (statusThreadId === this.threadId) {
          const continuingGoal =
            this.activeKind === "goal" && this.liveGoal?.status === "active";
          const running = sdkStatus.type === "active" || continuingGoal;
          this.active = running;
          this.markRunning(statusThreadId, running);
          if (continuingGoal && sdkStatus.type !== "active") {
            this.syncSessionStatus(statusThreadId, "running");
          }
          if (!running) {
            this.activeModel = null;
            this.activeReasoningEffort = null;
            this.busy = false;
          }
        }
      }
      if (event.type === "console.turn.starting") {
        this.active = true;
        this.activeKind = "turn";
        this.syncSessionStatus(event.thread_id, "starting");
        if (Object.hasOwn(event.data || {}, "model")) {
          this.activeModel = event.data.model || "";
        }
        if (Object.hasOwn(event.data || {}, "reasoning_effort")) {
          this.activeReasoningEffort = event.data.reasoning_effort || "";
        }
        this.markRunning(event.thread_id, true);
      }
      if (event.type === "console.turn.running") {
        this.active = true;
        this.activeKind = "turn";
        this.bindPendingUserMessagesToTurn(event.turn_id);
        this.syncSessionStatus(event.thread_id, "running");
        if (Object.hasOwn(event.data || {}, "model")) {
          this.activeModel = event.data.model || "";
        }
        if (Object.hasOwn(event.data || {}, "reasoning_effort")) {
          this.activeReasoningEffort = event.data.reasoning_effort || "";
        }
        this.markRunning(event.thread_id, true);
      }
      if (event.type === "console.turn.stopping") {
        this.syncSessionStatus(event.thread_id, "stopping");
      }
      if (event.type === "console.turn.idle") {
        this.active = false;
        this.activeKind = null;
        this.activeModel = null;
        this.activeReasoningEffort = null;
        this.syncSessionStatus(event.thread_id, "idle");
        this.markRunning(event.thread_id, false);
        this.busy = false;
        const completedTurnId = event.turn_id;
        this.bindPendingUserMessagesToTurn(completedTurnId);
        this.finishStreamingAgentMessages(completedTurnId);
        this.collapseToolCards();
        this.refreshThreadAndList()
          .then(() => {
            if (this.threadId === event.thread_id) {
              this.clearCompletedLiveMessages(completedTurnId);
            }
          })
          .catch((error) => this.showError(error));
      }
      if (event.type === "console.turn.error") {
        this.active = false;
        this.activeKind = null;
        this.activeModel = null;
        this.activeReasoningEffort = null;
        this.finishStreamingAgentMessages(event.turn_id);
        this.collapseToolCards();
        this.syncSessionStatus(event.thread_id, "error");
        this.markRunning(event.thread_id, false);
        this.busy = false;
        this.errorMessage = "The running turn ended with an error.";
      }
      if (event.type === "console.goal.starting") {
        this.active = true;
        this.activeKind = "goal";
        if (Object.hasOwn(event.data || {}, "model")) {
          this.activeModel = event.data.model || "";
        }
        if (Object.hasOwn(event.data || {}, "reasoning_effort")) {
          this.activeReasoningEffort = event.data.reasoning_effort || "";
        }
        this.syncSessionStatus(event.thread_id, "starting");
        this.markRunning(event.thread_id, true);
      }
      if (event.type === "console.goal.running") {
        this.active = true;
        this.activeKind = "goal";
        if (Object.hasOwn(event.data || {}, "model")) {
          this.activeModel = event.data.model || "";
        }
        if (Object.hasOwn(event.data || {}, "reasoning_effort")) {
          this.activeReasoningEffort = event.data.reasoning_effort || "";
        }
        this.syncGoalSnapshot(event.thread_id, event.data?.goal);
        this.syncSessionStatus(event.thread_id, "running");
        this.markRunning(event.thread_id, true);
      }
      if (event.type === "console.goal.stopping") {
        this.syncSessionStatus(event.thread_id, "stopping");
      }
      if (event.type === "console.goal.idle") {
        this.active = false;
        this.activeKind = null;
        this.activeModel = null;
        this.activeReasoningEffort = null;
        this.syncSessionStatus(event.thread_id, "idle");
        this.markRunning(event.thread_id, false);
        this.busy = false;
        const completedTurnId = event.turn_id;
        this.finishStreamingAgentMessages(completedTurnId);
        this.collapseToolCards();
        this.refreshThreadAndList()
          .then(() => {
            if (this.threadId === event.thread_id) {
              this.clearCompletedLiveMessages(completedTurnId);
            }
          })
          .catch((error) => this.showError(error));
      }
      if (event.type === "console.goal.error") {
        this.active = false;
        this.activeKind = null;
        this.activeModel = null;
        this.activeReasoningEffort = null;
        this.syncSessionStatus(event.thread_id, "error");
        this.markRunning(event.thread_id, false);
        this.busy = false;
        this.errorMessage = "The long-running goal ended with an error.";
      }
      if (this.isLiveDebugEvent(event)) this.queueLiveEvent(event);
    },

    isLiveDebugEvent(event) {
      const type = String(event?.type || "");
      return type === "codex.notification" || type.startsWith("console.");
    },

    queueLiveEvent(event) {
      this.pendingLiveEvents.push({
        ...event,
        localKey: `${event.sequence}-${this.eventCounter++}`,
      });
      if (this.liveFlushScheduled) return;
      this.liveFlushScheduled = true;
      window.requestAnimationFrame(() => {
        this.liveEvents.push(...this.pendingLiveEvents.splice(0));
        if (this.liveEvents.length > 1000) {
          this.liveEvents.splice(0, this.liveEvents.length - 1000);
        }
        this.liveFlushScheduled = false;
      });
    },

    queueAgentMessageDelta(event) {
      const data = event.data || {};
      if (typeof data.delta !== "string" || !data.delta) return;
      const itemId = data.item_id || data.itemId;
      if (!itemId) return;

      const timeline = document.querySelector("#timeline");
      this.pinTimelineAfterAgentFlush ||= this.timelineIsNearBottom(timeline);
      const turnId = event.turn_id || data.turn_id || data.turnId || "";
      this.pendingAgentMessageDeltas.push({
        key: `${turnId}:${itemId}:${this.liveResponseSegment}`,
        itemId: String(itemId),
        turnId: String(turnId),
        delta: data.delta,
        sequence: Number(event.sequence),
      });
      if (this.agentMessageFlushFrame !== null) return;

      this.agentMessageFlushFrame = window.requestAnimationFrame(() => {
        this.agentMessageFlushFrame = null;
        this.flushAgentMessageDeltas();
      });
    },

    flushAgentMessageDeltas() {
      for (const segment of this.pendingAgentMessageDeltas.splice(0)) {
        this.setStreamingAgentMessage(segment.key);
        const index = this.liveTimelineItems.findIndex(
          (item) => item.kind === "agent" && item.key === segment.key,
        );
        if (index === -1) {
          this.liveTimelineItems.push({
            key: segment.key,
            kind: "agent",
            itemId: segment.itemId,
            turnId: segment.turnId,
            text: segment.delta,
            streaming: true,
            sequence: segment.sequence,
          });
        } else {
          this.liveTimelineItems[index].text += segment.delta;
          this.liveTimelineItems[index].sequence = Math.max(
            Number(this.liveTimelineItems[index].sequence) || 0,
            Number(segment.sequence) || 0,
          );
        }
      }

      const shouldPin = this.pinTimelineAfterAgentFlush;
      this.pinTimelineAfterAgentFlush = false;
      if (!shouldPin) return;
      const threadId = this.threadId;
      window.requestAnimationFrame(() => {
        if (this.threadId !== threadId) return;
        const timeline = document.querySelector("#timeline");
        if (timeline) timeline.scrollTop = timeline.scrollHeight;
      });
    },

    timelineIsNearBottom(timeline) {
      if (!timeline || timeline.clientHeight === 0) return false;
      return (
        timeline.scrollHeight - timeline.scrollTop - timeline.clientHeight < 120
      );
    },

    flushQueuedAgentMessages() {
      if (this.agentMessageFlushFrame !== null) {
        window.cancelAnimationFrame(this.agentMessageFlushFrame);
        this.agentMessageFlushFrame = null;
      }
      if (this.pendingAgentMessageDeltas.length) {
        this.flushAgentMessageDeltas();
      }
    },

    recordCompletedToolItem(event) {
      const data = event.data || {};
      const item = data.item?.root || data.item;
      if (!this.isToolTimelineItem(item)) return;
      const itemId = String(item.id || data.item_id || data.itemId || "");
      if (!itemId) return;

      const timeline = document.querySelector("#timeline");
      const shouldPin = this.timelineIsNearBottom(timeline);
      const turnId = String(event.turn_id || data.turn_id || data.turnId || "");
      const key = `tool:${turnId}:${itemId}`;
      const liveItem = {
        key,
        kind: "tool",
        itemId,
        turnId,
        tool: { ...item },
        sequence: Number(event.sequence),
      };
      const index = this.liveTimelineItems.findIndex(
        (candidate) => candidate.key === key,
      );

      this.flushQueuedAgentMessages();
      this.finishStreamingAgentMessages(turnId);
      if (index === -1) {
        this.liveResponseSegment += 1;
        this.liveTimelineItems.push(liveItem);
      } else {
        this.liveTimelineItems.splice(index, 1, liveItem);
      }
      this.scheduleToolCardToggleStateSync();
      if (shouldPin) this.scrollTimelineToBottom();
    },

    recordCompletedAgentMessage(event) {
      const data = event.data || {};
      const item = data.item?.root || data.item;
      const itemType = item?.type || data.item_type || data.itemType;
      if (itemType !== "agentMessage") return;
      const itemId = String(item?.id || data.item_id || data.itemId || "");
      if (!itemId) return;
      const turnId = String(event.turn_id || data.turn_id || data.turnId || "");
      const text = String(item?.text || data.text || "");
      this.flushQueuedAgentMessages();
      let index = this.liveTimelineItems.findIndex(
        (candidate) => (
          candidate.kind === "agent" &&
          candidate.itemId === itemId &&
          candidate.turnId === turnId
        ),
      );
      if (index === -1) {
        this.liveTimelineItems.push({
          key: `${turnId}:${itemId}:${this.liveResponseSegment}`,
          kind: "agent",
          itemId,
          turnId,
          text,
          streaming: false,
          sequence: Number(event.sequence),
        });
        index = this.liveTimelineItems.length - 1;
      } else {
        this.liveTimelineItems[index] = {
          ...this.liveTimelineItems[index],
          text,
          streaming: false,
          sequence: Number(event.sequence),
        };
      }
      this.liveResponseSegment += 1;
    },

    isToolTimelineItem(item) {
      if (!item || typeof item !== "object" || !item.type) return false;
      return ![
        "agentMessage",
        "hookPrompt",
        "plan",
        "reasoning",
        "userMessage",
      ].includes(item.type);
    },

    appendOptimisticUserMessage(text, turnId = "") {
      this.flushQueuedAgentMessages();
      this.finishStreamingAgentMessages();
      this.liveResponseSegment += 1;
      const key = `user-${this.eventCounter++}`;
      this.liveTimelineItems.push({
        key,
        kind: "user",
        turnId: String(turnId || ""),
        text,
      });
      this.scrollTimelineToBottom();
      return key;
    },

    setStreamingAgentMessage(key) {
      this.liveTimelineItems = this.liveTimelineItems.map((item) =>
        item.kind === "agent"
          ? { ...item, streaming: item.key === key }
          : item,
      );
    },

    finishStreamingAgentMessages(turnId = "") {
      const normalizedTurnId = String(turnId || "");
      this.liveTimelineItems = this.liveTimelineItems.map((item) =>
        item.kind === "agent" && (!normalizedTurnId || item.turnId === normalizedTurnId)
          ? { ...item, streaming: false }
          : item,
      );
    },

    collapseToolCards() {
      document
        .querySelectorAll("#timeline details.tool-card[open]")
        .forEach((card) => {
          card.open = false;
        });
      this.syncToolCardToggleState();
    },

    collapsibleToolCards() {
      return Array.from(
        document.querySelectorAll("#timeline details.tool-card"),
      );
    },

    syncToolCardToggleState() {
      const cards = this.collapsibleToolCards();
      this.collapsibleToolCardCount = cards.length;
      this.allToolCardsExpanded = cards.length > 0 && cards.every((card) => card.open);
    },

    scheduleToolCardToggleStateSync() {
      const threadId = this.threadId;
      window.requestAnimationFrame(() => {
        if (this.threadId === threadId) this.syncToolCardToggleState();
      });
    },

    toggleToolCards() {
      const cards = this.collapsibleToolCards();
      if (!cards.length) {
        this.syncToolCardToggleState();
        return;
      }
      const expand = !cards.every((card) => card.open);
      cards.forEach((card) => {
        card.open = expand;
      });
      this.syncToolCardToggleState();
    },

    bindLiveMessageToTurn(key, turnId, sequence = null) {
      const normalizedTurnId = String(turnId || "");
      if (!normalizedTurnId) return;
      const item = this.liveTimelineItems.find((candidate) => candidate.key === key);
      if (item) {
        item.turnId = normalizedTurnId;
        const durableSequence = Number(sequence);
        if (Number.isSafeInteger(durableSequence) && durableSequence >= 0) {
          item.sequence = durableSequence;
        }
      }
    },

    bindPendingUserMessagesToTurn(turnId) {
      const normalizedTurnId = String(turnId || "");
      if (!normalizedTurnId) return;
      for (const item of this.liveTimelineItems) {
        if (item.kind === "user" && !item.turnId) {
          item.turnId = normalizedTurnId;
        }
      }
    },

    removeLiveMessage(key) {
      this.liveTimelineItems = this.liveTimelineItems.filter(
        (item) => item.key !== key,
      );
    },

    scrollTimelineToBottom() {
      const threadId = this.threadId;
      window.requestAnimationFrame(() => {
        if (this.threadId !== threadId) return;
        const timeline = document.querySelector("#timeline");
        if (timeline) timeline.scrollTop = timeline.scrollHeight;
      });
    },

    clearCompletedLiveMessages(turnId) {
      const normalizedTurnId = String(turnId || "");
      if (!normalizedTurnId) {
        if (!this.active) this.resetLiveTimeline();
        return;
      }
      this.liveTimelineItems = this.liveTimelineItems.filter(
        (item) =>
          item.turnId !== normalizedTurnId && (item.turnId || this.active),
      );
      this.pendingAgentMessageDeltas = this.pendingAgentMessageDeltas.filter(
        (segment) =>
          segment.turnId !== normalizedTurnId && (segment.turnId || this.active),
      );
      this.scheduleToolCardToggleStateSync();
    },

    resetLiveTimeline() {
      if (this.agentMessageFlushFrame !== null) {
        window.cancelAnimationFrame(this.agentMessageFlushFrame);
      }
      this.agentMessageFlushFrame = null;
      this.pendingAgentMessageDeltas = [];
      this.pinTimelineAfterAgentFlush = false;
      this.liveTimelineItems = [];
      this.liveResponseSegment = 0;
      this.scheduleToolCardToggleStateSync();
    },

    resizeComposer(textarea) {
      if (!textarea) return;
      textarea.style.height = "auto";
      const maxHeight = Number.parseFloat(
        window.getComputedStyle(textarea).maxHeight,
      );
      const height = Number.isFinite(maxHeight)
        ? Math.min(textarea.scrollHeight, maxHeight)
        : textarea.scrollHeight;
      textarea.style.height = `${Math.ceil(height)}px`;
      textarea.style.overflowY = textarea.scrollHeight > height
        ? "auto"
        : "hidden";
    },

    observeComposerDock(composer) {
      this.composerResizeObserver?.disconnect();
      this.composerResizeObserver = null;
      if (!composer) return;

      const conversation = composer.closest(".conversation-panel");
      const updateClearance = () => {
        if (!conversation) return;
        const height = Math.ceil(composer.getBoundingClientRect().height);
        conversation.style.setProperty("--composer-clearance", `${height}px`);
      };
      updateClearance();
      if (!window.ResizeObserver) return;

      this.composerResizeObserver = new ResizeObserver(updateClearance);
      this.composerResizeObserver.observe(composer);
    },

    async submitPrompt() {
      const text = this.prompt.trim();
      if (!text || !this.threadId || this.busy) return;
      if (text === "/goal" || text.startsWith("/goal ")) {
        await this.handleGoalCommand(text);
        return;
      }
      if (this.active) {
        await this.steerTurn(text);
        return;
      }
      if (!this.streamReady) {
        this.errorMessage = "Wait for the live stream before sending.";
        return;
      }
      const threadId = this.threadId;
      this.prompt = "";
      this.busy = true;
      this.active = true;
      this.activeModel = this.model || "";
      this.activeReasoningEffort = this.reasoningEffort || "";
      this.syncSessionStatus(threadId, "starting");
      this.markRunning(threadId, true);
      const messageKey = this.appendOptimisticUserMessage(text);
      this.liveEvents.push({
        localKey: `optimistic-${this.eventCounter++}`,
        type: "console.user.optimistic",
        method: "You",
        data: { text },
      });
      try {
        const result = await this.api(
          `/api/codex/threads/${encodeURIComponent(threadId)}/turns`,
          {
            method: "POST",
            body: JSON.stringify({
              prompt: text,
              model: this.model || null,
              reasoning_effort: this.reasoningEffort || null,
            }),
          },
        );
        this.bindLiveMessageToTurn(
          messageKey,
          result.turn_id,
          result.journal_cursor,
        );
      } catch (error) {
        this.removeLiveMessage(messageKey);
        this.markRunning(threadId, false);
        if (this.threadId === threadId) {
          this.active = false;
          this.activeModel = null;
          this.activeReasoningEffort = null;
          this.syncSessionStatus(threadId, "error");
          this.prompt = text;
        }
        this.showError(error);
      } finally {
        this.busy = false;
      }
    },

    async handleGoalCommand(command) {
      const argument = command.slice("/goal".length).trim();
      this.prompt = "";
      if (!argument) {
        await this.showGoal();
        return;
      }

      const action = argument.toLowerCase();
      if (action === "pause") {
        await this.pauseGoal();
        return;
      }
      if (action === "resume") {
        await this.resumeGoal();
        return;
      }
      if (action === "clear") {
        await this.clearGoal(false);
        return;
      }
      const objective = action.startsWith("set ")
        ? argument.slice(4).trim()
        : argument;
      if (!objective) {
        this.errorMessage = "Use /goal followed by an objective.";
        return;
      }
      await this.startGoal(objective, null);
    },

    async showGoal() {
      if (!this.threadId) return;
      try {
        this.busy = true;
        const threadId = this.threadId;
        const payload = await this.api(
          `/api/codex/threads/${encodeURIComponent(threadId)}/goal`,
        );
        this.syncGoalSnapshot(threadId, payload.goal);
        this.mobileTab = "plan";
        if (!payload.goal) this.openGoalEditor(false);
        await this.$nextTick();
        document.querySelector("#goal-panel")?.scrollIntoView({ block: "start" });
      } catch (error) {
        this.showError(error);
      } finally {
        this.busy = false;
      }
    },

    openGoalEditor(replace = false) {
      if (this.active) {
        this.errorMessage = "Pause the running operation before replacing its goal.";
        return;
      }
      this.goalObjective = replace ? String(this.liveGoal?.objective || "") : "";
      this.goalTokenBudget = replace && this.liveGoal?.tokenBudget
        ? String(this.liveGoal.tokenBudget)
        : "";
      this.goalEditorOpen = true;
      this.$nextTick(() => this.$refs.goalObjective?.focus());
    },

    async startGoalFromEditor() {
      const objective = this.goalObjective.trim();
      if (!objective) return;
      const rawBudget = String(this.goalTokenBudget || "").trim();
      const tokenBudget = rawBudget ? Number(rawBudget) : null;
      if (
        tokenBudget !== null &&
        (!Number.isInteger(tokenBudget) || tokenBudget < 1 || tokenBudget > 2000000000)
      ) {
        this.errorMessage = "Token budget must be a whole number between 1 and 2,000,000,000.";
        return;
      }
      await this.startGoal(objective, tokenBudget);
    },

    async startGoal(objective, tokenBudget = null) {
      if (!this.threadId || this.busy) return;
      const threadId = this.threadId;
      const requestedModel = this.model || this.currentModelId || null;
      const requestedReasoningEffort =
        this.reasoningEffort || this.defaultReasoningEffort || null;
      try {
        this.busy = true;
        this.active = true;
        this.activeKind = "goal";
        this.activeModel = requestedModel || "";
        this.activeReasoningEffort = requestedReasoningEffort || "";
        this.markRunning(threadId, true);
        this.syncSessionStatus(threadId, "starting");
        const result = await this.api(
          `/api/codex/threads/${encodeURIComponent(threadId)}/goal`,
          {
            method: "POST",
            body: JSON.stringify({
              objective,
              token_budget: tokenBudget,
              model: requestedModel,
              reasoning_effort: requestedReasoningEffort,
            }),
          },
        );
        this.activeModel = result.model || requestedModel || "";
        this.activeReasoningEffort =
          result.reasoning_effort || requestedReasoningEffort || "";
        this.syncGoalSnapshot(threadId, result.goal);
        this.goalEditorOpen = false;
        this.goalObjective = "";
        this.goalTokenBudget = "";
        this.mobileTab = "plan";
      } catch (error) {
        if (this.threadId === threadId) {
          this.active = false;
          this.activeKind = null;
          this.activeModel = null;
          this.activeReasoningEffort = null;
          this.markRunning(threadId, false);
          this.syncSessionStatus(threadId, "error");
          this.goalObjective = objective;
          this.goalTokenBudget = tokenBudget == null ? "" : String(tokenBudget);
          this.goalEditorOpen = true;
        }
        this.showError(error);
      } finally {
        this.busy = false;
      }
    },

    async pauseGoal() {
      if (!this.threadId || this.busy || !this.liveGoal) return;
      const threadId = this.threadId;
      try {
        this.busy = true;
        this.syncSessionStatus(threadId, "stopping");
        const result = await this.api(
          `/api/codex/threads/${encodeURIComponent(threadId)}/goal`,
          {
            method: "PATCH",
            body: JSON.stringify({ status: "paused" }),
          },
        );
        this.syncGoalSnapshot(threadId, result.goal);
      } catch (error) {
        this.showError(error);
      } finally {
        this.busy = false;
      }
    },

    async resumeGoal() {
      if (!this.threadId || this.busy || this.active || !this.liveGoal) return;
      const threadId = this.threadId;
      const requestedModel = this.model || this.currentModelId || null;
      const requestedReasoningEffort =
        this.reasoningEffort || this.defaultReasoningEffort || null;
      try {
        this.busy = true;
        this.active = true;
        this.activeKind = "goal";
        this.activeModel = requestedModel || "";
        this.activeReasoningEffort = requestedReasoningEffort || "";
        this.markRunning(threadId, true);
        this.syncSessionStatus(threadId, "starting");
        const result = await this.api(
          `/api/codex/threads/${encodeURIComponent(threadId)}/goal`,
          {
            method: "PATCH",
            body: JSON.stringify({
              status: "active",
              model: requestedModel,
              reasoning_effort: requestedReasoningEffort,
            }),
          },
        );
        this.activeModel = result.model || requestedModel || "";
        this.activeReasoningEffort =
          result.reasoning_effort || requestedReasoningEffort || "";
        this.syncGoalSnapshot(threadId, result.goal);
      } catch (error) {
        if (this.threadId === threadId) {
          this.active = false;
          this.activeKind = null;
          this.activeModel = null;
          this.activeReasoningEffort = null;
          this.markRunning(threadId, false);
          this.syncSessionStatus(threadId, "error");
        }
        this.showError(error);
      } finally {
        this.busy = false;
      }
    },

    async clearGoal(confirmClear = true) {
      if (!this.threadId || this.busy || !this.liveGoal) return;
      if (confirmClear && !window.confirm("Clear this long-running goal?")) return;
      const threadId = this.threadId;
      try {
        this.busy = true;
        await this.api(
          `/api/codex/threads/${encodeURIComponent(threadId)}/goal`,
          { method: "DELETE" },
        );
        this.syncGoalSnapshot(threadId, null);
        this.goalEditorOpen = false;
      } catch (error) {
        this.showError(error);
      } finally {
        this.busy = false;
      }
    },

    async steerTurn(text) {
      const threadId = this.threadId;
      this.prompt = "";
      this.busy = true;
      const messageKey = this.appendOptimisticUserMessage(text);
      this.liveEvents.push({
        localKey: `optimistic-${this.eventCounter++}`,
        type: "console.user.optimistic",
        method: "You",
        data: { text },
      });
      try {
        const result = await this.api(
          `/api/codex/threads/${encodeURIComponent(threadId)}/steer`,
          {
            method: "POST",
            body: JSON.stringify({ prompt: text }),
          },
        );
        this.bindLiveMessageToTurn(
          messageKey,
          result.turn_id,
          result.journal_cursor,
        );
      } catch (error) {
        this.removeLiveMessage(messageKey);
        if (this.threadId === threadId) this.prompt = text;
        this.showError(error);
      } finally {
        this.busy = false;
      }
    },

    async interruptTurn() {
      if (!this.threadId || !this.active) return;
      this.syncSessionStatus(this.threadId, "stopping");
      try {
        await this.api(`/api/codex/threads/${encodeURIComponent(this.threadId)}/interrupt`, {
          method: "POST",
        });
      } catch (error) {
        this.syncSessionStatus(this.threadId, "running");
        this.showError(error);
      }
    },

    async renameThread(threadId = this.threadId) {
      if (!threadId) return;
      const name = window.prompt("New session name")?.trim();
      if (!name) return;
      await this.mutateThread({ name }, threadId);
    },

    async togglePin(pinned, threadId = this.threadId) {
      if (!threadId) return;
      await this.mutateThread({ pinned }, threadId);
    },

    async mutateThread(body, threadId = this.threadId) {
      if (!threadId) return;
      try {
        await this.api(`/api/codex/threads/${encodeURIComponent(threadId)}`, {
          method: "PATCH",
          body: JSON.stringify(body),
        });
        if (threadId === this.threadId) {
          await this.refreshThreadAndList();
        } else {
          await this.refreshThreads();
        }
      } catch (error) {
        this.showError(error);
      }
    },

    async forkThread(threadId = this.threadId) {
      if (!threadId) return;
      try {
        const thread = await this.api(
          `/api/codex/threads/${encodeURIComponent(threadId)}/fork`,
          { method: "POST" },
        );
        await this.refreshThreads();
        await this.selectThread(thread.id);
      } catch (error) {
        this.showError(error);
      }
    },

    async archiveThread(threadId = this.threadId) {
      if (!threadId || !window.confirm("Archive this session?")) return;
      try {
        await this.api(
          `/api/codex/threads/${encodeURIComponent(threadId)}/archive`,
          { method: "POST" },
        );
        if (threadId === this.threadId) {
          this.threadId = "";
          this.closeEvents();
          this.clearThreadPanels();
          await this.savePreferences({ selected_thread_id: null }).catch(() => {});
        }
        await this.refreshThreads();
      } catch (error) {
        this.showError(error);
      }
    },

    async deleteThread(threadId = this.threadId) {
      if (
        !threadId ||
        !window.confirm("Delete this session permanently? This cannot be undone.")
      ) return;
      let deleteError = null;
      try {
        await this.api(`/api/codex/threads/${encodeURIComponent(threadId)}`, {
          method: "DELETE",
        });
        this.forgetEventSequence(threadId);
        if (threadId === this.threadId) {
          this.threadId = "";
          this.closeEvents();
          this.clearThreadPanels();
          await this.savePreferences({ selected_thread_id: null }).catch(() => {});
        }
      } catch (error) {
        deleteError = error;
        this.showError(error);
      } finally {
        try {
          await this.refreshThreads();
        } catch (refreshError) {
          if (!deleteError) this.showError(refreshError);
        }
      }
    },

    async unarchiveThread(threadId = this.threadId) {
      if (!threadId) return;
      try {
        await this.api(
          `/api/codex/threads/${encodeURIComponent(threadId)}/unarchive`,
          { method: "POST" },
        );
        if (threadId === this.threadId) {
          this.threadId = "";
          this.closeEvents();
          this.clearThreadPanels();
          await this.savePreferences({ selected_thread_id: null }).catch(() => {});
        }
        await this.refreshThreads();
      } catch (error) {
        this.showError(error);
      }
    },

    clearThreadPanels() {
      this.modelSettingsOpen = false;
      this.conversationTab = "timeline";
      this.collapsibleToolCardCount = 0;
      this.allToolCardsExpanded = false;
      this.liveEvents = [];
      this.pendingLiveEvents = [];
      this.resetLiveTimeline();
      this.livePlans = [];
      this.liveDiff = "";
      this.liveDiffSequence = null;
      this.liveDiffSource = null;
      this.liveUsage = null;
      this.liveUsageSequence = null;
      this.liveUsageSource = null;
      this.liveGoal = null;
      this.goalEditorOpen = false;
      this.goalObjective = "";
      this.goalTokenBudget = "";
      this.activeModel = null;
      this.activeReasoningEffort = null;
      this.activeKind = null;
      this.sessionStatus = this.normalizeSessionStatus("idle");
      document.querySelector("#timeline").textContent = "Choose a session, or create a new one.";
      document.querySelector("#latest-changes").textContent = "Select a session to review its latest file changes.";
      document.querySelector("#composer").textContent = "Select a session to connect its live stream.";
      document.querySelector("#inspector-content").textContent = "Session details will appear here.";
    },

    isRunning(threadId) {
      return this.runningThreadIds.includes(threadId);
    },

    markRunning(threadId, running) {
      const known = this.runningThreadIds.includes(threadId);
      if (running && !known) {
        this.runningThreadIds.push(threadId);
      } else if (!running && known) {
        this.runningThreadIds = this.runningThreadIds.filter((item) => item !== threadId);
      }
    },

    syncThreadActive(
      threadId,
      running,
      model = null,
      reasoningEffort = null,
      kind = null,
    ) {
      this.markRunning(threadId, running);
      if (this.threadId === threadId) {
        this.active = running;
        this.activeModel = running ? model || "" : null;
        this.activeReasoningEffort = running ? reasoningEffort || "" : null;
        this.activeKind = running ? kind || "turn" : null;
      }
    },

    normalizeSessionStatus(value = "idle") {
      let status = value;
      if (status && typeof status === "object" && status.root) {
        status = status.root;
      }
      if (typeof status === "string") {
        return { type: status || "idle", activeFlags: [] };
      }
      if (!status || typeof status !== "object") {
        return { type: "idle", activeFlags: [] };
      }

      const rawFlags = status.activeFlags || status.active_flags || [];
      return {
        type: String(status.type || status.status || "idle"),
        activeFlags: Array.isArray(rawFlags)
          ? rawFlags.map((flag) => String(flag?.value || flag))
          : [],
      };
    },

    syncSessionStatus(threadId, status = "idle") {
      if (this.threadId !== threadId) return;
      this.sessionStatus = this.normalizeSessionStatus(status);
    },

    syncGoalSnapshot(threadId, value = null) {
      if (this.threadId !== threadId) return;
      this.liveGoal = this.normalizeGoal(value);
    },

    normalizeGoal(value = null) {
      let goal = value;
      if (goal && typeof goal === "object" && goal.root) goal = goal.root;
      if (!goal || typeof goal !== "object") return null;
      const rawStatus = goal.status?.value || goal.status || "paused";
      const tokenBudget = goal.token_budget ?? goal.tokenBudget;
      return {
        threadId: String(goal.thread_id || goal.threadId || this.threadId),
        objective: String(goal.objective || ""),
        status: String(rawStatus),
        tokenBudget: tokenBudget == null ? null : this.tokenNumber(tokenBudget),
        tokensUsed: this.tokenNumber(goal.tokens_used ?? goal.tokensUsed),
        timeUsedSeconds: this.tokenNumber(
          goal.time_used_seconds ?? goal.timeUsedSeconds,
        ),
        createdAt: this.tokenNumber(goal.created_at ?? goal.createdAt),
        updatedAt: this.tokenNumber(goal.updated_at ?? goal.updatedAt),
      };
    },

    formatGoalDuration(value) {
      let seconds = Math.floor(this.tokenNumber(value));
      const hours = Math.floor(seconds / 3600);
      seconds %= 3600;
      const minutes = Math.floor(seconds / 60);
      seconds %= 60;
      if (hours) return `${hours}h ${minutes}m`;
      if (minutes) return `${minutes}m ${seconds}s`;
      return `${seconds}s`;
    },

    syncPlanHistory(threadId, plans = []) {
      if (this.threadId !== threadId) return;
      this.livePlans = this.recentPlans([
        ...plans.map((plan, index) => ({
          key: plan.key || `history-plan-${index}`,
          text: String(plan.text || "").trim(),
        })),
        ...this.livePlans,
      ]);
    },

    recordPlanUpdate(event) {
      const text = this.formatPlanUpdate(event.data);
      if (!text) return;
      this.livePlans = this.recentPlans([
        ...this.livePlans,
        {
          key: `live-plan-${event.sequence ?? this.eventCounter++}`,
          text,
          sequence: Number(event.sequence),
        },
      ]);
    },

    recentPlans(plans) {
      const seen = new Set();
      const recent = [];
      for (let index = plans.length - 1; index >= 0; index -= 1) {
        const plan = plans[index];
        const text = String(plan?.text || "").trim();
        if (!text || seen.has(text)) continue;
        seen.add(text);
        recent.unshift({ key: plan.key, text, sequence: plan.sequence });
      }
      return recent.slice(-3);
    },

    syncDiffSnapshot(threadId, diff, cursor) {
      if (this.threadId !== threadId) return;
      const snapshotCursor = Number(cursor);
      const liveSequence = Number(this.liveDiffSequence);
      if (
        Number.isSafeInteger(liveSequence) &&
        Number.isSafeInteger(snapshotCursor) &&
        liveSequence > snapshotCursor
      ) return;
      this.liveDiff = String(diff || "");
      this.liveDiffSequence = null;
      this.liveDiffSource = this.liveDiff ? "journal" : null;
    },

    syncUsageSnapshot(threadId, usage, cursor) {
      if (this.threadId !== threadId) return;
      const snapshotCursor = Number(cursor);
      const liveSequence = Number(this.liveUsageSequence);
      if (
        Number.isSafeInteger(liveSequence) &&
        Number.isSafeInteger(snapshotCursor) &&
        liveSequence > snapshotCursor
      ) return;
      this.liveUsage = usage ? this.normalizeUsage(usage) : null;
      this.liveUsageSequence = null;
      this.liveUsageSource = this.liveUsage ? "journal" : null;
    },

    formatPlanUpdate(data = {}) {
      const explanation = String(data?.explanation || "").trim();
      const steps = Array.isArray(data?.plan)
        ? data.plan
            .filter((item) => item?.step)
            .map((item) => `${this.planStepMarker(item.status)} ${item.step}`)
        : [];
      return [explanation, steps.join("\n")].filter(Boolean).join("\n");
    },

    planStepMarker(status) {
      return {
        completed: "✓",
        inProgress: "→",
        in_progress: "→",
        pending: "○",
      }[status] || "•";
    },

    eventText(event) {
      const data = event.data || {};
      return data.delta || data.diff || data.output || data.text || data.message || data.error_code || JSON.stringify(data, null, 2);
    },

    eventClass(event) {
      const method = event.method || "";
      if (method.includes("command")) return "live-event-command";
      if (method.includes("diff") || method.includes("fileChange")) return "live-event-diff";
      if (method.includes("plan")) return "live-event-plan";
      return "";
    },

    liveToolStatus(tool = {}) {
      const status = tool.status || (
        tool.success === true ? "completed" : tool.success === false ? "failed" : ""
      );
      const exitCode = tool.exit_code ?? tool.exitCode;
      return [status, exitCode == null ? "" : `exit ${exitCode}`]
        .filter(Boolean)
        .join(" · ");
    },

    liveToolInput(tool = {}) {
      if (tool.type === "commandExecution") return String(tool.command || "");
      if (tool.arguments != null) return this.formatLiveToolValue(tool.arguments);
      if (tool.prompt) return String(tool.prompt);
      if (tool.query) return String(tool.query);
      if (tool.path) return String(tool.path);
      return "";
    },

    liveToolOutput(tool = {}) {
      if (tool.type === "commandExecution") {
        return String(tool.aggregated_output ?? tool.aggregatedOutput ?? "");
      }
      if (tool.error != null) return this.formatLiveToolValue(tool.error);
      if (tool.result != null) return this.formatLiveToolValue(tool.result);
      const contentItems = tool.content_items ?? tool.contentItems;
      if (contentItems != null) return this.formatLiveToolValue(contentItems);
      const agentStates = tool.agents_states ?? tool.agentsStates;
      if (agentStates != null) return this.formatLiveToolValue(agentStates);
      if (tool.action != null) return this.formatLiveToolValue(tool.action);
      if (tool.saved_path || tool.savedPath) {
        return String(tool.saved_path || tool.savedPath);
      }
      return "";
    },

    formatLiveToolValue(value) {
      if (typeof value === "string") return value;
      try {
        return JSON.stringify(value, null, 2);
      } catch (_error) {
        return String(value ?? "");
      }
    },

    liveToolActionEntries(tool = {}) {
      const action = tool.action?.root || tool.action;
      if (!action || typeof action !== "object") return [];
      const preferredOrder = ["type", "query", "queries", "url", "pattern"];
      const keys = [
        ...preferredOrder.filter((key) => Object.hasOwn(action, key)),
        ...Object.keys(action).filter((key) => !preferredOrder.includes(key)),
      ];
      return keys
        .filter((key) => action[key] != null && action[key] !== "")
        .map((key) => ({
          key,
          label: key === "url" ? "URL" : key,
          value: Array.isArray(action[key])
            ? action[key].map((value) => this.formatLiveToolValue(value)).join(" · ")
            : this.formatLiveToolValue(action[key]),
        }));
    },

    liveFileChangeNames(tool = {}) {
      if (!Array.isArray(tool.changes)) return "";
      return tool.changes
        .map((change) => (
          change && typeof change === "object" ? change.path : change
        ))
        .map((path) => String(path || "").replaceAll("\\", "/").split("/").at(-1))
        .filter(Boolean)
        .join(" · ");
    },

    diffLines(source) {
      const text = String(source || "").replaceAll("\r\n", "\n");
      const lines = text.split("\n");
      if (lines.at(-1) === "") lines.pop();
      return lines.map((line) => ({
        text: line,
        kind: this.diffLineKind(line),
      }));
    },

    diffLineKind(line) {
      if (line.startsWith("diff --git ") || line.startsWith("index ")) return "meta";
      if (line.startsWith("@@")) return "hunk";
      if (line.startsWith("+++") || line.startsWith("---")) return "file";
      if (line.startsWith("+")) return "addition";
      if (line.startsWith("-")) return "deletion";
      if (line.startsWith("\\ No newline")) return "meta";
      return "context";
    },

    diffLineClass(kind) {
      return {
        addition: "diff-line-addition",
        deletion: "diff-line-deletion",
        file: "diff-line-file",
        hunk: "diff-line-hunk",
        meta: "diff-line-meta",
      }[kind] || "diff-line-context";
    },

    diffStats(source) {
      const lines = this.diffLines(source);
      let files = lines.filter((line) => line.text.startsWith("diff --git ")).length;
      if (!files) {
        files = lines.filter(
          (line) => line.text.startsWith("+++") && !line.text.includes("/dev/null"),
        ).length;
      }
      return {
        files,
        additions: lines.filter((line) => line.kind === "addition").length,
        deletions: lines.filter((line) => line.kind === "deletion").length,
      };
    },

    normalizeUsage(data = {}) {
      const usage = data?.token_usage || data?.tokenUsage || data || {};
      const contextWindow = usage.model_context_window ?? usage.modelContextWindow;
      return {
        total: this.normalizeTokenBreakdown(usage.total),
        last: this.normalizeTokenBreakdown(usage.last),
        contextWindow: this.tokenNumber(contextWindow),
      };
    },

    normalizeTokenBreakdown(value = {}) {
      return {
        total: this.tokenNumber(value?.total_tokens ?? value?.totalTokens),
        input: this.tokenNumber(value?.input_tokens ?? value?.inputTokens),
        cachedInput: this.tokenNumber(
          value?.cached_input_tokens ?? value?.cachedInputTokens,
        ),
        output: this.tokenNumber(value?.output_tokens ?? value?.outputTokens),
        reasoning: this.tokenNumber(
          value?.reasoning_output_tokens ?? value?.reasoningOutputTokens,
        ),
      };
    },

    tokenNumber(value) {
      const number = Number(value);
      return Number.isFinite(number) && number >= 0 ? number : 0;
    },

    formatTokenCount(value, compact = false) {
      const count = this.tokenNumber(value);
      return new Intl.NumberFormat("en-US", {
        notation: compact ? "compact" : "standard",
        maximumFractionDigits: compact ? 1 : 0,
      }).format(count);
    },

    handleHtmxError(event) {
      this.errorMessage = `A console panel failed to refresh (HTTP ${event.detail.xhr.status}).`;
    },

    showError(error) {
      if (
        error?.code === "codex_unavailable" &&
        this.projectKey &&
        !this.threadId
      ) {
        this.errorMessage =
          "No session is connected yet. Create or select a session to connect to Codex.";
        return;
      }
      this.errorMessage = error?.message || "The request failed.";
    },
  };
};
