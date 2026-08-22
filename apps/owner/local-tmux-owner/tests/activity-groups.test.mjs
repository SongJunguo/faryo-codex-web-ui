import assert from "node:assert/strict";
import test from "node:test";

import {
  activityItemCollapsible,
  activityGroupSummary,
  activityItemSummary,
  groupActivityBlocks,
  isReasoningPlaceholder,
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
