(function initComposerLayout(root, factory) {
  'use strict';

  const api = factory();
  if (root) root.FaryoComposerLayout = api;
  if (typeof module === 'object' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function composerLayoutFactory() {
  'use strict';

  const DEFAULT_PROPERTY = '--faryo-composer-reserve';
  const RESERVE_SELECTOR = '[data-faryo-layout-reserve]';

  function measuredHeight(element) {
    const bounds = element?.getBoundingClientRect?.();
    const height = Number(bounds?.height || 0);
    if (!Number.isFinite(height)) return 0;
    let top = Number(bounds?.top);
    let bottom = Number(bounds?.bottom);
    if (!Number.isFinite(top) || !Number.isFinite(bottom)) return Math.max(0, Math.ceil(height));
    for (const reserved of element.querySelectorAll?.(RESERVE_SELECTOR) || []) {
      const rect = reserved.getBoundingClientRect?.();
      const reservedHeight = Number(rect?.height || 0);
      const reservedTop = Number(rect?.top);
      const reservedBottom = Number(rect?.bottom);
      if (!Number.isFinite(reservedHeight) || reservedHeight <= 0
        || !Number.isFinite(reservedTop) || !Number.isFinite(reservedBottom)) continue;
      top = Math.min(top, reservedTop);
      bottom = Math.max(bottom, reservedBottom);
    }
    return Math.max(0, Math.ceil(Math.max(height, bottom - top)));
  }

  function createComposerLayout(view, options = {}) {
    if (!view?.document) throw new TypeError('composer layout requires a window');
    const rootElement = options.root || view.document.documentElement;
    const footer = options.footer || view.document.querySelector?.('footer');
    if (!rootElement?.style || !footer) throw new TypeError('composer layout requires a root and footer');

    const property = options.property || DEFAULT_PROPERTY;
    const requestFrame = view.requestAnimationFrame || ((callback) => view.setTimeout(callback, 0));
    const cancelFrame = view.cancelAnimationFrame || view.clearTimeout;
    const ResizeObserverCtor = options.ResizeObserver || view.ResizeObserver;
    const MutationObserverCtor = options.MutationObserver || view.MutationObserver;
    let destroyed = false;
    let frame = 0;
    let height = -1;

    const measure = (force = false) => {
      frame = 0;
      if (destroyed) return { height: Math.max(0, height), changed: false, tailPinned: false };
      const nextHeight = measuredHeight(footer);
      const changed = force || nextHeight !== height;
      if (!changed) return { height: nextHeight, changed: false, tailPinned: false };
      const previousHeight = Math.max(0, height);
      const tailPinned = Boolean(options.isTailPinned?.());
      height = nextHeight;
      rootElement.style.setProperty(property, `${nextHeight}px`);
      if (rootElement.dataset) {
        rootElement.dataset.faryoComposerLayout = 'transparent-overlay';
        rootElement.dataset.faryoComposerReserve = String(nextHeight);
      }
      const snapshot = { height: nextHeight, previousHeight, changed: true, tailPinned };
      options.onChange?.(snapshot);
      return snapshot;
    };

    const schedule = () => {
      if (destroyed || frame) return;
      frame = requestFrame.call(view, () => measure());
    };
    const resizeObserver = typeof ResizeObserverCtor === 'function'
      ? new ResizeObserverCtor(schedule)
      : null;
    resizeObserver?.observe(footer);
    for (const reserved of footer.querySelectorAll?.(RESERVE_SELECTOR) || []) {
      resizeObserver?.observe(reserved);
    }
    const mutationObserver = typeof MutationObserverCtor === 'function'
      ? new MutationObserverCtor(schedule)
      : null;
    mutationObserver?.observe(footer, {
      attributes: true,
      childList: true,
      characterData: true,
      subtree: true,
    });
    mutationObserver?.observe(rootElement, {
      attributes: true,
      attributeFilter: ['data-size'],
    });
    view.addEventListener?.('resize', schedule, { passive: true });
    measure(true);

    return {
      update: measure,
      getSnapshot() { return { height: Math.max(0, height), changed: false, tailPinned: false }; },
      destroy() {
        if (destroyed) return;
        destroyed = true;
        if (frame) cancelFrame?.call(view, frame);
        resizeObserver?.disconnect();
        mutationObserver?.disconnect();
        view.removeEventListener?.('resize', schedule);
        rootElement.style.removeProperty(property);
        if (rootElement.dataset) {
          delete rootElement.dataset.faryoComposerLayout;
          delete rootElement.dataset.faryoComposerReserve;
        }
      },
    };
  }

  return { createComposerLayout, measuredHeight };
});
