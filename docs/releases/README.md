# Historical Release Notes

Files before `v1.2.0` record inherited/upstream packaged releases. They are
retained as historical evidence and must not be read as the current personal
fork's installation guide, platform support statement, or binary release
promise. Starting with `v1.2.0`, this directory also contains the maintained
fork's source-only GitHub Release notes.

The maintained fork is source-only and Codex-focused. Use the repository root
[`README.md`](../../README.md) for current scope, deployment, and validation.
The current maintained release is [`v1.12.0`](v1.12.0.md). It replaces the
shared App Server writer with one read-only control plane and one isolated
worker per structured Web session, so recovery of one worker does not restart
healthy sessions, ordinary Codex CLI processes or Codex TUI tmux sessions. The
source-only distribution and security boundary are unchanged.
