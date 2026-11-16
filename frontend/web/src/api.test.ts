import { afterEach, describe, expect, it, vi } from "vitest";
import {
  ApiError,
  API_BASE_URL,
  apiRequest,
  apiUrl,
  clearCsrfToken,
  extractApiError,
  logoutBrowserSession,
  setCsrfToken,
} from "./api";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
  clearCsrfToken();
});

describe("apiUrl", () => {
  it("appends an endpoint to the configured API base URL", () => {
    expect(apiUrl("/auth/me")).toBe(`${API_BASE_URL}/auth/me`);
  });

  it("normalizes paths without a leading slash", () => {
    expect(apiUrl("organizations")).toBe(`${API_BASE_URL}/organizations`);
  });
});

describe("extractApiError", () => {
  it("returns FastAPI string details", () => {
    expect(extractApiError({ detail: "Not authorized" }, "Fallback")).toBe(
      "Not authorized",
    );
  });

  it("formats FastAPI validation details", () => {
    expect(
      extractApiError(
        { detail: [{ msg: "Field required" }, { msg: "Invalid value" }] },
        "Fallback",
      ),
    ).toBe("Field required, Invalid value");
  });

  it("uses the fallback for unknown response shapes", () => {
    expect(extractApiError({ message: "unknown" }, "Request failed")).toBe(
      "Request failed",
    );
  });
});

describe("apiRequest", () => {
  it("retries a transient network failure for safe read requests", async () => {
    vi.useFakeTimers();
    const fetchMock = vi
      .fn()
      .mockRejectedValueOnce(new TypeError("Connection refused"))
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ id: "user-1" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      );
    vi.stubGlobal("fetch", fetchMock);

    const request = apiRequest("/auth/browser-session");
    await vi.runAllTimersAsync();
    await expect(request).resolves.toEqual({ id: "user-1" });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("does not retry network failures for writes", async () => {
    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, _init?: RequestInit): Promise<Response> => {
      if (String(input).endsWith("/auth/csrf")) {
        return new Response(JSON.stringify({ csrf_token: "csrf-test" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
        throw new TypeError("Connection refused");
      },
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      apiRequest("/auth/browser-session/login", { method: "POST", body: "{}" }),
    ).rejects.toThrow("Aurora API could not be reached");
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls[1][1]?.method).toBe("POST");
  });

  it("sends same-origin credentials for browser sessions", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ id: "user-1" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await apiRequest("/auth/browser-session");

    expect(fetchMock).toHaveBeenCalledWith(
      apiUrl("/auth/browser-session"),
      expect.objectContaining({ credentials: "same-origin" }),
    );
  });

  it("adds the CSRF header to unsafe requests", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    setCsrfToken("csrf-test-token");

    await apiRequest("/organizations", {
      method: "POST",
      body: JSON.stringify({ name: "Test" }),
    });

    const [, options] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(new Headers(options.headers).get("X-CSRF-Token")).toBe("csrf-test-token");
  });

  it("bootstraps a CSRF token before an unsafe request", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ csrf_token: "csrf-from-server" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ ok: true }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      );
    vi.stubGlobal("fetch", fetchMock);

    await apiRequest("/auth/browser-session/login", {
      method: "POST",
      body: JSON.stringify({ email: "person@example.com", password: "secret" }),
    });

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls[0][0]).toBe(apiUrl("/auth/csrf"));
    const [, options] = fetchMock.mock.calls[1] as [string, RequestInit];
    expect(new Headers(options.headers).get("X-CSRF-Token")).toBe("csrf-from-server");
  });


  it("refreshes a stale CSRF token once when another tab rotated it", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ detail: "CSRF validation failed." }), {
          status: 403,
          headers: { "Content-Type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ csrf_token: "fresh-csrf-token" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ ok: true }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      );
    vi.stubGlobal("fetch", fetchMock);
    setCsrfToken("stale-csrf-token");

    await apiRequest("/organizations", {
      method: "POST",
      body: JSON.stringify({ name: "Cross-tab" }),
    });

    expect(fetchMock).toHaveBeenCalledTimes(3);
    const [, retryOptions] = fetchMock.mock.calls[2] as [string, RequestInit];
    expect(new Headers(retryOptions.headers).get("X-CSRF-Token")).toBe(
      "fresh-csrf-token",
    );
  });


  it("treats a confirmed expired session as successfully signed out", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ csrf_token: "csrf-for-logout" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ detail: "Session expired" }), {
          status: 401,
          headers: { "Content-Type": "application/json" },
        }),
      );
    vi.stubGlobal("fetch", fetchMock);

    await expect(logoutBrowserSession()).resolves.toBeUndefined();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("propagates logout failures so callers retain authenticated state", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ csrf_token: "csrf-for-logout" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ detail: "Logout temporarily unavailable" }), {
          status: 503,
          statusText: "Service Unavailable",
          headers: { "Content-Type": "application/json" },
        }),
      );
    vi.stubGlobal("fetch", fetchMock);

    await expect(logoutBrowserSession()).rejects.toMatchObject({
      name: "ApiError",
      status: 503,
      message: "Logout temporarily unavailable",
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("propagates network failures during logout", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ csrf_token: "csrf-for-logout" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      )
      .mockRejectedValueOnce(new TypeError("Network unavailable"));
    vi.stubGlobal("fetch", fetchMock);

    await expect(logoutBrowserSession()).rejects.toMatchObject({
      message:
        "Aurora API could not be reached. Check that the backend is running on port 8000 and that the /api proxy is available, then retry.",
      cause: expect.objectContaining({ message: "Network unavailable" }),
    });
  });

  it("preserves the HTTP status on API errors", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail: "Session expired" }), {
          status: 401,
          statusText: "Unauthorized",
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );

    await expect(apiRequest("/auth/me")).rejects.toMatchObject({
      name: "ApiError",
      message: "Session expired",
      status: 401,
    });
  });

  it("exposes API errors as typed errors", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail: "Forbidden" }), {
          status: 403,
          statusText: "Forbidden",
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );

    await apiRequest("/organizations").catch((error: unknown) => {
      expect(error).toBeInstanceOf(ApiError);
      expect((error as ApiError).status).toBe(403);
    });
  });
});
