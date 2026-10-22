const DEFAULT_STORAGE_KEY = "aoi-talk.mobile-gateway.auth.v1";
const DEFAULT_PENDING_TTL_MS = 10 * 60 * 1000;
const DEFAULT_EXPIRY_SKEW_MS = 30 * 1000;
const DEFAULT_TIMEOUT_MS = 15 * 1000;
const DEFAULT_BROWSER_CALLBACK_GRACE_MS = 2500;
const DEFAULT_RUNTIME_TIMEOUT_MS = 180 * 1000;
const DEFAULT_RUNTIME_POLL_MS = 500;
const MAX_RUNTIME_TIMEOUT_MS = 180 * 1000;
const MAX_RUNTIME_RESPONSE_BYTES = 16 * 1024;
const DEFAULT_MAX_ACCESS_LIFETIME_SEC = 24 * 60 * 60;
const DEFAULT_MAX_GRANT_LIFETIME_SEC = 366 * 24 * 60 * 60;
const DEFAULT_MIN_GRANT_LIFETIME_SEC = 0;
const DEFAULT_MIN_REFRESH_LIFETIME_SEC = 0;
const GATEWAY_HEADER = "X-AZV-Gateway-Authorization";
const GATEWAY_ERROR_HEADER = "X-AZV-Gateway-Error";
const BASE64URL = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
const DEFINITIVE_AUTH_ERRORS = new Set([
  "invalid_grant",
  "invalid_refresh_token",
  "refresh_token_expired",
  "token_revoked",
  "refresh_revoked",
  "revoked",
  "grant_revoked",
  "invalid_token",
]);

export class GatewayAuthError extends Error {
  constructor(code, message, options = {}) {
    super(message);
    this.name = "GatewayAuthError";
    this.code = code;
    this.retryable = options.retryable === true;
    this.status = options.status ?? null;
    this.cause = options.cause;
  }
}

/**
 * Classify a response produced by the Gateway's mobile auth boundary.
 *
 * OAuth `invalid_token` is also a valid application response (for example
 * when an AoiTalk JWT or Hydrus Access Key has expired), so the shared
 * classifier deliberately accepts only the Gateway-owned marker together
 * with its 401 status.  The Worker strips this response header from upstream
 * responses before proxying them to keep the marker unforgeable by apps.
 */
export function isGatewayAuthResponse(response) {
  if (!response || response.status !== 401) return false;
  const marker = response.headers?.get?.(GATEWAY_ERROR_HEADER);
  return typeof marker === "string" && marker.trim().toLowerCase() === "invalid_token";
}

export function createMobileGatewayAuth(config, adapters) {
  const normalized = normalizeConfig(config);
  const deps = normalizeAdapters(adapters);
  let loadedState = null;
  let loadPromise = null;
  let stateWrite = Promise.resolve();
  let refreshInFlight = null;
  const exchangeInFlight = new Map();
  const runtimeFlights = new Map();
  const callbackWaiters = new Map();
  let generation = 0;
  let status = makeStatus("signed_out");
  const subscribers = new Set();

  async function ensureLoaded() {
    if (loadedState) return loadedState;
    if (!loadPromise) {
      loadPromise = (async () => {
        let raw;
        try {
          raw = await deps.storage.get(normalized.storageKey);
        } catch (error) {
          throw new GatewayAuthError("storage_error", "auth state could not be read", {
            retryable: true,
            cause: error,
          });
        }
        loadedState = parseState(raw, normalized);
        const credential = loadedState.credential;
        if (credential) {
          updateStatus("authenticated", credentialStatus(credential));
        }
        return loadedState;
      })().catch((error) => {
        loadPromise = null;
        throw error;
      });
    }
    return loadPromise;
  }

  function mutateState(mutator, guard = null) {
    const operation = stateWrite.then(async () => {
      const current = await ensureLoaded();
      if (guard && !guard(current, "before")) return null;
      const before = current;
      const next = sanitizeState(mutator(copyState(current)), normalized.binding);
      if (guard && !guard(next, "after")) return null;
      loadedState = next;
      try {
        await deps.storage.set(normalized.storageKey, JSON.stringify(next));
      } catch (error) {
        loadedState = before;
        throw new GatewayAuthError("storage_error", "auth state could not be saved", {
          retryable: true,
          cause: error,
        });
      }
      return next;
    });
    stateWrite = operation.catch(() => undefined);
    return operation;
  }

  async function authenticate(options = {}) {
    const state = await ensureLoaded();
    const force = options.force === true;
    if (!force && state.credential && isAccessUsable(state.credential, normalized)) {
      updateStatus("authenticated", credentialStatus(state.credential));
      return authResult("authenticated", state.credential);
    }

    let pending = validPending(state.pending, normalized) ? state.pending : null;
    if (pending && Date.now() > pending.expiresAt) {
      await mutateState((next) => {
        if (pendingMatches(next, pending)) next.pending = null;
        return next;
      }, (current, phase) => phase === "after" || pendingMatches(current, pending));
      updateStatus("reauth_required", {}, "pending_expired");
      pending = null;
    }
    if (pending?.exchangeCode ?? pending?.code) {
      return resumePendingExchange();
    }
    if (!pending || force) {
      generation += 1;
      invalidateCallbackWaiters();
      invalidateRuntimeFlights();
      pending = await createPending(generation);
    }
    const pendingGeneration = generation;
    const authorizeUrl = makeAuthorizeUrl(pending, normalized);
    updateStatus("authorizing", pendingStatus(pending));
    let browserResult;
    const callbackWaiter = createCallbackWaiter(pending);
    try {
      browserResult = await deps.browser.openAuthSession(authorizeUrl, normalized.redirectUri);
    } catch (error) {
      cancelCallbackWaiter(pending);
      updateStatus("transient", pendingStatus(pending), "browser_error");
      throw asTransient(error, "browser authentication failed");
    }

    if (browserResult == null) {
      cancelCallbackWaiter(pending);
      return {
        status: "pending",
        requestId: pending.exchangeRequestId ?? pending.request_id,
        expiresAt: pending.expiresAt,
      };
    }
    if (isCancelResult(browserResult)) {
      try {
        const delayed = await callbackWaiter;
        if (delayed && delayed.status) return delayed;
      } catch (error) {
        if (error instanceof GatewayAuthError && error.code === "signed_out") throw error;
        throw error;
      }
      if (generation !== pendingGeneration) {
        throw new GatewayAuthError("signed_out", "authentication request was superseded", { retryable: true });
      }
      updateStatus("cancelled", pendingStatus(pending), "user_cancelled");
      return { status: "cancelled", reason: "user_cancelled" };
    }
    const callbackUrl = callbackUrlFromResult(browserResult);
    if (!callbackUrl) {
      cancelCallbackWaiter(pending);
      updateStatus("transient", pendingStatus(pending), "callback_missing");
      throw new GatewayAuthError("callback_invalid", "authentication callback is missing", {
        retryable: true,
      });
    }
    return completeCallback(callbackUrl);
  }

  async function createPending(expectedGeneration) {
    const now = Date.now();
    const codeVerifier = await makeCodeVerifier(deps);
    const stateValue = await makeRandomString(deps, 32);
    const codeChallenge = await makeCodeChallenge(deps, codeVerifier);
    const exchangeRequestId = await makeRandomString(deps, 24);
    const pending = {
      kind: "exchange",
      state: stateValue,
      codeVerifier,
      codeChallenge,
      exchangeRequestId,
      request_id: exchangeRequestId,
      exchangeCode: null,
      code: null,
      createdAt: now,
      expiresAt: now + normalized.pendingTtlMs,
      redirectUri: normalized.redirectUri,
      clientId: normalized.clientId,
      binding: normalized.binding,
    };
    const saved = await mutateState((next) => {
      next.pending = pending;
      next.lastExchange = null;
      return next;
    }, (current) => generation === expectedGeneration);
    if (!saved) throw new GatewayAuthError("signed_out", "authentication request was superseded", { retryable: true });
    return saved.pending;
  }

  function callbackKey(pending) {
    return `${pending.state}:${pending.exchangeRequestId ?? pending.request_id}`;
  }

  function createCallbackWaiter(pending) {
    const key = callbackKey(pending);
    const existing = callbackWaiters.get(key);
    if (existing) return existing.promise;
    let timerId;
    let settled = false;
    let resolveWaiter;
    let rejectWaiter;
    const promise = new Promise((resolve, reject) => {
      resolveWaiter = resolve;
      rejectWaiter = reject;
    });
    // The browser promise can still be pending when force/logout invalidates
    // this waiter. Attach a sink until authenticate() reaches its await.
    promise.catch(() => undefined);
    const waiter = {
      promise,
      resolve: (value) => {
        if (settled) return;
        settled = true;
        deps.timer.clearTimeout(timerId);
        callbackWaiters.delete(key);
        resolveWaiter(value);
      },
      reject: (error) => {
        if (settled) return;
        settled = true;
        deps.timer.clearTimeout(timerId);
        callbackWaiters.delete(key);
        rejectWaiter(error);
      },
    };
    timerId = deps.timer.setTimeout(() => waiter.resolve(null), normalized.browserCallbackGraceMs);
    callbackWaiters.set(key, waiter);
    return promise;
  }

  function settleCallbackWaiter(pending, task) {
    const waiter = callbackWaiters.get(callbackKey(pending));
    if (!waiter) return task;
    Promise.resolve(task).then(waiter.resolve, waiter.reject);
    return task;
  }

  function rejectCallbackWaiter(pending, error) {
    const waiter = callbackWaiters.get(callbackKey(pending));
    if (waiter) waiter.reject(error);
  }

  function cancelCallbackWaiter(pending) {
    const waiter = callbackWaiters.get(callbackKey(pending));
    if (waiter) waiter.resolve(null);
  }

  function invalidateCallbackWaiters() {
    const error = new GatewayAuthError("signed_out", "authentication request was superseded", { retryable: true });
    for (const waiter of callbackWaiters.values()) waiter.reject(error);
    callbackWaiters.clear();
  }

  function invalidateRuntimeFlights() {
    const error = new GatewayAuthError("signed_out", "service readiness request was superseded", { retryable: true });
    for (const [key, flight] of runtimeFlights) {
      flight.cancelled = true;
      try { flight.controller?.abort(); } catch { /* adapter may not expose abort */ }
      flight.settled = true;
      flight.reject(error);
      flight.listeners.clear();
      if (runtimeFlights.get(key) === flight) runtimeFlights.delete(key);
    }
  }

  async function completeCallback(input) {
    const callbackUrl = callbackUrlFromResult(input);
    if (!callbackUrl) {
      throw new GatewayAuthError("callback_invalid", "authentication callback is missing", { retryable: true });
    }
    const state = await ensureLoaded();
    const pending = validPending(state.pending, normalized) ? state.pending : null;
    if (!pending) {
      if (state.lastExchange && state.credential) return completeDuplicateCallback(callbackUrl, state);
      throw new GatewayAuthError("pending_missing", "no live authentication request exists", { retryable: true });
    }
    const callbackGeneration = generation;
    if (Date.now() > pending.expiresAt) {
      const cleared = await mutateState((next) => {
        if (pendingMatches(next, pending)) next.pending = null;
        return next;
      }, (current, phase) => generation === callbackGeneration && (phase === "after" || pendingMatches(current, pending)));
      if (cleared) updateStatus("reauth_required", {}, "pending_expired");
      const error = new GatewayAuthError("pending_expired", "authentication request expired", { retryable: true });
      rejectCallbackWaiter(pending, error);
      throw error;
    }

    const callback = parseAndValidateCallback(callbackUrl, pending, normalized);
    if (callback.error) {
      if (callback.error === "access_denied" || callback.error === "cancelled") {
        const cleared = await mutateState((next) => {
          if (pendingMatches(next, pending)) next.pending = null;
          return next;
        }, (current, phase) => generation === callbackGeneration && (phase === "after" || pendingMatches(current, pending)));
        if (cleared) updateStatus("cancelled", {}, callback.error);
        const result = { status: "cancelled", reason: callback.error };
        settleCallbackWaiter(pending, Promise.resolve(result));
        return result;
      }
      if (["temporarily_unavailable", "server_error", "timeout"].includes(callback.error)) {
        updateStatus("transient", pendingStatus(pending), callback.error);
        const error = new GatewayAuthError("transient", "authorization provider is temporarily unavailable", { retryable: true });
        rejectCallbackWaiter(pending, error);
        throw error;
      }
      const cleared = await mutateState((next) => {
        if (pendingMatches(next, pending)) next.pending = null;
        return next;
      }, (current, phase) => generation === callbackGeneration && (phase === "after" || pendingMatches(current, pending)));
      if (cleared) updateStatus("reauth_required", {}, callback.error);
      const error = new GatewayAuthError("authorization_denied", "authorization was denied", { retryable: true });
      rejectCallbackWaiter(pending, error);
      throw error;
    }

    let saved;
    try {
      saved = await persistExchangeCode(pending, callback.code, callbackGeneration);
    } catch (error) {
      rejectCallbackWaiter(pending, error);
      throw error;
    }
    if (!saved) {
      const current = await ensureLoaded();
      if (current.lastExchange?.state === pending.state && current.credential) return completeDuplicateCallback(callbackUrl, current);
      const error = new GatewayAuthError("signed_out", "authentication request was superseded", { retryable: true });
      rejectCallbackWaiter(pending, error);
      throw error;
    }
    return settleCallbackWaiter(pending, exchangePendingCode(saved.pending, callbackGeneration));
  }

  async function resumePendingExchange() {
    const state = await ensureLoaded();
    if (!validPending(state.pending, normalized) || !(state.pending.exchangeCode ?? state.pending.code)) {
      throw new GatewayAuthError("pending_missing", "no persisted authorization code exists", { retryable: true });
    }
    if (Date.now() > state.pending.expiresAt) {
      const pending = state.pending;
      await mutateState((next) => {
        if (pendingMatches(next, pending)) next.pending = null;
        return next;
      }, (current, phase) => phase === "after" || pendingMatches(current, pending));
      updateStatus("reauth_required", {}, "pending_expired");
      const error = new GatewayAuthError("pending_expired", "authentication request expired", { retryable: true });
      rejectCallbackWaiter(pending, error);
      throw error;
    }
    return exchangePendingCode(state.pending, generation);
  }

  async function retryExchange() {
    return resumePendingExchange();
  }

  async function persistExchangeCode(pending, code, expectedGeneration) {
    const saved = await mutateState((next) => {
      if (pendingMatches(next, pending) && !(next.pending.exchangeCode ?? next.pending.code)) {
        next.pending.exchangeCode = code;
        next.pending.code = code;
      }
      return next;
    }, (current) => generation === expectedGeneration && pendingMatches(current, pending));
    return saved;
  }

  async function exchangePendingCode(pending, expectedGeneration) {
    const exchangeKey = `${pending.state}:${pending.exchangeRequestId ?? pending.request_id}`;
    const existingExchange = exchangeInFlight.get(exchangeKey);
    if (existingExchange) return existingExchange;
    const task = (async () => {
      const state = await ensureLoaded();
      const live = state.pending;
      if (!pendingMatches(state, pending)) {
        if (state.lastExchange?.state === pending.state && state.credential) return authResult("authenticated", state.credential);
        throw new GatewayAuthError("signed_out", "authentication request was superseded", { retryable: true });
      }
      const code = live.exchangeCode ?? live.code;
      if (!code) throw new GatewayAuthError("callback_invalid", "authorization code is missing", { retryable: true });
      const requestId = live.exchangeRequestId ?? live.request_id;
      const previousCredential = state.credential;
      updateStatus("exchanging", pendingStatus(live));
      let tokenResponse;
      try {
        tokenResponse = await postToken({
          grant_type: "authorization_code",
          client_id: normalized.clientId,
          redirect_uri: normalized.redirectUri,
          code,
          code_verifier: live.codeVerifier,
          request_id: requestId,
        }, "exchange");
      } catch (error) {
        if (isDefinitive(error)) {
          const cleared = await mutateState((next) => {
            if (pendingMatches(next, live)) next.pending = null;
            return next;
          }, (current) => generation === expectedGeneration && pendingMatches(current, live));
          if (cleared) updateStatus("reauth_required", previousCredential ? credentialStatus(previousCredential) : {}, "invalid_grant");
        } else {
          updateStatus("transient", pendingStatus(live), error.code);
        }
        throw error;
      }
      let credential;
      try {
        credential = tokenToCredential(tokenResponse, normalized);
      } catch (error) {
        updateStatus("transient", pendingStatus(live), error.code);
        throw error;
      }
      const committed = await mutateState((next) => {
        if (!pendingMatches(next, live)) return next;
        next.credential = credential;
        next.pending = null;
        next.lastExchange = {
          state: live.state,
          code,
          requestId,
          redirectUri: live.redirectUri,
          completedAt: Date.now(),
          binding: normalized.binding,
        };
        return next;
      }, (current, phase) => generation === expectedGeneration && (phase === "after" ||
        (pendingMatches(current, live) && (current.pending.exchangeCode ?? current.pending.code) === code)));
      if (!committed) {
        const current = await ensureLoaded();
        if (current.lastExchange?.state === live.state && current.credential) return authResult("authenticated", current.credential);
        throw new GatewayAuthError("signed_out", "authentication result was discarded", { retryable: false });
      }
      updateStatus("authenticated", credentialStatus(credential));
      return authResult("authenticated", credential);
    })();
    exchangeInFlight.set(exchangeKey, task);
    try {
      return await task;
    } finally {
      if (exchangeInFlight.get(exchangeKey) === task) exchangeInFlight.delete(exchangeKey);
    }
  }

  function completeDuplicateCallback(callbackUrl, state) {
    const duplicate = parseAndValidateCallback(callbackUrl, {
      state: state.lastExchange.state,
      redirectUri: state.lastExchange.redirectUri,
      clientId: normalized.clientId,
      binding: normalized.binding,
    }, normalized);
    if (duplicate.error || duplicate.code !== state.lastExchange.code) {
      throw new GatewayAuthError("callback_invalid", "callback was already consumed", { retryable: false });
    }
    return Promise.resolve(authResult("authenticated", state.credential));
  }

  async function refresh(options = {}) {
    await ensureLoaded();
    if (refreshInFlight) return refreshInFlight;
    const force = options.force !== false;
    const task = doRefresh(force);
    refreshInFlight = task;
    try {
      return await task;
    } finally {
      if (refreshInFlight === task) refreshInFlight = null;
    }
  }

  async function doRefresh(force) {
    const state = await ensureLoaded();
    const existing = state.credential;
    if (!existing) {
      updateStatus("reauth_required", {}, "missing_credential");
      throw new GatewayAuthError("reauth_required", "authentication is required", { retryable: true });
    }
    if (!force && isAccessUsable(existing, normalized)) {
      updateStatus("authenticated", credentialStatus(existing));
      return authResult("authenticated", existing);
    }
    const requestGeneration = generation;
    if (!existing.refreshToken) {
      await expireCredential("refresh_missing", requestGeneration, existing);
      throw new GatewayAuthError("reauth_required", "refresh authentication is required", { retryable: true });
    }
    if (isRefreshExpired(existing, normalized)) {
      await expireCredential("refresh_expired", requestGeneration, existing);
      throw new GatewayAuthError("reauth_required", "refresh authentication expired", { retryable: true });
    }

    let pending = state.pending;
    if (!pending || pending.kind !== "refresh" || pending.refreshToken !== existing.refreshToken ||
        !(pending.refreshRequestId ?? pending.request_id) || !bindingMatches(pending.binding, normalized.binding)) {
      pending = {
        kind: "refresh",
        refreshToken: existing.refreshToken,
        refreshRequestId: await makeRandomString(deps, 24),
        createdAt: Date.now(),
        binding: normalized.binding,
      };
      pending.request_id = pending.refreshRequestId;
      const saved = await mutateState((next) => {
        next.pending = pending;
        return next;
      }, (current, phase) => generation === requestGeneration && (phase === "after" || current.credential?.refreshToken === existing.refreshToken));
      if (!saved) throw new GatewayAuthError("signed_out", "refresh request was superseded", { retryable: true });
    }
    updateStatus("refreshing", credentialStatus(existing));
    let tokenResponse;
    try {
      tokenResponse = await postToken({
        grant_type: "refresh_token",
        client_id: normalized.clientId,
        refresh_token: pending.refreshToken,
        request_id: pending.refreshRequestId ?? pending.request_id,
      }, "refresh");
    } catch (error) {
      if (isDefinitive(error)) {
        const cleared = await mutateState((next) => {
          next.credential = null;
          next.pending = null;
          return next;
        }, (current, phase) => generation === requestGeneration && (phase === "after" || current.credential?.refreshToken === existing.refreshToken));
        if (cleared) updateStatus("revoked", credentialStatus(existing), "invalid_grant");
      } else {
        updateStatus("transient", credentialStatus(existing), error.code);
      }
      throw error;
    }
    if (requestGeneration !== generation) {
      throw new GatewayAuthError("signed_out", "refresh result was discarded", { retryable: false });
    }
    let credential;
    try {
      credential = tokenToCredential(tokenResponse, normalized);
    } catch (error) {
      updateStatus("transient", credentialStatus(existing), error.code);
      throw error;
    }
    const committed = await mutateState((next) => {
      if (!next.pending || next.pending.kind !== "refresh" ||
          (next.pending.refreshRequestId ?? next.pending.request_id) !== (pending.refreshRequestId ?? pending.request_id)) return next;
      next.credential = credential;
      next.pending = null;
      return next;
    }, (current, phase) => generation === requestGeneration && (phase === "after" ||
      current.credential?.refreshToken === existing.refreshToken && current.pending?.kind === "refresh" &&
      (current.pending.refreshRequestId ?? current.pending.request_id) === (pending.refreshRequestId ?? pending.request_id)));
    if (!committed) {
      const current = await ensureLoaded();
      if (current.credential?.accessToken === credential.accessToken) return authResult("authenticated", current.credential);
      throw new GatewayAuthError("signed_out", "refresh result was discarded", { retryable: false });
    }
    updateStatus("authenticated", credentialStatus(credential));
    return authResult("authenticated", credential);
  }

  async function awaitServiceReady(options = {}) {
    const waitOptions = normalizeRuntimeWaitOptions(options);
    if (waitOptions.signal?.aborted) throw runtimeAbortError();
    const key = runtimeBindingKey(normalized.binding);
    let flight = runtimeFlights.get(key);
    if (!flight || flight.cancelled || flight.generation !== generation) {
      if (flight && runtimeFlights.get(key) === flight) {
        flight.cancelled = true;
        try { flight.controller?.abort(); } catch { /* adapter may not expose abort */ }
        flight.settled = true;
        flight.reject(new GatewayAuthError("signed_out", "service readiness request was superseded", { retryable: true }));
        runtimeFlights.delete(key);
      }
      flight = createRuntimeFlight(key, generation);
      flight.pollMs = waitOptions.pollMs;
      runtimeFlights.set(key, flight);
      runRuntimeFlight(flight)
        .then((value) => { flight.settled = true; flight.resolve(value); }, (error) => { flight.settled = true; flight.reject(error); })
        .finally(() => {
          if (runtimeFlights.get(key) === flight) runtimeFlights.delete(key);
        })
        .catch(() => undefined);
    }
    return attachRuntimeWaiter(flight, waitOptions);
  }

  function createRuntimeFlight(key, expectedGeneration) {
    let resolve;
    let reject;
    const promise = new Promise((promiseResolve, promiseReject) => {
      resolve = promiseResolve;
      reject = promiseReject;
    });
    // A cancelled generation can reject before a caller attaches its waiter.
    promise.catch(() => undefined);
    return {
      key,
      generation: expectedGeneration,
      controller: typeof AbortController === "function" ? new AbortController() : null,
      deadline: Date.now() + MAX_RUNTIME_TIMEOUT_MS,
      state: "starting",
      listeners: new Set(),
      cancelled: false,
      settled: false,
      promise,
      resolve,
      reject,
    };
  }

  function attachRuntimeWaiter(flight, options) {
    const signal = options.signal;
    if (signal?.aborted) return Promise.reject(runtimeAbortError());
    return new Promise((resolve, reject) => {
      let attached = true;
      const waiter = { onState: options.onState };
      const detach = () => {
        if (!attached) return;
        attached = false;
        flight.listeners.delete(waiter);
        signal?.removeEventListener?.("abort", onAbort);
        if (timerId !== undefined) deps.timer.clearTimeout(timerId);
        if (flight.listeners.size === 0 && !flight.settled && !flight.cancelled) {
          flight.cancelled = true;
          try { flight.controller?.abort(); } catch { /* adapter may not expose abort */ }
          flight.reject(runtimeAbortError());
        }
      };
      const onAbort = () => {
        detach();
        reject(runtimeAbortError());
      };
      const onTimeout = () => {
        if (typeof waiter.onState === "function") {
          try { waiter.onState("failed"); } catch { /* UI callbacks cannot interrupt readiness */ }
        }
        detach();
        reject(new GatewayAuthError("timeout", "service readiness timed out", { retryable: true }));
      };
      flight.listeners.add(waiter);
      if (signal?.addEventListener) signal.addEventListener("abort", onAbort, { once: true });
      const timerId = deps.timer.setTimeout(onTimeout, Math.max(0, options.timeoutMs));
      if (typeof waiter.onState === "function") {
        try { waiter.onState(flight.state); } catch { /* UI callbacks cannot interrupt readiness */ }
      }
      flight.promise.then(
        (value) => { detach(); resolve(value); },
        (error) => { detach(); reject(error); },
      );
    });
  }

  function emitRuntimeState(flight, state) {
    if (flight.cancelled) return;
    flight.state = state;
    for (const waiter of flight.listeners) {
      if (typeof waiter.onState !== "function") continue;
      try { waiter.onState(state); } catch { /* UI callbacks cannot interrupt readiness */ }
    }
  }

  async function runRuntimeFlight(flight) {
    const endpoint = new URL(`${normalizePathBase(normalized.servicePath)}/.gateway/mobile/runtime`);
    const deadline = flight.deadline;
    let started = false;
    try {
      const activation = await runtimeRequest(endpoint, "POST", flight);
      if (activation.response.status < 200 || activation.response.status >= 300) {
        throw runtimeFailureError(activation.response.status);
      }
      started = activation.response.status === 202 || activation.payload?.started === true;

      while (true) {
        assertRuntimeGeneration(flight);
        const current = await runtimeRequest(endpoint, "GET", flight);
        const status = runtimeStateFromResponse(current);
        if (status === "ready") {
          emitRuntimeState(flight, "ready");
          return { state: "ready", serviceId: normalized.serviceId, started };
        }
        if (status === "failed") throw runtimeFailureError(current.response.status, current.payload?.error_code);
        emitRuntimeState(flight, "starting");
        const remaining = deadline - Date.now();
        if (remaining <= 0) throw new GatewayAuthError("timeout", "service readiness timed out", { retryable: true });
        await waitForRuntimePoll(Math.min(flight.pollMs, remaining), flight);
      }
    } catch (error) {
      if (!flight.cancelled && error?.code !== "signed_out" && error?.code !== "aborted") emitRuntimeState(flight, "failed");
      throw error;
    }
  }

  async function runtimeRequest(endpoint, method, flight, allowAuthRetry = true) {
    assertRuntimeGeneration(flight);
    const authorization = await runtimeGatewayHeaders(endpoint, flight.generation);
    const headers = authorization.headers;
    assertRuntimeGeneration(flight);
    const requestOptions = {
      method,
      headers,
      credentials: "omit",
      redirect: "manual",
      cache: "no-store",
    };
    // Expo 54 Android maps a null POST body to a synthetic one-byte NUL body
    // to satisfy OkHttp. The Worker deliberately rejects non-empty activation
    // bodies, so use an explicit empty string to normalize to zero bytes.
    if (method === "POST") requestOptions.body = "";
    let response;
    try {
      response = await fetchRuntimeWithTimeout(endpoint.href, requestOptions, flight);
    } catch (error) {
      throw error instanceof GatewayAuthError ? error : asTransient(error, "runtime request failed");
    }
    if (response && (response.type === "opaqueredirect" || response.redirected || (response.status >= 300 && response.status < 400))) {
      discardRuntimeResponseBody(response);
      throw new GatewayAuthError("redirect_blocked", "runtime redirect was not followed", { retryable: true, status: response.status });
    }
    if (response?.status === 401) {
      const gatewayAuthFailure = isGatewayAuthResponse(response);
      discardRuntimeResponseBody(response);
      if (!gatewayAuthFailure) throw runtimeFailureError(response.status, "runtime_unavailable");
      const unauthorized = await runtimeUnauthorizedFailure(flight.generation, authorization.credential);
      if (unauthorized.retry && method === "GET" && allowAuthRetry) return runtimeRequest(endpoint, method, flight, false);
      if (unauthorized.retry) throw new GatewayAuthError("transient", "runtime authorization changed", { retryable: true, status: 401 });
      throw unauthorized.error;
    }
    if (method === "POST") {
      discardRuntimeResponseBody(response);
      return { response, payload: null };
    }
    const payload = await readRuntimeJson(response, flight);
    return { response, payload };
  }

  async function runtimeGatewayHeaders(endpoint, expectedGeneration) {
    assertRuntimeGeneration({ generation: expectedGeneration, cancelled: false });
    const target = validateTrustedTarget(endpoint);
    const targetOrigin = target.protocol === "wss:" ? `https://${target.host}` : target.origin;
    if (targetOrigin !== normalized.serviceOrigin || !pathHasPrefix(target.pathname, normalized.servicePrefix)) {
      throw new GatewayAuthError("target_invalid", "runtime target is outside the configured service boundary");
    }
    await ensureLoaded();
    if (generation !== expectedGeneration) throw new GatewayAuthError("signed_out", "service readiness request was superseded", { retryable: true });
    let credential = loadedState.credential;
    if (!credential && validPending(loadedState.pending, normalized) && (loadedState.pending.exchangeCode ?? loadedState.pending.code)) {
      await resumePendingExchange();
      credential = loadedState.credential;
    }
    if (!credential) {
      updateStatus("reauth_required", {}, "missing_credential");
      throw new GatewayAuthError("reauth_required", "authentication is required", { retryable: true });
    }
    if (!isAccessUsable(credential, normalized)) {
      await refresh({ force: true });
      credential = loadedState.credential;
      if (!credential) throw new GatewayAuthError("reauth_required", "authentication is required", { retryable: true });
    }
    if (generation !== expectedGeneration) throw new GatewayAuthError("signed_out", "service readiness request was superseded", { retryable: true });
    return {
      headers: { [GATEWAY_HEADER]: `${credential.tokenType} ${credential.accessToken}` },
      credential,
    };
  }

  async function runtimeUnauthorizedFailure(expectedGeneration, sentCredential) {
    if (generation !== expectedGeneration) return { error: new GatewayAuthError("signed_out", "service readiness request was superseded", { retryable: true }) };
    if (refreshInFlight) {
      try { await refreshInFlight; } catch { /* retain the current binding for the comparison below */ }
    }
    if (generation !== expectedGeneration) return { error: new GatewayAuthError("signed_out", "service readiness request was superseded", { retryable: true }) };
    await ensureLoaded();
    const previous = loadedState?.credential;
    if (sentCredential?.accessToken && previous?.accessToken && sentCredential.accessToken !== previous.accessToken) {
      return { retry: true };
    }
    const cleared = await mutateState((next) => {
      next.credential = null;
      next.pending = null;
      return next;
    }, (current, phase) => generation === expectedGeneration && (phase === "after" || current.credential?.accessToken === sentCredential?.accessToken));
    if (cleared) updateStatus("revoked", previous ? credentialStatus(previous) : {}, "invalid_token");
    return { error: new GatewayAuthError("revoked", "runtime authorization is no longer valid", { retryable: false, status: 401 }) };
  }

  async function fetchRuntimeWithTimeout(url, options, flight) {
    assertRuntimeGeneration(flight);
    const controller = typeof AbortController === "function" ? new AbortController() : null;
    let timerId;
    let timedOut = false;
    const abortFlight = () => { try { controller?.abort(); } catch { /* ignore adapter abort errors */ } };
    flight.controller?.signal?.addEventListener?.("abort", abortFlight, { once: true });
    const requestOptions = controller ? { ...options, signal: controller.signal } : options;
    const request = Promise.resolve().then(() => deps.fetch(url, requestOptions));
    let rejectCancelled;
    const cancelled = new Promise((_, reject) => { rejectCancelled = reject; });
    const onFlightAbort = () => {
      abortFlight();
      rejectCancelled(new GatewayAuthError("signed_out", "service readiness request was superseded", { retryable: true }));
    };
    flight.controller?.signal?.addEventListener?.("abort", onFlightAbort, { once: true });
    const timeout = new Promise((_, reject) => {
      const remainingGlobalBudget = flight.deadline - Date.now();
      if (remainingGlobalBudget <= 0) {
        reject(new GatewayAuthError("timeout", "runtime request timed out", { retryable: true }));
        return;
      }
      const requestTimeoutMs = Math.min(DEFAULT_TIMEOUT_MS, remainingGlobalBudget);
      timerId = deps.timer.setTimeout(() => {
        timedOut = true;
        abortFlight();
        reject(new GatewayAuthError("timeout", "runtime request timed out", { retryable: true }));
      }, Math.max(1, requestTimeoutMs));
    });
    try {
      return await Promise.race([request, timeout, cancelled]);
    } catch (error) {
      if (flight.cancelled || flight.generation !== generation) {
        throw new GatewayAuthError("signed_out", "service readiness request was superseded", { retryable: true });
      }
      if (timedOut) throw error;
      throw asTransient(error, "runtime request failed");
    } finally {
      deps.timer.clearTimeout(timerId);
      flight.controller?.signal?.removeEventListener?.("abort", abortFlight);
      flight.controller?.signal?.removeEventListener?.("abort", onFlightAbort);
    }
  }

  function waitForRuntimePoll(delayMs, flight) {
    assertRuntimeGeneration(flight);
    if (delayMs <= 0) return Promise.resolve();
    return new Promise((resolve, reject) => {
      let settled = false;
      const finish = (error = null) => {
        if (settled) return;
        settled = true;
        flight.controller?.signal?.removeEventListener?.("abort", onAbort);
        deps.timer.clearTimeout(timerId);
        if (error) reject(error); else resolve();
      };
      const onAbort = () => finish(new GatewayAuthError("signed_out", "service readiness request was superseded", { retryable: true }));
      let timerId = deps.timer.setTimeout(finish, delayMs);
      flight.controller?.signal?.addEventListener?.("abort", onAbort, { once: true });
    });
  }

  function assertRuntimeGeneration(flight) {
    if (flight.cancelled || flight.generation !== generation) {
      throw new GatewayAuthError("signed_out", "service readiness request was superseded", { retryable: true });
    }
  }

  function runtimeStateFromResponse(result) {
    const state = result.payload?.state;
    if (result.payload?.service !== normalized.serviceId) {
      throw runtimeFailureError(result.response.status, "runtime_unavailable");
    }
    if (state === "ready" && result.response.status === 200 && result.payload.ready === true) return "ready";
    if (state === "starting" && result.response.status === 202 && result.payload.ready === false) return "starting";
    if (state === "stopped" && result.response.status === 200 && result.payload.ready === false) return "starting";
    if (state === "failed" && result.response.status === 503 && result.payload.ready === false) return "failed";
    throw runtimeFailureError(result.response.status, "runtime_unavailable");
  }

  function runtimeFailureError(status = null, errorCode = null) {
    const code = typeof errorCode === "string" && /^[a-z][a-z0-9_]{0,40}$/u.test(errorCode) ? errorCode : "runtime_failed";
    return new GatewayAuthError("runtime_failed", "service runtime is unavailable", { retryable: true, status, cause: code });
  }

  async function readRuntimeJson(response, flight) {
    if (!response) return null;
    const contentLength = Number(response.headers?.get?.("content-length") ?? 0);
    if (Number.isFinite(contentLength) && contentLength > MAX_RUNTIME_RESPONSE_BYTES) {
      discardRuntimeResponseBody(response);
      return null;
    }
    const body = response.body;
    if (!body || typeof body.getReader !== "function") {
      discardRuntimeResponseBody(response);
      return null;
    }
    const reader = body.getReader();
    const decoder = new TextDecoder();
    const chunks = [];
    let total = 0;
    let timerId;
    let timedOut = false;
    let aborted = false;
    const cancelReader = () => {
      aborted = true;
      try { void reader.cancel(); } catch { /* response body is intentionally discarded */ }
    };
    const remaining = Math.max(1, Math.min(DEFAULT_TIMEOUT_MS, flight.deadline - Date.now()));
    const timeout = new Promise((_, reject) => {
      timerId = deps.timer.setTimeout(() => {
        timedOut = true;
        cancelReader();
        reject(new GatewayAuthError("timeout", "runtime response timed out", { retryable: true }));
      }, remaining);
    });
    let rejectAbort;
    const abortPromise = new Promise((_, reject) => { rejectAbort = reject; });
    const onFlightAbort = () => {
      cancelReader();
      rejectAbort(new GatewayAuthError("signed_out", "service readiness request was superseded", { retryable: true }));
    };
    flight.controller?.signal?.addEventListener?.("abort", onFlightAbort, { once: true });
    try {
      const read = (async () => {
        while (true) {
          const part = await reader.read();
          if (part.done) break;
          const value = part.value instanceof Uint8Array ? part.value : new Uint8Array(part.value ?? []);
          total += value.byteLength;
          if (total > MAX_RUNTIME_RESPONSE_BYTES) {
            cancelReader();
            return null;
          }
          chunks.push(value);
        }
        const merged = new Uint8Array(total);
        let offset = 0;
        for (const chunk of chunks) {
          merged.set(chunk, offset);
          offset += chunk.byteLength;
        }
        const value = JSON.parse(decoder.decode(merged));
        return value && typeof value === "object" && !Array.isArray(value) ? value : null;
      })();
      const payload = await Promise.race([read, timeout, abortPromise]);
      if (aborted || timedOut) return null;
      return payload;
    } catch (error) {
      if (flight.cancelled || flight.generation !== generation) {
        throw new GatewayAuthError("signed_out", "service readiness request was superseded", { retryable: true });
      }
      if (timedOut) throw error;
      return null;
    } finally {
      deps.timer.clearTimeout(timerId);
      flight.controller?.signal?.removeEventListener?.("abort", onFlightAbort);
      try { void reader.cancel(); } catch { /* response body is intentionally discarded */ }
      try { reader.releaseLock?.(); } catch { /* ignore response reader release failures */ }
    }
  }

  function discardRuntimeResponseBody(response) {
    try {
      const body = response?.body;
      if (body && typeof body.cancel === "function") void body.cancel();
    } catch { /* response body is intentionally discarded */ }
  }

  async function getHeaders(target) {
    const targetUrl = validateTrustedTarget(target);
    await ensureLoaded();
    let credential = loadedState.credential;
    if (!targetWithinConfig(targetUrl, normalized, credential)) {
      throw new GatewayAuthError("target_invalid", "target is outside the configured service boundary");
    }
    if (!credential) {
      if (validPending(loadedState.pending, normalized) && (loadedState.pending.exchangeCode ?? loadedState.pending.code)) {
        await resumePendingExchange();
        credential = loadedState.credential;
      }
    }
    if (!credential) {
      updateStatus("reauth_required", {}, "missing_credential");
      throw new GatewayAuthError("reauth_required", "authentication is required", { retryable: true });
    }
    if (!isAccessUsable(credential, normalized)) {
      await refresh({ force: true });
      credential = loadedState.credential;
      if (!credential) {
        throw new GatewayAuthError("reauth_required", "authentication is required", { retryable: true });
      }
    } else {
      updateStatus("authenticated", credentialStatus(credential));
    }

    const headers = {};
    const appHeaders = await resolveAppAuthorization(targetUrl.href);
    for (const [name, value] of Object.entries(appHeaders)) {
      if (name.toLowerCase() === GATEWAY_HEADER.toLowerCase()) {
        throw new GatewayAuthError("config_invalid", "app authorization cannot override the gateway header");
      }
      headers[name] = value;
    }
    headers[GATEWAY_HEADER] = `${credential.tokenType} ${credential.accessToken}`;
    return headers;
  }

  async function signOut(options = {}) {
    await ensureLoaded();
    generation += 1;
    invalidateCallbackWaiters();
    invalidateRuntimeFlights();
    const revoke = options.revoke !== false;
    const oldCredential = loadedState.credential;
    let revocation = loadedState.revocation;
    if (revoke && oldCredential?.refreshToken) {
      if (!revocation || revocation.refreshToken !== oldCredential.refreshToken) {
        revocation = {
          refreshToken: oldCredential.refreshToken,
          requestId: await makeRandomString(deps, 24),
          createdAt: Date.now(),
          binding: normalized.binding,
        };
        revocation.request_id = revocation.requestId;
      }
    }
    await mutateState((next) => {
      next.credential = null;
      next.pending = null;
      next.revocation = revoke ? revocation : next.revocation;
      return next;
    });
    updateStatus("signed_out", {}, "user_signed_out");

    if (revoke && revocation) {
      try {
        await flushRevocation();
        return { status: "signed_out", revocation: "sent" };
      } catch (error) {
        return { status: "signed_out", revocation: "pending", error: safeErrorInfo(error) };
      }
    }
    return { status: "signed_out", revocation: "skipped" };
  }

  async function retryRevocation() {
    await ensureLoaded();
    if (!loadedState.revocation) return { status: "none" };
    try {
      await flushRevocation();
      return { status: "sent" };
    } catch (error) {
      return { status: "pending", error: safeErrorInfo(error) };
    }
  }

  async function flushRevocation() {
    const state = await ensureLoaded();
    const record = state.revocation;
    if (!record) return;
    try {
      await postJson(`${normalized.portalOrigin}/.gateway/mobile/revoke`, {
        client_id: normalized.clientId,
        refresh_token: record.refreshToken,
        request_id: record.requestId ?? record.request_id,
      }, "revoke");
    } catch (error) {
      if (isDefinitive(error)) {
        await mutateState((next) => {
          next.revocation = null;
          return next;
        });
      }
      throw error;
    }
    await mutateState((next) => {
      if ((next.revocation?.requestId ?? next.revocation?.request_id) === (record.requestId ?? record.request_id)) next.revocation = null;
      return next;
    });
  }

  async function expireCredential(reason, expectedGeneration = generation, expectedCredential = loadedState?.credential) {
    const previous = loadedState?.credential;
    const cleared = await mutateState((next) => {
      next.credential = null;
      next.pending = null;
      return next;
    }, (current, phase) => generation === expectedGeneration && (phase === "after" ||
      (!expectedCredential || current.credential?.refreshToken === expectedCredential.refreshToken)));
    if (cleared) updateStatus("reauth_required", previous ? credentialStatus(previous) : {}, reason);
  }

  async function postToken(body, operation) {
    const payload = await postJson(`${normalized.portalOrigin}/.gateway/mobile/token`, body, operation);
    return validateTokenResponse(payload, operation);
  }

  async function postJson(url, body, operation) {
    let response;
    try {
      response = await fetchWithTimeout(url, {
        method: "POST",
        headers: {
          accept: "application/json",
          "content-type": "application/json",
        },
        body: JSON.stringify(body),
        credentials: "omit",
        redirect: "manual",
      });
    } catch (error) {
      if (error instanceof GatewayAuthError) throw error;
      throw asTransient(error, `${operation} request failed`);
    }
    if (response && (response.type === "opaqueredirect" || response.redirected || (response.status >= 300 && response.status < 400))) {
      throw new GatewayAuthError("redirect_blocked", `${operation} redirect was not followed`, {
        retryable: true,
        status: response.status,
      });
    }
    const responseOk = Boolean(response && (response.ok ?? (response.status >= 200 && response.status < 300)));
    let payload = null;
    try {
      payload = await readJson(response);
    } catch (error) {
      if (responseOk && (response.status === 204 || operation === "revoke")) {
        payload = null;
      } else if (responseOk) {
        throw new GatewayAuthError("protocol_error", `${operation} response was not JSON`, {
          retryable: true,
          status: response.status,
          cause: error,
        });
      }
    }
    if (!responseOk) {
      const serverCode = errorCodeFromPayload(payload);
      if (serverCode && DEFINITIVE_AUTH_ERRORS.has(serverCode)) {
        throw new GatewayAuthError("revoked", `${operation} authorization is no longer valid`, {
          retryable: false,
          status: response?.status ?? null,
        });
      }
      throw new GatewayAuthError("transient", `${operation} request was rejected`, {
        retryable: true,
        status: response?.status ?? null,
      });
    }
    return payload;
  }

  async function fetchWithTimeout(url, options) {
    const controller = typeof AbortController === "function" ? new AbortController() : null;
    const requestOptions = controller ? { ...options, signal: controller.signal } : options;
    let timer;
    let timedOut = false;
    const request = Promise.resolve().then(() => deps.fetch(url, requestOptions));
    const timeout = new Promise((_, reject) => {
      timer = setTimeout(() => {
        timedOut = true;
        try {
          controller?.abort();
        } catch {
          // An adapter may expose a nonstandard AbortController.
        }
        reject(new GatewayAuthError("timeout", "gateway request timed out", { retryable: true }));
      }, normalized.timeoutMs);
    });
    try {
      return await Promise.race([request, timeout]);
    } catch (error) {
      if (timedOut) throw error;
      throw asTransient(error, "gateway request failed");
    } finally {
      clearTimeout(timer);
    }
  }

  function subscribe(listener) {
    if (typeof listener !== "function") throw new TypeError("listener must be a function");
    subscribers.add(listener);
    try {
      listener(getStatus());
    } catch {
      // A subscriber must not interrupt authentication.
    }
    return () => subscribers.delete(listener);
  }

  async function test() {
    await ensureLoaded();
    return {
      ok: true,
      storage: true,
      crypto: true,
      fetch: true,
      storageKey: normalized.storageKey,
      portalOrigin: normalized.portalOrigin,
      servicePath: normalized.servicePath,
      redirectUri: normalized.redirectUri,
      hasCredential: Boolean(loadedState.credential),
    };
  }

  return {
    authenticate,
    completeCallback,
    resumePendingExchange,
    retryExchange,
    awaitServiceReady,
    getHeaders,
    refresh,
    signOut,
    retryRevocation,
    getStatus,
    subscribe,
    test,
  };

  function getStatus() {
    return { ...status };
  }

  function updateStatus(nextStatus, details = {}, reason = null) {
    status = makeStatus(nextStatus, details, reason);
    const snapshot = getStatus();
    for (const listener of subscribers) {
      try {
        listener(snapshot);
      } catch {
        // A subscriber must not interrupt authentication.
      }
    }
  }

  function resolveAppAuthorization(target) {
    const configured = normalized.appAuthorization;
    if (!configured) return {};
    if (typeof configured === "function") {
      return Promise.resolve(configured(target)).then((value) => appAuthorizationHeaders(value));
    }
    return appAuthorizationHeaders(configured);
  }
}

export const createAuthClient = createMobileGatewayAuth;
export default createMobileGatewayAuth;

function normalizeConfig(config = {}) {
  config = config ?? {};
  const issuerConfig = config.issuerConfig && typeof config.issuerConfig === "object" ? config.issuerConfig : {};
  const merged = { ...issuerConfig, ...config };
  const portalOrigin = normalizePortalOrigin(merged.portalOrigin);
  const service = normalizeServicePath(merged.servicePath);
  if (service.origin !== portalOrigin) {
    throw new GatewayAuthError("config_invalid", "servicePath must use the portalOrigin origin");
  }
  const redirectUri = normalizeRedirectUri(merged.redirectUri);
  const clientId = requiredString(merged.clientId, "clientId");
  const serviceId = requiredString(merged.serviceId, "serviceId");
  const expectedApiBasePrefix = normalizePrefix(merged.expectedApiBasePrefix ?? service.pathname);
  const issuer = merged.issuer ? normalizePortalOrigin(merged.issuer) : portalOrigin;
  const binding = Object.freeze({
    portalOrigin,
    serviceOrigin: service.origin,
    servicePath: service.href,
    expectedApiBasePrefix,
    clientId,
    serviceId,
    redirectUri,
    issuer,
  });
  return {
    portalOrigin,
    servicePath: service.href,
    serviceOrigin: service.origin,
    servicePrefix: service.pathname,
    expectedApiBasePrefix,
    redirectUri,
    clientId,
    serviceId,
    deviceName: requiredString(merged.deviceName ?? "AoiTalk", "deviceName"),
    storageKey: requiredString(merged.storageKey ?? DEFAULT_STORAGE_KEY, "storageKey"),
    pendingTtlMs: positiveNumber(merged.pendingTtlMs ?? DEFAULT_PENDING_TTL_MS, "pendingTtlMs"),
    expirySkewMs: nonNegativeNumber(merged.expirySkewMs ?? DEFAULT_EXPIRY_SKEW_MS, "expirySkewMs"),
    timeoutMs: positiveNumber(merged.timeoutMs ?? DEFAULT_TIMEOUT_MS, "timeoutMs"),
    maxAccessLifetimeSec: positiveNumber(merged.maxAccessLifetimeSec ?? DEFAULT_MAX_ACCESS_LIFETIME_SEC, "maxAccessLifetimeSec"),
    maxGrantLifetimeSec: positiveNumber(merged.maxGrantLifetimeSec ?? DEFAULT_MAX_GRANT_LIFETIME_SEC, "maxGrantLifetimeSec"),
    minGrantLifetimeSec: nonNegativeNumber(merged.minGrantLifetimeSec ?? DEFAULT_MIN_GRANT_LIFETIME_SEC, "minGrantLifetimeSec"),
    minRefreshLifetimeSec: nonNegativeNumber(merged.minRefreshLifetimeSec ?? DEFAULT_MIN_REFRESH_LIFETIME_SEC, "minRefreshLifetimeSec"),
    browserCallbackGraceMs: positiveNumber(merged.browserCallbackGraceMs ?? DEFAULT_BROWSER_CALLBACK_GRACE_MS, "browserCallbackGraceMs"),
    appAuthorization: merged.appAuthorization ?? null,
    requireIssuer: true,
    issuer,
    binding,
  };
}

function normalizeRuntimeWaitOptions(value) {
  const options = value && typeof value === "object" ? value : {};
  if (options.onState !== undefined && typeof options.onState !== "function") {
    throw new GatewayAuthError("config_invalid", "onState must be a function");
  }
  const requestedTimeout = Number(options.timeoutMs);
  const timeoutMs = Number.isFinite(requestedTimeout)
    ? Math.max(0, Math.min(MAX_RUNTIME_TIMEOUT_MS, requestedTimeout))
    : DEFAULT_RUNTIME_TIMEOUT_MS;
  const requestedPoll = Number(options.pollMs);
  const pollMs = Number.isFinite(requestedPoll) ? Math.max(0, requestedPoll) : DEFAULT_RUNTIME_POLL_MS;
  return {
    signal: options.signal ?? null,
    onState: options.onState,
    timeoutMs,
    pollMs,
  };
}

function runtimeBindingKey(binding) {
  return [binding?.portalOrigin, binding?.servicePath, binding?.serviceId, binding?.clientId, binding?.redirectUri, binding?.issuer].join("\u0000");
}

function runtimeAbortError() {
  return new GatewayAuthError("aborted", "service readiness wait was aborted", { retryable: true });
}

function normalizeAdapters(adapters = {}) {
  adapters = adapters ?? {};
  const storage = adapters.storage;
  const crypto = adapters.crypto;
  const browser = adapters.browser;
  const fetchImpl = adapters.fetch;
  if (!storage || typeof storage.get !== "function" || typeof storage.set !== "function" || typeof storage.delete !== "function") {
    throw new GatewayAuthError("config_invalid", "storage adapter must provide get, set, and delete");
  }
  if (!crypto || typeof crypto.randomBytes !== "function" ||
      (typeof crypto.sha256Base64Url !== "function" && typeof crypto.sha256Base64url !== "function")) {
    throw new GatewayAuthError("config_invalid", "crypto adapter must provide randomBytes and sha256Base64Url");
  }
  if (!browser || typeof browser.openAuthSession !== "function") {
    throw new GatewayAuthError("config_invalid", "browser adapter must provide openAuthSession");
  }
  if (typeof fetchImpl !== "function") {
    throw new GatewayAuthError("config_invalid", "fetch adapter is required; provide a manual-redirect-capable native adapter");
  }
  return {
    storage,
    browser,
    fetch: fetchImpl,
    timer: {
      setTimeout: adapters.timer?.setTimeout?.bind(adapters.timer) ?? ((callback, delay) => setTimeout(callback, delay)),
      clearTimeout: adapters.timer?.clearTimeout?.bind(adapters.timer) ?? ((timerId) => clearTimeout(timerId)),
    },
    crypto: {
      randomBytes: crypto.randomBytes.bind(crypto),
      sha256Base64Url: (value) => (crypto.sha256Base64Url ?? crypto.sha256Base64url).call(crypto, value),
    },
  };
}

function normalizePortalOrigin(value) {
  if (typeof value !== "string" || !value) throw new GatewayAuthError("config_invalid", "portalOrigin is required");
  let url;
  try {
    url = new URL(value);
  } catch {
    throw new GatewayAuthError("config_invalid", "portalOrigin must be an absolute URL");
  }
  if (url.protocol !== "https:" || url.username || url.password || url.search || url.hash ||
      !["/", "/index.html"].includes(url.pathname)) {
    throw new GatewayAuthError("config_invalid", "portalOrigin must be an HTTPS portal root");
  }
  return url.origin;
}

function normalizeServicePath(value) {
  if (typeof value !== "string" || !value) throw new GatewayAuthError("config_invalid", "servicePath is required");
  let url;
  try {
    url = new URL(value);
  } catch {
    throw new GatewayAuthError("config_invalid", "servicePath must be an absolute URL");
  }
  if (url.protocol !== "https:" || url.username || url.password || url.search || url.hash) {
    throw new GatewayAuthError("config_invalid", "servicePath must be an HTTPS path without credentials or query");
  }
  url.pathname = normalizePrefix(url.pathname);
  return url;
}

function normalizeRedirectUri(value) {
  if (typeof value !== "string" || !value) throw new GatewayAuthError("config_invalid", "redirectUri is required");
  let url;
  try {
    url = new URL(value);
  } catch {
    throw new GatewayAuthError("config_invalid", "redirectUri must be an absolute URI");
  }
  if (url.protocol !== "https:" || url.username || url.password || url.search || url.hash) {
    throw new GatewayAuthError("config_invalid", "redirectUri must be an HTTPS URL without query or fragment");
  }
  const reserved = new Set(["state", "code", "error", "access_token", "refresh_token", "id_token"]);
  for (const key of url.searchParams.keys()) {
    if (reserved.has(key.toLowerCase())) {
      throw new GatewayAuthError("config_invalid", "redirectUri contains an OAuth response parameter");
    }
  }
  return url.href;
}

function normalizePrefix(value) {
  if (typeof value !== "string" || !value.startsWith("/")) {
    throw new GatewayAuthError("config_invalid", "API path prefix must begin with /");
  }
  if (/%(?:2f|5c|2e)/i.test(value)) {
    throw new GatewayAuthError("config_invalid", "API path prefix contains encoded path traversal");
  }
  let prefix = value.replace(/\/+/g, "/");
  if (!prefix.startsWith("/")) prefix = `/${prefix}`;
  if (prefix.length > 1) prefix = prefix.replace(/\/+$/, "");
  return prefix || "/";
}

function normalizeTargetUrl(value) {
  if (value instanceof URL) return new URL(value.href);
  if (typeof value !== "string" || !value) throw new GatewayAuthError("target_invalid", "target URL is required");
  try {
    return new URL(value);
  } catch {
    throw new GatewayAuthError("target_invalid", "target must be an absolute URL");
  }
}

function validateTrustedTarget(value) {
  const target = normalizeTargetUrl(value);
  const queryKeys = [...target.searchParams.keys()].map((key) => key.toLowerCase());
  if (target.username || target.password || target.hash || /%(?:2f|5c|2e)/i.test(target.pathname) ||
      queryKeys.includes("access_token") || queryKeys.includes("refresh_token")) {
    throw new GatewayAuthError("target_invalid", "target URL contains prohibited credentials");
  }
  const isWebSocket = target.protocol === "wss:";
  if (target.protocol !== "https:" && !isWebSocket) {
    throw new GatewayAuthError("target_invalid", "target must use HTTPS or WSS");
  }
  return target;
}

function appAuthorizationHeaders(value) {
  if (value == null || value === "") return {};
  if (typeof value === "string") {
    if (/[\r\n]/.test(value)) throw new GatewayAuthError("config_invalid", "appAuthorization contains invalid header characters");
    return { Authorization: value };
  }
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new GatewayAuthError("config_invalid", "appAuthorization must be a string or headers object");
  }
  const headers = {};
  for (const [name, headerValue] of Object.entries(value)) {
    if (!name || /[\r\n]/.test(name) || typeof headerValue !== "string" || /[\r\n]/.test(headerValue)) {
      throw new GatewayAuthError("config_invalid", "appAuthorization headers must contain strings");
    }
    headers[name] = headerValue;
  }
  return headers;
}

function targetWithinConfig(target, normalized, credential) {
  let expectedOrigin = normalized.serviceOrigin;
  if (credential?.apiBaseUrl) {
    try {
      expectedOrigin = new URL(credential.apiBaseUrl).origin;
    } catch {
      return false;
    }
    if (expectedOrigin !== normalized.serviceOrigin) return false;
  }
  const targetOrigin = target.protocol === "wss:" ? `https://${target.host}` : target.origin;
  if (targetOrigin !== expectedOrigin) return false;
  return pathHasPrefix(target.pathname, normalized.expectedApiBasePrefix);
}

function pathHasPrefix(pathname, prefix) {
  const p = normalizePrefix(prefix);
  return p === "/" || pathname === p || pathname.startsWith(`${p}/`);
}

function makeAuthorizeUrl(pending, config) {
  const url = new URL(`${normalizePathBase(config.servicePath)}/.gateway/mobile/authorize`);
  url.searchParams.set("client_id", pending.clientId);
  url.searchParams.set("redirect_uri", pending.redirectUri);
  url.searchParams.set("state", pending.state);
  url.searchParams.set("code_challenge", pending.codeChallenge);
  url.searchParams.set("code_challenge_method", "S256");
  url.searchParams.set("device_name", config.deviceName);
  return url.href;
}
function normalizePathBase(value) {
  return value.replace(/\/+$/, "");
}

function parseAndValidateCallback(value, pending, config) {
  let callback;
  try {
    callback = new URL(value);
  } catch {
    throw new GatewayAuthError("callback_invalid", "callback URL is invalid", { retryable: true });
  }
  const expected = new URL(pending.redirectUri);
  if (callback.username || callback.password) {
    throw new GatewayAuthError("callback_invalid", "callback URL contains credentials", { retryable: true });
  }
  if (!sameCallbackEndpoint(callback, expected)) {
    throw new GatewayAuthError("callback_invalid", "callback URL does not match redirectUri", { retryable: true });
  }
  if (callback.search || !callback.hash || callback.hash === "#") {
    throw new GatewayAuthError("callback_invalid", "callback must carry OAuth response data in a fragment", { retryable: true });
  }
  const fragment = new URLSearchParams(callback.hash.slice(1));
  const allowedFragmentKeys = new Set(["code", "state", "iss", "error", "error_description"]);
  for (const key of fragment.keys()) {
    if (!allowedFragmentKeys.has(key)) {
      throw new GatewayAuthError("callback_invalid", "callback contains an unknown fragment parameter", { retryable: true });
    }
  }
  for (const forbidden of ["access_token", "refresh_token", "token_type", "expires_in", "id_token"]) {
    if (fragment.has(forbidden)) {
      throw new GatewayAuthError("callback_invalid", "callback must not contain tokens", { retryable: true });
    }
  }
  const stateValues = fragment.getAll("state");
  if (stateValues.length !== 1 || stateValues[0] !== pending.state) {
    throw new GatewayAuthError("callback_state_mismatch", "callback state does not match", { retryable: true });
  }
  if (pending.clientId !== requiredString(pending.clientId, "clientId")) {
    throw new GatewayAuthError("callback_invalid", "pending clientId is invalid", { retryable: true });
  }
  const issuerValues = fragment.getAll("iss");
  const normalizedIssuer = issuerValues.length === 1 ? normalizeIssuerClaim(issuerValues[0]) : null;
  if ((config.requireIssuer || config.issuer) && (issuerValues.length !== 1 || normalizedIssuer !== config.issuer)) {
    throw new GatewayAuthError("callback_invalid", "callback issuer does not match configuration", { retryable: true });
  }
  if (!config.requireIssuer && !config.issuer && issuerValues.length > 1) {
    throw new GatewayAuthError("callback_invalid", "callback contains duplicate issuers", { retryable: true });
  }
  const errors = fragment.getAll("error");
  if (errors.length > 1) throw new GatewayAuthError("callback_invalid", "callback contains duplicate errors", { retryable: true });
  const codes = fragment.getAll("code");
  if (errors.length === 1 && codes.length > 0) {
    throw new GatewayAuthError("callback_invalid", "callback cannot contain both code and error", { retryable: true });
  }
  if (errors.length === 1) return { error: errors[0].trim().toLowerCase() };
  if (fragment.has("error_description")) {
    throw new GatewayAuthError("callback_invalid", "callback error_description is missing error", { retryable: true });
  }
  if (codes.length !== 1 || !codes[0]) {
    throw new GatewayAuthError("callback_invalid", "callback code is missing", { retryable: true });
  }
  return { code: codes[0] };
}

function normalizeIssuerClaim(value) {
  try {
    return normalizePortalOrigin(value);
  } catch {
    return value;
  }
}

function sameCallbackEndpoint(actual, expected) {
  return actual.protocol === expected.protocol &&
    actual.hostname === expected.hostname &&
    actual.port === expected.port &&
    actual.pathname === expected.pathname;
}

function callbackUrlFromResult(input) {
  if (typeof input === "string") return input;
  if (input instanceof URL) return input.href;
  if (input && typeof input === "object") {
    if (typeof input.url === "string") return input.url;
    if (input.url instanceof URL) return input.url.href;
  }
  return null;
}

function isCancelResult(value) {
  if (value === "cancel" || value === "cancelled") return true;
  return Boolean(value && typeof value === "object" && ["cancel", "cancelled", "dismiss"].includes(String(value.type).toLowerCase()));
}

function validPending(pending, config) {
  return Boolean(pending && pending.kind === "exchange" && typeof pending.state === "string" &&
    typeof pending.codeVerifier === "string" && typeof pending.codeChallenge === "string" &&
    typeof (pending.exchangeRequestId ?? pending.request_id) === "string" && Number.isFinite(pending.expiresAt) &&
    typeof pending.clientId === "string" && pending.clientId === config.clientId &&
    pending.redirectUri === config.redirectUri && bindingMatches(pending.binding, config.binding));
}

async function makeCodeVerifier(deps) {
  const bytes = await deps.crypto.randomBytes(96);
  const value = bytesToBase64Url(bytes);
  if (value.length < 43 || value.length > 128) {
    throw new GatewayAuthError("config_invalid", "random source did not produce an RFC 7636 verifier");
  }
  return value;
}

async function makeRandomString(deps, bytes) {
  return bytesToBase64Url(await deps.crypto.randomBytes(bytes));
}

async function makeCodeChallenge(deps, verifier) {
  const result = await deps.crypto.sha256Base64Url(verifier);
  if (typeof result !== "string" || !/^[A-Za-z0-9_-]+$/.test(result)) {
    throw new GatewayAuthError("config_invalid", "SHA-256 adapter must return base64url text");
  }
  return result;
}

function tokenToCredential(payload, config) {
  const accessToken = stringValue(payload?.access_token);
  if (!accessToken || /[\r\n\s]/.test(accessToken)) {
    throw new GatewayAuthError("protocol_error", "token response contained an invalid access_token", { retryable: true });
  }
  const tokenType = stringValue(payload?.token_type);
  if (!tokenType || tokenType.toLowerCase() !== "bearer") {
    throw new GatewayAuthError("protocol_error", "token response used an unsupported token type", { retryable: true });
  }
  const expiresIn = finiteSeconds(payload?.expires_in);
  if (expiresIn == null || expiresIn <= 0 || expiresIn > config.maxAccessLifetimeSec) {
    throw new GatewayAuthError("protocol_error", "token response omitted expires_in", { retryable: true });
  }
  const refreshToken = stringValue(payload?.refresh_token);
  if (!refreshToken) {
    throw new GatewayAuthError("protocol_error", "token response omitted refresh_token", { retryable: true });
  }
  const apiBaseUrl = stringValue(payload?.api_base_url);
  if (!apiBaseUrl) throw new GatewayAuthError("protocol_error", "token response omitted api_base_url", { retryable: true });
  assertApiBaseUrl(apiBaseUrl, config);
  const responseServiceId = stringValue(payload?.service_id);
  if (!responseServiceId) {
    throw new GatewayAuthError("protocol_error", "token response omitted service_id", { retryable: true });
  }
  const serviceId = responseServiceId;
  if (serviceId !== config.serviceId) {
    throw new GatewayAuthError("protocol_error", "token response service_id did not match configuration", { retryable: true });
  }
  const deviceId = stringValue(payload?.device_id);
  if (!deviceId) throw new GatewayAuthError("protocol_error", "token response omitted device_id", { retryable: true });
  const refreshExpiresAtMs = absoluteSeconds(payload?.refresh_expires_at);
  const grantExpiresAtMs = absoluteSeconds(payload?.grant_expires_at);
  const now = Date.now();
  const nowSeconds = Math.floor(now / 1000);
  if (refreshExpiresAtMs == null || grantExpiresAtMs == null ||
      refreshExpiresAtMs <= (nowSeconds + config.minRefreshLifetimeSec) * 1000 ||
      grantExpiresAtMs <= (nowSeconds + config.minGrantLifetimeSec) * 1000 ||
      refreshExpiresAtMs > (nowSeconds + config.maxGrantLifetimeSec) * 1000 ||
      grantExpiresAtMs > (nowSeconds + config.maxGrantLifetimeSec) * 1000 ||
      grantExpiresAtMs < refreshExpiresAtMs) {
    throw new GatewayAuthError("protocol_error", "token response expiry was outside the configured lifetime", { retryable: true });
  }
  return {
    accessToken,
    tokenType: "Bearer",
    expiresAtMs: now + expiresIn * 1000,
    refreshToken,
    refreshExpiresAtMs,
    grantExpiresAtMs,
    deviceId,
    serviceId,
    apiBaseUrl: normalizeApiBaseUrl(apiBaseUrl),
    binding: config.binding,
  };
}

function assertApiBaseUrl(value, config) {
  let url;
  try {
    url = new URL(value);
  } catch {
    throw new GatewayAuthError("protocol_error", "api_base_url was invalid", { retryable: true });
  }
  if (url.protocol !== "https:" || url.username || url.password || url.search || url.hash || /%(?:2f|5c|2e)/i.test(url.pathname) ||
      url.origin !== config.serviceOrigin || normalizePrefix(url.pathname) !== config.expectedApiBasePrefix) {
    throw new GatewayAuthError("protocol_error", "api_base_url was outside the configured service boundary", { retryable: true });
  }
}

function normalizeApiBaseUrl(value) {
  const url = new URL(value);
  url.pathname = normalizePrefix(url.pathname);
  return url.href;
}

function validateTokenResponse(payload) {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new GatewayAuthError("protocol_error", "token response was not an object", { retryable: true });
  }
  return payload;
}

async function readJson(response) {
  if (!response) throw new Error("missing response");
  if (typeof response.json === "function") return response.json();
  if (typeof response.text === "function") return JSON.parse(await response.text());
  throw new Error("response has no JSON reader");
}

function errorCodeFromPayload(payload) {
  if (!payload || typeof payload !== "object") return null;
  const value = payload.error_code ?? payload.error?.code ?? payload.error ?? payload.code;
  if (typeof value !== "string") return null;
  return value.trim().toLowerCase();
}

function isDefinitive(error) {
  return error instanceof GatewayAuthError && (error.code === "revoked" || error.code === "invalid_grant");
}

function asTransient(error, message) {
  if (error instanceof GatewayAuthError) return error;
  return new GatewayAuthError("transient", message, { retryable: true, cause: error });
}

function safeErrorInfo(error) {
  return {
    code: error instanceof GatewayAuthError ? error.code : "transient",
    retryable: error instanceof GatewayAuthError ? error.retryable : true,
  };
}

function authResult(statusValue, credential) {
  return {
    status: statusValue,
    expiresAt: credential.expiresAtMs,
    refreshExpiresAt: credential.refreshExpiresAtMs,
    grantExpiresAt: credential.grantExpiresAtMs,
    deviceId: credential.deviceId,
    serviceId: credential.serviceId,
    apiBaseUrl: credential.apiBaseUrl,
  };
}

function makeStatus(value, details = {}, reason = null) {
  return {
    status: value,
    reason,
    expiresAt: details.expiresAt ?? null,
    refreshExpiresAt: details.refreshExpiresAt ?? null,
    grantExpiresAt: details.grantExpiresAt ?? null,
  };
}

function credentialStatus(credential) {
  return {
    expiresAt: credential.expiresAtMs,
    refreshExpiresAt: credential.refreshExpiresAtMs,
    grantExpiresAt: credential.grantExpiresAtMs,
  };
}

function pendingStatus(pending) {
  return { expiresAt: pending.expiresAt };
}

function isAccessUsable(credential, config) {
  return Boolean(credential && Number.isFinite(credential.expiresAtMs) && Date.now() < credential.expiresAtMs - config.expirySkewMs);
}

function isRefreshExpired(credential, config) {
  const limits = [credential.refreshExpiresAtMs, credential.grantExpiresAtMs].filter(Number.isFinite);
  if (limits.length === 0) return true;
  const limit = Math.min(...limits);
  const guardMs = Math.max(config.expirySkewMs, config.minGrantLifetimeSec * 1000);
  return Date.now() + guardMs >= limit;
}

function absoluteSeconds(value) {
  const seconds = finiteSeconds(value);
  return seconds == null ? null : seconds * 1000;
}

function finiteSeconds(value) {
  const number = typeof value === "number" || typeof value === "string" ? Number(value) : NaN;
  return Number.isFinite(number) ? number : null;
}

function stringValue(value) {
  return typeof value === "string" && value.length > 0 ? value : null;
}

function requiredString(value, name) {
  if (typeof value !== "string" || value.length === 0) {
    throw new GatewayAuthError("config_invalid", `${name} is required`);
  }
  return value;
}

function positiveNumber(value, name) {
  const number = Number(value);
  if (!Number.isFinite(number) || number <= 0) throw new GatewayAuthError("config_invalid", `${name} must be positive`);
  return number;
}

function nonNegativeNumber(value, name) {
  const number = Number(value);
  if (!Number.isFinite(number) || number < 0) throw new GatewayAuthError("config_invalid", `${name} must not be negative`);
  return number;
}

function bytesToBase64Url(value) {
  const bytes = value instanceof Uint8Array ? value : value instanceof ArrayBuffer ? new Uint8Array(value) : Uint8Array.from(value ?? []);
  if (bytes.length === 0) throw new GatewayAuthError("config_invalid", "random source returned no bytes");
  let output = "";
  for (let index = 0; index < bytes.length; index += 3) {
    const a = bytes[index];
    const b = index + 1 < bytes.length ? bytes[index + 1] : 0;
    const c = index + 2 < bytes.length ? bytes[index + 2] : 0;
    output += BASE64URL[a >> 2];
    output += BASE64URL[((a & 3) << 4) | (b >> 4)];
    if (index + 1 < bytes.length) output += BASE64URL[((b & 15) << 2) | (c >> 6)];
    if (index + 2 < bytes.length) output += BASE64URL[c & 63];
  }
  return output;
}

function copyState(state) {
  return JSON.parse(JSON.stringify(state));
}

function parseState(raw, config) {
  if (typeof raw !== "string" || raw.length === 0) return emptyState(config.binding);
  try {
    const value = JSON.parse(raw);
    if (!value || typeof value !== "object" || !bindingMatches(value.binding, config.binding)) return emptyState(config.binding);
    const state = sanitizeState(value, config.binding);
    if (state.credential && !bindingMatches(state.credential.binding, config.binding)) state.credential = null;
    if (state.pending && !bindingMatches(state.pending.binding, config.binding)) state.pending = null;
    if (state.revocation && !bindingMatches(state.revocation.binding, config.binding)) state.revocation = null;
    if (state.lastExchange && !bindingMatches(state.lastExchange.binding, config.binding)) state.lastExchange = null;
    return state;
  } catch {
    return emptyState(config.binding);
  }
}

function emptyState(binding = null) {
  return { version: 1, binding, credential: null, pending: null, revocation: null, lastExchange: null };
}

function sanitizeState(value, binding = null) {
  const state = value && typeof value === "object" && !Array.isArray(value) ? value : emptyState(binding);
  state.version = 1;
  state.binding = binding ?? state.binding ?? null;
  if (!state.credential || typeof state.credential !== "object") state.credential = null;
  if (!state.pending || typeof state.pending !== "object") state.pending = null;
  if (!state.revocation || typeof state.revocation !== "object") state.revocation = null;
  if (!state.lastExchange || typeof state.lastExchange !== "object") state.lastExchange = null;
  return state;
}

function bindingMatches(actual, expected) {
  if (!actual || !expected) return false;
  return ["portalOrigin", "serviceOrigin", "servicePath", "expectedApiBasePrefix", "clientId", "serviceId", "redirectUri", "issuer"]
    .every((key) => actual[key] === expected[key]);
}

function pendingMatches(stateOrPending, pending) {
  const actual = stateOrPending?.pending ?? stateOrPending;
  return Boolean(actual && pending && actual.kind === pending.kind && actual.state === pending.state &&
    actual.clientId === pending.clientId && actual.redirectUri === pending.redirectUri &&
    bindingMatches(actual.binding, pending.binding) &&
    (actual.exchangeRequestId ?? actual.request_id) === (pending.exchangeRequestId ?? pending.request_id));
}
