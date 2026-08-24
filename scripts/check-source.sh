#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  scripts/check-source.sh

Runs the maintained source, browser-bundle, and runtime-contract checks. Binary
package builders were removed when this fork became source-deployment only.
USAGE
}

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="${1:-}"
# shellcheck source=runtime-env.sh
source "$ROOT/scripts/runtime-env.sh"

[[ "$TARGET" == "-h" || "$TARGET" == "--help" ]] && { usage; exit 0; }
[[ -z "$TARGET" ]] || { echo "unsupported argument: $TARGET" >&2; usage >&2; exit 2; }

PYTHON_BIN="$(faryo_resolve_python)"
NODE_BIN="$(faryo_resolve_node)"
export FARYO_PYTHON="$PYTHON_BIN" FARYO_NODE_BIN="$NODE_BIN" PYTHONDONTWRITEBYTECODE=1
"$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' || {
  echo "Faryo requires Python 3.10 or newer: $PYTHON_BIN" >&2
  exit 1
}
"$PYTHON_BIN" -c 'import bcrypt, starlette, uvicorn' >/dev/null 2>&1 || {
  echo "Gateway runtime dependencies are missing from: $PYTHON_BIN" >&2
  echo "Install apps/gateway/requirements.txt in the selected environment." >&2
  exit 1
}
"$PYTHON_BIN" - <<'PY'
from importlib.metadata import version

expected = {
    "anyio": "4.14.2",
    "bcrypt": "5.0.0",
    "click": "8.4.2",
    "h11": "0.16.0",
    "idna": "3.19",
    "starlette": "1.6.0",
    "uvicorn": "0.52.4",
    "websockets": "16.1.1",
}
actual = {name: version(name) for name in expected}
if actual != expected:
    raise SystemExit(f"Gateway runtime dependency drift: {actual}")
PY
"$PYTHON_BIN" -c 'import ruff' >/dev/null 2>&1 || {
  echo "Ruff is missing from: $PYTHON_BIN" >&2
  echo "Install requirements-dev.txt in the selected environment." >&2
  exit 1
}
"$NODE_BIN" -e 'process.exit(Number(process.versions.node.split(".")[0]) >= 20 ? 0 : 1)' || {
  echo "Faryo source checks require Node.js 20 or newer: $NODE_BIN" >&2
  exit 1
}
printf 'runtime: %s · %s\n' \
  "$("$PYTHON_BIN" -c 'import platform; print("Python " + platform.python_version())')" \
  "$("$NODE_BIN" --version)"

release_checks() {
  "$PYTHON_BIN" -m ruff check \
    "$ROOT/apps" \
    "$ROOT/scripts" \
    "$ROOT/src" \
    "$ROOT/tests"
  (cd "$ROOT" && PATH="$(dirname "$NODE_BIN"):$PATH" npm run --silent check:lint)
  (cd "$ROOT" && PATH="$(dirname "$NODE_BIN"):$PATH" npm run --silent check:format)
  (cd "$ROOT" && PATH="$(dirname "$NODE_BIN"):$PATH" npm run --silent test:browser-harness)
  (cd "$ROOT" && PATH="$(dirname "$NODE_BIN"):$PATH" npm run --silent test:owner-layout-browser)
  (cd "$ROOT" && PATH="$(dirname "$NODE_BIN"):$PATH" npm run --silent check:diff-review)
  (cd "$ROOT" && PATH="$(dirname "$NODE_BIN"):$PATH" npm run --silent test:diff-review)
  (cd "$ROOT" && PATH="$(dirname "$NODE_BIN"):$PATH" npm run --silent check:gateway-preact)
  (cd "$ROOT" && PATH="$(dirname "$NODE_BIN"):$PATH" npm run --silent test:gateway-preact)
  (cd "$ROOT" && PATH="$(dirname "$NODE_BIN"):$PATH" npm run --silent typecheck:owner-ui)
  (cd "$ROOT" && PATH="$(dirname "$NODE_BIN"):$PATH" npm run --silent check:owner-ui)
  bash -n \
    "$ROOT/scripts/check-source.sh" \
    "$ROOT"/scripts/*.sh \
    "$ROOT"/apps/owner/scripts/*.sh \
    "$ROOT"/apps/owner/local-tmux-owner/tests/*.sh \
    "$ROOT"/apps/gateway/scripts/*.sh
  bash "$ROOT/scripts/runtime-env.test.sh"
  "$PYTHON_BIN" -m py_compile \
    "$ROOT/apps/owner/local-tmux-owner/server.py" \
    "$ROOT/apps/owner/local-tmux-owner/attachment_storage.py" \
    "$ROOT/apps/owner/local-tmux-owner/path_policy.py" \
    "$ROOT/apps/owner/local-tmux-owner/tmux_runtime.py" \
    "$ROOT/apps/owner/local-tmux-owner/delivery_store.py" \
    "$ROOT/apps/owner/local-tmux-owner/delivery_service.py" \
    "$ROOT/apps/owner/local-tmux-owner/owner_http.py" \
    "$ROOT/apps/owner/local-tmux-owner/codex_history.py" \
    "$ROOT/apps/owner/local-tmux-owner/codex_app_server.py" \
    "$ROOT/apps/owner/local-tmux-owner/codex_writer_guard.py" \
    "$ROOT/apps/owner/local-tmux-owner/appserver_capabilities.py" \
    "$ROOT/apps/owner/local-tmux-owner/appserver_commands.py" \
    "$ROOT/apps/owner/local-tmux-owner/command_timeline.py" \
    "$ROOT/apps/owner/local-tmux-owner/appserver_events.py" \
    "$ROOT/apps/owner/local-tmux-owner/appserver_history.py" \
    "$ROOT/apps/owner/local-tmux-owner/appserver_protocol.py" \
    "$ROOT/apps/owner/local-tmux-owner/appserver_registry.py" \
    "$ROOT/apps/owner/local-tmux-owner/appserver_requests.py" \
    "$ROOT/apps/owner/local-tmux-owner/appserver_runtime.py" \
    "$ROOT/apps/owner/local-tmux-owner/appserver_session_supervisor.py" \
    "$ROOT/apps/owner/local-tmux-owner/appserver_session.py" \
    "$ROOT/apps/owner/local-tmux-owner/appserver_transport.py" \
    "$ROOT/apps/owner/local-tmux-owner/session_launch.py" \
    "$ROOT/apps/owner/local-tmux-owner/session_namespace.py" \
    "$ROOT/apps/owner/local-tmux-owner/owner_asgi.py" \
    "$ROOT/apps/owner/local-tmux-owner/owner_asgi_control.py" \
    "$ROOT/apps/owner/local-tmux-owner/owner_asgi_events.py" \
    "$ROOT/apps/owner/local-tmux-owner/owner_asgi_read.py" \
    "$ROOT/apps/owner/local-tmux-owner/owner_asgi_support.py" \
    "$ROOT/apps/owner/local-tmux-owner/run_owner_asgi.py" \
    "$ROOT/apps/owner/local-tmux-owner/workspace_changes.py" \
    "$ROOT/apps/owner/local-tmux-owner/runtime_diagnostics.py" \
    "$ROOT/apps/owner/local-tmux-owner/tests/owner-archive-roundtrip.py" \
    "$ROOT/apps/owner/local-tmux-owner/tests/real-appserver-streaming.py" \
    "$ROOT/apps/gateway/server/server.py" \
    "$ROOT/apps/gateway/server/gateway_security.py" \
    "$ROOT/apps/gateway/server/asgi_app.py" \
    "$ROOT/apps/gateway/server/asgi_agents.py" \
    "$ROOT/apps/gateway/server/asgi_auth.py" \
    "$ROOT/apps/gateway/server/asgi_bridge.py" \
    "$ROOT/apps/gateway/server/asgi_control.py" \
    "$ROOT/apps/gateway/server/asgi_mcp.py" \
    "$ROOT/apps/gateway/server/asgi_owner_proxy.py" \
    "$ROOT/apps/gateway/server/asgi_read.py" \
    "$ROOT/apps/gateway/server/asgi_support.py" \
    "$ROOT/apps/gateway/server/bridge_packages.py" \
    "$ROOT/apps/gateway/server/control_audit.py" \
    "$ROOT/apps/gateway/server/gateway_config.py" \
    "$ROOT/apps/gateway/server/run_asgi.py" \
    "$ROOT/apps/gateway/server/owner_client.py" \
    "$ROOT/apps/gateway/server/mcp_service.py" \
    "$ROOT/apps/gateway/server/workbench_service.py" \
    "$ROOT/apps/gateway/scripts/generate-gateway-auth-config.py"
  "$PYTHON_BIN" -m py_compile \
    "$ROOT/src/faryo_cli/__init__.py" \
    "$ROOT/src/faryo_cli/__main__.py" \
    "$ROOT/src/faryo_cli/application.py" \
    "$ROOT/src/faryo_cli/appserver_workers.py" \
    "$ROOT/src/faryo_cli/browser_contract.py" \
    "$ROOT/src/faryo_cli/cli.py" \
    "$ROOT/src/faryo_cli/codex_runtime.py" \
    "$ROOT/src/faryo_cli/diagnostics.py" \
    "$ROOT/src/faryo_cli/error_contract.py" \
    "$ROOT/src/faryo_cli/installer.py" \
    "$ROOT/src/faryo_cli/maintenance.py" \
    "$ROOT/src/faryo_cli/migration.py" \
    "$ROOT/src/faryo_cli/operations.py" \
    "$ROOT/src/faryo_cli/runtime.py" \
    "$ROOT/src/faryo_cli/session_backend.py" \
    "$ROOT/src/faryo_cli/updates.py"
  for js_file in \
    "$ROOT/apps/owner/local-tmux-owner/static/compact-rules-codex.js" \
    "$ROOT/apps/owner/local-tmux-owner/static/event-stream.js" \
    "$ROOT/apps/owner/local-tmux-owner/static/internal-annotations.js" \
    "$ROOT/apps/owner/local-tmux-owner/static/local-file-view.js" \
    "$ROOT/apps/owner/local-tmux-owner/static/stable-blocks.js" \
    "$ROOT/apps/owner/local-tmux-owner/static/question-navigator.js" \
    "$ROOT/apps/owner/local-tmux-owner/static/codex-commands.js" \
    "$ROOT/apps/owner/local-tmux-owner/static/copy-fidelity.js" \
    "$ROOT/apps/owner/local-tmux-owner/static/clipboard-images.js" \
    "$ROOT/apps/owner/local-tmux-owner/static/immersive-mode.js" \
    "$ROOT/apps/owner/local-tmux-owner/static/keyboard-layout.js" \
    "$ROOT/apps/owner/local-tmux-owner/static/composer-layout.js" \
    "$ROOT/apps/owner/local-tmux-owner/static/owner/changes-panel.mjs" \
    "$ROOT/apps/owner/local-tmux-owner/static/owner/activity-groups.mjs" \
    "$ROOT/apps/owner/local-tmux-owner/static/owner/api-client.mjs" \
    "$ROOT/apps/owner/local-tmux-owner/static/owner/attachment-controller.mjs" \
    "$ROOT/apps/owner/local-tmux-owner/static/owner/history-controller.mjs" \
    "$ROOT/apps/owner/local-tmux-owner/static/owner/rich-block-controller.mjs" \
    "$ROOT/apps/owner/local-tmux-owner/static/owner/capture-controller.mjs" \
    "$ROOT/apps/owner/local-tmux-owner/static/owner/composer-delivery.mjs" \
    "$ROOT/apps/owner/local-tmux-owner/static/owner/goal-status.mjs" \
    "$ROOT/apps/owner/local-tmux-owner/static/owner/status-controller.mjs" \
    "$ROOT/apps/owner/local-tmux-owner/static/vendor/markdown-ast/markdown-ast.min.js" \
    "$ROOT/apps/owner/local-tmux-owner/static/live-scroll.js" \
    "$ROOT/apps/shared/static/appearance.js" \
    "$ROOT/apps/owner/local-tmux-owner/static/app.js" \
    "$ROOT/apps/gateway/server/static/workbench.js" \
    "$ROOT/apps/gateway/server/static/workbench-preact.js" \
    "$ROOT/apps/owner/local-tmux-owner/static/owner-ui.js" \
    "$ROOT/apps/gateway/ui/session-model.mjs" \
    "$ROOT/tools/gateway-preact/build.mjs"
  do
    "$NODE_BIN" --check "$js_file"
  done
  while IFS= read -r js_file; do
    "$NODE_BIN" --check "$js_file"
  done < <(find "$ROOT/apps/owner/local-tmux-owner/static/vendor/markdown-ast/highlight" -type f -name '*.js' -print | sort)
  "$NODE_BIN" --check "$ROOT/apps/owner/local-tmux-owner/tests/browser-katex-smoke.mjs"
  "$NODE_BIN" --check "$ROOT/apps/owner/local-tmux-owner/tests/browser-immersive-smoke.mjs"
  "$NODE_BIN" --check "$ROOT/apps/owner/local-tmux-owner/tests/browser-workspace-changes-smoke.mjs"
  "$NODE_BIN" --check "$ROOT/apps/owner/local-tmux-owner/tests/browser-owner-ui-smoke.mjs"
  "$NODE_BIN" --check "$ROOT/apps/owner/local-tmux-owner/tests/browser-owner-layout-smoke.mjs"
  "$NODE_BIN" --check "$ROOT/apps/owner/local-tmux-owner/tests/browser-empty-conversation-smoke.mjs"
  "$NODE_BIN" --check "$ROOT/apps/owner/local-tmux-owner/tests/browser-fast-toggle-smoke.mjs"
  "$NODE_BIN" --check "$ROOT/apps/owner/local-tmux-owner/tests/browser-live-resilience.mjs"
  "$NODE_BIN" --check "$ROOT/apps/owner/local-tmux-owner/tests/browser-real-appserver-streaming.mjs"
  "$NODE_BIN" --check "$ROOT/apps/owner/local-tmux-owner/tests/browser-structured-interactions.mjs"
  "$NODE_BIN" --check "$ROOT/apps/owner/local-tmux-owner/tests/browser-goal-details.mjs"
  "$NODE_BIN" --check "$ROOT/apps/owner/local-tmux-owner/tests/browser-command-activity.mjs"
  "$NODE_BIN" --check "$ROOT/apps/owner/local-tmux-owner/tests/browser-tui-history-boundaries.mjs"
  "$NODE_BIN" --check "$ROOT/apps/owner/local-tmux-owner/tests/browser-real-command-timeline.mjs"
  "$NODE_BIN" --check "$ROOT/apps/gateway/server/tests/browser-workbench-smoke.mjs"
  "$NODE_BIN" --check "$ROOT/apps/gateway/server/tests/browser-resume-preflight.mjs"
  "$NODE_BIN" "$ROOT/apps/owner/local-tmux-owner/tests/markdown-ast-bundle.test.js"
  "$NODE_BIN" "$ROOT/apps/owner/local-tmux-owner/tests/internal-annotations.test.js"
  "$NODE_BIN" "$ROOT/apps/owner/local-tmux-owner/tests/event-stream.test.js"
  "$NODE_BIN" "$ROOT/apps/owner/local-tmux-owner/tests/stable-blocks.test.js"
  "$NODE_BIN" "$ROOT/apps/owner/local-tmux-owner/tests/question-navigator.test.js"
  "$NODE_BIN" "$ROOT/apps/owner/local-tmux-owner/tests/live-scroll.test.js"
  "$NODE_BIN" "$ROOT/apps/owner/local-tmux-owner/tests/compact-rules-codex.test.js"
  "$NODE_BIN" "$ROOT/apps/owner/local-tmux-owner/tests/codex-commands.test.js"
  "$NODE_BIN" "$ROOT/apps/owner/local-tmux-owner/tests/copy-fidelity.test.js"
  "$NODE_BIN" "$ROOT/apps/owner/local-tmux-owner/tests/clipboard-images.test.js"
  "$NODE_BIN" "$ROOT/apps/owner/local-tmux-owner/tests/immersive-mode.test.js"
  "$NODE_BIN" "$ROOT/apps/owner/local-tmux-owner/tests/keyboard-layout.test.js"
  "$NODE_BIN" "$ROOT/apps/owner/local-tmux-owner/tests/composer-layout.test.js"
  "$NODE_BIN" --test "$ROOT/apps/owner/local-tmux-owner/tests/changes-panel.test.mjs"
  "$NODE_BIN" --test "$ROOT/apps/owner/local-tmux-owner/tests/activity-groups.test.mjs"
  "$NODE_BIN" --test "$ROOT/apps/owner/local-tmux-owner/tests/api-client.test.mjs"
  "$NODE_BIN" --test "$ROOT/apps/owner/local-tmux-owner/tests/attachment-controller.test.mjs"
  "$NODE_BIN" --test "$ROOT/apps/owner/local-tmux-owner/tests/history-controller.test.mjs"
  "$NODE_BIN" --test "$ROOT/apps/owner/local-tmux-owner/tests/rich-block-controller.test.mjs"
  "$NODE_BIN" --test "$ROOT/apps/owner/local-tmux-owner/tests/capture-controller.test.mjs"
  "$NODE_BIN" --test "$ROOT/apps/owner/local-tmux-owner/tests/composer-delivery.test.mjs"
  "$NODE_BIN" --test "$ROOT/apps/owner/local-tmux-owner/tests/goal-status.test.mjs"
  "$NODE_BIN" --test "$ROOT/apps/owner/local-tmux-owner/tests/status-controller.test.mjs"
  "$NODE_BIN" --test "$ROOT/apps/owner/local-tmux-owner/tests/terminal-delivery-receiver.test.mjs"
  PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}" \
    "$PYTHON_BIN" -m unittest discover -s "$ROOT/apps/owner/local-tmux-owner/tests" -p 'test_*.py'
  PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}" \
    "$PYTHON_BIN" -m unittest discover -s "$ROOT/apps/gateway/server/tests" -p 'test_*.py'
  PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}" \
    "$PYTHON_BIN" -m unittest discover -s "$ROOT/tests" -p 'test_*.py'
  "$PYTHON_BIN" - "$ROOT" <<'PY'
from pathlib import Path
import json
import re
import sys
try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib
root = Path(sys.argv[1])

def reject_duplicate_json_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise AssertionError(f"duplicate JSON key: {key}")
        result[key] = value
    return result

json.loads(
    (root / "package.json").read_text(encoding="utf-8"),
    object_pairs_hook=reject_duplicate_json_keys,
)
index = (root / "apps/owner/local-tmux-owner/static/index.html").read_text(encoding="utf-8")
keyboard_layout_source = (root / "apps/owner/local-tmux-owner/static/keyboard-layout.js").read_text(encoding="utf-8")
composer_layout_source = (root / "apps/owner/local-tmux-owner/static/composer-layout.js").read_text(encoding="utf-8")
owner_style = (root / "apps/owner/local-tmux-owner/static/style.css").read_text(encoding="utf-8")
owner_server = (root / "apps/owner/local-tmux-owner/server.py").read_text(encoding="utf-8")
owner_asgi_source = (root / "apps/owner/local-tmux-owner/owner_asgi.py").read_text(encoding="utf-8")
owner_asgi_read_source = (root / "apps/owner/local-tmux-owner/owner_asgi_read.py").read_text(encoding="utf-8")
owner_asgi_control_source = (root / "apps/owner/local-tmux-owner/owner_asgi_control.py").read_text(encoding="utf-8")
owner_asgi_events_source = (root / "apps/owner/local-tmux-owner/owner_asgi_events.py").read_text(encoding="utf-8")
owner_asgi_runner = (root / "apps/owner/local-tmux-owner/run_owner_asgi.py").read_text(encoding="utf-8")
appserver_commands_source = (root / "apps/owner/local-tmux-owner/appserver_commands.py").read_text(encoding="utf-8")
appserver_session_source = (root / "apps/owner/local-tmux-owner/appserver_session.py").read_text(encoding="utf-8")
appserver_history_source = (root / "apps/owner/local-tmux-owner/appserver_history.py").read_text(encoding="utf-8")
appserver_rollout_source = (root / "apps/owner/local-tmux-owner/appserver_rollout.py").read_text(encoding="utf-8")
appserver_registry_source = (root / "apps/owner/local-tmux-owner/appserver_registry.py").read_text(encoding="utf-8")
appserver_runtime_source = (root / "apps/owner/local-tmux-owner/appserver_runtime.py").read_text(encoding="utf-8")
appserver_supervisor_source = (root / "apps/owner/local-tmux-owner/appserver_session_supervisor.py").read_text(encoding="utf-8")
session_namespace_source = (root / "apps/owner/local-tmux-owner/session_namespace.py").read_text(encoding="utf-8")
command_timeline_source = (root / "apps/owner/local-tmux-owner/command_timeline.py").read_text(encoding="utf-8")
real_appserver_browser_source = (root / "apps/owner/local-tmux-owner/tests/browser-real-appserver-streaming.mjs").read_text(encoding="utf-8")
durable_activity_browser_source = (root / "apps/owner/local-tmux-owner/tests/browser-durable-activity.mjs").read_text(encoding="utf-8")
command_activity_browser_source = (root / "apps/owner/local-tmux-owner/tests/browser-command-activity.mjs").read_text(encoding="utf-8")
owner_session_catalog_source = (root / "apps/owner/local-tmux-owner/session_catalog.py").read_text(encoding="utf-8")
owner_session_launch_source = (root / "apps/owner/local-tmux-owner/session_launch.py").read_text(encoding="utf-8")
owner_backend_source = "\n".join((
    owner_server,
    owner_asgi_source,
    owner_asgi_read_source,
    owner_asgi_control_source,
    appserver_commands_source,
    owner_session_catalog_source,
    owner_session_launch_source,
))
workspace_changes_source = (root / "apps/owner/local-tmux-owner/workspace_changes.py").read_text(encoding="utf-8")
runtime_diagnostics_source = (root / "apps/owner/local-tmux-owner/runtime_diagnostics.py").read_text(encoding="utf-8")
owner_http_source = (root / "apps/owner/local-tmux-owner/owner_http.py").read_text(encoding="utf-8")
gateway = (root / "apps/gateway/server/server.py").read_text(encoding="utf-8")
gateway_config_source = (root / "apps/gateway/server/gateway_config.py").read_text(encoding="utf-8")
gateway_audit_source = (root / "apps/gateway/server/control_audit.py").read_text(encoding="utf-8")
gateway_asgi_support = (root / "apps/gateway/server/asgi_support.py").read_text(encoding="utf-8")
gateway_runner = (root / "apps/gateway/scripts/run-gateway.sh").read_text(encoding="utf-8")
gateway_asgi_runner = (root / "apps/gateway/server/run_asgi.py").read_text(encoding="utf-8")
gateway_workbench = (root / "apps/gateway/server/static/workbench.js").read_text(encoding="utf-8")
gateway_preact_source = (root / "apps/gateway/ui/preact-workbench.jsx").read_text(encoding="utf-8")
owner_status_source = (root / "apps/owner/ui/StatusShell.tsx").read_text(encoding="utf-8")
owner_interaction_source = (root / "apps/owner/ui/InteractionHost.tsx").read_text(encoding="utf-8")
owner_transcript_source = (root / "apps/owner/ui/TranscriptShell.tsx").read_text(encoding="utf-8")
owner_conversation_store_source = (root / "apps/owner/ui/ConversationStore.ts").read_text(encoding="utf-8")
application_source = (root / "src/faryo_cli/application.py").read_text(encoding="utf-8")
gateway_ui = gateway + "\n" + gateway_workbench
assert "firstAvailableDirectoryPage" in gateway_workbench and "for (const candidate of candidates)" in gateway_workbench, "directory picker must try every recent cwd before root fallback"
assert "START_DIRECTORY_MAX_ENTRIES" not in owner_server and "folders = (data.directories || []).map" in gateway_workbench, "directory picker must not cap or cross-section-filter real child folders"
assert "class SessionNamespace" in session_namespace_source and "SessionNamespaceConflict" in session_namespace_source and "namespace_lock=core.RUNTIME_LOCK" in owner_asgi_source and "reserved_names=self.support.app_server_session_names" in owner_asgi_control_source and "reserved_names=reserved_names" in owner_session_launch_source and "self.registry.reassign_conflicts" in appserver_runtime_source, "App Server and TUI sessions must share one fail-closed faryo-number namespace"
ci_workflow = (root / ".github/workflows/ci.yml").read_text(encoding="utf-8")
release_workflow = (root / ".github/workflows/release.yml").read_text(encoding="utf-8")
codeql_workflow = (root / ".github/workflows/codeql.yml").read_text(encoding="utf-8")
dependabot_config = (root / ".github/dependabot.yml").read_text(encoding="utf-8")
check_script = (root / "scripts/check-source.sh").read_text(encoding="utf-8")
assert "pull_request:" in ci_workflow and "branches: [main]" in ci_workflow, "source CI must cover PR and main"
assert "scripts/check-source.sh" in ci_workflow and "scripts/check-source.sh" in release_workflow, "CI and release must share source checks"
assert "ubuntu-22.04" in ci_workflow and "python-version: '3.10'" in ci_workflow, "CI must retain the real Ubuntu 22.04/Python 3.10 minimum lane"
for workflow in (ci_workflow, release_workflow, codeql_workflow):
    uses = re.findall(r"^\s*-?\s*uses:\s*([^\s#]+)", workflow, flags=re.MULTILINE)
    assert uses and all(re.fullmatch(r"[^@]+@[0-9a-f]{40}", item) for item in uses), "GitHub Actions must use immutable commit SHAs"
assert "github/codeql-action/init@" in codeql_workflow and "github/codeql-action/analyze@" in codeql_workflow, "CodeQL must scan Python and JavaScript"
assert "language: [python, javascript-typescript]" in codeql_workflow, "CodeQL language matrix is incomplete"
workflow_concurrency = codeql_workflow.split("jobs:", 1)[0]
assert "matrix." not in workflow_concurrency, "workflow-level concurrency cannot use the job-only matrix context"
for ecosystem in ("npm", "pip", "github-actions"):
    assert f"package-ecosystem: {ecosystem}" in dependabot_config, f"Dependabot does not cover {ecosystem}"
assert dependabot_config.count("interval: weekly") == 3 and "patterns: ['*']" in dependabot_config, "Dependabot must use low-noise weekly grouped updates"
assert "package-client.sh" not in release_workflow, "retired package workflow must not return"
assert "faryo_${version}_all.deb" not in release_workflow and "macos.tar.gz" not in release_workflow, "release must remain source-only"
assert "git archive --format=tar.gz" in release_workflow and "sha256sum" in release_workflow, "release must build a verified source archive"
assert "install-faryo.sh" in release_workflow and "gh release upload" in release_workflow, "release must publish the reviewed source installer"
assert "apps/gateway/server/tests" in check_script, "canonical checks must include Gateway tests"
assert (root / "package-lock.json").is_file(), "development JavaScript dependencies must be locked"
package = json.loads((root / "package.json").read_text(encoding="utf-8"))
assert package.get("devDependencies", {}).get("preact") == "10.29.8", "Preact pilot must remain exact-pinned"
assert package.get("devDependencies", {}).get("vite") == "8.2.2" and package.get("devDependencies", {}).get("typescript") == "7.0.2", "Owner UI build dependencies must remain exact-pinned"
pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
project = pyproject.get("project") or {}
assert project.get("name") == "faryo" and project.get("scripts", {}).get("faryo") == "faryo_cli.cli:main", "Faryo CLI package metadata is incomplete"
assert pyproject.get("build-system", {}).get("requires") == ["setuptools==83.0.0"], "Faryo CLI build backend must remain exact-pinned"
assert "harden_venv_cli" in application_source and '"-I"' in application_source and "installed_cli_matches_version" in application_source, "Faryo CLI must ignore ambient Python paths and verify its exact installed version"
runtime_requirements = {
    line.strip() for line in (root / "apps/gateway/requirements.txt").read_text(encoding="utf-8").splitlines()
    if line.strip() and not line.lstrip().startswith("#")
}
assert set(project.get("dependencies") or []) == runtime_requirements, "CLI package runtime pins must match Gateway requirements"
release_metadata = dict(
    line.split("=", 1)
    for line in (root / "apps/owner/RELEASE").read_text(encoding="utf-8").splitlines()
    if "=" in line
)
release_version = release_metadata.get("version", "")
assert re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", release_version), "Owner release version must be semantic"
assert (root / "docs/releases" / f"{release_version}.md").is_file(), "current source version needs release notes"
package_version = str(project.get("version") or "")
cli_init = (root / "src/faryo_cli/__init__.py").read_text(encoding="utf-8")
assert release_version == f"v{package_version}", "Owner release and Python package versions must match"
assert f'__version__ = "{package_version}"' in cli_init, "CLI runtime and package versions must match"
assert (root / "requirements-dev.txt").is_file(), "development Python dependencies must be pinned"
assert "faryo_resolve_python" in check_script and "faryo_resolve_node" in check_script, "canonical checks must resolve runtimes"
python_runtime_tests = (
    "start-codex-runtime.sh",
    "browser-workspace-changes.sh",
    "browser-session-send-isolation.sh",
    "browser-live-selection.sh",
    "browser-full-history.sh",
    "browser-protected-resources.sh",
    "browser-delivery-matrix.sh",
    "send-restart-idempotency.sh",
)
test_script_root = root / "apps/owner/local-tmux-owner/tests"
for name in python_runtime_tests:
    source = (test_script_root / name).read_text(encoding="utf-8")
    assert "scripts/runtime-env.sh" in source and "faryo_resolve_python" in source, f"{name} bypasses shared Python discovery"
for name in (
    "browser-workspace-changes.sh",
    "browser-session-send-isolation.sh",
    "browser-live-selection.sh",
    "browser-full-history.sh",
    "browser-protected-resources.sh",
    "browser-delivery-matrix.sh",
    "send-restart-idempotency.sh",
):
    assert "faryo_resolve_node" in (test_script_root / name).read_text(encoding="utf-8"), f"{name} bypasses shared Node discovery"
command_inventory_source = (test_script_root / "codex-command-inventory.sh").read_text(encoding="utf-8")
assert "faryo_resolve_codex" in command_inventory_source and "faryo_resolve_node" in command_inventory_source, "Codex inventory must use shared runtime discovery"
for script_path in (
    root / "apps/owner/scripts/smoke-test.sh",
    root / "apps/owner/scripts/verify-reverse-tunnel.sh",
    root / "apps/owner/scripts/diagnose-owner-gateway.sh",
):
    source = script_path.read_text(encoding="utf-8")
    assert 'python3 -' not in source and '"$FARYO_PYTHON"' in source, f"{script_path.name} bypasses configured Python"
owner_init_source = (root / "apps/owner/scripts/init-owner-env.sh").read_text(encoding="utf-8")
gateway_init_source = (root / "apps/gateway/scripts/init-local-gateway.sh").read_text(encoding="utf-8")
owner_unit_source = (root / "deploy/user-systemd/faryo-owner.service").read_text(encoding="utf-8")
worker_unit_source = (root / "deploy/user-systemd/faryo-appserver-worker@.service").read_text(encoding="utf-8")
gateway_unit_source = (root / "deploy/user-systemd/faryo-gateway.service").read_text(encoding="utf-8")
assert "KillMode=process" in owner_unit_source and "KillMode=mixed" not in owner_unit_source, "Owner restart must preserve tmux/Codex cgroup children"
assert "internal run-appserver-worker %i" in worker_unit_source and "KillMode=mixed" in worker_unit_source, "each App Server session worker needs one validated systemd cgroup"
assert "PYTHONPATH=" not in owner_unit_source + gateway_unit_source and "sanitized_agent_environment" in owner_server, "Faryo service internals must not leak into managed tmux/Codex environments"
assert "self.session_clients" in appserver_runtime_source and "_require_session_client" in appserver_runtime_source and "WorkerServiceManager" in owner_asgi_source, "App Server turns must route through per-session workers"
assert "class AppServerSessionSupervisor" in appserver_supervisor_source and "restart_requested" in appserver_supervisor_source and "circuit_until" in appserver_supervisor_source, "per-session worker recovery and circuit isolation must remain explicit"
assert "recycle_codex_app_server_service" not in owner_backend_source and "faryo-appserver-worker@.service" in (root / "src/faryo_cli/installer.py").read_text(encoding="utf-8"), "shared whole-service writer recycling must stay retired"
assert "faryo_resolve_python" in owner_init_source and "faryo_resolve_python" in gateway_init_source, "initializers must use shared Python discovery"
assert 'or "720"' in gateway_init_source and "1 <= parsed_session_hours <= 720" in gateway_init_source, "Gateway initializer must keep the 30-day session contract"
assert 'id="historySearchInput"' in gateway and 'data-history-period="7d"' in gateway, "Gateway must expose metadata history search"
assert "agent_history_text_matches" in owner_session_catalog_source and "codex_conversation_history_page" not in owner_session_catalog_source[owner_session_catalog_source.index("def codex_history_page("):owner_session_catalog_source.index("def codex_history_items(")], "session search must not scan conversation history"
assert "ThreadingHTTPServer" not in owner_server and "uvicorn.Config" in owner_asgi_runner and "Starlette" in owner_asgi_source, "legacy Owner HTTP server must not return"
assert "access_log=False" in owner_asgi_runner and "def safe_log_path" in owner_http_source, "Owner logs must omit private query strings"
assert "ControlAuditStore" in gateway_config_source and "target_digest" in gateway_audit_source and "append_audit" in gateway_asgi_support and 'id="securityActivity"' in gateway, "Gateway must expose body-free control auditing"
assert "GatewayHandler" not in gateway and "ThreadingHTTPServer" not in gateway, "legacy Gateway HTTP server must not return"
assert "run_asgi.py" in gateway_runner and "FARYO_GATEWAY_HTTP_ENGINE" not in gateway_runner, "Gateway runner must remain ASGI-only"
assert "class FaryoServer" in gateway_asgi_runner and "close_owner_streams" in gateway_asgi_runner, "Gateway shutdown must release active Owner streams before waiting"
assert "/api/session-history/archive" in gateway and "/api/session-history/unarchive" in gateway, "Gateway must expose reversible history lifecycle controls"
assert "/api/session-history/delete" not in gateway_ui and '"thread/delete"' not in owner_backend_source, "Faryo must not expose hard thread deletion"
assert 'class="brand" href="/" aria-label="Faryo home"' in gateway, "Gateway brand must remain on the session home"
for retired_marker in ("/projects", "/api/project-workbench", "/api/faryo/start", "/api/faryo/dispatch", "/api/workorder"):
    assert retired_marker not in gateway_ui and retired_marker not in owner_backend_source, f"retired project orchestration route returned: {retired_marker}"
assert "PORTAL_CSS" not in gateway and "PORTAL_JS_TEMPLATE" not in gateway, "Gateway portal assets must stay external"
assert "def gateway_asset_revision" in gateway and "GATEWAY_ASSET_REVISION = gateway_asset_revision()" in gateway, "Gateway mutable assets must use automatic content revisions"
assert "GATEWAY_ASSET_REVISION = gateway_asset_revision()" in gateway and 'href="/workbench.css?v={asset_version}"' in gateway and 'src="/workbench.js?v={asset_version}"' in gateway, "Gateway must content-version external workbench assets"
assert 'id="faryoRouteLabels" type="application/json"' in gateway, "Gateway route labels must use the nonce-protected JSON bootstrap"
assert 'id="attentionCenter"' in gateway and 'id="notificationControl"' in gateway, "Gateway must expose body-free attention controls"
assert "processAttention" in gateway_workbench and "A session completed or needs input." in gateway_workbench, "Gateway attention must use generic notification text"
assert 'id="workstationPicker"' in gateway and "bindWorkstationPicker" in gateway_workbench and "routes: entries" in gateway_workbench and "selectNewRoute" not in gateway_workbench, "New Codex must choose workstation, backend, directory and context in one sheet"
assert "clientLaunchId" in gateway_workbench and "clientLaunchId" in (root / "apps/gateway/server/asgi_agents.py").read_text(encoding="utf-8") and "returned a stale launch response" in gateway_workbench, "New Codex redirects must be fenced by the current launch identity"
assert "syncChildren" not in gateway_workbench and "FaryoPreactWorkbench" in gateway_workbench, "Gateway card lists must remain Preact keyed components"
assert "dangerouslySetInnerHTML" not in gateway_preact_source and "innerHTML" not in gateway_preact_source, "Gateway card components must render server strings as text"
assert 'workbench-preact.js?v={asset_version}' in gateway and "workbench-preact.LICENSE.txt" in gateway, "Gateway Preact bundle and notice must remain local and content-versioned"
assert '"starting", "running", "waiting", "exited", "desktop", "resumable"' in gateway, "Gateway must expose explicit session lifecycle states"
assert "compact-rules-codex.js" in index, "index.html must load compact-rules-codex.js"
assert "compact-rules-codex.js" in gateway, "gateway must allow compact-rules-codex.js"
assert "compact-rules-claude.js" not in index, "retired Claude rules must not return to the production page"
assert "compact-rules-claude.js" not in gateway, "Gateway must not proxy retired Claude rules"
assert 'NEW_SESSION_COMMANDS = {"codex"}' in gateway, "Gateway must expose only the maintained Codex launcher"
assert "stable-blocks.js" in index, "index.html must load stable-blocks.js"
assert "stable-blocks.js" in gateway, "gateway must allow stable-blocks.js"
assert "question-navigator.js" in index, "index.html must load question-navigator.js"
assert "question-navigator.js" in gateway, "gateway must allow question-navigator.js"
assert "codex-commands.js" in index and "codex-commands.js" in gateway, "Codex command inventory must be loaded and proxied"
assert "copy-fidelity.js" in index and "copy-fidelity.js" in gateway, "copy fidelity must be loaded and proxied"
assert "clipboard-images.js" in index and "clipboard-images.js" in gateway, "clipboard image paste must be loaded and proxied"
assert "immersive-mode.js" in index and "immersive-mode.js" in gateway, "immersive display controller must be loaded and proxied"
assert "keyboard-layout.js?v=faryo-keyboard-layout-3" in index and "keyboard-layout.js" in gateway, "current keyboard layout controller must be loaded and proxied"
assert "withInteractiveWidget" in keyboard_layout_source and "keyboard.overlaysContent = false" in keyboard_layout_source and "'resizes-content'" in keyboard_layout_source, "keyboard layout must enforce native viewport resizing"
assert "overlaysContent = true" not in keyboard_layout_source and "keyboard-inset-height" not in owner_style, "retired VirtualKeyboard overlay reserve must not return"
assert "composer-layout.js?v=__FARYO_RELEASE_VERSION__" in index and "composer-layout.js" in gateway, "transparent composer layout must be cache-busted and proxied"
assert "ResizeObserver" in composer_layout_source and "--faryo-composer-reserve" in composer_layout_source, "composer reserve must follow measured content instead of fixed pixels"
assert "background: transparent" in owner_style and "grid-template-columns: minmax(0, 1fr)" in owner_style, "transparent composer must use one explicit App Shell column"
assert "grid-row: 2 / 4; grid-column: 1" in owner_style and "grid-row: 3; grid-column: 1" in owner_style, "conversation and composer must overlap in the same explicit Grid column"
assert "scroll-surface.js" not in index and "scroll-surface.js" not in gateway, "retired document scroll adapter must not return"
assert 'rel="manifest" href="/manifest.json"' in index, "every maintained PWA page must reference the root manifest"
assert 'id="immersiveExitBtn"' in index and 'id="detailsFullscreenBtn"' in index, "fullscreen must expose explicit enter and exit controls"
assert 'id="changesPanel"' in index and 'id="detailsChangesBtn"' in index, "Owner must expose read-only workspace changes"
assert "internal-annotations.js" in index, "index.html must load internal annotation formatting"
assert "internal-annotations.js" in gateway, "gateway must proxy internal annotation formatting"
assert "event-stream.js" in index, "index.html must load the authenticated event-stream parser"
assert "event-stream.js" in gateway, "gateway must proxy the authenticated event-stream parser"
assert "local-file-view.js" in gateway, "gateway must proxy the CSP-safe local file controls"
assert "vendor/markdown-ast/markdown-ast.min.js" in index, "index.html must load the AST Markdown bundle"
assert re.search(r'<script\s+type="module"\s+src="vendor/markdown-ast/highlight/highlight\.js\?', index), "index.html must load the Shiki module locally"
assert '"vendor/markdown-ast/"' in gateway, "gateway must proxy AST Markdown assets"
assert "live-scroll.js" in index, "index.html must load live-scroll.js"
assert "live-scroll.js" in gateway, "gateway must allow live-scroll.js"
assert "cdn.jsdelivr.net/npm/katex" not in index, "KaTeX must not require an external CDN"
assert 'vendor/katex/katex.min.css?v=0.18.4' in index, "index.html must load local KaTeX CSS"
assert '"vendor/katex/"' in gateway, "gateway must proxy local KaTeX assets"
for relative in (
    "katex.min.css",
    "fonts/KaTeX_Main-Regular.woff2",
    "LICENSE",
):
    assert (root / "apps/owner/local-tmux-owner/static/vendor/katex" / relative).is_file(), f"missing vendored KaTeX asset: {relative}"
assert "cdn.jsdelivr.net" not in index, "Markdown and math must not require an external CDN"
assert '"vendor/diff-review/"' in gateway, "Gateway must proxy local diff-review assets"
assert '"owner/"' in gateway, "Gateway must proxy Owner native ES modules"
diff_review_root = root / "apps/owner/local-tmux-owner/static/vendor/diff-review"
diff_review_manifest = json.loads((diff_review_root / "manifest.json").read_text(encoding="utf-8"))
assert diff_review_manifest.get("schemaVersion") == 1, "unsupported diff-review manifest"
assert diff_review_manifest.get("packages") == {"diff2html": "3.4.56", "dompurify": "3.4.14"}, "diff-review versions drifted"
for relative in diff_review_manifest.get("assets", {}):
    assert (diff_review_root / relative).is_file(), f"missing diff-review asset: {relative}"
assert "vendor/markdown-it/" not in index, "legacy markdown-it must not remain in the production page"
assert "math-render.js" not in index, "legacy math DOM post-processing must not remain in the production page"
for relative in ("markdown-ast.min.js", "THIRD_PARTY_NOTICES.md", "THIRD_PARTY_LICENSES.txt", "highlight/highlight.js", "highlight/manifest.json"):
    assert (root / "apps/owner/local-tmux-owner/static/vendor/markdown-ast" / relative).is_file(), f"missing AST Markdown asset: {relative}"
asset_root = root / "apps/owner/local-tmux-owner/static/vendor/markdown-ast"
manifest = json.loads((asset_root / "highlight/manifest.json").read_text(encoding="utf-8"))
assert manifest.get("schemaVersion") == 1, "unsupported Shiki asset manifest"
assert manifest.get("entry") == "highlight/highlight.js", "unexpected Shiki entry"
manifest_paths = [item.get("path", "") for item in manifest.get("files", [])]
assert len(manifest_paths) == len(set(manifest_paths)) and manifest_paths, "Shiki manifest paths must be non-empty and unique"
for relative in manifest_paths:
    path = Path(relative)
    assert path.parts and path.parts[0] == "highlight" and ".." not in path.parts and not path.is_absolute(), f"unsafe Shiki manifest path: {relative}"
    assert (asset_root / path).is_file(), f"missing Shiki chunk: {relative}"
for grammar in ("python", "latex", "lean", "matlab", "markdown", "yaml", "html", "css", "cpp", "c", "rust", "go", "java", "sql"):
    assert any(Path(relative).name.startswith(grammar + "-") for relative in manifest_paths), f"missing lazy Shiki grammar: {grammar}"
assert (root / "tools/markdown-engine/package-lock.json").is_file(), "AST Markdown build must have a lockfile"
app = (root / "apps/owner/local-tmux-owner/static/app.js").read_text(encoding="utf-8")
changes_panel_source = (root / "apps/owner/local-tmux-owner/static/owner/changes-panel.mjs").read_text(encoding="utf-8")
activity_groups_source = (root / "apps/owner/local-tmux-owner/static/owner/activity-groups.mjs").read_text(encoding="utf-8")
api_client_source = (root / "apps/owner/local-tmux-owner/static/owner/api-client.mjs").read_text(encoding="utf-8")
attachment_controller_source = (root / "apps/owner/local-tmux-owner/static/owner/attachment-controller.mjs").read_text(encoding="utf-8")
history_controller_source = (root / "apps/owner/local-tmux-owner/static/owner/history-controller.mjs").read_text(encoding="utf-8")
rich_block_controller_source = (root / "apps/owner/local-tmux-owner/static/owner/rich-block-controller.mjs").read_text(encoding="utf-8")
capture_controller_source = (root / "apps/owner/local-tmux-owner/static/owner/capture-controller.mjs").read_text(encoding="utf-8")
composer_delivery_source = (root / "apps/owner/local-tmux-owner/static/owner/composer-delivery.mjs").read_text(encoding="utf-8")
goal_status_source = (root / "apps/owner/local-tmux-owner/static/owner/goal-status.mjs").read_text(encoding="utf-8")
status_controller_source = (root / "apps/owner/local-tmux-owner/static/owner/status-controller.mjs").read_text(encoding="utf-8")
assert "document.currentScript" in app and "assetRevision" in app and "encodeURIComponent(assetRevision)" in app, "Owner modules must inherit the content-versioned app revision"
for owner_module in (
    "changes-panel.mjs",
    "activity-groups.mjs",
    "api-client.mjs",
    "attachment-controller.mjs",
    "history-controller.mjs",
    "rich-block-controller.mjs",
    "capture-controller.mjs",
    "composer-delivery.mjs",
    "goal-status.mjs",
    "status-controller.mjs",
):
    assert f"ownerModule('{owner_module}')" in app, f"Owner module is not revisioned: {owner_module}"
assert "createStatusController" in app and "acceptScope" in status_controller_source and "refreshRunId" in status_controller_source, "Owner status projection must reject stale session responses"
assert "groupActivityBlocks" in app and "isReasoningPlaceholder" in activity_groups_source and "activityGroupSummary" in activity_groups_source and "compact-activity-card" in owner_style, "App Server activity must suppress empty reasoning and expose collapsed tool summaries by turn"
assert "statusRefreshRunId" not in app and "activeStatusRefreshController" not in app, "status request ownership must not return to app.js"
assert "/api/workspace-changes" in owner_asgi_read_source and "/api/workspace-changes" in changes_panel_source, "workspace changes must use the scoped read-only Owner API"
assert "/api/capabilities" in owner_asgi_read_source and "/api/diagnostics" in owner_asgi_read_source and "loadOwnerCapabilities" in app, "Owner must expose versioned redacted diagnostics"
assert '"pendingQueueManagement": False' in runtime_diagnostics_source and '"pendingQueue": "unsupported"' in runtime_diagnostics_source, "Faryo must not overclaim editable Codex queues"
assert '"browserEnvelope": "v1"' in runtime_diagnostics_source and '"appServerWriter"' in runtime_diagnostics_source and '"codexTuiWriter"' in runtime_diagnostics_source and '"webManagedWriter"' not in runtime_diagnostics_source, "browser protocol and backend diagnostics must use current domain names"
assert '"queuedSendNow": True' in runtime_diagnostics_source and '"queuedSendNow": "escape-when-advertised"' in runtime_diagnostics_source, "Faryo must expose only the explicit Esc send-now capability"
assert "shell=True" not in workspace_changes_source and "--no-ext-diff" in workspace_changes_source and "--no-textconv" in workspace_changes_source, "workspace diff must remain fixed and read-only"
stable_blocks_source = (root / "apps/owner/local-tmux-owner/static/stable-blocks.js").read_text(encoding="utf-8")
assert "stableBlocks.reconcile(output, models, createNode)" in app, "Compact Chat must reconcile stable DOM blocks"
assert '"X-Owner-Token": ownerToken' in api_client_source and '"X-Faryo-Csrf"' in api_client_source, "Owner API client must preserve token and CSRF headers"
assert "async function api(" not in app and "async function gatewayCsrfHeaders(" not in app, "Owner API implementation must not return to app.js"
assert "sessionStorage.setItem(OWNER_TOKEN_STORAGE_KEY" in app, "direct Owner auth must survive same-tab refresh without URL persistence"
assert "new EventSource" not in app, "Owner streaming must support the authentication header"
assert "token=${encodeURIComponent(ownerToken)}" not in app, "Owner streaming must not place the token in request URLs"
assert "authenticatedApiPath" not in app, "local resource DOM URLs must not append the Owner token"
assert "fetchProtectedResource" in app, "direct Owner resources must use authenticated fetches"
assert "data-faryo-fetch-href" in app, "protected file links must use deferred authenticated fetches"
assert "data-faryo-fetch-src" in app, "protected images must use deferred authenticated fetches"
assert "target.searchParams.delete('token')" in app, "protected resource fetches must strip query tokens"
assert 'id="statusShellRoot"' in index and 'id="quotaText"' in owner_status_source and 'id="detailsQuota"' in index, "Owner must expose Preact weekly quota and details"
for asset in ("style.css", "owner-ui.js", "copy-fidelity.js", "app.js"):
    assert f'{asset}?v=__FARYO_RELEASE_VERSION__' in index, f"Owner release must cache-bust {asset}"
owner_static_match = re.search(r"OWNER_STATIC_FILES\s*=\s*\{([^}]*)\}", gateway)
assert owner_static_match, "Gateway Owner static allowlist is unavailable"
owner_static_files = set(re.findall(r'"([A-Za-z0-9._-]+)"', owner_static_match.group(1)))
owner_static_prefixes = ("owner/", "vendor/", "icons/", "pet/")
for source in re.findall(r'<script(?:\s+type="[^"]+")?\s+src="([^"?]+)', index):
    assert source in owner_static_files or source.startswith(owner_static_prefixes), f"Gateway cannot proxy Owner script: {source}"
assert "Week ${remaining}% left" in app, "Owner must label weekly quota as remaining allowance"
assert "contextWindowSource === 'agent-reported'" in app, "Owner must distinguish reported context windows from fallbacks"
assert "usedTokens" in app and "contextWindow" in app, "Owner must show actual context token counts"
assert "sendWithDeliveryRecovery" in app, "Owner must reconcile ambiguous send responses idempotently"
assert "queuedSendNowAvailable" in app and "queuedFollowupExpedited" in app and "aria-keyshortcuts" in app, "Owner must expose queued follow-up Esc/send-now semantics"
assert "button.textContent = '⧉'" in app, "confirmed output copy button must remain unchanged"
assert "copyFidelity?.handleCopy(event)" in app, "Compact Chat selections must use source-faithful copy"
assert 'promptInput.addEventListener("paste"' in attachment_controller_source, "Owner composer must handle user-triggered image paste"
assert "MAX_ATTACHMENTS = 35" in app and "uploadConcurrency: 4" in app, "Owner must bound 35-file attachment batches to four concurrent uploads"
assert "olderLoadQueued" in history_controller_source and "function emptyConversationHistory(" not in app, "Owner paged history state must remain in its controller"
assert '"messageBlocks"' in owner_server and "def message_blocks(" in appserver_session_source and "def _project_message_blocks(" in appserver_history_source and "message_blocks=[" in owner_server and '"blocks": list(item["blocks"])' in appserver_history_source, "App Server roles must stay structured and grouped by turn through capture and history"
assert "appserver_rollout.activity_blocks" in owner_server and 'payload_type == "custom_tool_call"' in appserver_rollout_source and 'payload_type == "patch_apply_end"' in appserver_rollout_source and "durable_activity=durable_activity" in owner_server, "App Server history must recover durable command and file activity after reconnects"
assert "CommandTimelineStore" in command_timeline_source and "NON_DURABLE_COMMANDS" in command_timeline_source and "commandEvents" in owner_server, "browser-issued commands must use the private typed command lifecycle"
assert 'path == "/api/activity-detail"' in owner_asgi_read_source and "activity_projection" in appserver_session_source and "appserver_rollout.activity_detail" in owner_server, "typed activity details must remain authenticated, bounded and reconnect-safe"
assert "mergeCommandEvents" in app and "activity?.detailAvailable" in app and "page.reload" in command_activity_browser_source, "command rows and on-demand activity details need an ordinary-reload browser gate"
assert "const historyBlocks = displayBlocks()" in history_controller_source and "messageBlocks: mergeMessageBlocks(historyBlocks, liveBlocks, {" in history_controller_source and "streaming: capture.streaming === true" in history_controller_source and "if (capture.streaming) return capture" in history_controller_source, "settled history must reconcile rather than replace a live App Server capture"
assert "recoveredDuplicate" in history_controller_source and "historyActivityTypes.get(scope)?.has(activityType)" in history_controller_source, "settled App Server activity wrappers must not accumulate after durable history"
assert '"codex-jsonl"' in history_controller_source and "messageBlocks: historyBlocks" in history_controller_source and "def codex_history_turn_content(" in owner_server, "TUI JSONL history must preserve authoritative message boundaries"
assert "appserver-stream-progress" in app and "appserver-stream-progress" in owner_style, "App Server streaming must expose a visible progress state"
assert "state.activeLengthCount < 2" in real_appserver_browser_source and "state.loadedQuestionMarkers < 2" in real_appserver_browser_source and "state.userBlockCount < 2" in real_appserver_browser_source and "!questionJump.targetUser" in real_appserver_browser_source, "real App Server browser validation must prove incremental roles and a working question jump"
assert "FARYO_SMOKE_MIN_COMMANDS" in durable_activity_browser_source and "page.reload" in durable_activity_browser_source and "compact-activity-item" in durable_activity_browser_source, "durable activity browser validation must inspect real items after an ordinary reload"
assert "Keep one identity domain for the lifetime of an App Server" in owner_asgi_read_source and "core.web_conversation_history_page" in owner_asgi_read_source, "active App Server history must not switch to incompatible rollout question keys"
assert "terminal_target = None if web_managed else self.support.target(session)" in owner_asgi_events_source and "except self.core.OwnerError:\n                    return" in owner_asgi_events_source, "event streams must reject unknown sessions before headers and close cleanly when a session disappears"
assert "IntersectionObserver" in rich_block_controller_source and "shouldRenderEagerly" in app, "Owner long histories must render rich blocks near the viewport instead of mounting every formula at once"
assert "retryEventStream" in capture_controller_source and "function consumeEventStream(" not in app, "Owner capture transport must remain in its controller"
assert "isAmbiguousDeliveryError" in composer_delivery_source and "function isAmbiguousDeliveryError(" not in app, "Owner ambiguous delivery recovery must remain in its controller"
assert "goal-status.mjs" in app and 'id="goalPill"' in owner_status_source and 'id="detailsGoal"' in index, "Owner must render Preact goal status in the header and details"
assert "objective" not in goal_status_source and '"goalStatus": goal_status' in owner_server, "Owner goal UI must receive status-only metadata"
assert '"thread/goal/get"' in appserver_commands_source and 'path == "/api/goal"' in owner_asgi_read_source and "interaction-confirm-run" in owner_interaction_source, "Owner must expose on-demand Goal details and structured command confirmation"
assert "navigator.clipboard.read(" not in app + attachment_controller_source, "Owner must not read the clipboard outside a paste event"
assert "lastCompactCapture" in app and "lastFullCapture" in app and "renderModeLoading" in app, "Chat and Raw must keep isolated capture caches"
assert "renderOutput(lastCapture)" not in app, "compact callbacks must not replay a Raw capture"
assert 'id="approveSmallBtn"' not in index and 'class="key-nav"' not in index and "InteractionHost" in owner_interaction_source, "retired raw TUI buttons must stay replaced by the structured Preact interaction host"
assert all(route not in app + owner_backend_source for route in ("/api/approve", "/api/up", "/api/down")), "retired raw TUI key endpoints must not return"
assert "interaction-choose" in owner_interaction_source and "activeInteractionId" in owner_interaction_source, "structured fallback must expose Choose and isolate late responses"
assert "syncStructuredInteraction(null)" in app and "ignored: true" in app, "session switches must fence pending interaction responses"
assert "commandSuggestionIndex" not in app and "handleCommandSuggestionKey" not in app, "Preact CommandPalette must own selection and keyboard state"
assert "start_agent_runtime_async" in owner_asgi_control_source and "AGENT_START_MONITORS" in owner_server and '"state": launch_state or "starting"' in owner_asgi_control_source, "Codex launch must return an async starting receipt with a recoverable monitor"
assert "session unknown" not in index and 'id="transcriptShellRoot"' in index and "Starting Codex" in owner_transcript_source, "Owner startup UI must use the Preact transcript state instead of a false unknown-session state"
assert "generation" in owner_conversation_store_source and "accepts" in owner_conversation_store_source and "acceptScope" in capture_controller_source, "Owner transcript transport must reject obsolete session generations"
assert "CODEX_LIVE_TAIL_LINES = 180" in owner_server, "Live tmux must keep the bounded long tail"
assert "faryoTransient" in stable_blocks_source and "selectionInsideLivePanel" in app and "compact-live-copy" in app, "Live tmux DOM, selection, and copy must remain stable"
assert "compactOutputSources" not in app and "dataset.sourceIndex" not in app, "retired copy source indexing must not return"
appearance = (root / "apps/shared/static/appearance.css").read_text(encoding="utf-8")
assert "--bg: #0F1115" in appearance and "--accent: #7188FF" in appearance, "shared dark palette must match Owner"
assert "--bg: #F6F7F9" in appearance and "--accent: #5369E7" in appearance, "shared light palette must match Owner"
assert "Files to session" in gateway and "Send to…" in gateway_preact_source, "Gateway must expose explicit file-to-session controls"
assert "No handoff package" not in gateway_ui, "Gateway must not expose unexplained handoff copy"
for retired in (
    "apps/owner/local-tmux-owner/static/compact-rules-claude.js",
    "apps/owner/scripts/claude-session-stamp.sh",
    "scripts/package-client.sh",
    "scripts/install-macos-owner.sh",
    "scripts/status-runtime.sh",
    "deploy/launchd/dev.faryo.owner.keepalive.plist",
    "docs/assets/ui-targets",
    "docs/launch/faryo-1.0.0.md",
    "RELEASE",
    "apps/gateway/RELEASE",
    "apps/owner/local-tmux-owner/static/pet/pet-carrying.png",
    "apps/owner/local-tmux-owner/static/pet/pet-idle.png",
    "apps/owner/local-tmux-owner/static/pet/pet-offline.png",
    "apps/owner/local-tmux-owner/static/pet/pet-resting.png",
    "apps/owner/local-tmux-owner/static/pet/pet-working.png",
    "apps/gateway/server/static/projects.html",
    "apps/gateway/server/static/projects.css",
    "apps/gateway/server/static/projects.js",
    "apps/gateway/server/faryo_profile.md",
    "apps/gateway/server/templates/workorder.md",
    "apps/owner/local-tmux-owner/workbench_state.py",
    "apps/owner/scripts/sync-project-workbench.sh",
    "apps/shared/pd_state.py",
):
    assert not (root / retired).exists(), f"retired source returned: {retired}"
PY
}

release_checks
