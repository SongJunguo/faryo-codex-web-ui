(() => {
  'use strict';

  const EXCLUDED_SELECTOR = [
    'button', '.copy-output-block', '.markdown-code-copy', '.memory-reference-card',
    '.compact-live-terminal', '.compact-process-line', '.compact-status-line',
    '.compact-activity-card', '.command-timeline-row',
    '.compact-block.plan', '.question-navigator', '[aria-hidden="true"]',
  ].join(',');

  function elementForNode(node) {
    return node?.nodeType === 1 ? node : node?.parentElement || null;
  }

  function normalizedText(value) {
    return String(value || '').replace(/\u200b/g, '').replace(/\s+/g, ' ').trim();
  }

  function mathSources(source, parseMarkdown) {
    const text = String(source || '');
    if (typeof parseMarkdown !== 'function') return [];
    let tree;
    try { tree = parseMarkdown(text); } catch (_error) { return []; }
    const results = [];
    const visit = (node) => {
      if (!node || typeof node !== 'object') return;
      if (node.type === 'inlineMath' || node.type === 'math') {
        const display = node.type === 'math';
        const start = Number(node.position?.start?.offset);
        const end = Number(node.position?.end?.offset);
        const tex = String(node.value || '');
        const positioned = Number.isInteger(start) && Number.isInteger(end) && start >= 0 && end > start && end <= text.length;
        const raw = positioned ? text.slice(start, end) : (display ? `\\[\n${tex}\n\\]` : `\\(${tex}\\)`);
        results.push(Object.freeze({ raw, tex, display }));
      }
      for (const child of node.children || []) visit(child);
    };
    visit(tree);
    return results;
  }

  function stripExcluded(root) {
    for (const node of [...(root?.querySelectorAll?.(EXCLUDED_SELECTOR) || [])]) node.remove();
    return root;
  }

  function sanitizeHtmlFragment(fragment, formulaRecords = []) {
    const clone = fragment.cloneNode(true);
    stripExcluded(clone);
    const formulas = [...clone.querySelectorAll('.katex')];
    formulas.forEach((formula, index) => {
      const code = formula.ownerDocument.createElement('code');
      code.textContent = formulaRecords[index]?.raw || formula.querySelector('annotation[encoding="application/x-tex"]')?.textContent || '';
      formula.replaceWith(code);
    });
    for (const node of [...clone.querySelectorAll('script,style,iframe,object,embed,svg')]) node.remove();
    for (const image of [...clone.querySelectorAll('img')]) {
      image.replaceWith(image.ownerDocument.createTextNode(image.alt ? `[Image: ${image.alt}]` : '[Image]'));
    }
    for (const element of [...clone.querySelectorAll('*')]) {
      const href = element.tagName === 'A' ? String(element.getAttribute('href') || '') : '';
      for (const attribute of [...element.attributes]) element.removeAttribute(attribute.name);
      if (element.tagName === 'A' && /^https:\/\//i.test(href)) element.setAttribute('href', href);
    }
    const container = clone.ownerDocument.createElement('div');
    container.appendChild(clone);
    return container.innerHTML;
  }

  function serializeTable(table, serialize) {
    const serializeChildren = (node) => [...node.childNodes].map(serialize).join('');
    const rows = [...table.querySelectorAll('tr')].map((row) => [...row.querySelectorAll(':scope > th, :scope > td')]
      .map((cell) => serializeChildren(cell).replace(/\|/g, '\\|').replace(/\s*\n\s*/g, ' ').trim()));
    if (!rows.length) return '';
    const width = Math.max(...rows.map((row) => row.length));
    const padded = rows.map((row) => [...row, ...Array(Math.max(0, width - row.length)).fill('')]);
    const lines = [`| ${padded[0].join(' | ')} |`, `| ${Array(width).fill('---').join(' | ')} |`];
    for (const row of padded.slice(1)) lines.push(`| ${row.join(' | ')} |`);
    return lines.join('\n') + '\n\n';
  }

  function markdownFromFragment(fragment) {
    stripExcluded(fragment);
    const serialize = (node) => {
      if (node.nodeType === 3) return node.nodeValue || '';
      if (node.nodeType !== 1) return '';
      const tag = node.tagName;
      const children = () => [...node.childNodes].map(serialize).join('');
      if (node.matches(EXCLUDED_SELECTOR)) return '';
      if (node.classList.contains('markdown-code-block')) {
        const pre = node.querySelector('pre');
        const code = pre?.querySelector('code') || pre;
        const language = [...(pre?.classList || []), ...(code?.classList || [])]
          .map((name) => name.match(/^(?:language-|lang-)([\w+-]+)$/)?.[1]).find(Boolean) || '';
        return `\`\`\`${language}\n${String(code?.textContent || '').replace(/\n$/, '')}\n\`\`\`\n\n`;
      }
      if (node.classList.contains('markdown-table-scroll')) {
        const table = node.querySelector('table');
        return table ? serializeTable(table, serialize) : children();
      }
      if (/^H[1-6]$/.test(tag)) return `${'#'.repeat(Number(tag[1]))} ${children().trim()}\n\n`;
      if (tag === 'P') return `${children().trim()}\n\n`;
      if (tag === 'BR') return '\n';
      if (tag === 'STRONG' || tag === 'B') return `**${children()}**`;
      if (tag === 'EM' || tag === 'I') return `*${children()}*`;
      if (tag === 'S' || tag === 'DEL') return `~~${children()}~~`;
      if (tag === 'CODE' && node.parentElement?.tagName !== 'PRE') return `\`${node.textContent || ''}\``;
      if (tag === 'PRE') return `\`\`\`\n${String(node.textContent || '').replace(/\n$/, '')}\n\`\`\`\n\n`;
      if (tag === 'BLOCKQUOTE') return children().trim().split('\n').map((line) => `> ${line}`).join('\n') + '\n\n';
      if (tag === 'UL' || tag === 'OL') {
        return [...node.children].filter((child) => child.tagName === 'LI').map((item, index) => {
          const marker = tag === 'OL' ? `${index + 1}.` : '-';
          return `${marker} ${[...item.childNodes].map(serialize).join('').trim().replace(/\n/g, '\n  ')}`;
        }).join('\n') + '\n\n';
      }
      if (tag === 'TABLE') return serializeTable(node, serialize);
      if (tag === 'A') return children();
      if (tag === 'IMG') return node.alt ? `[Image: ${node.alt}]` : '[Image]';
      if (tag === 'HR') return '---\n\n';
      const value = children();
      return /^(?:DIV|SECTION|ARTICLE|FIGURE|DETAILS)$/.test(tag) ? `${value}\n` : value;
    };
    return [...fragment.childNodes].map(serialize).join('')
      .replace(/[ \t]+\n/g, '\n')
      .replace(/\n{3,}/g, '\n\n')
      .trim();
  }

  function create(options = {}) {
    const root = options.root;
    const view = root?.ownerDocument?.defaultView || globalThis;
    const parseMarkdown = options.parseMarkdown;
    let blockSources = new WeakMap();
    let formulaSources = new WeakMap();

    function beginRender() {
      blockSources = new WeakMap();
      formulaSources = new WeakMap();
    }

    function bindBlock(block, metadata = {}) {
      if (!block) return;
      const source = String(metadata.source || '');
      const renderSource = String(metadata.renderSource ?? source);
      blockSources.set(block, Object.freeze({ source, kind: String(metadata.kind || '') }));
      const records = mathSources(renderSource, parseMarkdown);
      const formulas = [...block.querySelectorAll('.katex')];
      formulas.forEach((formula, index) => {
        const annotation = formula.querySelector('annotation[encoding="application/x-tex"]')?.textContent || '';
        const display = Boolean(formula.closest('.katex-display'));
        const record = records[index] || Object.freeze({
          tex: annotation,
          display,
          raw: display ? `\\[\n${annotation}\n\\]` : `\\(${annotation}\\)`,
        });
        formulaSources.set(formula, record);
        const wrapper = formula.closest('.katex-display');
        if (wrapper) formulaSources.set(wrapper, record);
      });
    }

    function blockContentRange(block) {
      const nodes = [...block.childNodes].filter((node) => {
        const element = elementForNode(node);
        return !element?.matches?.(EXCLUDED_SELECTOR);
      });
      if (!nodes.length) return null;
      const range = block.ownerDocument.createRange();
      range.setStartBefore(nodes[0]);
      range.setEndAfter(nodes[nodes.length - 1]);
      return range;
    }

    function intersects(range, node) {
      try { return range.intersectsNode(node); } catch (_error) { return false; }
    }

    function clampRange(range, bounds) {
      const result = range.cloneRange();
      const RangeType = view.Range;
      if (result.compareBoundaryPoints(RangeType.START_TO_START, bounds) < 0) {
        result.setStart(bounds.startContainer, bounds.startOffset);
      }
      if (result.compareBoundaryPoints(RangeType.END_TO_END, bounds) > 0) {
        result.setEnd(bounds.endContainer, bounds.endOffset);
      }
      return result;
    }

    function formulaForNode(node) {
      let element = elementForNode(node);
      while (element && root.contains(element)) {
        if (formulaSources.has(element)) return { element, record: formulaSources.get(element) };
        element = element.parentElement;
      }
      return null;
    }

    function codeContainerForNode(node) {
      const code = elementForNode(node)?.closest('pre,code');
      return code && code !== root && root.contains(code) ? code : null;
    }

    function selectedFormula(selection) {
      const anchor = formulaForNode(selection?.anchorNode);
      const focus = formulaForNode(selection?.focusNode);
      if (!anchor || !focus || anchor.record !== focus.record) return null;
      return anchor.record;
    }

    function formulaRecordsForRange(range, block) {
      return [...block.querySelectorAll('.katex')]
        .filter((formula) => intersects(range, formula))
        .map((formula) => formulaSources.get(formula))
        .filter(Boolean);
    }

    function expandedFormulaRange(range) {
      const result = range.cloneRange();
      const start = formulaForNode(result.startContainer);
      const end = formulaForNode(result.endContainer);
      if (start) result.setStartBefore(start.element.closest('.katex-display') || start.element);
      if (end) result.setEndAfter(end.element.closest('.katex-display') || end.element);
      return result;
    }

    function fragmentPayload(range, block) {
      const bounds = blockContentRange(block);
      if (!bounds || !intersects(range, block)) return null;
      let selected = clampRange(range, bounds);
      selected = expandedFormulaRange(selected);
      selected = clampRange(selected, bounds);
      if (selected.collapsed) return null;
      const records = formulaRecordsForRange(selected, block);
      const plainFragment = selected.cloneContents();
      stripExcluded(plainFragment);
      [...plainFragment.querySelectorAll('.katex')].forEach((formula, index) => {
        formula.replaceWith(formula.ownerDocument.createTextNode(records[index]?.raw || formula.querySelector('annotation[encoding="application/x-tex"]')?.textContent || ''));
      });
      const htmlFragment = selected.cloneContents();
      return {
        plain: markdownFromFragment(plainFragment),
        html: sanitizeHtmlFragment(htmlFragment, records),
      };
    }

    function rangeCoversBlock(range, block) {
      const bounds = blockContentRange(block);
      if (!bounds) return false;
      const selected = clampRange(range, bounds);
      return normalizedText(selected.toString()) === normalizedText(bounds.toString());
    }

    function payloadForRange(range, selection = null) {
      if (!range || range.collapsed) return null;
      const startElement = elementForNode(range.startContainer);
      const endElement = elementForNode(range.endContainer);
      if (!startElement || !endElement || !root.contains(startElement) || !root.contains(endElement)) return null;
      const startCode = codeContainerForNode(range.startContainer);
      const endCode = codeContainerForNode(range.endContainer);
      if (startCode && startCode === endCode) return null;
      const formula = selectedFormula(selection);
      if (formula) return { plain: formula.raw, html: `<code>${escapeHtml(formula.raw)}</code>`, kind: 'formula' };
      const blocks = [...root.querySelectorAll(':scope > .compact-block.user, :scope > .compact-block.output')]
        .filter((block) => blockSources.has(block) && intersects(range, block));
      if (!blocks.length) return null;
      const pieces = [];
      const html = [];
      for (const block of blocks) {
        const metadata = blockSources.get(block);
        if (rangeCoversBlock(range, block)) {
          pieces.push(metadata.source);
          const bounds = blockContentRange(block);
          const records = formulaRecordsForRange(bounds, block);
          html.push(sanitizeHtmlFragment(bounds.cloneContents(), records));
        } else {
          const partial = fragmentPayload(range, block);
          if (partial?.plain) pieces.push(partial.plain);
          if (partial?.html) html.push(partial.html);
        }
      }
      const plain = pieces.filter(Boolean).join('\n\n').trim();
      return plain ? { plain, html: html.filter(Boolean).join('<br><br>'), kind: 'selection' } : null;
    }

    function payloadForSelection(selection = view.getSelection?.()) {
      if (!selection?.rangeCount || selection.isCollapsed) return null;
      return payloadForRange(selection.getRangeAt(0), selection);
    }

    function payloadForBlock(block) {
      const metadata = blockSources.get(block);
      const bounds = blockContentRange(block);
      if (!metadata || !bounds) return null;
      const records = formulaRecordsForRange(bounds, block);
      return { plain: metadata.source.trim(), html: sanitizeHtmlFragment(bounds.cloneContents(), records), kind: 'block' };
    }

    function handleCopy(event) {
      const payload = payloadForSelection();
      if (!payload || !event.clipboardData) return false;
      try {
        event.clipboardData.setData('text/plain', payload.plain);
        if (payload.html) event.clipboardData.setData('text/html', payload.html);
        event.preventDefault();
        return true;
      } catch (_error) {
        return false;
      }
    }

    async function write(payload) {
      if (!payload?.plain || !view.navigator?.clipboard) return false;
      if (typeof view.navigator.clipboard.write === 'function' && typeof view.ClipboardItem === 'function') {
        try {
          const parts = { 'text/plain': new view.Blob([payload.plain], { type: 'text/plain' }) };
          if (payload.html) parts['text/html'] = new view.Blob([payload.html], { type: 'text/html' });
          await view.navigator.clipboard.write([new view.ClipboardItem(parts)]);
          return true;
        } catch (_error) {}
      }
      if (typeof view.navigator.clipboard.writeText === 'function') {
        try { await view.navigator.clipboard.writeText(payload.plain); return true; } catch (_error) {}
      }
      return false;
    }

    return Object.freeze({ beginRender, bindBlock, handleCopy, payloadForBlock, payloadForRange, payloadForSelection, write });
  }

  function escapeHtml(value) {
    return String(value || '').replace(/[&<>"']/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
  }

  const api = Object.freeze({ version: '1', create, mathSources, markdownFromFragment });
  if (typeof module === 'object' && module.exports) module.exports = api;
  globalThis.FaryoCopyFidelity = api;
})();
