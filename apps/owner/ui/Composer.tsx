import { h } from "preact";
import type { JSX } from "preact";

export function Composer({
  sendVisible,
  plusVisible,
  onKeyDown,
}: {
  sendVisible: boolean;
  plusVisible: boolean;
  onKeyDown(event: JSX.TargetedKeyboardEvent<HTMLTextAreaElement>): void;
}) {
  return (
    <div class="prompt-shell">
      <div
        id="petControl"
        class="pet-control pet-offline"
        aria-label="Faryo offline; tap to interrupt"
      />
      <textarea
        id="promptInput"
        placeholder="Ask Faryo"
        rows={1}
        onKeyDown={onKeyDown}
      />
      <div class="composer-actions" aria-label="Input actions">
        <button
          id="dockPlusBtn"
          class={`dock-plus${plusVisible ? "" : " hidden"}`}
          type="button"
          aria-label="Open input tools"
          aria-expanded="false"
          aria-controls="dockMenu"
        >
          +
        </button>
        <button
          id="sendBtn"
          class={`dock-send${sendVisible ? "" : " hidden"}`}
          type="button"
          aria-label="Send"
        >
          ↑
        </button>
      </div>
    </div>
  );
}
