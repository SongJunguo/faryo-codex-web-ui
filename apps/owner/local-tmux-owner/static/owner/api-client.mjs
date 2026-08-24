const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);
export const BROWSER_ENVELOPE_VERSION = 1;

function applyErrorContract(error, value = {}) {
  error.errorCode = String(value.errorCode || "");
  error.errorTitle = String(value.errorTitle || "");
  error.recovery = String(value.recovery || "");
  error.retryable =
    typeof value.retryable === "boolean"
      ? value.retryable
      : Boolean(error.retryable);
  error.payload = value;
  return error;
}

export function validateBrowserEnvelope(value, { allowLegacy = true } = {}) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return value;
  const version = value.envelopeVersion;
  if (version === undefined && allowLegacy) return value;
  if (version !== BROWSER_ENVELOPE_VERSION) {
    const error = new Error("Unsupported Faryo browser protocol version");
    error.status = 409;
    error.protocolVersion = version;
    throw error;
  }
  return value;
}

function versionedJsonBody(body) {
  if (typeof body !== "string" || !body) return body;
  try {
    const value = JSON.parse(body);
    if (!value || typeof value !== "object" || Array.isArray(value))
      return body;
    return JSON.stringify({
      ...value,
      envelopeVersion: BROWSER_ENVELOPE_VERSION,
    });
  } catch (_error) {
    return body;
  }
}

export function sessionApiPath(path, session = "") {
  if (!session || !String(path).startsWith("/api/")) return String(path);
  const separator = String(path).includes("?") ? "&" : "?";
  return `${path}${separator}session=${encodeURIComponent(session)}`;
}

export function createApiClient(options = {}) {
  const routeBase = String(options.routeBase || "");
  const ownerToken = String(options.ownerToken || "");
  const fetchRequest = options.fetch;
  const FormDataType = options.FormData || globalThis.FormData;
  if (typeof fetchRequest !== "function")
    throw new TypeError("Owner API client requires fetch");
  let gatewayCsrfToken = "";

  function ownerHeaders() {
    return ownerToken ? { "X-Owner-Token": ownerToken } : {};
  }

  async function csrfHeaders() {
    if (!routeBase || ownerToken) return {};
    if (!gatewayCsrfToken) {
      let response;
      try {
        response = await fetchRequest("/api/csrf", { cache: "no-store" });
      } catch (_cause) {
        throw applyErrorContract(new Error("The browser could not reach Faryo."), {
          errorCode: "network_unavailable",
          errorTitle: "Connection unavailable",
          recovery: "Check this device's network connection and retry.",
          retryable: true,
        });
      }
      let data;
      try {
        data = await response.json();
      } catch (_cause) {
        throw applyErrorContract(new Error("Faryo returned an invalid sign-in response."), {
          errorCode: [401, 403].includes(response.status)
            ? "auth_required"
            : "invalid_response",
          errorTitle: [401, 403].includes(response.status)
            ? "Sign-in required"
            : "Invalid server response",
          recovery: "Refresh this page and sign in again.",
          retryable: false,
        });
      }
      validateBrowserEnvelope(data);
      if (!response.ok || !data.csrf) {
        const error = new Error(data.error || "CSRF token unavailable");
        error.status = response.status;
        throw applyErrorContract(error, data);
      }
      gatewayCsrfToken = data.csrf;
    }
    return { "X-Faryo-Csrf": gatewayCsrfToken };
  }

  async function request(path, requestOptions = {}) {
    const headers = { ...(requestOptions.headers || {}), ...ownerHeaders() };
    const method = String(requestOptions.method || "GET").toUpperCase();
    if (!SAFE_METHODS.has(method)) Object.assign(headers, await csrfHeaders());
    const isFormData = Boolean(
      requestOptions.body &&
      typeof FormDataType === "function" &&
      requestOptions.body instanceof FormDataType,
    );
    if (requestOptions.body && !headers["Content-Type"] && !isFormData) {
      headers["Content-Type"] = "application/json";
    }
    const body =
      !SAFE_METHODS.has(method) && !isFormData
        ? versionedJsonBody(requestOptions.body)
        : requestOptions.body;
    const requestPath = String(path).startsWith("/api/")
      ? `${routeBase}${path}`
      : String(path);
    let response;
    try {
      response = await fetchRequest(requestPath, {
        ...requestOptions,
        body,
        headers,
        cache: "no-store",
      });
    } catch (_cause) {
      throw applyErrorContract(new Error("The browser could not reach Faryo."), {
        errorCode: "network_unavailable",
        errorTitle: "Connection unavailable",
        recovery: "Check this device's network connection and retry.",
        retryable: true,
      });
    }
    const text = await response.text();
    let data = {};
    try {
      data = text ? JSON.parse(text) : {};
    } catch (_error) {
      const error = new Error(
        response.ok
          ? "API response is not JSON"
          : `${response.status} ${response.statusText || "API error"}`,
      );
      error.status = response.status;
      error.nonJson = true;
      throw applyErrorContract(error, {
        errorCode: [401, 403].includes(response.status)
          ? "auth_required"
          : [502, 503, 504].includes(response.status)
            ? "upstream_unavailable"
            : "invalid_response",
        errorTitle: [401, 403].includes(response.status)
          ? "Sign-in required"
          : [502, 503, 504].includes(response.status)
            ? "Faryo temporarily unavailable"
            : "Invalid server response",
        recovery: [401, 403].includes(response.status)
          ? "Refresh this page and sign in again."
          : "Reload the page and retry.",
        retryable: [502, 503, 504].includes(response.status),
      });
    }
    validateBrowserEnvelope(data);
    if (!response.ok || data.ok === false) {
      const error = new Error(
        data.error || `${response.status} ${response.statusText}`,
      );
      error.status = response.status;
      throw applyErrorContract(error, data);
    }
    return data;
  }

  return {
    request,
    csrfHeaders,
    ownerHeaders,
    hasOwnerToken: Boolean(ownerToken),
  };
}
