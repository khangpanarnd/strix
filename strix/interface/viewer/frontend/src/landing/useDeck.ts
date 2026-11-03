import { useCallback, useEffect, useRef, useState } from "react";

/**
 * Slide navigation for the landing page.
 *
 * The page is a vertical stack of full-height sections that scroll with the document;
 * scroll snapping is limited to while the page is mounted by `html:has(.lp)` in the
 * stylesheet, so nothing leaks onto the dashboard. Every section carries
 * `data-slide` and `data-name`. This hook tracks which one is in view, which ones
 * have been seen (to trigger their entrance animation), and wires the keyboard.
 */

export interface SlideInfo {
  id: string;
  name: string;
}

export interface Deck {
  rootRef: React.RefObject<HTMLDivElement | null>;
  slides: SlideInfo[];
  active: number;
  seen: ReadonlySet<number>;
  go: (index: number) => void;
}

/** Elements that own the arrow/space/enter keys while focused. */
const KEY_OWNERS = 'video, input, textarea, select, [contenteditable="true"], [role="tablist"]';
const ACTIVATES_ON_KEY = "button, a, summary";

function prefersReducedMotion(): boolean {
  return typeof window.matchMedia === "function" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

export function useDeck(): Deck {
  const rootRef = useRef<HTMLDivElement | null>(null);
  const elements = useRef<HTMLElement[]>([]);
  const activeRef = useRef(0);
  const [slides, setSlides] = useState<SlideInfo[]>([]);
  const [active, setActiveState] = useState(0);
  const [seen, setSeen] = useState<ReadonlySet<number>>(() => new Set([0]));

  const setActive = useCallback((index: number) => {
    activeRef.current = index;
    setActiveState(index);
  }, []);

  const go = useCallback(
    (index: number) => {
      const list = elements.current;
      if (list.length === 0) return;
      const next = Math.max(0, Math.min(list.length - 1, index));
      list[next].scrollIntoView({ behavior: prefersReducedMotion() ? "auto" : "smooth", block: "start" });
      setActive(next);
    },
    [setActive]
  );

  // Find the slides once, and watch which one is in view.
  useEffect(() => {
    const root = rootRef.current;
    if (!root) return;
    const list = Array.from(root.querySelectorAll<HTMLElement>("[data-slide]"));
    elements.current = list;
    setSlides(list.map((el) => ({ id: el.id, name: el.dataset.name ?? "" })));

    if (typeof IntersectionObserver === "undefined") {
      setSeen(new Set(list.map((_, i) => i)));
      return;
    }

    const indexOf = (target: Element) => list.indexOf(target as HTMLElement);
    const visible = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          const index = indexOf(entry.target);
          if (entry.isIntersecting && index >= 0) setActive(index);
        }
      },
      { rootMargin: "-45% 0px -45% 0px" }
    );
    const reveal = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          const index = indexOf(entry.target);
          if (!entry.isIntersecting || index < 0) continue;
          setSeen((previous) => (previous.has(index) ? previous : new Set(previous).add(index)));
          reveal.unobserve(entry.target);
        }
      },
      { threshold: 0.15 }
    );
    list.forEach((el) => {
      visible.observe(el);
      reveal.observe(el);
    });
    return () => {
      visible.disconnect();
      reveal.disconnect();
    };
  }, [setActive]);

  // Keyboard: arrows / page keys / space move between slides, F toggles full screen.
  useEffect(() => {
    const toggleFullscreen = () => {
      try {
        if (!document.fullscreenElement) void document.documentElement.requestFullscreen();
        else void document.exitFullscreen();
      } catch {
        /* full screen is optional */
      }
    };

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.altKey || event.ctrlKey || event.metaKey || event.defaultPrevented) return;
      // The zoom dialog and the screenshot tabs own their keys.
      if (rootRef.current?.querySelector("dialog[open]")) return;
      const target = event.target instanceof Element ? event.target : null;
      if (target?.closest(KEY_OWNERS)) return;
      const key = event.key;
      if ((key === " " || key === "Enter") && target?.closest(ACTIVATES_ON_KEY)) return;

      const here = activeRef.current;
      if (key === "ArrowRight" || key === "ArrowDown" || key === "PageDown" || (key === " " && !event.shiftKey)) {
        event.preventDefault();
        go(here + 1);
      } else if (key === "ArrowLeft" || key === "ArrowUp" || key === "PageUp" || (key === " " && event.shiftKey)) {
        event.preventDefault();
        go(here - 1);
      } else if (key === "Home") {
        event.preventDefault();
        go(0);
      } else if (key === "End") {
        event.preventDefault();
        go(elements.current.length - 1);
      } else if (key === "f" || key === "F") {
        toggleFullscreen();
      }
    };

    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [go]);

  return { rootRef, slides, active, seen, go };
}
