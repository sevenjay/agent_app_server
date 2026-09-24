const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { test } = require("node:test");
const vm = require("node:vm");

function composer(globals = {}) {
  const revoked = [];
  const context = {
    window: {}, localStorage: { getItem: () => null }, URLSearchParams,
    URL: { createObjectURL: file => `blob:${file.name}`, revokeObjectURL: url => revoked.push(url) },
    document: { querySelector: () => ({ textContent: "" }) },
    CSS: { escape: value => value },
    ...globals,
  };
  vm.runInNewContext(readFileSync("static/js/codex-console.js", "utf8"), context);
  const view = context.window.codexConsole();
  view.projectKey = "project";
  view.threadId = "thread";
  view.streamReady = true;
  view.prompt = "Inspect attached files";
  view.scrollTimelineToBottom = () => {};
  return { view, revoked };
}

const file = (name, size = 50) => ({ name, size });
const uploaded = (id, name = "test.log") => ({ id, name, mime: "text/plain", size: 50 });

test("selection, drops, and pasted images use one queue; text paste stays native", () => {
  const { view, revoked } = composer();
  view.addComposerFiles([file("same.png")]);
  let prevented = 0;
  view.pasteComposerFiles({ clipboardData: { items: [{ kind: "string", type: "text/plain" }] }, preventDefault: () => prevented++ });
  assert.equal(prevented, 0);
  view.pasteComposerFiles({ clipboardData: { items: [{ kind: "file", type: "image/png", getAsFile: () => file("same.png") }] }, preventDefault: () => prevented++ });
  view.dropComposerFiles({ dataTransfer: { files: [file("trace.log")] }, preventDefault: () => prevented++ });
  assert.equal(prevented, 2);
  assert.equal(view.composerAttachments.length, 3);
  assert.equal(new Set(view.composerAttachments.map(item => item.key)).size, 3);
  view.clearAttachmentDraft();
  assert.equal(revoked.length, 2);
});

test("client validates formats and attachment limits without losing the queue", () => {
  const { view } = composer();
  view.addComposerFiles([file("test.log")]);
  for (const files of [[file("x.pdf")], [file("x.log", 50 * 1024 * 1024 + 1)], Array.from({ length: 5 }, () => file("a.txt"))]) {
    view.addComposerFiles(files);
    assert.equal(view.composerAttachments.length, 1);
    assert.ok(view.errorMessage);
  }
  view.clearAttachmentDraft();
  view.addComposerFiles(Array.from({ length: 5 }, () => file("a.txt", 50 * 1024 * 1024)));
  assert.equal(view.composerAttachments.length, 5);
  assert.equal(view.errorMessage, "");
});

test("partial upload failure sends no message, then retries only the failed file", async () => {
  const { view } = composer();
  view.addComposerFiles([file("first.log"), file("second.log")]);
  const calls = [];
  let fail = true;
  view.api = async (url, options) => {
    calls.push({ url, options });
    if (url.includes("name=first.log")) return uploaded("a".repeat(32), "first.log");
    if (url.includes("name=second.log")) {
      if (fail) throw new Error("upload failed");
      return uploaded("b".repeat(32), "second.log");
    }
    return { turn_id: "turn", journal_cursor: 9 };
  };
  await view.submitPrompt();
  assert.equal(calls.length, 2);
  assert.equal(view.prompt, "Inspect attached files");
  assert.equal(view.composerAttachments[0].id, "a".repeat(32));
  assert.equal(view.composerAttachments[1].status, "Upload failed");
  assert.equal(view.attachmentSubmissionUncertain, false);
  fail = false;
  await view.submitPrompt();
  assert.equal(calls.filter(call => call.url.includes("first.log")).length, 1);
  const submitted = JSON.parse(calls.at(-1).options.body);
  assert.deepEqual(submitted.attachment_ids, ["a".repeat(32), "b".repeat(32)]);
  assert.equal(view.composerAttachments.length, 0);
  assert.equal(view.prompt, "");
  assert.equal(view.liveTimelineItems[0].attachments.length, 2);
  assert.equal(view.liveTimelineItems[0].attachments[0].pending, undefined);
});

test("new session sends initial_attachment_ids and only clears after success", async () => {
  const { view, revoked } = composer();
  view.threadId = "";
  view.draftSession = true;
  view.addComposerFiles([file("x.png")]);
  let selected;
  view.selectThread = async id => { selected = id; };
  view.api = async (url, options) => {
    if (url.includes("conversation-attachments")) return { ...uploaded("a".repeat(32), "x.png"), mime: "image/png" };
    const body = JSON.parse(options.body);
    assert.equal(url, "/api/codex/threads");
    assert.equal(body.initial_prompt, view.prompt);
    assert.deepEqual(body.initial_attachment_ids, ["a".repeat(32)]);
    assert.equal(view.composerAttachments.length, 1);
    return { id: "new-thread", accepted: true };
  };
  await view.submitPrompt();
  assert.equal(selected, "new-thread");
  assert.deepEqual(revoked, ["blob:x.png"]);
});

test("steer and goal attachments are blocked without upload or losing drafts", async () => {
  for (const mode of ["steer", "goal", "active_goal"]) {
    const { view } = composer();
    view.addComposerFiles([file("test.log")]);
    if (mode === "steer") view.active = true;
    if (mode === "goal") view.prompt = "/goal objective";
    if (mode === "active_goal") view.liveGoal = { status: "active" };
    const text = view.prompt;
    view.api = async () => assert.fail("Must not submit");
    await view.submitPrompt();
    assert.equal(view.prompt, text);
    assert.equal(view.composerAttachments.length, 1);
    assert.match(view.errorMessage, /new turn/);
  }
});

test("state changes during upload do not change submission into steer", async () => {
  const { view } = composer();
  view.addComposerFiles([file("test.log")]);
  let calls = 0;
  view.api = async () => { calls++; view.active = true; return uploaded("a".repeat(32)); };
  await view.submitPrompt();
  assert.equal(calls, 1);
  assert.equal(view.prompt, "Inspect attached files");
  assert.equal(view.composerAttachments.length, 1);
});

test("ambiguous submit failure retains draft and blocks accidental repeat", async () => {
  const { view } = composer();
  view.addComposerFiles([file("test.log")]);
  let submissions = 0;
  view.api = async url => {
    if (url.includes("conversation-attachments")) return uploaded("a".repeat(32));
    submissions++;
    throw new Error("connection lost");
  };
  await view.submitPrompt();
  await view.submitPrompt();
  assert.equal(submissions, 1);
  assert.equal(view.attachmentSubmissionUncertain, true);
  assert.equal(view.composerAttachments.length, 1);
  assert.equal(view.prompt, "Inspect attached files");
});

test("pending DELETE uses original project and draft cleanup releases previews", async () => {
  const { view, revoked } = composer();
  view.addComposerFiles([file("x.png")]);
  view.composerAttachments[0].id = "a".repeat(32);
  view.projectKey = "different";
  let deleted;
  view.api = async url => { deleted = url; };
  await view.removeComposerAttachment(view.composerAttachments[0].key);
  assert.match(deleted, /projects\/project\/conversation-attachments/);
  assert.deepEqual(revoked, ["blob:x.png"]);
});

test("manual and SDK user events reconcile with the optimistic attachment message", () => {
  const { view } = composer();
  view.appendOptimisticUserMessage("look", "", [{ ...uploaded("a".repeat(32)), available: true }]);
  for (const data of [
    { attachments_unavailable: true },
    { attachments: [{ ...uploaded("a".repeat(32)), delivery: "file_reference" }] },
  ]) view.recordCompletedUserMessage({ turn_id: "turn", sequence: 2, data: { item_type: "userMessage", text: "look", ...data } });
  assert.equal(view.liveTimelineItems.length, 1);
  assert.equal(view.liveTimelineItems[0].attachments.length, 1);
  assert.equal(view.liveTimelineItems[0].turnId, "turn");
});

function attachmentSnapshot({ cursor = 10, threadId = "thread", turnId = "turn", text = "總結下這個檔案內容", cards = true } = {}) {
  return {
    dataset: { threadId, journalCursor: String(cursor) },
    querySelectorAll: () => [{
      dataset: {
        userMessage: JSON.stringify({
          turnId, itemIds: ["console-user-original"],
          attachmentIds: cards ? ["a".repeat(32)] : [], attachmentsUnavailable: !cards,
        }),
      },
      querySelectorAll: () => [{ dataset: { markdown: text } }],
    }],
  };
}

function sdkAttachmentEcho({ sequence = 11, turnId = "turn", text = "總結下這個檔案內容", cards = false } = {}) {
  return {
    thread_id: "thread", turn_id: turnId, sequence,
    data: {
      item_type: "userMessage", item_id: "sdk-user-echo", text,
      attachments: cards ? [{ ...uploaded("a".repeat(32)), delivery: "file_reference" }] : [],
      attachments_unavailable: !cards,
    },
  };
}

test("SDK echo after a new-session snapshot does not append a duplicate prompt", () => {
  const snapshot = attachmentSnapshot();
  const { view } = composer({ document: { querySelector: () => snapshot } });
  view.convergeTimelineSnapshot();
  view.recordCompletedUserMessage(sdkAttachmentEcho());
  assert.equal(view.liveTimelineItems.length, 0);
});

test("snapshot removes an echo received during refresh even when its sequence is newer", () => {
  const snapshot = attachmentSnapshot();
  const { view } = composer({ document: { querySelector: () => snapshot } });
  view.recordCompletedUserMessage(sdkAttachmentEcho());
  assert.equal(view.liveTimelineItems.length, 1);
  view.convergeTimelineSnapshot();
  assert.equal(view.liveTimelineItems.length, 0);
  view.recordCompletedUserMessage(sdkAttachmentEcho({ sequence: 12 }));
  assert.equal(view.liveTimelineItems.length, 0);
});

test("snapshot reconciliation preserves the same prompt in another turn and a text-only steer", () => {
  const snapshot = attachmentSnapshot();
  const { view } = composer({ document: { querySelector: () => snapshot } });
  view.appendOptimisticUserMessage("總結下這個檔案內容", "turn");
  const steerKey = view.liveTimelineItems[0].key;
  view.convergeTimelineSnapshot();
  view.recordCompletedUserMessage(sdkAttachmentEcho());
  assert.equal(view.liveTimelineItems.length, 1);
  assert.equal(view.liveTimelineItems[0].key, steerKey);
  assert.equal(view.liveTimelineItems[0].sequence, undefined);
  view.recordCompletedUserMessage(sdkAttachmentEcho({ turnId: "next-turn" }));
  assert.equal(view.liveTimelineItems.length, 2);
  assert.equal(view.liveTimelineItems[1].turnId, "next-turn");
});

test("a snapshot with unavailable attachments refreshes when their metadata arrives", () => {
  const snapshot = attachmentSnapshot({ cards: false });
  const { view } = composer({ document: { querySelector: () => snapshot } });
  let refreshes = 0;
  view.refreshThreadAndList = async () => { refreshes++; };
  view.convergeTimelineSnapshot();
  view.recordCompletedUserMessage(sdkAttachmentEcho({ cards: true }));
  assert.equal(view.liveTimelineItems.length, 0);
  assert.equal(refreshes, 1);
});

test("SDK attachment echoes do not merge into an optimistic text-only steer", () => {
  const { view } = composer();
  view.appendOptimisticUserMessage("總結下這個檔案內容", "turn");
  view.recordCompletedUserMessage(sdkAttachmentEcho());
  assert.equal(view.liveTimelineItems.length, 2);
  assert.equal(view.liveTimelineItems[0].sequence, undefined);
});

test("an echo reconciles an unbound optimistic message after its snapshot arrived", () => {
  const snapshot = attachmentSnapshot();
  const { view } = composer({ document: { querySelector: () => snapshot } });
  view.appendOptimisticUserMessage("總結下這個檔案內容", "", [{ ...uploaded("a".repeat(32)), pending: true }]);
  view.convergeTimelineSnapshot();
  view.recordCompletedUserMessage(sdkAttachmentEcho());
  assert.equal(view.liveTimelineItems.length, 0);
});

test("HTTP acknowledgement reconciles an optimistic message when the snapshot arrived first", () => {
  const snapshot = attachmentSnapshot();
  const { view } = composer({ document: { querySelector: () => snapshot } });
  const key = view.appendOptimisticUserMessage("總結下這個檔案內容", "", [{ ...uploaded("a".repeat(32)), pending: true }]);
  view.convergeTimelineSnapshot();
  view.bindLiveMessageToTurn(key, "turn", 11);
  assert.equal(view.liveTimelineItems.length, 0);
});

test("snapshot matching is scoped to the selected thread and attachment IDs", () => {
  const snapshot = attachmentSnapshot();
  const { view } = composer({ document: { querySelector: () => snapshot } });
  view.convergeTimelineSnapshot();
  const differentFiles = sdkAttachmentEcho({ cards: true });
  differentFiles.data.attachments[0].id = "b".repeat(32);
  view.recordCompletedUserMessage(differentFiles);
  assert.equal(view.liveTimelineItems.length, 1);
  view.threadId = "different-thread";
  view.liveTimelineItems = [];
  view.recordCompletedUserMessage({ ...sdkAttachmentEcho(), thread_id: "different-thread" });
  assert.equal(view.liveTimelineItems.length, 1);
});

test("metadata received during a snapshot refresh updates the cards without duplicating text", () => {
  const snapshot = attachmentSnapshot({ cards: false });
  const { view } = composer({ document: { querySelector: () => snapshot } });
  let refreshes = 0;
  view.refreshThreadAndList = async () => { refreshes++; };
  view.recordCompletedUserMessage(sdkAttachmentEcho({ cards: true }));
  view.convergeTimelineSnapshot();
  assert.equal(view.liveTimelineItems.length, 0);
  assert.equal(refreshes, 1);
});

test("IME composition Enter does not submit", () => {
  const html = readFileSync("templates/_composer.html", "utf8");
  const directive = html.match(/@keydown.enter="([^"]+)"/)[1];
  let submits = 0;
  for (const event of [{ isComposing: true }, { keyCode: 229 }, { shiftKey: true }]) {
    vm.runInNewContext(directive, { $event: { preventDefault: () => assert.fail("IME prevented"), ...event }, submitPrompt: () => submits++ });
  }
  assert.equal(submits, 0);
});
