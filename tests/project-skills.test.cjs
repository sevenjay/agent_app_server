const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { test } = require("node:test");
const vm = require("node:vm");

function view(globals = {}) {
  const context = { window: { confirm: () => true }, localStorage: { getItem: () => null }, URLSearchParams, TextEncoder, ...globals };
  context.window.clearTimeout = () => {};
  vm.runInNewContext(readFileSync("static/js/project-skills.js", "utf8"), context);
  vm.runInNewContext(readFileSync("static/js/codex-console.js", "utf8"), context);
  const state = context.window.codexConsole();
  state.projectKey = state.skillsProjectKey = "project";
  state.skillDetail = { directory: "code-review", path: ".agents/skills/code-review", supported: true, revision: "skill-revision", files: [] };
  return state;
}

test("skills getters remain reactive after integration with the console", () => {
  const state = view();
  state.skills = [{ directory: "review", description: "Code quality" }, { directory: "release", description: "Notes" }];
  assert.equal(state.filteredSkills.length, 2);
  state.skillSearch = "quality";
  assert.equal(state.filteredSkills.length, 1);
  state.skillFileLoaded = true;
  state.skillFileRevision = "hash";
  assert.equal(state.skillDirty, false);
  state.skillDraft = "modified";
  assert.equal(state.skillDirty, true);
});

test("unsaved changes block tab, project, and session navigation when declined", async () => {
  const state = view({ window: { confirm: () => false } });
  state.conversationTab = "skills";
  state.skillFileLoaded = true;
  state.skillFileRevision = "hash";
  state.skillDraft = "draft";
  assert.equal(state.switchConversationTab("timeline"), false);
  await state.openFilesTab();
  await state.selectProject("other");
  await state.selectThread("session");
  await state.newThread();
  assert.equal(state.conversationTab, "skills");
  assert.equal(state.projectKey, "project");
  assert.equal(state.skillDraft, "draft");
});

test("empty newly created files can be saved and are protected as drafts", () => {
  const state = view({ window: { prompt: () => "references/empty.md", confirm: () => false } });
  state.newSkillFile();
  assert.equal(state.skillDraft, "");
  assert.equal(state.skillDirty, true);
  assert.equal(state.confirmSkillNavigation(), false);
});

test("save conflict preserves the draft and original revision", async () => {
  const state = view();
  state.skillFileLoaded = true;
  state.skillFileRevision = "old-hash";
  state.skillOriginal = "original";
  state.skillDraft = "draft";
  state.api = async (_url, options) => {
    assert.equal(JSON.parse(options.body).revision, "old-hash");
    const error = new Error("File changed externally");
    error.code = "skill_changed";
    throw error;
  };
  await state.saveSkillFile();
  assert.equal(state.skillDraft, "draft");
  assert.equal(state.skillOriginal, "original");
  assert.equal(state.skillFileRevision, "old-hash");
  assert.equal(state.skillBusy, false);
  assert.match(state.skillError, /externally/);
});

test("stale file reads cannot replace the newly selected skill", async () => {
  const state = view();
  let resolve;
  state.api = () => new Promise(done => { resolve = done; });
  const pending = state.openSkillFile("SKILL.md");
  state.skillDetail = { directory: "different" };
  resolve({ path: "SKILL.md", revision: "hash", content: "stale" });
  await pending;
  assert.equal(state.skillDraft, "");
  assert.equal(state.skillFileLoaded, false);
});

test("replacement imports require explicit confirmation", async () => {
  const state = view();
  state.skillImport = { token: "token", valid: true, exists: true };
  let requests = 0;
  state.api = async () => { requests++; return { skill: state.skillDetail }; };
  state.selectSkill = async () => {};
  state.refreshSkills = async () => {};
  state.resetProjectFiles = () => {};
  await state.commitSkillImport();
  assert.equal(requests, 0);
  state.skillReplace = true;
  await state.commitSkillImport();
  assert.equal(requests, 1);
  assert.equal(state.skillImport, null);
});

test("folder uploads preserve relative paths and binary file bytes", async () => {
  const state = view({ btoa: value => Buffer.from(value, "binary").toString("base64"), Uint8Array });
  const content = Uint8Array.from([0, 255, 13, 10]);
  state.api = async (url, options) => {
    assert.match(url, /\/imports\?kind=directory$/);
    const manifest = JSON.parse(options.body);
    assert.equal(manifest.files[0].path, "code-review/assets/blob.bin");
    assert.deepEqual(Buffer.from(manifest.files[0].content, "base64"), Buffer.from(content));
    return { valid: true, token: "preview" };
  };
  await state.uploadSkill({ currentTarget: { value: "folder", files: [{ name: "blob.bin", webkitRelativePath: "code-review/assets/blob.bin", size: 4, arrayBuffer: async () => content.buffer }] } }, "directory");
  assert.equal(state.skillImport.token, "preview");
  assert.equal(state.skillBusy, false);
});

test("successful mutations invalidate Files and retain a separate rescan result", async () => {
  const state = view();
  state.fileDirectories = { "": [{ name: ".agents" }] };
  state.api = async () => ({ skill: state.skillDetail, reload: { status: "unavailable", message: "Rescan unavailable" } });
  state.refreshSkills = async () => {};
  await state.runSkillMutation("/save", { method: "POST" }, "Saved.");
  assert.equal(Object.keys(state.fileDirectories).length, 0);
  assert.equal(state.skillStatus, "Saved.");
  assert.equal(state.skillReloadStatus, "Rescan unavailable");
});

test("beforeunload warns only for an unsaved draft or active operation", () => {
  const state = view();
  let prevented = 0;
  const event = { preventDefault() { prevented++; } };
  state.guardSkillUnload(event);
  assert.equal(prevented, 0);
  state.skillBusy = true;
  state.guardSkillUnload(event);
  assert.equal(prevented, 1);
  assert.equal(event.returnValue, "");
});
