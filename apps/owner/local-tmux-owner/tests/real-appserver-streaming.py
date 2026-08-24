#!/usr/bin/env python3
"""Opt-in real Codex App Server delta/final test with an isolated CODEX_HOME."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

import uvicorn


APP_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[4]
for value in (str(APP_DIR), str(REPO_ROOT / "src")):
    if value not in sys.path:
        sys.path.insert(0, value)

import appserver_history
from appserver_runtime import AppServerRuntime
import codex_history
from faryo_cli import codex_runtime
from faryo_cli.appserver_workers import validate_worker_id
import owner_asgi
import server


PROMPT = """Reply without tools. Write a short Chinese Markdown demonstration containing:
1. a heading;
2. the inline formula $x^2+y^2=z^2$;
3. the display formula $$\\int_0^1 x^2\\,dx=\\frac13$$;
4. a fenced Python code block.
End with the literal word STREAM_DONE.
"""
RESTART_PROMPT = """Reply without tools. Write 40 short numbered Chinese lines about restart-safe streaming,
then end with the literal word OWNER_RESTART_DONE.
"""
APPROVAL_PROMPT = """Create a file named approval-probe.txt in the current workspace containing exactly
FARYO_APPROVAL_COMMAND_OK, then read it back. This isolated workspace starts read-only, so request approval
for the required write. After it completes, briefly report the content and end with APPROVAL_DONE.
"""
USER_INPUT_PROMPT = """Use the request_user_input tool exactly once. Ask me to choose between Alpha and Beta,
with header Choice. After I answer, state the selected value and end with USER_INPUT_DONE. Do not use other tools.
"""
ISOLATION_SECOND_PROMPT = "Reply exactly with SECOND_WORKER_OK. Do not use tools."
ISOLATION_FIRST_RECOVERY_PROMPT = "Reply exactly with FIRST_WORKER_RECOVERED. Do not use tools."
ISOLATION_SECOND_AFTER_CRASH_PROMPT = "Reply exactly with SECOND_WORKER_UNAFFECTED. Do not use tools."
ISOLATION_CLI_PROMPT = "Reply exactly with ORDINARY_CODEX_CLI_OK. Do not use tools."


class SubprocessWorkerManager:
    """Real-test worker supervisor with the same one-process-per-session boundary."""

    def __init__(
        self,
        root: Path,
        executable: str,
        workspace: Path,
        environment: dict[str, str],
    ) -> None:
        self.root = root
        self.executable = executable
        self.workspace = workspace
        self.environment = environment
        self.processes: dict[str, subprocess.Popen[bytes]] = {}
        self.logs: dict[str, object] = {}
        self.lock = threading.RLock()

    def socket_path(self, worker_id: str) -> Path:
        return self.root / f"{validate_worker_id(worker_id)}.sock"

    def start(self, worker_id: str, *, timeout: float = 12.0) -> Path:
        selected = validate_worker_id(worker_id)
        with self.lock:
            process = self.processes.get(selected)
            path = self.socket_path(selected)
            if process is not None and process.poll() is None and path.is_socket():
                return path
            self._stop_locked(selected)
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.unlink(missing_ok=True)
            argv = codex_runtime.codex_argv(
                self.executable,
                "app-server",
                "--listen",
                f"unix://{path}",
            )
            handle = (self.root / f"{selected}.log").open("ab")
            process = subprocess.Popen(
                argv,
                cwd=self.workspace,
                env=self.environment,
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            self.logs[selected] = handle
            self.processes[selected] = process
        wait_for_socket(path, process, timeout)
        return path

    def restart(self, worker_id: str, *, timeout: float = 12.0) -> Path:
        self.stop(worker_id, timeout=timeout)
        return self.start(worker_id, timeout=timeout)

    def stop(self, worker_id: str, *, timeout: float = 12.0) -> None:
        selected = validate_worker_id(worker_id)
        with self.lock:
            self._stop_locked(selected, timeout=timeout)

    def pid(self, worker_id: str) -> int:
        selected = validate_worker_id(worker_id)
        with self.lock:
            process = self.processes.get(selected)
            if process is None or process.poll() is not None:
                raise RuntimeError("real App Server worker is not running")
            return process.pid

    def crash(self, worker_id: str) -> None:
        selected = validate_worker_id(worker_id)
        with self.lock:
            process = self.processes.pop(selected, None)
            handle = self.logs.pop(selected, None)
            if process is None or process.poll() is not None:
                raise RuntimeError("real App Server worker is not running")
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
            if handle is not None:
                handle.close()
            self.socket_path(selected).unlink(missing_ok=True)

    def _stop_locked(self, worker_id: str, *, timeout: float = 5.0) -> None:
        process = self.processes.pop(worker_id, None)
        handle = self.logs.pop(worker_id, None)
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=max(0.1, timeout))
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=5)
        if handle is not None:
            handle.close()
        self.socket_path(worker_id).unlink(missing_ok=True)

    def close_all(self) -> None:
        with self.lock:
            for worker_id in list(self.processes):
                self._stop_locked(worker_id)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex", default="")
    parser.add_argument("--auth-file", default="")
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--browser", action="store_true")
    parser.add_argument("--restart", action="store_true")
    parser.add_argument("--interactions", action="store_true")
    parser.add_argument("--isolation", action="store_true")
    return parser.parse_args()


def wait_for_socket(path: Path, process: subprocess.Popen[bytes], timeout: float = 12.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_socket():
            return
        if process.poll() is not None:
            raise RuntimeError("Codex App Server exited before opening its private socket")
        time.sleep(0.05)
    raise RuntimeError("Codex App Server did not open its private socket")


def jsonl_has_final(codex_home: Path, expected: str) -> bool:
    for path in codex_home.glob("sessions/**/*.jsonl"):
        try:
            with path.open(encoding="utf-8", errors="replace") as stream:
                for line in stream:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    message = codex_history.rollout_message(event)
                    if message == ("assistant", expected):
                        return True
        except OSError:
            continue
    return False


def free_port() -> int:
    with socket.socket() as handle:
        handle.bind(("127.0.0.1", 0))
        return int(handle.getsockname()[1])


def wait_for_interaction(runtime: AppServerRuntime, session: str, kind: str, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        interaction = (runtime.capture(session).get("snapshot") or {}).get("interaction")
        if isinstance(interaction, dict) and interaction.get("kind") == kind:
            return interaction
        time.sleep(0.05)
    raise RuntimeError(f"real Codex {kind} request did not arrive")


def wait_for_final_marker(runtime: AppServerRuntime, session: str, marker: str, timeout: float) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        capture = runtime.capture(session)
        assistants = [text for role, text in capture["messages"] if role == "assistant"]
        final = assistants[-1] if assistants else ""
        if capture["snapshot"].get("lifecycle") == "idle" and marker in final:
            return final
        time.sleep(0.05)
    raise RuntimeError(f"real Codex response did not finish with {marker}")


def start_plan_turn(runtime: AppServerRuntime, session: str, prompt: str, client_message_id: str) -> None:
    """Start one isolated plan-mode turn so real request_user_input is available."""

    record = runtime.registry.get(session)
    if record is None:
        raise RuntimeError("real Codex session registry entry is unavailable")
    capture = runtime.capture(session)
    thread = capture.get("snapshot", {}).get("thread") or {}
    model = str(thread.get("model") or record.model or "")
    client = runtime._require_session_client(session)  # Test-only access to the session worker.
    if not model:
        result = runtime._submit(client.rpc("model/list", {"includeHidden": False, "limit": 100}), 15)
        models = result.get("data") if isinstance(result, dict) else None
        first = models[0] if isinstance(models, list) and models else None
        model = str((first or {}).get("model") or (first or {}).get("id") or "")
    if not model:
        raise RuntimeError("real Codex model catalog is empty")
    runtime._submit(
        client.rpc(
            "turn/start",
            {
                "threadId": record.thread_id,
                "input": [{"type": "text", "text": prompt}],
                "clientUserMessageId": client_message_id,
                "collaborationMode": {"mode": "plan", "settings": {"model": model}},
            },
        ),
        15,
    )


def main() -> int:
    args = parse_args()
    source_home = Path.home()
    auth_file = Path(args.auth_file).expanduser() if args.auth_file else source_home / ".codex/auth.json"
    if not auth_file.is_file():
        raise RuntimeError("Codex authentication is unavailable for the isolated test")
    executable = codex_runtime.resolve_codex(args.codex, source_home, os.environ)
    if not executable:
        raise RuntimeError("Codex CLI is unavailable")

    with tempfile.TemporaryDirectory(prefix="faryo-real-appserver-") as temp:
        root = Path(temp)
        codex_home = root / "codex-home"
        workspace = root / "workspace"
        runtime_root = root / "runtime"
        for path in (codex_home, workspace, runtime_root):
            path.mkdir(mode=0o700)
        copied_auth = codex_home / "auth.json"
        shutil.copyfile(auth_file, copied_auth)
        copied_auth.chmod(0o600)
        if args.interactions:
            config_file = codex_home / "config.toml"
            config_file.write_text(
                'approval_policy = "on-request"\nsandbox_mode = "read-only"\n',
                encoding="utf-8",
            )
            config_file.chmod(0o600)
        socket_path = runtime_root / "codex-app-server.sock"
        registry_path = runtime_root / "sessions.json"
        argv = codex_runtime.codex_argv(
            executable,
            "app-server",
            "--listen",
            f"unix://{socket_path}",
        )
        environment = codex_runtime.codex_environment(argv, os.environ)
        environment["CODEX_HOME"] = str(codex_home)
        log_path = runtime_root / "appserver.log"
        process: subprocess.Popen[bytes] | None = None
        worker_manager: SubprocessWorkerManager | None = None
        runtime: AppServerRuntime | None = None
        web_server: uvicorn.Server | None = None
        web_thread: threading.Thread | None = None
        try:
            with log_path.open("wb") as log:
                process = subprocess.Popen(
                    argv,
                    cwd=workspace,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
                wait_for_socket(socket_path, process)
                worker_manager = SubprocessWorkerManager(
                    runtime_root / "workers",
                    executable,
                    workspace,
                    environment,
                )
                runtime = AppServerRuntime(
                    socket_path=socket_path,
                    registry_path=registry_path,
                    client_version="real-test",
                    worker_manager=worker_manager,
                )
                runtime.start()
                if not runtime.wait_ready(12):
                    raise RuntimeError("Faryo did not initialize the real Codex App Server")
                started = runtime.start_session(
                    cwd=str(workspace),
                    title="Faryo streaming test",
                    launch_id="real_appserver_streaming_test",
                )
                session = str(started["session"])
                cursor = runtime.replay(None).latest.render()
                runtime.send(session, PROMPT, "real_appserver_streaming_message")

                events = []
                partial_lengths: list[int] = []
                final_text = ""
                deadline = time.monotonic() + max(30.0, args.timeout)
                while time.monotonic() < deadline:
                    replay = runtime.wait_for_events(cursor, 1.0)
                    cursor = replay.latest.render()
                    events.extend(replay.events)
                    capture = runtime.capture(session)
                    assistants = [text for role, text in capture["messages"] if role == "assistant"]
                    if assistants:
                        partial_lengths.append(len(assistants[-1]))
                        final_text = assistants[-1]
                    observed_kinds = {event.kind for event in events}
                    if (
                        capture["snapshot"].get("lifecycle") == "idle"
                        and final_text
                        and {"item.delta", "item.final", "turn.completed"} <= observed_kinds
                    ):
                        break
                else:
                    raise RuntimeError("real Codex turn did not settle before the timeout")

                kinds = [event.kind for event in events]
                if "item.delta" not in kinds or "item.final" not in kinds or "turn.completed" not in kinds:
                    raise RuntimeError("real Codex notifications did not include delta, final, and turn completion")
                if not all(marker in final_text for marker in ("x^2+y^2=z^2", "\\int_0^1", "```", "STREAM_DONE")):
                    raise RuntimeError("real Codex final response lost Markdown or TeX structure")
                if "STREAM_DONE" in repr([event.payload for event in events]):
                    raise RuntimeError("the replay journal retained message body content")

                capture = runtime.capture(session)
                history = appserver_history.conversation_history_page(
                    capture["snapshot"],
                    thread_id=str(started["threadId"]),
                    limit=12,
                    max_page_turns=24,
                    page_char_budget=2 * 1024 * 1024,
                    preview_chars=96,
                    updated_at=lambda: "now",
                )
                if history["totalTurns"] < 1 or "STREAM_DONE" not in history["turns"][-1]["text"]:
                    raise RuntimeError("live App Server history did not converge to the final turn")
                jsonl_deadline = time.monotonic() + 5
                while time.monotonic() < jsonl_deadline and not jsonl_has_final(codex_home, final_text):
                    time.sleep(0.05)
                if not jsonl_has_final(codex_home, final_text):
                    raise RuntimeError("Codex JSONL did not contain the authoritative final response")

                delta_events = [event for event in events if event.kind == "item.delta"]
                print(
                    "real-appserver-streaming=PASS "
                    f"delta_batches={len(delta_events)} "
                    f"max_batch={max(int(event.payload.get('batchCount') or 1) for event in delta_events)} "
                    f"observed_lengths={len(set(partial_lengths))} "
                    "markdown=yes tex=yes jsonl=yes body_free_journal=yes"
                )
                if args.isolation:
                    second_started = runtime.start_session(
                        cwd=str(workspace),
                        title="Faryo isolation peer",
                        launch_id="real_appserver_isolation_peer",
                    )
                    second_session = str(second_started["session"])
                    runtime.send(
                        second_session,
                        ISOLATION_SECOND_PROMPT,
                        "real_appserver_isolation_second_initial",
                    )
                    wait_for_final_marker(
                        runtime,
                        second_session,
                        "SECOND_WORKER_OK",
                        max(30.0, args.timeout),
                    )
                    first_record = runtime.registry.get(session)
                    second_record = runtime.registry.get(second_session)
                    if first_record is None or second_record is None:
                        raise RuntimeError("real isolation registry entries are unavailable")
                    first_client = runtime._require_session_client(session)
                    second_client = runtime._require_session_client(second_session)
                    first_loaded = runtime._submit(
                        first_client.rpc("thread/loaded/list", {}),
                        5,
                    )
                    second_loaded = runtime._submit(
                        second_client.rpc("thread/loaded/list", {}),
                        5,
                    )
                    control_client = runtime._require_control_client()
                    control_loaded = runtime._submit(
                        control_client.rpc("thread/loaded/list", {}),
                        5,
                    )
                    if set(first_loaded.get("data") or []) != {first_record.thread_id}:
                        raise RuntimeError("first real worker loaded more than its own thread")
                    if set(second_loaded.get("data") or []) != {second_record.thread_id}:
                        raise RuntimeError("second real worker loaded more than its own thread")
                    if {first_record.thread_id, second_record.thread_id} & set(control_loaded.get("data") or []):
                        raise RuntimeError("control App Server loaded a Faryo Web thread")

                    cli_output = runtime_root / "ordinary-cli-final.txt"
                    cli_result = subprocess.run(
                        codex_runtime.codex_argv(
                            executable,
                            "exec",
                            "--skip-git-repo-check",
                            "--ignore-user-config",
                            "--ignore-rules",
                            "--sandbox",
                            "read-only",
                            "--output-last-message",
                            str(cli_output),
                            ISOLATION_CLI_PROMPT,
                        ),
                        cwd=workspace,
                        env=environment,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=max(30.0, args.timeout),
                        check=False,
                    )
                    cli_final = cli_output.read_text(encoding="utf-8") if cli_output.is_file() else ""
                    if cli_result.returncode or cli_final.strip() != "ORDINARY_CODEX_CLI_OK":
                        raise RuntimeError("ordinary Codex CLI failed while App Server workers were active")

                    conflict_started = time.monotonic()
                    try:
                        runtime._submit(
                            control_client.rpc(
                                "thread/resume",
                                {"threadId": first_record.thread_id},
                                timeout=3.0,
                                overload_attempts=1,
                            ),
                            4,
                        )
                    except Exception:
                        conflict_elapsed = time.monotonic() - conflict_started
                    else:
                        raise RuntimeError("a second App Server writer unexpectedly resumed the same thread")
                    if conflict_elapsed >= 4:
                        raise RuntimeError("same-thread writer conflict did not fail quickly")

                    first_generation = first_record.worker_generation
                    second_generation = second_record.worker_generation
                    second_pid = worker_manager.pid(second_record.worker_id)
                    worker_manager.crash(first_record.worker_id)
                    recovery_deadline = time.monotonic() + 20
                    while time.monotonic() < recovery_deadline:
                        recovered_record = runtime.registry.get(session)
                        if (
                            recovered_record is not None
                            and recovered_record.worker_generation > first_generation
                            and recovered_record.worker_state == "ready"
                        ):
                            break
                        time.sleep(0.05)
                    else:
                        raise RuntimeError("crashed real worker did not recover independently")
                    second_after = runtime.registry.get(second_session)
                    if second_after is None or second_after.worker_generation != second_generation:
                        raise RuntimeError("peer worker generation changed during isolated recovery")
                    if runtime._require_session_client(second_session) is not second_client:
                        raise RuntimeError("peer worker client changed during isolated recovery")
                    if worker_manager.pid(second_record.worker_id) != second_pid:
                        raise RuntimeError("peer worker process changed during isolated recovery")

                    runtime.send(
                        second_session,
                        ISOLATION_SECOND_AFTER_CRASH_PROMPT,
                        "real_appserver_isolation_second_after_crash",
                    )
                    second_final = wait_for_final_marker(
                        runtime,
                        second_session,
                        "SECOND_WORKER_UNAFFECTED",
                        max(30.0, args.timeout),
                    )
                    runtime.send(
                        session,
                        ISOLATION_FIRST_RECOVERY_PROMPT,
                        "real_appserver_isolation_first_recovered",
                    )
                    first_final = wait_for_final_marker(
                        runtime,
                        session,
                        "FIRST_WORKER_RECOVERED",
                        max(30.0, args.timeout),
                    )
                    if "SECOND_WORKER_UNAFFECTED" not in second_final or "FIRST_WORKER_RECOVERED" not in first_final:
                        raise RuntimeError("real isolation final responses did not converge")
                    runtime.close_session(second_session)
                    print(
                        "real-appserver-isolation=PASS "
                        "control=read-only writers=one-per-worker cli=new-thread conflict=fast "
                        "crash=isolated peer_pid=stable recovery=same-thread"
                    )
                if args.interactions:
                    runtime.send(
                        session,
                        APPROVAL_PROMPT,
                        "real_appserver_approval_message",
                    )
                    approval = wait_for_interaction(runtime, session, "approval", max(30.0, args.timeout))
                    allow = next(
                        (option for option in approval.get("options") or [] if option.get("label") == "Allow once"),
                        None,
                    )
                    details = approval.get("details") or {}
                    if not isinstance(allow, dict) or not (
                        str(details.get("command") or "") or str(details.get("path") or "")
                    ):
                        raise RuntimeError("real Codex approval request lost its safe action projection")
                    runtime.respond_interaction(
                        session,
                        interaction_id=str(approval["id"]),
                        option_id=str(allow["id"]),
                        client_request_id="real_approval_response_1",
                    )
                    approval_final = wait_for_final_marker(
                        runtime,
                        session,
                        "APPROVAL_DONE",
                        max(30.0, args.timeout),
                    )
                    if "FARYO_APPROVAL_COMMAND_OK" not in approval_final:
                        raise RuntimeError("approved real command output did not reach the final response")
                    print("real-appserver-approval=PASS request=received decision=accepted command=executed")

                    start_plan_turn(
                        runtime,
                        session,
                        USER_INPUT_PROMPT,
                        "real_appserver_user_input_message",
                    )
                    question = wait_for_interaction(runtime, session, "user_input", max(30.0, args.timeout))
                    questions = question.get("questions") or []
                    if not questions or not isinstance(questions[0], dict):
                        raise RuntimeError("real Codex user-input request lost its question")
                    question_id = str(questions[0].get("id") or "")
                    option_labels = {
                        str(option.get("label") or "")
                        for option in questions[0].get("options") or []
                        if isinstance(option, dict)
                    }
                    if not question_id or len(option_labels) < 2:
                        raise RuntimeError("real Codex user-input options were incomplete")
                    selected_answer = next(
                        (label for label in option_labels if "alpha" in label.lower()),
                        sorted(option_labels)[0],
                    )
                    runtime.respond_interaction(
                        session,
                        interaction_id=str(question["id"]),
                        answers={question_id: [selected_answer]},
                        client_request_id="real_user_input_response_1",
                    )
                    input_final = wait_for_final_marker(
                        runtime,
                        session,
                        "USER_INPUT_DONE",
                        max(30.0, args.timeout),
                    )
                    expected_answer = "Alpha" if "alpha" in selected_answer.lower() else selected_answer
                    if expected_answer not in input_final:
                        raise RuntimeError("real user-input answer did not reach the final response")
                    print(
                        "real-appserver-interactions=PASS "
                        f"approval=resolved command=executed user_input=resolved options={len(option_labels)} answer=applied"
                    )
                if args.browser:
                    browser_cursor = runtime.replay(None).latest.render()
                    port = free_port()
                    app = owner_asgi.create_app(
                        server,
                        server.Config(server.DEFAULT_SESSION, "fixture-owner-token", 0),
                        runtime,
                    )
                    web_server = uvicorn.Server(
                        uvicorn.Config(
                            app,
                            host="127.0.0.1",
                            port=port,
                            access_log=False,
                            lifespan="on",
                            log_level="error",
                        )
                    )
                    web_thread = threading.Thread(target=web_server.run, daemon=True)
                    web_thread.start()
                    web_deadline = time.monotonic() + 5
                    while time.monotonic() < web_deadline and not web_server.started:
                        time.sleep(0.01)
                    if not web_server.started:
                        raise RuntimeError("isolated Owner browser fixture did not start")
                    browser_environment = dict(environment)
                    browser_environment["FARYO_SMOKE_URL"] = (
                        f"http://127.0.0.1:{port}/?token=fixture-owner-token&session={session}"
                    )
                    node = shutil.which("node", path=browser_environment.get("PATH"))
                    if not node:
                        raise RuntimeError("matching Node runtime is unavailable for the browser test")
                    browser = subprocess.run(
                        [
                            node,
                            str(APP_DIR / "tests/browser-real-appserver-streaming.mjs"),
                        ],
                        cwd=REPO_ROOT,
                        env=browser_environment,
                        text=True,
                        capture_output=True,
                        timeout=max(60.0, args.timeout + 30),
                        check=False,
                    )
                    if browser.returncode:
                        detail = (browser.stderr or browser.stdout or "browser check failed")[-4000:]
                        raise RuntimeError(detail)
                    print(browser.stdout.strip())
                    browser_replay = runtime.replay(browser_cursor)
                    if not any(event.kind == "item.delta" for event in browser_replay.events):
                        raise RuntimeError("browser-submitted turn did not produce replayable deltas")

                if args.restart:
                    runtime.send(
                        session,
                        RESTART_PROMPT,
                        "real_appserver_owner_restart_message",
                    )
                    before_restart = runtime.capture(session)
                    before_restart_answers = [
                        text for role, text in before_restart["messages"] if role == "assistant"
                    ]
                    if any("OWNER_RESTART_DONE" in text for text in before_restart_answers):
                        raise RuntimeError("restart test turn settled before Owner disconnect")
                    if web_server is not None:
                        web_server.should_exit = True
                    if web_thread is not None:
                        web_thread.join(5)
                    web_server = None
                    web_thread = None
                    runtime.stop()
                    runtime = AppServerRuntime(
                        socket_path=socket_path,
                        registry_path=registry_path,
                        client_version="real-test-restarted",
                        worker_manager=worker_manager,
                    )
                    runtime.start()
                    if not runtime.wait_ready(12):
                        raise RuntimeError("restarted Owner did not reconnect to the existing App Server")
                    restart_deadline = time.monotonic() + max(30.0, args.timeout)
                    restart_final = ""
                    while time.monotonic() < restart_deadline:
                        restart_capture = runtime.capture(session)
                        assistants = [
                            text
                            for role, text in restart_capture["messages"]
                            if role == "assistant"
                        ]
                        restart_final = assistants[-1] if assistants else ""
                        if (
                            restart_capture["snapshot"].get("lifecycle") == "idle"
                            and "OWNER_RESTART_DONE" in restart_final
                        ):
                            break
                        time.sleep(0.05)
                    else:
                        raise RuntimeError("Owner restart did not recover the in-flight final response")
                    if not jsonl_has_final(codex_home, restart_final):
                        raise RuntimeError("restarted Owner final did not converge to Codex JSONL")
                    print("real-appserver-owner-restart=PASS active_turn=preserved final=jsonl")

                runtime.close_session(session)
        finally:
            if web_server is not None:
                web_server.should_exit = True
            if web_thread is not None:
                web_thread.join(5)
            if runtime is not None:
                runtime.stop()
            if worker_manager is not None:
                worker_manager.close_all()
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
