const WORKING_PLACEHOLDER_RE = /^Working$/iu;
const LONG_ACTIVITY_ITEM_CHARS = 180;

export function isReasoningPlaceholder(text) {
  return WORKING_PLACEHOLDER_RE.test(String(text || "").trim());
}

export function activityItemCollapsible(text) {
  const value = String(text || "");
  return (
    value.length > LONG_ACTIVITY_ITEM_CHARS || value.split("\n").length > 2
  );
}

export function activityItemSummary(text, index = 0) {
  const value = String(text || "").trim();
  const number = Math.max(0, Number(index) || 0) + 1;
  const exit = value.match(/\s+·\s+exit\s+(-?\d+)$/iu)?.[1];
  if (/^Ran\b/iu.test(value)) {
    return `Command ${number}${exit === undefined ? "" : ` · exit ${exit}`}`;
  }
  if (/^Running\b/iu.test(value)) return `Running command ${number}`;
  if (/^(?:Searched|Searching the web)\b/iu.test(value))
    return `Search ${number}`;
  if (/^(?:Edited|Editing)\b/iu.test(value)) return `File change ${number}`;
  if (/^(?:Called|Calling)\b/iu.test(value)) return `Tool call ${number}`;
  return value.length <= 96 ? value : `${value.slice(0, 95)}…`;
}

export function activityGroupSummary(items) {
  const counts = { commands: 0, edits: 0, searches: 0, tools: 0, other: 0 };
  for (const item of Array.isArray(items) ? items : []) {
    const text = String(item?.text || "").trim();
    if (/^(?:Ran|Running)\b/iu.test(text)) counts.commands += 1;
    else if (/^(?:Edited|Editing)\b/iu.test(text)) counts.edits += 1;
    else if (/^(?:Searched|Searching the web)\b/iu.test(text)) counts.searches += 1;
    else if (/^(?:Called|Calling)\b/iu.test(text)) counts.tools += 1;
    else counts.other += 1;
  }
  const labels = [
    ["command", "commands", counts.commands],
    ["edit", "edits", counts.edits],
    ["search", "searches", counts.searches],
    ["tool", "tools", counts.tools],
    ["step", "steps", counts.other],
  ]
    .filter(([_one, _many, count]) => count > 0)
    .map(([one, many, count]) => `${count} ${count === 1 ? one : many}`);
  return labels.length ? `Activity · ${labels.join(" · ")}` : "Activity";
}

export function groupActivityBlocks(value) {
  const source = Array.isArray(value) ? value : [];
  const result = [];
  const groups = new Map();

  for (const item of source) {
    const kind = String(item?.kind || "");
    const text = String(item?.text || "").trim();
    if (kind !== "process") {
      result.push(item);
      continue;
    }
    if (!text || isReasoningPlaceholder(text)) continue;

    const turnKey = String(item?.turnKey || "");
    if (!turnKey) {
      result.push(item);
      continue;
    }

    let group = groups.get(turnKey);
    if (!group) {
      group = {
        id: `activity-${turnKey}`,
        turnKey,
        kind: "activity",
        role: "process",
        text: "",
        items: [],
        keyHint: `activity:${turnKey}`,
        mutable: true,
        final: true,
        revision: 0,
      };
      groups.set(turnKey, group);
      result.push(group);
    }
    group.items.push({
      id: String(item?.id || ""),
      text,
      final: item?.final !== false,
    });
    group.final = group.final && item?.final !== false;
    group.revision = Math.max(group.revision, Number(item?.revision || 0));
  }

  for (const group of groups.values()) {
    group.text = group.items.map((item) => item.text).join("\n\n");
    group.summary = activityGroupSummary(group.items);
  }
  return result;
}
