import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { useModalFocus } from "./useModalFocus";

/**
 * AMP has eleven drawers that render as `role="dialog" aria-modal="true"` over
 * the page. All of them close on Escape. None of them managed focus.
 *
 * For a keyboard user that means opening a drawer changes nothing about where
 * they are: focus stays on the trigger button, now behind an overlay, and the
 * next Tab walks into the page *underneath* the drawer - controls they cannot
 * see and did not ask for. `aria-modal="true"` tells a screen reader the rest
 * of the page is inert while the browser happily keeps tabbing through it.
 * Closing the drawer then drops focus on `<body>`, so the next Tab restarts
 * from the top of the document rather than from the control they came from.
 *
 * jsdom does not move focus on Tab by itself, so these tests do not assert
 * "Tab moved focus" - they assert the three things the hook itself does:
 * pull focus in on open, redirect a Tab that would leave, and put focus back
 * where it came from on close.
 */

function tabFrom(element: Element, shiftKey = false) {
  const event = new KeyboardEvent("keydown", {
    key: "Tab",
    shiftKey,
    bubbles: true,
    cancelable: true,
  });
  element.dispatchEvent(event);
  return event;
}

function Drawer({ empty = false, withDisabled = false }) {
  const ref = useModalFocus<HTMLDivElement>();
  return (
    <div ref={ref} role="dialog" aria-modal="true" tabIndex={-1} data-testid="drawer">
      {!empty && (
        <>
          {withDisabled && <button disabled data-testid="disabled">disabled</button>}
          <button data-testid="first">first</button>
          <button data-testid="middle">middle</button>
          <button data-testid="last">last</button>
        </>
      )}
    </div>
  );
}

/** A button outside the drawer, standing in for the page behind the overlay. */
function mountTrigger() {
  const trigger = document.createElement("button");
  trigger.setAttribute("data-testid", "trigger");
  document.body.appendChild(trigger);
  trigger.focus();
  return trigger;
}

describe("useModalFocus", () => {
  it("pulls focus into the drawer when it opens", () => {
    const trigger = mountTrigger();
    expect(document.activeElement).toBe(trigger);

    const { getByTestId } = render(<Drawer />);

    // Without this, the user is still on the trigger, behind the overlay.
    expect(document.activeElement).toBe(getByTestId("first"));
  });

  it("focuses the dialog itself when it has nothing focusable inside", () => {
    mountTrigger();
    const { getByTestId } = render(<Drawer empty />);

    // A loading or empty drawer still has to take focus, or Tab keeps walking
    // the page underneath.
    expect(document.activeElement).toBe(getByTestId("drawer"));
  });

  it("skips disabled controls when choosing where to land", () => {
    mountTrigger();
    const { getByTestId } = render(<Drawer withDisabled />);

    expect(document.activeElement).toBe(getByTestId("first"));
  });

  it("wraps Tab from the last control back to the first", () => {
    mountTrigger();
    const { getByTestId } = render(<Drawer />);
    const last = getByTestId("last") as HTMLButtonElement;
    last.focus();

    const event = tabFrom(last);

    expect(event.defaultPrevented).toBe(true);
    expect(document.activeElement).toBe(getByTestId("first"));
  });

  it("wraps Shift+Tab from the first control back to the last", () => {
    mountTrigger();
    const { getByTestId } = render(<Drawer />);
    const first = getByTestId("first") as HTMLButtonElement;
    first.focus();

    const event = tabFrom(first, true);

    expect(event.defaultPrevented).toBe(true);
    expect(document.activeElement).toBe(getByTestId("last"));
  });

  it("leaves an ordinary Tab in the middle of the drawer alone", () => {
    mountTrigger();
    const { getByTestId } = render(<Drawer />);
    const middle = getByTestId("middle") as HTMLButtonElement;
    middle.focus();

    const event = tabFrom(middle);

    // Trapping every Tab would break normal movement inside the drawer.
    expect(event.defaultPrevented).toBe(false);
    expect(document.activeElement).toBe(middle);
  });

  it("pulls focus back if it has escaped to the page behind", () => {
    const trigger = mountTrigger();
    const { getByTestId } = render(<Drawer />);
    trigger.focus(); // e.g. a click landed outside, or a stray programmatic focus

    const event = tabFrom(trigger);

    expect(event.defaultPrevented).toBe(true);
    expect(document.activeElement).toBe(getByTestId("first"));
  });

  it("returns focus to whatever opened it when the drawer closes", () => {
    const trigger = mountTrigger();
    const { unmount } = render(<Drawer />);
    expect(document.activeElement).not.toBe(trigger);

    unmount();

    // Otherwise focus falls to <body> and the next Tab restarts from the top of
    // the page, not from the control the user opened the drawer with.
    expect(document.activeElement).toBe(trigger);
  });

  it("stops trapping once the drawer is gone", () => {
    const trigger = mountTrigger();
    const { unmount } = render(<Drawer />);
    unmount();

    // A listener left on document would hijack Tab for the rest of the session.
    const event = tabFrom(trigger);
    expect(event.defaultPrevented).toBe(false);
  });
});
