"""Structured pending-interaction lifecycle for a real Codex TUI."""

from __future__ import annotations

from contextlib import nullcontext
import hashlib
import hmac
from http import HTTPStatus
import re
import threading
import time
from typing import Any, Callable

import codex_command_policy
from interaction_types import DetectedInteraction


CLIENT_REQUEST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")


class InteractionServiceError(Exception):
    def __init__(self, message: str, status: HTTPStatus = HTTPStatus.BAD_REQUEST) -> None:
        super().__init__(message)
        self.status = status


class InteractionService:
    """Own opaque ids, generations, idempotency and real TUI actions."""

    def __init__(
        self,
        runtime: Any,
        detector: Callable[[str], DetectedInteraction | None],
        *,
        command_timeline: Any | None = None,
        transition_timeout: float = 2.2,
        poll_interval: float = 0.05,
        receipt_ttl: float = 48 * 60 * 60,
    ) -> None:
        self.runtime = runtime
        self.detector = detector
        self.command_timeline = command_timeline
        self.transition_timeout = transition_timeout
        self.poll_interval = poll_interval
        self.receipt_ttl = receipt_ttl
        self.lock = threading.RLock()
        self.session_states: dict[str, dict[str, Any]] = {}
        self.receipts: dict[str, dict[str, Any]] = {}

    def _command_owner_key(self, config: Any) -> str:
        resolver = getattr(self.runtime, "command_owner_key", None)
        if callable(resolver):
            value = str(resolver(config) or "").strip()
            if value:
                return value
        return f"tui:{config.session}"

    def _timeline_public(self, event: dict[str, Any] | None) -> dict[str, Any] | None:
        if event is None or self.command_timeline is None:
            return None
        projector = getattr(self.command_timeline, "public_event", None)
        return projector(event) if callable(projector) else None

    def _now(self) -> float:
        monotonic = getattr(self.runtime, "monotonic", None)
        return float(monotonic() if callable(monotonic) else time.monotonic())

    def _sleep(self, seconds: float) -> None:
        sleeper = getattr(self.runtime, "sleep", None)
        (sleeper if callable(sleeper) else time.sleep)(seconds)

    @staticmethod
    def _clean_request_id(value: object) -> str | None:
        request_id = str(value or "").strip()
        return request_id if CLIENT_REQUEST_RE.fullmatch(request_id) else None

    def _session_lock(self, session: str):
        factory = getattr(self.runtime, "session_lock", None)
        return factory(session) if callable(factory) else nullcontext()

    @staticmethod
    def _secret(config: Any) -> bytes:
        value = str(getattr(config, "token", "") or "")
        return value.encode("utf-8")

    def _opaque(self, config: Any, namespace: str, value: str, length: int = 32) -> str:
        digest = hmac.new(
            self._secret(config),
            f"{namespace}\0{value}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()[:length]
        return f"{namespace}_{digest}"

    def _state_for_detection(
        self,
        config: Any,
        detected: DetectedInteraction | None,
    ) -> dict[str, Any]:
        session = str(config.session)
        fingerprint = detected.fingerprint() if detected is not None else ""
        with self.lock:
            previous = self.session_states.get(session)
            if previous is None:
                generation = 1
            elif previous["fingerprint"] == fingerprint:
                generation = int(previous["generation"])
            else:
                generation = int(previous["generation"]) + 1
            state = {
                "fingerprint": fingerprint,
                "generation": generation,
                "detected": detected,
            }
            self.session_states[session] = state
            return state

    def _public_payload(
        self,
        config: Any,
        state: dict[str, Any],
    ) -> dict[str, Any] | None:
        detected = state.get("detected")
        if not isinstance(detected, DetectedInteraction):
            return None
        generation = int(state["generation"])
        fingerprint = str(state["fingerprint"])
        interaction_id = self._opaque(
            config,
            "ix",
            f"{config.session}\0{generation}\0{fingerprint}",
        )
        options = []
        for index, option in enumerate(detected.options):
            option_id = self._opaque(
                config,
                "opt",
                f"{interaction_id}\0{index}\0{option.key}",
                24,
            )
            options.append(
                {
                    "id": option_id,
                    "label": option.label,
                    "description": option.description,
                    "selected": option.selected,
                    "current": option.current,
                    "disabled": option.disabled,
                }
            )
        return {
            "id": interaction_id,
            "generation": generation,
            "kind": detected.kind,
            "title": detected.title,
            "prompt": detected.prompt,
            "options": options,
            "actions": list(detected.actions),
            "source": detected.source,
            "status": "pending",
        }

    def snapshot(self, config: Any) -> dict[str, Any]:
        if not self.runtime.has_session(config):
            return {"interaction": None, "interactionRevision": "none"}
        return self.snapshot_from_capture(config, self.runtime.capture(config))

    def snapshot_from_capture(self, config: Any, capture: str) -> dict[str, Any]:
        detected = self.detector(capture)
        state = self._state_for_detection(config, detected)
        payload = self._public_payload(config, state)
        revision = self._opaque(
            config,
            "ixr",
            f"{config.session}\0{state['generation']}\0{state['fingerprint']}",
            20,
        )
        return {"interaction": payload, "interactionRevision": revision}

    def _prune_receipts(self) -> None:
        cutoff = self._now() - self.receipt_ttl
        with self.lock:
            for request_id in [
                key
                for key, value in self.receipts.items()
                if float(value.get("updatedAt") or 0) < cutoff
            ]:
                self.receipts.pop(request_id, None)

    def _existing_receipt(
        self,
        request_id: str,
        session: str,
        identity: str,
    ) -> dict[str, Any] | None:
        with self.lock:
            receipt = self.receipts.get(request_id)
            if receipt is None:
                return None
            if receipt.get("session") != session or receipt.get("identity") != identity:
                raise InteractionServiceError(
                    "client request id was already used for another interaction",
                    HTTPStatus.CONFLICT,
                )
            result = dict(receipt["payload"])
            result["duplicate"] = True
            receipt["updatedAt"] = self._now()
            return result

    def _remember_receipt(
        self,
        request_id: str,
        session: str,
        identity: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        with self.lock:
            self.receipts[request_id] = {
                "session": session,
                "identity": identity,
                "payload": dict(payload),
                "updatedAt": self._now(),
            }
        return payload

    def _current_or_stale(self, config: Any, interaction_id: str) -> tuple[dict[str, Any], DetectedInteraction]:
        snapshot = self.snapshot(config)
        payload = snapshot.get("interaction")
        if not isinstance(payload, dict) or payload.get("id") != interaction_id:
            raise InteractionServiceError(
                "interaction is no longer current",
                HTTPStatus.CONFLICT,
            )
        with self.lock:
            detected = self.session_states[str(config.session)].get("detected")
        if not isinstance(detected, DetectedInteraction):
            raise InteractionServiceError("interaction is no longer current", HTTPStatus.CONFLICT)
        return payload, detected

    @staticmethod
    def _keys_for_response(
        payload: dict[str, Any],
        detected: DetectedInteraction,
        *,
        action: str,
        option_id: str,
    ) -> list[str]:
        if option_id:
            public_options = list(payload.get("options") or [])
            target_index = next(
                (
                    index
                    for index, option in enumerate(public_options)
                    if isinstance(option, dict) and option.get("id") == option_id
                ),
                None,
            )
            if target_index is None:
                raise InteractionServiceError("unknown interaction option", HTTPStatus.CONFLICT)
            if public_options[target_index].get("disabled"):
                raise InteractionServiceError("interaction option is disabled", HTTPStatus.CONFLICT)
            selected_index = detected.selected_index
            if selected_index is None:
                raise InteractionServiceError("interaction selection is unavailable", HTTPStatus.CONFLICT)
            delta = target_index - selected_index
            return (["Down"] * delta if delta > 0 else ["Up"] * (-delta)) + ["Enter"]
        key = {
            "previous": "Up",
            "next": "Down",
            "choose": "Enter",
            "cancel": "Escape",
        }.get(action)
        if key is None or action not in detected.actions:
            raise InteractionServiceError("unsupported interaction action")
        return [key]

    def _wait_for_transition(
        self,
        config: Any,
        prior_id: str,
    ) -> tuple[dict[str, Any], bool]:
        deadline = self._now() + self.transition_timeout
        latest = self.snapshot(config)
        while self._now() < deadline:
            interaction = latest.get("interaction")
            if not isinstance(interaction, dict) or interaction.get("id") != prior_id:
                return latest, True
            self._sleep(self.poll_interval)
            latest = self.snapshot(config)
        return latest, False

    def respond(
        self,
        config: Any,
        *,
        interaction_id: object,
        action: object = "",
        option_id: object = "",
        client_request_id: object,
    ) -> dict[str, Any]:
        request_id = self._clean_request_id(client_request_id)
        if request_id is None:
            raise InteractionServiceError("invalid client request id")
        clean_interaction_id = str(interaction_id or "").strip()
        clean_action = str(action or "").strip().lower()
        clean_option_id = str(option_id or "").strip()
        if not clean_interaction_id or not (clean_action or clean_option_id):
            raise InteractionServiceError("missing interaction response")
        self._prune_receipts()
        with self._session_lock(str(config.session)):
            response_identity = hashlib.sha256(
                f"{clean_interaction_id}\0{clean_action}\0{clean_option_id}".encode("utf-8")
            ).hexdigest()
            if existing := self._existing_receipt(request_id, str(config.session), response_identity):
                return existing
            payload, detected = self._current_or_stale(config, clean_interaction_id)
            timeline_event = None
            if self.command_timeline is not None:
                timeline_event = self.command_timeline.event_for_interaction(
                    self._command_owner_key(config),
                    clean_interaction_id,
                )
            keys = self._keys_for_response(
                payload,
                detected,
                action=clean_action,
                option_id=clean_option_id,
            )
            for key in keys:
                self.runtime.send_key(config, key)
                self._sleep(self.poll_interval)
            latest, changed = self._wait_for_transition(config, clean_interaction_id)
            if not changed and (clean_option_id or clean_action in {"choose", "cancel"}):
                raise InteractionServiceError(
                    "Codex did not apply the interaction response",
                    HTTPStatus.GATEWAY_TIMEOUT,
                )
            result = {
                "ok": True,
                "requestId": request_id,
                "interaction": latest.get("interaction"),
                "interactionRevision": latest.get("interactionRevision"),
                "changed": changed,
                "resolved": latest.get("interaction") is None,
                "duplicate": False,
            }
            if timeline_event is not None:
                metadata = {"action": "cancel"} if clean_action == "cancel" else None
                next_interaction = result.get("interaction")
                updated = self.command_timeline.update(
                    str(timeline_event["id"]),
                    status="completed" if result["resolved"] else "waiting",
                    metadata=metadata,
                    interaction_id=(
                        str(next_interaction.get("id") or "")
                        if isinstance(next_interaction, dict)
                        else ""
                    ),
                )
                result["commandEvent"] = self._timeline_public(updated)
            return self._remember_receipt(
                request_id,
                str(config.session),
                response_identity,
                result,
            )

    def begin_command(
        self,
        config: Any,
        *,
        command: object,
        client_request_id: object,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        invocation = codex_command_policy.command_invocation(command)
        entry = codex_command_policy.command_entry(invocation)
        if invocation is None or entry is None:
            raise InteractionServiceError("unsupported Codex command")
        behavior = entry.behavior
        if behavior in {"dangerous", "unclassified"} and not confirmed:
            raise InteractionServiceError(
                "Codex command requires explicit confirmation",
                HTTPStatus.CONFLICT,
            )
        request_id = self._clean_request_id(client_request_id)
        if request_id is None:
            raise InteractionServiceError("invalid client request id")
        self._prune_receipts()
        with self._session_lock(str(config.session)):
            command_identity = hashlib.sha256(invocation.encode("utf-8")).hexdigest()
            if existing := self._existing_receipt(request_id, str(config.session), command_identity):
                return existing
            if not self.runtime.has_session(config) or not self.runtime.is_codex(config):
                raise InteractionServiceError("Codex TUI is unavailable", HTTPStatus.CONFLICT)
            if self.snapshot(config).get("interaction") is not None:
                raise InteractionServiceError(
                    "Codex is already waiting for an interaction",
                    HTTPStatus.CONFLICT,
                )
            ready = self.runtime.ready_for_input(config)
            turn_running = getattr(self.runtime, "turn_running", None)
            running = bool(callable(turn_running) and turn_running(config))
            if not ready and not (entry.available_during_task and running):
                readiness_deadline = self._now() + min(0.75, self.transition_timeout)
                while self._now() < readiness_deadline:
                    if callable(turn_running) and turn_running(config):
                        running = True
                        break
                    self._sleep(self.poll_interval)
                    if self.snapshot(config).get("interaction") is not None:
                        raise InteractionServiceError(
                            "Codex is already waiting for an interaction",
                            HTTPStatus.CONFLICT,
                        )
                    if self.runtime.ready_for_input(config):
                        ready = True
                        break
            if not ready and not (entry.available_during_task and running):
                message = (
                    f"{entry.command} is disabled while the current Codex task is in progress"
                    if running
                    else "Codex is not ready for a local command"
                )
                raise InteractionServiceError(
                    message,
                    HTTPStatus.CONFLICT,
                )
            if self.runtime.composer_has_draft(config):
                raise InteractionServiceError(
                    "Codex TUI already has an unsent draft",
                    HTTPStatus.CONFLICT,
                )
            timeline_event = None
            timeline_duplicate = False
            if self.command_timeline is not None:
                anchor_resolver = getattr(self.runtime, "command_anchor_key", None)
                anchor_key = ""
                if callable(anchor_resolver):
                    try:
                        anchor_key = str(anchor_resolver(config) or "")
                    except Exception:
                        # Timeline placement is best-effort presentation data;
                        # unavailable rollout history must not block a valid
                        # local Codex command.
                        anchor_key = ""
                try:
                    timeline_event, timeline_duplicate = self.command_timeline.begin(
                        owner_key=self._command_owner_key(config),
                        request_id=request_id,
                        invocation=invocation,
                        anchor_key=anchor_key,
                    )
                except RuntimeError as exc:
                    raise InteractionServiceError(str(exc), HTTPStatus.CONFLICT) from exc
                if timeline_duplicate and timeline_event is not None:
                    if timeline_event.get("status") == "failed":
                        raise InteractionServiceError("the previous command attempt failed", HTTPStatus.CONFLICT)
                    return {
                        "ok": True,
                        "requestId": request_id,
                        "command": entry.command,
                        "behavior": behavior,
                        "commandState": str(timeline_event.get("status") or "completed"),
                        "interaction": self.snapshot(config).get("interaction"),
                        "interactionRevision": self.snapshot(config).get("interactionRevision"),
                        "changed": False,
                        "duplicate": True,
                        "commandEvent": self._timeline_public(timeline_event),
                    }
            try:
                baseline = self.runtime.capture(config)
                self.runtime.send_literal(config, invocation)
                draft_deadline = self._now() + min(1.2, self.transition_timeout)
                while self._now() < draft_deadline and not self.runtime.composer_contains(config, invocation):
                    self._sleep(self.poll_interval)
                if not self.runtime.composer_contains(config, invocation):
                    raise InteractionServiceError(
                        "Codex command could not be placed in the composer",
                        HTTPStatus.GATEWAY_TIMEOUT,
                    )
                # Wait for the exact completion row when the runtime can observe
                # it.  A fixed one-frame delay is too short on a real Codex TUI and
                # can make Enter merely accept autocomplete instead of executing
                # the command.  This path still sends Enter exactly once.
                completion_ready = getattr(self.runtime, "command_completion_ready", None)
                completion_deadline = self._now() + min(0.9, self.transition_timeout)
                if callable(completion_ready):
                    while self._now() < completion_deadline and not completion_ready(config, entry.command):
                        self._sleep(self.poll_interval)
                else:
                    self._sleep(max(0.35, self.poll_interval))
                self.runtime.send_key(config, "Enter")

                deadline = self._now() + self.transition_timeout
                latest = self.snapshot(config)
                while self._now() < deadline:
                    if not self.runtime.is_codex(config):
                        status = "completed"
                        latest = {"interaction": None, "interactionRevision": "none"}
                        break
                    pending = latest.get("interaction")
                    if isinstance(pending, dict):
                        status = "pending"
                        break
                    command_still_present = self.runtime.composer_contains(config, invocation)
                    if not command_still_present:
                        if self.runtime.ready_for_input(config):
                            status = "completed"
                            break
                        turn_running = getattr(self.runtime, "turn_running", None)
                        if callable(turn_running) and turn_running(config):
                            status = "running"
                            break
                    self._sleep(self.poll_interval)
                    latest = self.snapshot(config)
                else:
                    status = "ambiguous"
                if status == "ambiguous":
                    raise InteractionServiceError(
                        "Codex did not accept the local command; no key was retried",
                        HTTPStatus.GATEWAY_TIMEOUT,
                    )
                changed = self.runtime.capture(config) != baseline
                public_event = None
                if timeline_event is not None:
                    # `running` here describes the pre-existing Codex turn,
                    # not the local slash command.  Once the exact command has
                    # left the composer without opening a menu, its lifecycle
                    # is complete.
                    timeline_status = "waiting" if status == "pending" else "completed"
                    interaction = latest.get("interaction")
                    updated = self.command_timeline.update(
                        str(timeline_event["id"]),
                        status=timeline_status,
                        interaction_id=str(interaction.get("id") or "") if isinstance(interaction, dict) else "",
                    )
                    public_event = self._timeline_public(updated)
                result = {
                    "ok": True,
                    "requestId": request_id,
                    "command": entry.command,
                    "behavior": behavior,
                    "commandState": status,
                    "interaction": latest.get("interaction"),
                    "interactionRevision": latest.get("interactionRevision"),
                    "changed": changed,
                    "duplicate": False,
                }
                if public_event is not None:
                    result["commandEvent"] = public_event
                return self._remember_receipt(
                    request_id,
                    str(config.session),
                    command_identity,
                    result,
                )
            except Exception as exc:
                if timeline_event is not None:
                    self.command_timeline.update(
                        str(timeline_event["id"]),
                        status="failed",
                        error=str(exc),
                    )
                raise
