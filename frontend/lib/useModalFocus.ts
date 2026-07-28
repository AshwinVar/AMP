import { useEffect, useRef } from "react";

/**
 * Focus management for the drawers.
 *
 * AMP's drawers render as `role="dialog" aria-modal="true"` over the page and
 * close on Escape, but none of them moved focus. Opening one left the user on
 * the trigger button behind the overlay, and the next Tab walked into the page
 * *underneath* the drawer — controls they cannot see. `aria-modal="true"` tells
 * a screen reader the rest of the page is inert while the browser keeps tabbing
 * through it, which is worse than saying nothing. Closing dropped focus on
 * `<body>`, restarting the next Tab from the top of the document.
 *
 * Attach the returned ref to the dialog element (give it `tabIndex={-1}` so it
 * can hold focus itself when it has no focusable children, e.g. while loading).
 */

const FOCUSABLE = [
  "a[href]",
  "button:not([disabled])",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  '[tabindex]:not([tabindex="-1"])',
].join(", ");

function focusableWithin(node: HTMLElement) {
  return Array.from(node.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
    (el) => !el.hasAttribute("disabled") && el.getAttribute("aria-hidden") !== "true",
  );
}

export function useModalFocus<T extends HTMLElement>() {
  const ref = useRef<T>(null);

  useEffect(() => {
    const node = ref.current;
    if (!node) return;

    // Whatever opened the drawer — we owe them focus back on close.
    const opener = document.activeElement as HTMLElement | null;

    const inside = focusableWithin(node);
    // An empty or still-loading drawer takes focus itself rather than leaving
    // the user behind the overlay.
    (inside[0] ?? node).focus();

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Tab") return;

      const focusable = focusableWithin(node);
      const active = document.activeElement;

      if (focusable.length === 0) {
        event.preventDefault();
        node.focus();
        return;
      }

      const first = focusable[0];
      const last = focusable[focusable.length - 1];

      // Focus escaped the drawer (a click outside, a stray programmatic focus).
      if (!node.contains(active)) {
        event.preventDefault();
        first.focus();
        return;
      }

      // Only the two edges are redirected; Tab inside the drawer is left alone.
      if (event.shiftKey && active === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && active === last) {
        event.preventDefault();
        first.focus();
      }
    };

    document.addEventListener("keydown", onKeyDown, true);

    return () => {
      document.removeEventListener("keydown", onKeyDown, true);
      // Only take focus back if the drawer still holds it — if something else
      // has claimed focus in the meantime, stealing it would be the same rudeness
      // this hook exists to fix.
      if (!node.contains(document.activeElement) && document.activeElement !== document.body) {
        return;
      }
      opener?.focus?.();
    };
  }, []);

  return ref;
}
