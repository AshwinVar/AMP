import { act, renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { describeActionFailure, useActionError } from "./useActionError";

/**
 * Pins the message the user actually reads when a write fails.
 *
 * Before this, every create/update/delete handler on the dashboard ended in
 * `console.error(error)` and rendered nothing, so a rejected save was
 * indistinguishable from one that worked - except that the row never appeared.
 *
 * The interesting part is digging the useful sentence out of what lib/api.ts
 * throws, because it throws two different shapes, and because #382 put real
 * field-level messages in the body that were being discarded.
 */

// What apiPost / apiPut throw: the raw response body.
const rawBody = (body: unknown) => new Error(JSON.stringify(body));
// What apiGet / apiPatch / apiDelete throw.
const wrapped = (status: number, body: unknown) =>
  new Error(`Failed request: /work-orders/1 | ${status} | ${JSON.stringify(body)}`);

describe("describeActionFailure", () => {
  it("surfaces the field-level message the backend went to the trouble of writing", () => {
    // #382's whole point: the 400 names the offending field.
    const error = rawBody({ detail: "quantity must be a whole number" });

    expect(describeActionFailure(error, "add machine")).toBe(
      "Could not add machine: quantity must be a whole number",
    );
  });

  it("reads the detail out of the wrapped shape too", () => {
    const error = wrapped(400, { detail: "Remnant: 'unit' is required" });

    expect(describeActionFailure(error, "update work order")).toBe(
      "Could not update work order: Remnant: 'unit' is required",
    );
  });

  it("joins a FastAPI validation list into one sentence", () => {
    const error = wrapped(422, {
      detail: [
        { loc: ["body", "quantity"], msg: "value is not a valid integer" },
        { loc: ["body", "unit"], msg: "field required" },
      ],
    });

    expect(describeActionFailure(error, "create purchase order")).toBe(
      "Could not create purchase order: value is not a valid integer; field required",
    );
  });

  it("says something useful when the body is not JSON at all", () => {
    // A 502 from a proxy is an HTML page; there is nothing quotable in it, and
    // dumping markup at the user would be worse than saying nothing.
    const error = new Error("Failed request: /machines | 502 | <html><body>Bad Gateway</body></html>");

    expect(describeActionFailure(error, "add machine")).toBe("Could not add machine. Please try again.");
  });

  it("distinguishes never reaching the server from being refused by it", () => {
    // fetch() rejects with a TypeError when the request never landed. "Check
    // your connection" is right here and wrong for a 400.
    const error = new TypeError("Failed to fetch");

    expect(describeActionFailure(error, "delete supplier")).toBe(
      "Could not delete supplier — no response from the server. Check your connection and try again.",
    );
  });

  it("truncates a runaway detail instead of pasting a wall of text", () => {
    const error = rawBody({ detail: "x".repeat(500) });
    const message = describeActionFailure(error, "create cost");

    expect(message.length).toBeLessThan(200);
    expect(message.endsWith("…")).toBe(true);
  });

  it("survives an empty body, a bare string, and a thrown non-Error", () => {
    expect(describeActionFailure(new Error(""), "add shift")).toBe(
      "Could not add shift. Please try again.",
    );
    expect(describeActionFailure("boom", "add shift")).toBe(
      "Could not add shift. Please try again.",
    );
    expect(describeActionFailure(undefined, "add shift")).toBe(
      "Could not add shift. Please try again.",
    );
  });

  it("ignores a detail that is present but empty", () => {
    expect(describeActionFailure(rawBody({ detail: "   " }), "add shift")).toBe(
      "Could not add shift. Please try again.",
    );
  });
});

describe("useActionError", () => {
  it("holds the latest failure and lets the user dismiss it", () => {
    const { result } = renderHook(() => useActionError());
    expect(result.current.error).toBeNull();

    act(() => result.current.report(rawBody({ detail: "nope" }), "add machine"));
    expect(result.current.error).toBe("Could not add machine: nope");

    act(() => result.current.clear());
    expect(result.current.error).toBeNull();
  });

  it("replaces an older failure rather than stacking them", () => {
    const { result } = renderHook(() => useActionError());

    act(() => result.current.report(rawBody({ detail: "first" }), "add machine"));
    act(() => result.current.report(rawBody({ detail: "second" }), "add shift"));

    expect(result.current.error).toBe("Could not add shift: second");
  });
});
