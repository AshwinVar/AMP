import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

// The copilot asks /ai/status once on mount (feature detection) and posts each
// question. Both are mocked; what this suite watches is the BODY of each post.
vi.mock("../lib/api", () => ({
  apiGet: vi.fn(() => Promise.resolve({ enabled: false })),
  apiPost: vi.fn(),
}));

import AICopilot from "./AICopilot";
import { apiPost } from "../lib/api";

/**
 * Follow-up questions (ADR-0035). The screen sends the conversation so far as a
 * `thread` -- each prior question and the calls AMP ran for it, never the
 * answer or its evidence -- so "and its downtime?" can be resolved server-side,
 * and it says which machine a pronoun landed on.
 */

const post = apiPost as unknown as ReturnType<typeof vi.fn>;

function answer(over: Record<string, unknown> = {}) {
  return {
    answer: "CNC-01 is running.",
    view: "machines",
    evidence: [{ id: "F1", label: "Status", value: "Running", provenance: "MEASURED FACT" }],
    tools: [{ tool: "get_machine_history", state: "OK", summary: "", notes: [], elapsed_ms: 1 }],
    state: "OK",
    engine: "rules",
    plan: { planner: "rules", calls: [{ tool: "get_machine_history", arguments: { machine: "CNC-01" } }] },
    thread: { turns: 0, resolved: null },
    ...over,
  };
}

async function ask(text: string) {
  const before = post.mock.calls.length;
  const box = screen.getByRole("textbox");
  fireEvent.change(box, { target: { value: text } });
  fireEvent.keyDown(box, { key: "Enter", code: "Enter" });
  await waitFor(() => expect(post.mock.calls.length).toBeGreaterThan(before));
}

describe("AICopilot follow-ups (ADR-0035)", () => {
  beforeEach(() => {
    post.mockReset();
  });

  it("sends the prior question and the calls AMP ran, never the answer or its evidence", async () => {
    post.mockResolvedValueOnce(answer());
    post.mockResolvedValueOnce(answer({ answer: "CNC-01 had 12 minutes of downtime.", thread: { turns: 1, resolved: { machine: "CNC-01" } } }));
    render(<AICopilot />);
    await ask("How is CNC-01 doing?");
    expect(post.mock.calls[0][1]).toEqual({ question: "How is CNC-01 doing?", thread: [] });
    await screen.findByText("CNC-01 is running.");
    await ask("and its downtime?");
    const body = post.mock.calls[1][1] as { question: string; thread: unknown[] };
    expect(body.question).toBe("and its downtime?");
    expect(body.thread).toEqual([
      { question: "How is CNC-01 doing?", calls: [{ tool: "get_machine_history", arguments: { machine: "CNC-01" } }] },
    ]);
    const sent = JSON.stringify(body.thread);
    expect(sent).not.toContain("CNC-01 is running.");
    expect(sent).not.toContain("MEASURED FACT");
  });

  it("says which machine a pronoun was taken to mean", async () => {
    post.mockResolvedValueOnce(answer());
    post.mockResolvedValueOnce(answer({ answer: "12 minutes.", thread: { turns: 1, resolved: { machine: "CNC-01" } } }));
    render(<AICopilot />);
    await ask("How is CNC-01 doing?");
    await screen.findByText("CNC-01 is running.");
    expect(screen.queryByText("About CNC-01")).toBeNull();
    await ask("and its downtime?");
    await screen.findByText("12 minutes.");
    expect(screen.getByText("About CNC-01")).toBeTruthy();
  });

  it("sends at most the last six turns", async () => {
    for (let i = 0; i < 8; i++) post.mockResolvedValueOnce(answer({ answer: `answer ${i}` }));
    render(<AICopilot />);
    for (let i = 0; i < 8; i++) {
      await ask(`question ${i}`);
      await screen.findByText(`answer ${i}`);
    }
    const last = post.mock.calls[7][1] as { thread: { question: string }[] };
    expect(last.thread.map((t) => t.question)).toEqual(["question 1", "question 2", "question 3", "question 4", "question 5", "question 6"]);
  });
});
