import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

import {
  createApiClient,
  sessionApiPath,
  validateBrowserEnvelope,
} from "../static/owner/api-client.mjs";

const repoRoot = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../../../..",
);

function response(value, options = {}) {
  return {
    ok: options.ok !== false,
    status: options.status || 200,
    statusText: options.statusText || "OK",
    async json() {
      return value;
    },
    async text() {
      return typeof value === "string" ? value : JSON.stringify(value);
    },
  };
}

test("session API paths preserve queries and encode the selected session", () => {
  assert.equal(
    sessionApiPath("/api/status", "codex one"),
    "/api/status?session=codex%20one",
  );
  assert.equal(
    sessionApiPath("/api/events?lines=320", "codex"),
    "/api/events?lines=320&session=codex",
  );
  assert.equal(sessionApiPath("/asset.js", "codex"), "/asset.js");
});

test("direct Owner requests add only the Owner token", async () => {
  const calls = [];
  const client = createApiClient({
    ownerToken: "fixture-owner-token",
    routeBase: "",
    fetch: async (...args) => {
      calls.push(args);
      return response({ ok: true, value: 1 });
    },
  });

  const result = await client.request("/api/status");

  assert.equal(result.value, 1);
  assert.equal(calls[0][0], "/api/status");
  assert.deepEqual(calls[0][1].headers, {
    "X-Owner-Token": "fixture-owner-token",
  });
});

test("Gateway writes cache CSRF and retain route-local API paths", async () => {
  const calls = [];
  const client = createApiClient({
    routeBase: "/lab",
    fetch: async (path, options) => {
      calls.push([path, options]);
      return path === "/api/csrf"
        ? response({ ok: true, csrf: "fixture-csrf" })
        : response({ ok: true, envelopeVersion: 1 });
    },
  });

  await client.request("/api/send", { method: "POST", body: "{}" });

  assert.equal(calls.filter(([path]) => path === "/api/csrf").length, 1);
  assert.equal(calls[1][0], "/lab/api/send");
  assert.equal(calls[1][1].headers["X-Faryo-Csrf"], "fixture-csrf");
  assert.equal(calls[1][1].headers["Content-Type"], "application/json");
  assert.equal(JSON.parse(calls[1][1].body).envelopeVersion, 1);
});

test("browser envelope accepts legacy reads and rejects explicit future versions", async () => {
  const values = [
    { ok: true, legacy: true },
    { ok: true, envelopeVersion: 1, current: true },
  ];
  const client = createApiClient({
    fetch: async () =>
      response(values.shift() || { ok: true, envelopeVersion: 2 }),
  });

  assert.equal((await client.request("/api/status")).legacy, true);
  assert.equal((await client.request("/api/status")).current, true);
  await assert.rejects(
    client.request("/api/status"),
    (error) => error.status === 409 && error.protocolVersion === 2,
  );
});

test("public browser envelope fixture matches the JavaScript contract", () => {
  const fixture = JSON.parse(
    readFileSync(
      path.join(repoRoot, "tests/fixtures/browser-envelope-v1.json"),
      "utf8",
    ),
  );
  assert.equal(validateBrowserEnvelope(fixture), fixture);
  assert.equal(fixture.futureField, "ignored-by-older-readers");
});

test("non-JSON responses become bounded API errors", async () => {
  const client = createApiClient({
    fetch: async () =>
      response("<!doctype html>", {
        ok: false,
        status: 502,
        statusText: "Bad Gateway",
      }),
  });

  await assert.rejects(
    client.request("/api/status"),
    (error) =>
      error.status === 502 &&
      error.nonJson === true &&
      error.errorCode === "upstream_unavailable" &&
      error.retryable === true &&
      error.recovery.includes("Reload"),
  );
});

test("structured server errors preserve recovery metadata", async () => {
  const client = createApiClient({
    fetch: async () =>
      response(
        {
          ok: false,
          envelopeVersion: 1,
          errorContractVersion: 1,
          errorCode: "thread_in_use",
          errorTitle: "Conversation still open",
          error: "This conversation is still open in another Codex client.",
          recovery: "Close that Codex client and retry.",
          retryable: false,
        },
        { ok: false, status: 409, statusText: "Conflict" },
      ),
  });

  await assert.rejects(
    client.request("/api/agent-session/archive"),
    (error) =>
      error.status === 409 &&
      error.errorCode === "thread_in_use" &&
      error.errorTitle === "Conversation still open" &&
      error.recovery.includes("Close") &&
      error.retryable === false,
  );
});

test("network failures become retryable connection errors", async () => {
  const client = createApiClient({
    ownerToken: "fixture-owner-token",
    fetch: async () => {
      throw new TypeError("private network stack detail");
    },
  });

  await assert.rejects(
    client.request("/api/status"),
    (error) =>
      error.errorCode === "network_unavailable" &&
      error.errorTitle === "Connection unavailable" &&
      error.retryable === true &&
      error.recovery.includes("network connection") &&
      !error.message.includes("private"),
  );
});
