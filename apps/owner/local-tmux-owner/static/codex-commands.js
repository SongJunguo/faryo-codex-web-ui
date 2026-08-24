(() => {
  'use strict';

  const TESTED_CODEX_VERSION = '0.149.1';

  const busySafeCommands = new Set([
    '/model', '/fast', '/ide', '/permissions', '/approve', '/skills', '/hooks',
    '/rename', '/resume', '/goal', '/agents', '/side', '/copy', '/raw', '/diff',
    '/mention', '/pwd', '/status', '/usage', '/title', '/statusline', '/mcp',
    '/plugins', '/exit', '/feedback', '/ps', '/stop',
  ]);

  let inventory = Object.freeze([
    { command: '/model', description: 'Choose the model and reasoning effort', category: 'Model' },
    { command: '/fast', description: 'Toggle faster responses with increased usage', category: 'Model' },
    { command: '/ide', description: 'Include IDE selection, open files, and context', category: 'Context' },
    { command: '/permissions', description: 'Choose what Codex is allowed to do', category: 'Security', risk: 'permission' },
    { command: '/keymap', description: 'Remap TUI shortcuts', category: 'Interface' },
    { command: '/vim', description: 'Toggle Vim mode for the composer', category: 'Interface' },
    { command: '/experimental', description: 'Toggle experimental features', category: 'Settings' },
    { command: '/approve', description: 'Approve one retry of an auto-review denial', category: 'Security', risk: 'permission' },
    { command: '/memories', description: 'Configure memory use and generation', category: 'Context' },
    { command: '/skills', description: 'Use installed skills for specialized tasks', category: 'Context' },
    { command: '/import', description: 'Import setup, project context, and recent chats', category: 'Context', risk: 'imports data' },
    { command: '/hooks', description: 'View and manage lifecycle hooks', category: 'Tools', risk: 'configuration' },
    { command: '/review', description: 'Review current changes and find issues', category: 'Work' },
    { command: '/rename', insert: '/rename ', argumentHint: '<name>', description: 'Rename the current thread', category: 'Conversation' },
    { command: '/new', description: 'Start a new chat in the current terminal', category: 'Conversation', risk: 'changes thread' },
    { command: '/archive', description: 'Archive this session and exit', category: 'Conversation', risk: 'ends session' },
    { command: '/delete', description: 'Permanently delete this session and exit', category: 'Conversation', risk: 'destructive' },
    { command: '/resume', description: 'Resume a saved chat', category: 'Conversation' },
    { command: '/fork', description: 'Fork the current chat', category: 'Conversation' },
    { command: '/init', description: 'Create an AGENTS.md file for Codex', category: 'Project' },
    { command: '/compact', description: 'Summarize the conversation to free context', category: 'Conversation' },
    { command: '/plan', description: 'Switch to Plan mode', category: 'Work' },
    { command: '/goal', description: 'Set or view a long-running task goal', category: 'Work' },
    { command: '/agents', description: 'View and switch between active agent sessions', category: 'Agents' },
    { command: '/side', aliases: ['/btw'], description: 'Start a side conversation in an ephemeral fork', category: 'Agents' },
    { command: '/copy', description: 'Copy the last response as Markdown', category: 'Export' },
    { command: '/export', description: 'Export the conversation as Markdown', category: 'Export' },
    { command: '/raw', description: 'Toggle copy-friendly raw scrollback', category: 'Interface' },
    { command: '/diff', description: 'Show the Git diff, including untracked files', category: 'Inspect' },
    { command: '/mention', insert: '/mention ', argumentHint: '<path>', description: 'Mention a file', category: 'Context' },
    { command: '/cd', description: 'Change the current working directory', category: 'Context' },
    { command: '/pwd', description: 'Show the current working directory', category: 'Inspect' },
    { command: '/status', description: 'Show session configuration and token usage', category: 'Inspect' },
    { command: '/usage', description: 'View account usage or reset a usage limit', category: 'Inspect' },
    { command: '/title', description: 'Configure terminal-title items', category: 'Interface' },
    { command: '/statusline', description: 'Configure status-line items', category: 'Interface' },
    { command: '/theme', description: 'Choose a syntax-highlighting theme', category: 'Interface' },
    { command: '/pets', aliases: ['/pet'], description: 'Choose or hide the terminal pet', category: 'Interface' },
    { command: '/mcp', description: 'List configured MCP tools; accepts verbose', category: 'Tools', argumentHint: '[verbose]' },
    { command: '/plugins', description: 'Browse plugins', category: 'Tools' },
    { command: '/logout', description: 'Log out of Codex', category: 'Account', risk: 'account' },
    { command: '/exit', aliases: ['/quit'], description: 'Exit Codex', category: 'Session', risk: 'ends session' },
    { command: '/feedback', description: 'Send logs to the Codex maintainers', category: 'Support', risk: 'sends logs' },
    { command: '/ps', description: 'List background terminals', category: 'Runtime' },
    { command: '/stop', description: 'Stop all background terminals', category: 'Runtime', risk: 'interrupts work' },
    { command: '/clear', description: 'Clear the terminal and start a new chat', category: 'Conversation', risk: 'changes thread' },
  ].map((entry) => Object.freeze({
    ...entry,
    aliases: Object.freeze(entry.aliases || []),
    availableDuringTask: busySafeCommands.has(entry.command),
  })));

  const launchInventory = Object.freeze([
    { command: 'codex', description: 'Start Codex in the current shell', category: 'CLI' },
    { command: 'codex resume', description: 'Resume a saved Codex session', category: 'CLI' },
    { command: 'codex fork', description: 'Fork a saved Codex session', category: 'CLI' },
    { command: 'codex review', description: 'Run a non-interactive code review', category: 'CLI' },
    { command: 'codex doctor', description: 'Diagnose the local Codex installation', category: 'CLI' },
  ].map((entry) => Object.freeze({ ...entry, aliases: Object.freeze([]) })));

  function subsequence(haystack, needle) {
    let index = 0;
    for (const char of haystack) if (char === needle[index]) index += 1;
    return index === needle.length;
  }

  function score(entry, needle) {
    const names = [entry.command, ...entry.aliases].map((value) => value.slice(1).toLowerCase());
    if (!needle) return { score: 0, alias: '' };
    const prefix = names.findIndex((name) => name.startsWith(needle));
    if (prefix >= 0) return { score: prefix === 0 ? 0 : 1, alias: prefix ? entry.aliases[prefix - 1] : '' };
    const contains = names.findIndex((name) => name.includes(needle));
    if (contains >= 0) return { score: contains === 0 ? 2 : 3, alias: contains ? entry.aliases[contains - 1] : '' };
    if (names.some((name) => subsequence(name, needle))) return { score: 4, alias: '' };
    const searchable = `${entry.description} ${entry.category} ${entry.argumentHint || ''}`.toLowerCase();
    return searchable.includes(needle) ? { score: 5, alias: '' } : null;
  }

  function slashMatches(query) {
    if (!/^\/[^\s]*$/.test(query)) return [];
    if (inventory.some((entry) => [entry.command, ...entry.aliases].some((name) => name.toLowerCase() === query))) return [];
    const needle = query.slice(1).toLowerCase();
    return inventory.map((entry, order) => {
      const result = score(entry, needle);
      if (!result) return null;
      const matchedCommand = result.alias || entry.command;
      const value = result.alias || entry.insert || entry.command;
      if (query.toLowerCase() === matchedCommand.toLowerCase()) return null;
      return { ...entry, value, matchedAlias: result.alias, score: result.score, order };
    }).filter(Boolean).sort((left, right) => left.score - right.score || left.order - right.order);
  }

  function launchMatches(query) {
    if (!(query.length >= 2 && 'codex'.startsWith(query)) && !/^codex(?:\s+[-\w]*){0,3}\s*$/.test(query)) return [];
    return launchInventory.filter((entry) => entry.command.startsWith(query) && entry.command !== query)
      .map((entry, order) => ({ ...entry, value: entry.command, score: 0, order }));
  }

  function directoryMatches(query, recentDirectories) {
    if (!/^cd(?:\s.*)?$/.test(query)) return [];
    return (recentDirectories || []).filter((value) => value.toLowerCase().startsWith(query) && value.toLowerCase() !== query)
      .map((value, order) => ({ command: value, value, description: 'Open a recent working directory', category: 'Directory', aliases: [], score: 0, order }));
  }

  function match(input, options = {}) {
    const query = String(input || '').trimStart().toLowerCase();
    const matches = query.startsWith('/')
      ? slashMatches(query)
      : query.startsWith('cd')
        ? directoryMatches(query, options.recentDirectories)
        : launchMatches(query);
    return matches.slice(0, Math.max(1, Number(options.limit) || 64));
  }

  function replaceInventory(entries, metadata = {}) {
    if (!Array.isArray(entries) || !entries.length) return false;
    const observedVersion = String(metadata.observedCodexVersion || '');
    const allowBusyCapabilities = Boolean(observedVersion && observedVersion === TESTED_CODEX_VERSION);
    const known = new Map(inventory.map((entry) => [entry.command, entry]));
    const next = [];
    const seen = new Set();
    for (const raw of entries) {
      const command = String(raw?.command || '').trim().toLowerCase();
      if (!/^\/[a-z][a-z-]*$/.test(command) || seen.has(command)) continue;
      const fallback = known.get(command) || {};
      const aliases = Array.isArray(raw.aliases) ? raw.aliases.filter((value) => /^\/[a-z][a-z-]*$/.test(value)) : (fallback.aliases || []);
      const argumentHint = String(raw.argumentHint || fallback.argumentHint || '');
      next.push(Object.freeze({
        ...fallback,
        command,
        description: String(raw.description || fallback.description || 'New Codex command'),
        category: String(raw.category || fallback.category || 'Unclassified'),
        behavior: String(raw.behavior || fallback.behavior || 'unclassified'),
        argumentHint,
        availableDuringTask: allowBusyCapabilities && (typeof raw.availableDuringTask === 'boolean'
          ? raw.availableDuringTask
          : Boolean(fallback.availableDuringTask)),
        insert: fallback.insert || (argumentHint && !argumentHint.startsWith('[') ? `${command} ` : undefined),
        aliases: Object.freeze(aliases),
        risk: fallback.risk || (raw.behavior === 'unclassified' ? 'unclassified' : ''),
      }));
      seen.add(command);
    }
    if (!next.length) return false;
    inventory = Object.freeze(next);
    api.observedCodexVersion = observedVersion;
    api.catalogDrifted = Boolean(metadata.drifted);
    return true;
  }

  const api = {
    version: '1',
    testedCodexVersion: TESTED_CODEX_VERSION,
    observedCodexVersion: '',
    catalogDrifted: false,
    get inventory() { return inventory; },
    launchInventory,
    match,
    replaceInventory,
  };
  if (typeof module === 'object' && module.exports) module.exports = api;
  globalThis.FaryoCodexCommands = api;
})();
