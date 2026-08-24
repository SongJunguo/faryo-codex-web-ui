'use strict';

const assert = require('node:assert/strict');
const composerLayout = require('../static/composer-layout.js');

assert.equal(composerLayout.measuredHeight(null), 0);
assert.equal(composerLayout.measuredHeight({ getBoundingClientRect: () => ({ height: 81.2 }) }), 82);
assert.equal(composerLayout.measuredHeight({
  getBoundingClientRect: () => ({ top: 100, bottom: 196, height: 96 }),
  querySelectorAll: () => [{ getBoundingClientRect: () => ({ top: 76, bottom: 88, height: 12 }) }],
}), 120);

function fakeView() {
  const properties = new Map();
  const dataset = {};
  const listeners = new Map();
  const frames = new Map();
  const observers = [];
  let nextFrame = 1;
  let footerHeight = 96;
  class ResizeObserver {
    constructor(callback) { this.callback = callback; observers.push(this); }
    observe(element) { this.element = element; }
    disconnect() { this.disconnected = true; }
  }
  const root = {
    dataset,
    style: {
      setProperty(name, value) { properties.set(name, value); },
      removeProperty(name) { properties.delete(name); },
    },
  };
  const footer = { getBoundingClientRect: () => ({ height: footerHeight }) };
  const view = {
    document: { documentElement: root, querySelector: () => footer },
    ResizeObserver,
    addEventListener(name, callback) { listeners.set(name, callback); },
    removeEventListener(name, callback) { if (listeners.get(name) === callback) listeners.delete(name); },
    requestAnimationFrame(callback) { const id = nextFrame++; frames.set(id, callback); return id; },
    cancelAnimationFrame(id) { frames.delete(id); },
  };
  return {
    view,
    root,
    footer,
    properties,
    dataset,
    listeners,
    observers,
    setFooterHeight(value) { footerHeight = value; },
    flush() {
      const pending = [...frames.values()];
      frames.clear();
      for (const callback of pending) callback();
    },
  };
}

const fixture = fakeView();
let pinned = true;
const changes = [];
const controller = composerLayout.createComposerLayout(fixture.view, {
  isTailPinned: () => pinned,
  onChange: (snapshot) => changes.push(snapshot),
});
assert.equal(fixture.properties.get('--faryo-composer-reserve'), '96px');
assert.equal(fixture.dataset.faryoComposerLayout, 'transparent-overlay');
assert.equal(fixture.dataset.faryoComposerReserve, '96');
assert.deepEqual(changes.at(-1), {
  height: 96,
  previousHeight: 0,
  changed: true,
  tailPinned: true,
});
assert.equal(fixture.observers[0].element, fixture.footer);

pinned = false;
fixture.setFooterHeight(121.1);
fixture.observers[0].callback();
fixture.observers[0].callback();
assert.equal(fixture.properties.get('--faryo-composer-reserve'), '96px');
fixture.flush();
assert.equal(fixture.properties.get('--faryo-composer-reserve'), '122px');
assert.equal(changes.at(-1).tailPinned, false);
assert.equal(changes.at(-1).previousHeight, 96);

fixture.listeners.get('resize')();
fixture.flush();
assert.equal(changes.length, 2);
assert.deepEqual(controller.getSnapshot(), { height: 122, changed: false, tailPinned: false });
controller.destroy();
assert.equal(fixture.properties.has('--faryo-composer-reserve'), false);
assert.equal('faryoComposerLayout' in fixture.dataset, false);
assert.equal(fixture.observers[0].disconnected, true);
assert.equal(fixture.listeners.size, 0);

console.log('composer layout tests passed');
