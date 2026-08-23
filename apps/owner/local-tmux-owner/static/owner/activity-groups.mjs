const WORKING_PLACEHOLDER_RE = /^Working$/iu;
const LONG_ACTIVITY_ITEM_CHARS = 180;
const COMMAND_STATUSES = new Set(["running", "waiting", "completed", "failed"]);

export function isReasoningPlaceholder(text) {
  return WORKING_PLACEHOLDER_RE.test(String(text || "").trim());
}

function legacyActivityType(text) {
  const value = String(text || "").trim();
  if (/^(?:Ran|Running)\b/iu.test(value)) return "command";
  if (/^(?:Edited|Editing)\b/iu.test(value)) return "file_change";
  if (/^(?:Searched|Searching the web)\b/iu.test(value)) return "search";
  if (/^(?:Called|Calling)\b/iu.test(value)) return "tool";
  return "unknown";
}

export function activityType(item) {
  return String(item?.activity?.type || legacyActivityType(item?.text));
}

export function activityStatus(item) {
  const explicit = String(item?.activity?.status || "");
  if (explicit) return explicit;
  const text = String(item?.text || "");
  if (/\b(?:failed|error|exit\s+(?!0\b)-?\d+)\b/iu.test(text)) return "failed";
  if (/^(?:Running|Editing|Calling|Searching)\b/iu.test(text) || item?.final === false) return "running";
  return "completed";
}

export function activityItemCollapsible(value) {
  const item = value && typeof value === "object" ? value : { text: value };
  const text = String(item?.text || "");
  return Boolean(item?.activity?.detailAvailable)
    || text.length > LONG_ACTIVITY_ITEM_CHARS
    || text.split("\n").length > 2;
}

export function activityItemSummary(value, index = 0) {
  const item = value && typeof value === "object" ? value : { text: value };
  const text = String(item?.text || "").trim();
  const number = Math.max(0, Number(index) || 0) + 1;
  const activity = item?.activity;
  const status = activityStatus(item);
  const suffix = status === "failed" ? " · failed"
    : status === "waiting" ? " · waiting"
      : status === "running" ? " · running" : "";
  if (activity && typeof activity === "object") {
    const title = String(activity.title || activity.summary || "").trim();
    return `${title || `Activity ${number}`}${suffix}`;
  }
  const exit = text.match(/\s+·\s+exit\s+(-?\d+)$/iu)?.[1];
  if (/^Ran\b/iu.test(text)) {
    return `Command ${number}${exit === undefined ? "" : ` · exit ${exit}`}`;
  }
  if (/^Running\b/iu.test(text)) return `Running command ${number}`;
  if (/^(?:Searched|Searching the web)\b/iu.test(text)) return `Search ${number}`;
  if (/^(?:Edited|Editing)\b/iu.test(text)) return `File change ${number}`;
  if (/^(?:Called|Calling)\b/iu.test(text)) return `Tool call ${number}`;
  return text.length <= 96 ? text : `${text.slice(0, 95)}…`;
}

export function activityGroupSummary(items) {
  const counts = { command: 0, file_change: 0, search: 0, mcp: 0, tool: 0, other: 0 };
  let attention = 0;
  for (const item of Array.isArray(items) ? items : []) {
    const type = activityType(item);
    if (Object.prototype.hasOwnProperty.call(counts, type)) counts[type] += 1;
    else counts.other += 1;
    if (["failed", "declined", "waiting"].includes(activityStatus(item))) attention += 1;
  }
  const labels = [
    ["command", "commands", counts.command],
    ["edit", "edits", counts.file_change],
    ["search", "searches", counts.search],
    ["MCP call", "MCP calls", counts.mcp],
    ["tool", "tools", counts.tool],
    ["step", "steps", counts.other],
  ]
    .filter(([_one, _many, count]) => count > 0)
    .map(([one, many, count]) => `${count} ${count === 1 ? one : many}`);
  if (attention) labels.push(`${attention} need${attention === 1 ? "s" : ""} attention`);
  return labels.length ? `Activity · ${labels.join(" · ")}` : "Activity";
}

export function groupActivityBlocks(value) {
  const source = Array.isArray(value) ? value : [];
  const result = [];
  const groups = [];
  let activeGroup = null;

  for (const item of source) {
    const kind = String(item?.kind || "");
    const text = String(item?.text || "").trim();
    if (kind !== "process") {
      result.push(item);
      activeGroup = null;
      continue;
    }
    if (!text || isReasoningPlaceholder(text)) continue;

    const turnKey = String(item?.turnKey || "");
    const segmentKey = String(item?.segmentKey || turnKey);
    if (!segmentKey) {
      result.push(item);
      continue;
    }

    if (!activeGroup || activeGroup.segmentKey !== segmentKey) {
      const firstItemId = String(item?.id || groups.length);
      activeGroup = {
        id: `activity-${segmentKey}-${firstItemId}`,
        turnKey,
        segmentKey,
        kind: "activity",
        role: "process",
        text: "",
        items: [],
        keyHint: `activity:${segmentKey}:${firstItemId}`,
        mutable: true,
        final: true,
        revision: 0,
        openByDefault: false,
      };
      groups.push(activeGroup);
      result.push(activeGroup);
    }
    const normalized = {
      id: String(item?.id || ""),
      text,
      final: item?.final !== false,
      activity: item?.activity && typeof item.activity === "object" ? { ...item.activity } : null,
    };
    activeGroup.items.push(normalized);
    activeGroup.final = activeGroup.final && normalized.final;
    activeGroup.revision = Math.max(activeGroup.revision, Number(item?.revision || 0));
    const status = activityStatus(normalized);
    if (["failed", "declined", "waiting", "running"].includes(status)) activeGroup.openByDefault = true;
  }

  for (const group of groups) {
    group.text = group.items
      .map((item) => [item.id, activityType(item), activityStatus(item), item.activity?.title || item.text].join("\u0000"))
      .join("\n");
    group.summary = activityGroupSummary(group.items);
  }
  return result;
}

export function normalizeCommandEvents(value) {
  if (!Array.isArray(value)) return [];
  return value.flatMap((event) => {
    const id = String(event?.id || "");
    const name = String(event?.name || "");
    const status = String(event?.status || "");
    const summary = String(event?.summary || "").trim();
    if (!/^cmd_[A-Za-z0-9_-]{16,80}$/.test(id) || !/^\/[a-z][a-z-]*$/.test(name) || !COMMAND_STATUSES.has(status) || !summary) {
      return [];
    }
    return [{
      id,
      kind: "command",
      role: "system",
      text: summary,
      keyHint: `command:${id}`,
      mutable: status === "running" || status === "waiting",
      final: status === "completed" || status === "failed",
      anchorKey: String(event?.anchorKey || ""),
      command: {
        name,
        label: String(event?.label || name).slice(0, 160),
        status,
        startedAt: Math.max(0, Number(event?.startedAt || 0)),
        completedAt: Math.max(0, Number(event?.completedAt || 0)),
      },
    }];
  }).sort((left, right) => left.command.startedAt - right.command.startedAt);
}

export function mergeCommandEvents(blocks, events) {
  const result = [...(Array.isArray(blocks) ? blocks : [])];
  const commands = normalizeCommandEvents(events);
  for (const command of commands) {
    let position = 0;
    if (command.anchorKey) {
      position = -1;
      for (let index = result.length - 1; index >= 0; index -= 1) {
        const item = result[index];
        const exactItem = String(item?.id || "") === command.anchorKey
          || (Array.isArray(item?.items) && item.items.some((entry) => String(entry?.id || "") === command.anchorKey));
        if (exactItem) {
          position = index + 1;
          break;
        }
      }
      if (position < 0 && command.anchorKey.startsWith("appserver-question-")) {
        for (let index = result.length - 1; index >= 0; index -= 1) {
          if (
            String(result[index]?.segmentKey || "") === command.anchorKey
            || String(result[index]?.questionKey || "") === command.anchorKey
          ) {
            position = index + 1;
            break;
          }
        }
      }
      if (position < 0) {
        const matching = result
          .map((item, index) => ({ item, index }))
          .filter(({ item }) => String(item?.turnKey || "") === command.anchorKey);
        const segments = [...new Set(matching.map(({ item }) => String(item?.segmentKey || "")))];
        if (segments.length > 1) {
          const latestSegment = segments[segments.length - 1];
          position = matching.find(({ item }) => String(item?.segmentKey || "") === latestSegment)?.index ?? -1;
        } else if (matching.length) {
          position = matching[matching.length - 1].index + 1;
        }
      }
      // An anchored command belongs to a history page that may not be loaded.
      // Hiding it until that page arrives preserves chronology; appending it to
      // the live tail would make an old command look permanently current.
      if (position < 0) continue;
    }
    while (
      position < result.length
      && result[position]?.kind === "command"
      && Number(result[position]?.command?.startedAt || 0) <= command.command.startedAt
    ) position += 1;
    result.splice(position, 0, command);
  }
  return result;
}
