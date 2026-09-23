const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { test } = require("node:test");
const vm = require("node:vm");

function consoleView() {
  const context = {
    window: {},
    localStorage: { getItem: () => null },
  };
  vm.runInNewContext(readFileSync("static/js/codex-console.js", "utf8"), context);
  return context.window.codexConsole();
}

function tool(key, type, status = "completed", exitCode = 0, turnId = "turn-1") {
  return { key, kind: "tool", turnId, tool: { type, status, exit_code: exitCode } };
}

test("live blocks group only adjacent tools of the same type and turn", () => {
  const view = consoleView();
  view.liveTimelineItems = [
    tool("one", "commandExecution"),
    tool("two", "commandExecution", "completed", 1),
    { key: "message", kind: "agent", turnId: "turn-1", text: "done" },
    tool("three", "commandExecution"),
    tool("four", "commandExecution", "completed", 0, "turn-2"),
    tool("five", "fileChange"),
    tool("six", "fileChange"),
    tool("seven", "webSearch"),
    tool("eight", "webSearch"),
  ];

  const blocks = view.liveTimelineBlocks;
  assert.deepEqual(JSON.parse(JSON.stringify(blocks.map((block) => [block.key, block.items.length, block.grouped]))), [
    ["one", 2, true],
    ["message", 1, false],
    ["three", 1, false],
    ["four", 1, false],
    ["five", 2, true],
    ["seven", 2, true],
  ]);
  assert.equal(view.toolBlockLabel(blocks[0]), "2 Commands");
  assert.equal(view.toolBlockStatus(blocks[0]), "1 completed · 1 failed");
  assert.equal(view.toolBlockLabel(blocks[4]), "2 File changes");
  assert.equal(blocks[4].files.count, 0);
  assert.equal(view.toolBlockLabel(blocks.at(-1)), "2 Web searches");
});

test("file summaries deduplicate paths and retain all files for the available width", () => {
  const view = consoleView();
  const first = tool("first", "fileChange");
  first.tool.changes = [{ path: "src/app.py" }, "styles\\input.css", { path: "src/app.py" }];
  const second = tool("second", "fileChange");
  second.tool.changes = [{ path: "tests/app.py" }, "README.md", "styles/input.css", null, {}];
  view.liveTimelineItems = [first, second];

  const block = view.liveTimelineBlocks[0];
  assert.equal(block.grouped, true);
  assert.deepEqual(JSON.parse(JSON.stringify(block.files)), {
    count: 4,
    entries: [
      { path: "src/app.py", label: "src/app.py" },
      { path: "styles/input.css", label: "input.css" },
      { path: "tests/app.py", label: "tests/app.py" },
      { path: "README.md", label: "README.md" },
    ],
  });
  second.tool.changes.push("new.py");
  assert.equal(view.liveTimelineBlocks[0].key, "first");
  assert.equal(view.liveTimelineBlocks[0].files.count, 5);
});

test("Expand block controls groups while Expand all controls groups and cards", () => {
  const view = consoleView();
  const cards = [{ open: false }, { open: false }];
  const blocks = [{ open: false }];
  view.collapsibleToolCards = () => cards;
  view.collapsibleToolBlocks = () => blocks;

  view.toggleToolBlocks();
  assert.equal(blocks[0].open, true);
  assert.equal(cards[0].open, false);
  assert.equal(view.allToolBlocksExpanded, true);
  assert.equal(view.allToolCardsExpanded, false);

  view.toggleToolCards();
  assert.equal(cards[0].open, true);
  assert.equal(cards[1].open, true);
  assert.equal(blocks[0].open, true);
  assert.equal(view.allToolCardsExpanded, true);

  view.toggleToolCards();
  assert.equal(cards[0].open, false);
  assert.equal(blocks[0].open, false);
});

test("Expand all also works for file groups without collapsible inner cards", () => {
  const view = consoleView();
  const blocks = [{ open: false }];
  view.collapsibleToolCards = () => [];
  view.collapsibleToolBlocks = () => blocks;
  view.toggleToolCards();
  assert.equal(blocks[0].open, true);
  assert.equal(view.allToolCardsExpanded, true);
  view.toggleToolBlocks();
  assert.equal(blocks[0].open, false);
  assert.equal(view.allToolCardsExpanded, false);
});
