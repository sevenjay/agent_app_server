const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { test } = require("node:test");
const vm = require("node:vm");

function consoleView(globals = {}) {
  const context = { window: {}, localStorage: { getItem: () => null }, URLSearchParams, ...globals };
  vm.runInNewContext(readFileSync("static/js/codex-console.js", "utf8"), context);
  return context.window.codexConsole();
}

test("row actions target the hovered entry without changing the current selection", async () => {
  const html = readFileSync("static/index.html", "utf8");
  const actions = html.split('class="file-tree-actions"')[1].split('</div>')[0];
  const handlers = [...actions.matchAll(/@click.stop="([^"]+)"/g)].map(match => match[1]);
  const calls = [];
  const view = consoleView();
  view.projectKey = "project";
  view.fileSelectedPath = "selected.txt";
  const entry = { path: "hovered/folder", name: "folder", type: "directory" };
  for (const method of ["downloadProjectFile", "renameProjectFile", "deleteProjectFile", "copyProjectFilePath"]) {
    view[method] = value => calls.push([method, value.path]);
  }
  for (const handler of handlers) vm.runInNewContext(`with (view) { ${handler} }`, { view, entry });
  assert.deepEqual(calls, [
    ["downloadProjectFile", "hovered/folder"], ["renameProjectFile", "hovered/folder"], ["deleteProjectFile", "hovered/folder"],
    ["copyProjectFilePath", "hovered/folder"],
  ]);
  assert.equal(view.fileSelectedPath, "selected.txt");
  assert.equal(view.fileInfoPath, "hovered/folder");
  const infoHandler = handlers.find(handler => handler.includes("fileInfoPath ="));
  vm.runInNewContext(`with (view) { ${infoHandler} }`, { view, entry });
  assert.equal(view.fileInfoPath, "");
  assert.ok(actions.indexOf('title="Preview"') < actions.indexOf("'Download'"));
  assert.ok(actions.indexOf("'Download'") < actions.indexOf('title="Rename"'));
  assert.ok(actions.indexOf('title="Rename"') < actions.indexOf('title="Delete"'));
  assert.ok(actions.indexOf('title="Delete"') < actions.indexOf('title="Info"'));
  assert.ok(actions.indexOf('title="Info"') < actions.indexOf('title="Copy path"'));
  assert.match(actions, /target="_blank"/);
  assert.match(actions, /rel="noopener noreferrer"/);
});

test("preview URLs preserve special names and hidden folder preference", () => {
  const view = consoleView();
  view.projectKey = "files_project";
  const url = view.projectFilesUrl("/preview", { path: "中文 & notes/#1?.txt", show_hidden: true });
  const params = new URLSearchParams(url.split("?")[1]);
  assert.equal(params.get("path"), "中文 & notes/#1?.txt");
  assert.equal(params.get("show_hidden"), "true");
});

test("modified file and folder statuses link to their scoped diff in a new tab", () => {
  const view = consoleView();
  view.projectKey = "files_project";
  const path = "中文 & notes/#1?.txt";
  for (const type of ["file", "directory"]) {
    const url = view.projectFileDiffUrl({ type, path, git_status: "modified" });
    assert.ok(url.startsWith("/api/projects/files_project/files/diff?"));
    assert.equal(new URLSearchParams(url.split("?")[1]).get("path"), path);
  }
  for (const git_status of [null, "ignored", "untracked"]) {
    assert.equal(view.projectFileDiffUrl({ path, git_status }), null);
  }
  const html = readFileSync("static/index.html", "utf8");
  const badge = html.split('class="file-git-status"')[1].split('</a>')[0];
  assert.match(badge, /:href="projectFileDiffUrl\(entry\)"/);
  assert.match(badge, /target="_blank"/);
  assert.match(badge, /rel="noopener noreferrer"/);
  assert.match(badge, /@click.stop/);
});

test("folder downloads save a ZIP without requiring a selected row", async () => {
  const link = { click() {}, remove() {} };
  const requests = [];
  const view = consoleView({
    fetch: async url => { requests.push(url); return { ok: true, blob: async () => ({}) }; },
    URL: { createObjectURL: () => "blob:archive", revokeObjectURL() {} },
    document: { createElement: () => link, body: { append() {} } },
  });
  view.projectKey = "files_project";
  await view.downloadProjectFile({ path: "folder", name: "folder", type: "directory" });
  assert.equal(link.download, "folder.zip");
  assert.equal(requests[0], "/api/projects/files_project/files/download?path=folder");
  assert.equal(view.fileOperationBusy, false);
  assert.equal(view.fileError, "");
});

test("Git markers omit clean entries and describe staged or nested changes", () => {
  const view = consoleView();
  assert.equal(view.fileGitMarker({ git_status: null }), "");
  assert.equal(view.fileGitLabel({ git_status: null }), "");
  assert.equal(view.fileGitLabel({ type: "file", git_status: "modified", git_status_code: "M " }), "Git: Modified (staged)");
  assert.equal(view.fileGitLabel({ type: "file", git_status: "modified", git_status_code: " M" }), "Git: Modified (unstaged)");
  assert.equal(view.fileGitLabel({ type: "file", git_status: "modified", git_status_code: "MM" }), "Git: Modified (staged and unstaged)");
  assert.equal(view.fileGitLabel({ type: "directory", git_status: "deleted" }), "Git: Deleted contents");
  assert.equal(view.fileGitLabel({ type: "directory", git_status: "ignored" }), "Git: Ignored");
  assert.equal(view.fileGitLabel({ type: "file", git_status: "ignored", git_status_code: "!!" }), "Git: Ignored");
  assert.equal(view.fileGitMarker({ git_status: "conflicted" }), "U");
  assert.equal(view.formatFileSize(0), "0 B");
  assert.equal(view.formatFileSize(1024), "1.0 KiB");
  assert.equal(view.formatFileSize(null), "—");
});

test("uploading into a folder refreshes its Git badge in the parent listing", async () => {
  const view = consoleView({ TextEncoder });
  view.projectKey = "project";
  view.fileCurrentPath = "src";
  view.fileExpandedPaths = ["src"];
  view.fileDirectories = { "": [{ name: "src", path: "src", type: "directory", git_status: null }], src: [] };
  let uploaded = false;
  view.api = async (url, options = {}) => {
    if (options.method === "POST") { uploaded = true; return {}; }
    assert.ok(uploaded);
    const path = new URLSearchParams(url.split("?")[1]).get("path");
    return { data: path === "src"
      ? [{ name: "new.txt", path: "src/new.txt", type: "file", git_status: "untracked" }]
      : [{ name: "src", path: "src", type: "directory", git_status: "untracked" }],
    };
  };
  await view.uploadProjectFiles({ currentTarget: { value: "file", files: [{ name: "new.txt", type: "text/plain" }] } });
  assert.equal(view.visibleFileEntries[0].git_status, "untracked");
  assert.equal(view.visibleFileEntries[1].path, "src/new.txt");
  assert.equal(view.fileCurrentPath, "src");
  assert.equal(view.fileError, "");
});
