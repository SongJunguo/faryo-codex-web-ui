import { h, render } from "preact";
import { useLayoutEffect, useState } from "preact/hooks";

export interface StatusShellView {
  contextText: string;
  contextTitle: string;
  quotaText: string;
  quotaTitle: string;
  quotaPercent: number;
  quotaWeekPercent: number;
  modelText: string;
  modelTitle: string;
  fastVisible: boolean;
  fastActive: boolean;
  fastDisabled: boolean;
  fastText: string;
  fastTitle: string;
  subtitleTitle: string;
  goalVisible: boolean;
  goalText: string;
  goalTitle: string;
  goalTone: string;
  gitText: string;
  gitTitle: string;
  gitState: string;
}

export interface StatusShellController {
  update(values: Partial<StatusShellView>): void;
  destroy(): void;
}

interface StatusShellOptions {
  onGoalClick(): void;
  onFastToggle(): void | Promise<void>;
}

const INITIAL_STATUS: StatusShellView = {
  contextText: "Ctx --",
  contextTitle: "Unavailable",
  quotaText: "Week --",
  quotaTitle: "Quota unknown",
  quotaPercent: 0,
  quotaWeekPercent: 0,
  modelText: "Connecting...",
  modelTitle: "Connecting",
  fastVisible: false,
  fastActive: false,
  fastDisabled: true,
  fastText: "Default",
  fastTitle: "Session speed is unavailable",
  subtitleTitle: "Connecting",
  goalVisible: false,
  goalText: "",
  goalTitle: "",
  goalTone: "none",
  gitText: "Git …",
  gitTitle: "Loading Git status",
  gitState: "muted",
};

function createStatusStore() {
  let value = INITIAL_STATUS;
  const listeners = new Set<(value: StatusShellView) => void>();
  return {
    get: () => value,
    set(next: Partial<StatusShellView>) {
      value = { ...value, ...next };
      for (const listener of listeners) listener(value);
    },
    subscribe(listener: (value: StatusShellView) => void) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
  };
}

type StatusStore = ReturnType<typeof createStatusStore>;

function StatusShell({
  store,
  options,
}: {
  store: StatusStore;
  options: StatusShellOptions;
}) {
  const [view, setView] = useState(store.get());
  const [fastBusy, setFastBusy] = useState(false);
  useLayoutEffect(() => store.subscribe(setView), [store]);
  const quotaStyle = `--quota-pct:${view.quotaPercent};--quota-week-pct:${view.quotaWeekPercent}`;
  return (
    <div
      class="meta-row"
      tabIndex={0}
      aria-label="Session status; scroll horizontally for more"
    >
      <div id="subTitle" class="subtitle meta-main" title={view.subtitleTitle}>
        <span id="ctxText" title={view.contextTitle}>
          {view.contextText}
        </span>
        <button
          id="quotaTop"
          class="quota-status quota-top"
          type="button"
          aria-label={view.quotaTitle}
          title={view.quotaTitle}
          style={quotaStyle}
        >
          <span id="quotaText" class="quota-label">
            {view.quotaText}
          </span>
          <span class="quota-bars" aria-hidden="true">
            <span class="quota-bar">
              <span id="quotaFill" />
            </span>
            <span class="quota-bar week-bar">
              <span id="quotaWeekFill" />
            </span>
          </span>
        </button>
        <span id="modelText" title={view.modelTitle}>
          {view.modelText}
        </span>
        <button
          id="fastToggle"
          class={`speed-toggle${view.fastActive ? " active" : ""}`}
          type="button"
          aria-label={view.fastTitle}
          aria-pressed={view.fastActive}
          aria-busy={fastBusy}
          title={view.fastTitle}
          disabled={view.fastDisabled || fastBusy}
          hidden={!view.fastVisible}
          onClick={async () => {
            if (fastBusy || view.fastDisabled) return;
            setFastBusy(true);
            try {
              await options.onFastToggle();
            } finally {
              setFastBusy(false);
            }
          }}
        >
          {view.fastText}
        </button>
      </div>
      <div class="meta-pills">
        <button
          id="goalPill"
          class={`pill goal-pill${view.goalVisible ? ` goal-${view.goalTone}` : ""}`}
          type="button"
          aria-live="polite"
          aria-controls="detailsPanel"
          aria-expanded="false"
          aria-label={view.goalTitle || undefined}
          title={view.goalTitle || undefined}
          hidden={!view.goalVisible}
          onClick={options.onGoalClick}
        >
          {view.goalText}
        </button>
        <span
          id="phasePill"
          class={`pill git-pill ${view.gitState || "muted"}`}
          title={view.gitTitle}
        >
          {view.gitText}
        </span>
      </div>
    </div>
  );
}

export function mountStatusShell(
  container: HTMLElement,
  options: StatusShellOptions,
): StatusShellController {
  const store = createStatusStore();
  render(<StatusShell store={store} options={options} />, container);
  return {
    update(values) {
      store.set(values);
    },
    destroy() {
      render(null, container);
    },
  };
}
