import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import DocumentsSection, { documentLink } from "./DocumentsSection";
import type { ComplianceDocument } from "../lib/mega-pack1-types";

/**
 * A compliance document's storage link is where the document lives. The API
 * stored it and nothing showed it, and the form never asked for it. Now the
 * form asks, the table opens it -- and only when it is an http(s) URL, because
 * an <a href> is the one place a stored string becomes something a click runs.
 */

afterEach(cleanup);

const noop = () => undefined;

function doc(over: Partial<ComplianceDocument> = {}): ComplianceDocument {
  return {
    id: 1,
    document_no: "SOP-001",
    title: "Line start-up",
    document_type: "SOP",
    department: "Production",
    version: "1.0",
    owner: "QA Lead",
    approval_status: "Draft",
    review_due_date: "2026-12-01",
    storage_link: null,
    notes: null,
    ...over,
  } as ComplianceDocument;
}

function mount(documents: ComplianceDocument[]) {
  return render(
    <DocumentsSection
      documents={documents}
      analytics={null}
      form={{ document_no: "", title: "", document_type: "SOP", department: "Production", version: "1.0", owner: "QA Lead",
        approval_status: "Draft", review_due_date: "2026-12-01", storage_link: "", notes: "" }}
      setForm={noop}
      createDocument={(e) => e.preventDefault()}
      updateDocument={noop}
      deleteDocument={noop}
      generateReviewEscalations={noop}
    />,
  );
}

describe("documentLink", () => {
  it("opens only http(s)", () => {
    expect(documentLink("https://dms.example.com/sop-001")).toBe("https://dms.example.com/sop-001");
    expect(documentLink("  http://intranet/sop ")).toBe("http://intranet/sop");
    expect(documentLink("javascript:alert(1)")).toBeNull();
    expect(documentLink("file:///C:/sops/sop.pdf")).toBeNull();
    expect(documentLink("")).toBeNull();
    expect(documentLink(null)).toBeNull();
  });
});

describe("DocumentsSection: the storage link", () => {
  it("asks for the link when adding a document", () => {
    mount([]);
    expect(screen.getByPlaceholderText(/Link to the document/)).toBeTruthy();
  });

  it("opens a stored http(s) link in a new tab, and shows nothing for a document without one", () => {
    mount([doc({ id: 1, storage_link: "https://dms.example.com/sop-001" }), doc({ id: 2, document_no: "SOP-002" })]);
    const link = screen.getByTestId("document-link-1") as HTMLAnchorElement;
    expect(link.getAttribute("href")).toBe("https://dms.example.com/sop-001");
    expect(link.getAttribute("target")).toBe("_blank");
    expect(link.getAttribute("rel")).toContain("noopener");
    expect(screen.queryByTestId("document-link-2")).toBeNull();
  });

  it("never turns a stored javascript: link into something a click runs", () => {
    mount([doc({ id: 3, storage_link: "javascript:alert(1)" })]);
    expect(screen.queryByTestId("document-link-3")).toBeNull();
    expect(document.querySelector('a[href^="javascript:"]')).toBeNull();
  });
});
