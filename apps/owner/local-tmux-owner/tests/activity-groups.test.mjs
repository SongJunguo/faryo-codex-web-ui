import assert from "node:assert/strict";
import test from "node:test";

import {
  activityItemCollapsible,
  activityGroupSummary,
  activityItemSummary,
  activityStatus,
  groupActivityBlocks,
  isReasoningPlaceholder,
  mergeCommandEvents,
} from "../static/owner/activity-groups.mjs";

test("reasoning placeholders disappear and each turn gets one activity card", () => {
  const blocks = [
    { id: "q1", turnKey: "turn-1", kind: "user", text: "Question" },
    ...Array.from({ length: 50 }, (_, index) => ({
      id: `reason-${index}`,
      turnKey: "turn-1",
      kind: "process",
      text: "Working",
    })),
    {
      id: "run-1",
      turnKey: "turn-1",
      kind: "process",
      text: `Ran ${"x".repeat(900)} · exit 0`,
    },
    {
      id: "search-1",
      turnKey: "turn-1",
      kind: "process",
      text: "Searched current docs",
    },
    { id: "a1", turnKey: "turn-1", kind: "output", text: "Answer" },
    { id: "q2", turnKey: "turn-2", kind: "user", text: "Follow-up" },
    { id: "edit-2", turnKey: "turn-2", kind: "process", text: "Edited app.py" },
    { id: "a2", turnKey: "turn-2", kind: "output", text: "Done" },
  ];

  const grouped = groupActivityBlocks(blocks);
  assert.equal(grouped.filter((item) => item.kind === "activity").length, 2);
  assert.equal(
    grouped.some((item) => item.text === "Working"),
    false,
  );
  assert.deepEqual(
    grouped.map((item) => item.kind),
    ["user", "activity", "output", "user", "activity", "output"],
  );
  assert.equal(grouped[1].summary, "Activity · 1 command · 1 search");
  assert.equal(grouped[1].items.length, 2);
  assert.equal(grouped[4].summary, "Activity · 1 edit");
});

test("collapsed activity titles expose what history contains", () => {
  assert.equal(
    activityGroupSummary([
      { text: "Ran git status" },
      { text: "Ran npm test" },
      { text: "Edited app.py" },
      { text: "Searched current docs" },
    ]),
    "Activity · 2 commands · 1 edit · 1 search",
  );
});

test("long command details default to a concise label", () => {
  const command = `Ran ${"long-command ".repeat(40)}· exit 7`;
  assert.equal(isReasoningPlaceholder("Working"), true);
  assert.equal(
    isReasoningPlaceholder("Working with a background agent"),
    false,
  );
  assert.equal(activityItemCollapsible(command), true);
  assert.equal(activityItemCollapsible("Edited app.py"), false);
  assert.equal(activityItemSummary(command, 2), "Command 3 · exit 7");
});

test("unscoped process notices remain visible instead of being misgrouped", () => {
  const grouped = groupActivityBlocks([
    { id: "gap", kind: "process", text: "… earlier turns not loaded …" },
  ]);
  assert.deepEqual(grouped, [
    { id: "gap", kind: "process", text: "… earlier turns not loaded …" },
  ]);
});

test("typed activity drives labels, attention and details without text regexes", () => {
  const grouped = groupActivityBlocks([
    { id: "q", turnKey: "turn-a", kind: "user", text: "Question" },
    {
      id: "tool-a",
      turnKey: "turn-a",
      kind: "process",
      text: "localized text that has no legacy prefix",
      final: true,
      activity: {
        type: "mcp",
        status: "failed",
        title: "reference.lookup",
        detailAvailable: true,
      },
    },
    { id: "a", turnKey: "turn-a", kind: "output", text: "Answer" },
  ]);
  const activity = grouped[1];
  assert.equal(activity.summary, "Activity · 1 MCP call · 1 needs attention");
  assert.equal(activity.openByDefault, true);
  assert.equal(activityItemCollapsible(activity.items[0]), true);
  assert.equal(activityItemSummary(activity.items[0]), "reference.lookup · failed");
  assert.equal(activityStatus(activity.items[0]), "failed");
});

test("command lifecycle rows keep stable ids and anchor after their turn", () => {
  const blocks = [
    { id: "q", turnKey: "turn-a", kind: "user", text: "Question" },
    { id: "a", turnKey: "turn-a", kind: "output", text: "Answer" },
  ];
  const merged = mergeCommandEvents(blocks, [{
    id: "cmd_abcdefghijklmnop",
    name: "/rename",
    label: "Conversation title",
    summary: "Renamed conversation to Anonymous",
    status: "completed",
    anchorKey: "turn-a",
    startedAt: 10,
    completedAt: 11,
  }]);
  assert.deepEqual(merged.map((item) => item.kind), ["user", "output", "command"]);
  assert.equal(merged[2].keyHint, "command:cmd_abcdefghijklmnop");
  assert.equal(merged[2].command.status, "completed");
});

test("commands never stick to the live tail when their history anchor is absent", () => {
  const blocks = [
    { id: "q", turnKey: "turn-new", kind: "user", text: "New question" },
    { id: "a", turnKey: "turn-new", kind: "output", text: "New answer" },
  ];
  const legacy = {
    id: "cmd_legacyabcdefghijkl",
    name: "/model",
    label: "Model",
    summary: "Model selection completed",
    status: "completed",
    anchorKey: "",
    startedAt: 10,
    completedAt: 11,
  };
  const unloaded = {
    ...legacy,
    id: "cmd_unloadedabcdefghij",
    anchorKey: "turn-old",
    startedAt: 12,
  };

  assert.deepEqual(
    mergeCommandEvents(blocks, [legacy]).map((item) => item.kind),
    ["command", "user", "output"],
  );
  assert.deepEqual(
    mergeCommandEvents(blocks, [unloaded]).map((item) => item.kind),
    ["user", "output"],
  );
});
