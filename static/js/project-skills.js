"use strict";

window.projectSkills = function projectSkills() {
  return {
    skillsProjectKey: "",
    skills: [],
    skillDetail: null,
    skillSearch: "",
    skillLoading: false,
    skillBusy: false,
    skillError: "",
    skillStatus: "",
    skillReloadStatus: "",
    skillView: "preview",
    skillFilePath: "SKILL.md",
    skillFileRevision: "missing",
    skillOriginal: "",
    skillDraft: "",
    skillFileLoaded: false,
    skillRequestId: 0,
    skillFileRequestId: 0,
    skillImport: null,
    skillReplace: false,
    skillNewOpen: false,
    skillNewName: "",
    skillNewDescription: "",
    skillLimits: { max_bytes: 26214400, max_files: 1000, max_text_bytes: 1048576 },

    get filteredSkills() {
      const query = this.skillSearch.trim().toLowerCase();
      return this.skills.filter(skill => `${skill.directory} ${skill.description}`.toLowerCase().includes(query));
    },

    get skillDirty() { return this.skillFileLoaded && (this.skillFileRevision === "missing" || this.skillDraft !== this.skillOriginal); },
    get skillActionsDisabled() { return this.skillBusy || this.skillLoading || this.fileOperationBusy || this.projectOperationBusy; },
    get skillLocation() { return this.selectedProjectPath ? `${this.selectedProjectPath.replace(/\/+$/, "")}/.agents/skills` : ".agents/skills"; },
    get skillPreview() {
      const source = this.skillDraft;
      if (!/\.(md|markdown)$/i.test(this.skillFilePath)) return "";
      // Metadata has its own summary; render only the Markdown body for SKILL.md.
      return window.renderMarkdown(this.skillFilePath === "SKILL.md" ? source.replace(/^---\r?\n[\s\S]*?\r?\n---(?:\r?\n|$)/, "") : source);
    },

    skillsUrl(suffix = "", query = {}, projectKey = this.projectKey) {
      const parameters = new URLSearchParams(query);
      return `/api/projects/${encodeURIComponent(projectKey)}/skills${suffix}${parameters.size ? `?${parameters}` : ""}`;
    },

    skillUrl(suffix = "", query = {}) {
      return this.skillsUrl(`/${encodeURIComponent(this.skillDetail.directory)}${suffix}`, query);
    },

    confirmSkillNavigation() {
      if (this.skillBusy) { this.skillError = "Wait for the current skill operation to finish."; return false; }
      if (this.skillDirty) {
        if (!window.confirm(`Discard unsaved changes to ${this.skillFilePath}?`)) return false;
        this.skillDraft = this.skillOriginal;
        if (this.skillFileRevision === "missing") this.skillFileLoaded = false;
      }
      return true;
    },

    guardSkillUnload(event) {
      if (!this.skillDirty && !this.skillBusy) return;
      event.preventDefault();
      event.returnValue = "";
    },

    resetProjectSkills(projectKey = "") {
      if (this.skillImport?.token && this.skillsProjectKey) {
        this.api(this.skillsUrl(`/imports/${this.skillImport.token}`, {}, this.skillsProjectKey), { method: "DELETE" }).catch(() => {});
      }
      this.skillsProjectKey = projectKey;
      this.skillRequestId += 1;
      this.skillFileRequestId += 1;
      this.skills = [];
      this.skillDetail = null;
      this.skillImport = null;
      this.skillDraft = this.skillOriginal = "";
      this.skillFileLoaded = false;
      this.skillFilePath = "SKILL.md";
      this.skillFileRevision = "missing";
      this.skillView = "preview";
      this.skillSearch = this.skillError = this.skillStatus = this.skillReloadStatus = "";
      this.skillBusy = this.skillLoading = this.skillNewOpen = false;
    },

    async openSkillsTab() {
      if (!this.confirmSkillNavigation()) return;
      this.conversationTab = "skills";
      this.$nextTick?.(() => this.$refs?.skillsTab?.scrollIntoView({ block: "nearest", inline: "nearest" }));
      if (!this.projectKey) return;
      if (this.skillsProjectKey !== this.projectKey) this.resetProjectSkills(this.projectKey);
      await this.refreshSkills({ reload: false });
    },

    async refreshSkills({ reload = true, preserveEditor = false } = {}) {
      if (!this.projectKey || (!preserveEditor && !this.confirmSkillNavigation())) return;
      const projectKey = this.projectKey;
      const requestId = ++this.skillRequestId;
      this.skillLoading = true;
      this.skillError = "";
      try {
        const result = await this.api(this.skillsUrl(reload ? "/refresh" : ""), reload ? { method: "POST" } : {});
        if (this.projectKey !== projectKey || this.skillRequestId !== requestId) return;
        this.skills = result.data || [];
        this.skillLimits = result.limits || this.skillLimits;
        if (result.reload) this.skillReloadStatus = result.reload.message;
        if (!preserveEditor && this.skillDetail) {
          const current = this.skills.find(skill => skill.directory === this.skillDetail.directory);
          if (current?.supported) await this.selectSkill(current, true);
          else {
            this.skillDetail = current || null;
            this.skillFileLoaded = false;
          }
        }
      } catch (error) {
        if (this.projectKey === projectKey && this.skillRequestId === requestId) this.skillError = error.message;
      } finally {
        if (this.projectKey === projectKey && this.skillRequestId === requestId) this.skillLoading = false;
      }
    },

    async selectSkill(skill, confirmed = false) {
      if (!confirmed && !this.confirmSkillNavigation()) return;
      const projectKey = this.projectKey;
      const requestId = ++this.skillFileRequestId;
      this.skillDetail = skill;
      this.skillFileLoaded = false;
      this.skillDraft = this.skillOriginal = "";
      this.skillFilePath = "SKILL.md";
      this.skillView = "preview";
      this.skillError = "";
      if (!skill.supported) return;
      try {
        const detail = await this.api(this.skillsUrl(`/${encodeURIComponent(skill.directory)}`));
        if (projectKey !== this.projectKey || requestId !== this.skillFileRequestId) return;
        this.skillDetail = detail;
        if (detail.files.some(file => file.path === "SKILL.md" && file.type === "file")) {
          await this.openSkillFile("SKILL.md", "preview", true);
        } else {
          this.skillView = "files";
        }
      } catch (error) {
        if (projectKey === this.projectKey && requestId === this.skillFileRequestId) this.skillError = error.message;
      }
    },

    async openSkillFile(path, view = "edit", confirmed = false) {
      if (!this.skillDetail?.supported || (!confirmed && !this.confirmSkillNavigation())) return;
      const projectKey = this.projectKey;
      const directory = this.skillDetail.directory;
      const requestId = ++this.skillFileRequestId;
      this.skillError = "";
      try {
        const file = await this.api(this.skillUrl("/file", { path }));
        if (projectKey !== this.projectKey || directory !== this.skillDetail?.directory || requestId !== this.skillFileRequestId) return;
        this.skillFilePath = file.path;
        this.skillFileRevision = file.revision;
        this.skillDraft = this.skillOriginal = file.content;
        this.skillFileLoaded = true;
        this.skillView = view;
      } catch (error) {
        if (projectKey === this.projectKey && requestId === this.skillFileRequestId) {
          this.skillFileLoaded = false;
          this.skillView = "files";
          this.skillError = error.message;
        }
      }
    },

    async runSkillMutation(url, options, message, after) {
      if (this.skillActionsDisabled) return;
      const projectKey = this.projectKey;
      this.skillBusy = true;
      this.skillError = "";
      this.skillStatus = "Saving…";
      try {
        const result = await this.api(url, options);
        if (projectKey !== this.projectKey) return;
        this.skillStatus = message;
        this.skillReloadStatus = result.reload?.message || "";
        if (result.skill) this.skillDetail = result.skill;
        this.resetProjectFiles(projectKey);
        if (after) await after(result);
        await this.refreshSkills({ reload: false, preserveEditor: true });
      } catch (error) {
        if (projectKey === this.projectKey) {
          this.skillError = error.message;
          this.skillStatus = "";
        }
      } finally {
        if (projectKey === this.projectKey) this.skillBusy = false;
      }
    },

    async saveSkillFile() {
      if (!this.skillFileLoaded || !this.skillDirty) return;
      if (new TextEncoder().encode(this.skillDraft).length > this.skillLimits.max_text_bytes) {
        this.skillError = "The editor supports files up to 1 MiB."; return;
      }
      const path = this.skillFilePath;
      await this.runSkillMutation(this.skillUrl("/file"), {
        method: "PUT", body: JSON.stringify({ path, content: this.skillDraft, revision: this.skillFileRevision }),
      }, "File saved.", async () => { await this.openSkillFile(path, "edit", true); });
    },

    cancelSkillEdit() {
      if (!this.confirmSkillNavigation()) return;
      this.skillView = "preview";
    },

    async createSkill() {
      if (!this.confirmSkillNavigation()) return;
      await this.runSkillMutation(this.skillsUrl(), {
        method: "POST", body: JSON.stringify({ name: this.skillNewName.trim(), description: this.skillNewDescription.trim() }),
      }, "Skill created.", async result => {
        this.skillNewOpen = false;
        this.skillNewName = this.skillNewDescription = "";
        await this.selectSkill(result.skill, true);
        this.skillView = "edit";
      });
    },

    async renameSkill() {
      if (!this.skillDetail?.supported || this.skillActionsDisabled || !this.confirmSkillNavigation()) return;
      const name = window.prompt("New skill name", this.skillDetail.directory)?.trim();
      if (!name || name === this.skillDetail.directory) return;
      await this.runSkillMutation(this.skillUrl(), {
        method: "PATCH", body: JSON.stringify({ name, revision: this.skillDetail.revision }),
      }, "Skill renamed.", async result => this.selectSkill(result.skill, true));
    },

    async deleteSkill() {
      if (!this.skillDetail?.supported || this.skillActionsDisabled || !this.confirmSkillNavigation()) return;
      if (!window.confirm(`Delete skill "${this.skillDetail.directory}" and everything inside:\n${this.skillDetail.path}\n\nThis cannot be undone.`)) return;
      await this.runSkillMutation(this.skillUrl("", { revision: this.skillDetail.revision }), { method: "DELETE" }, "Skill deleted.", async () => {
        this.skillDetail = null;
        this.skillFileLoaded = false;
        this.skillDraft = this.skillOriginal = "";
      });
    },

    newSkillFile() {
      if (this.skillActionsDisabled || !this.confirmSkillNavigation()) return;
      const path = window.prompt("New file path relative to this skill (for example references/guide.md)")?.trim();
      if (!path) return;
      if (this.skillDetail.files.some(file => file.path === path)) { this.skillError = "This path already exists. Open the existing file to edit it."; return; }
      this.skillFilePath = path;
      this.skillFileRevision = "missing";
      this.skillDraft = this.skillOriginal = "";
      this.skillFileLoaded = true;
      this.skillView = "edit";
    },

    async deleteSkillFile(file) {
      if (this.skillActionsDisabled || !this.confirmSkillNavigation()) return;
      if (!window.confirm(`Delete ${file.path}${file.type === "directory" ? " and everything inside it" : ""}?`)) return;
      await this.runSkillMutation(this.skillUrl("/file", { path: file.path, revision: this.skillDetail.revision }), { method: "DELETE" }, "File removed.", async result => {
        await this.selectSkill(result.skill, true);
        this.skillView = "files";
      });
    },

    async uploadSkillFile(event) {
      const file = event.currentTarget.files?.[0];
      event.currentTarget.value = "";
      if (!file || this.skillActionsDisabled || !this.confirmSkillNavigation()) return;
      const path = window.prompt("File path relative to this skill", file.name)?.trim();
      if (!path) return;
      const existing = this.skillDetail.files.find(item => item.path === path);
      if (existing && !window.confirm(`Replace ${path}?`)) return;
      if (file.size > this.skillLimits.max_bytes) { this.skillError = "The file exceeds the configured size limit."; return; }
      await this.runSkillMutation(this.skillUrl("/upload", { path, revision: existing?.revision || "missing" }), {
        method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: file,
      }, "File uploaded.", async result => { await this.selectSkill(result.skill, true); this.skillView = "files"; });
    },

    skillFileUrl(file, action = "preview") {
      if (!this.skillDetail) return "";
      return this.projectFilesUrl(`/${action}`, { path: `${this.skillDetail.path}/${file.path}`, show_hidden: true });
    },

    async uploadSkill(event, kind) {
      const files = Array.from(event.currentTarget.files || []);
      event.currentTarget.value = "";
      if (!files.length || this.skillActionsDisabled || !this.confirmSkillNavigation()) return;
      if (files.length > this.skillLimits.max_files || files.reduce((sum, file) => sum + file.size, 0) > this.skillLimits.max_bytes) {
        this.skillError = "This skill exceeds the configured size or file count limit."; return;
      }
      const projectKey = this.projectKey;
      this.skillBusy = true;
      this.skillError = "";
      this.skillStatus = "Preparing import…";
      try {
        await this.cancelSkillImport(true);
        let body = files[0];
        if (kind === "directory") {
          const manifest = [];
          for (const file of files) {
            const bytes = new Uint8Array(await file.arrayBuffer());
            let binary = "";
            for (let offset = 0; offset < bytes.length; offset += 8192) binary += String.fromCharCode(...bytes.subarray(offset, offset + 8192));
            manifest.push({ path: file.webkitRelativePath || file.name, content: btoa(binary) });
          }
          body = JSON.stringify({ files: manifest });
        }
        const result = await this.api(this.skillsUrl("/imports", { kind }, projectKey), {
          method: "POST", headers: { "Content-Type": kind === "zip" ? "application/zip" : "application/json" }, body,
        });
        if (this.projectKey !== projectKey) {
          if (result.token) this.api(this.skillsUrl(`/imports/${result.token}`, {}, projectKey), { method: "DELETE" }).catch(() => {});
          return;
        }
        this.skillImport = result;
        this.skillReplace = false;
        this.skillStatus = "";
      } catch (error) {
        if (this.projectKey === projectKey) { this.skillError = error.message; this.skillStatus = ""; }
      } finally {
        if (this.projectKey === projectKey) this.skillBusy = false;
      }
    },

    async cancelSkillImport(duringUpload = false) {
      if (this.skillBusy && !duringUpload) return;
      const preview = this.skillImport;
      this.skillImport = null;
      this.skillReplace = false;
      if (preview?.token) {
        await this.api(this.skillsUrl(`/imports/${preview.token}`), { method: "DELETE" }).catch(() => {});
      }
    },

    async commitSkillImport() {
      if (!this.skillImport?.token || (this.skillImport.exists && !this.skillReplace)) return;
      if (!this.confirmSkillNavigation()) return;
      await this.runSkillMutation(this.skillsUrl(`/imports/${this.skillImport.token}`), {
        method: "POST", body: JSON.stringify({ replace: this.skillReplace }),
      }, "Skill imported.", async result => {
        this.skillImport = null;
        await this.selectSkill(result.skill, true);
      });
    },

    async downloadSkill() {
      if (!this.skillDetail || this.skillActionsDisabled) return;
      this.skillBusy = true;
      this.skillError = "";
      try {
        const response = await fetch(this.projectFilesUrl("/download", { path: this.skillDetail.path }));
        if (!response.ok) throw new Error("Could not download the skill.");
        const blob = await response.blob();
        const url = URL.createObjectURL(blob);
        const link = document.createElement("a");
        try {
          link.href = url;
          link.download = `${this.skillDetail.directory}.zip`;
          document.body.append(link);
          link.click();
        } finally { link.remove(); URL.revokeObjectURL(url); }
      } catch (error) { this.skillError = error.message; }
      finally { this.skillBusy = false; }
    },
  };
};
