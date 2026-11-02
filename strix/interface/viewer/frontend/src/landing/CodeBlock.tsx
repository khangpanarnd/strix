import { useEffect, useRef, useState, type ReactNode } from "react";

interface CodeBlockProps {
  /** The exact text placed on the clipboard (one line, no prompt). */
  text: string;
  /** The same command, with flags wrapped in <span className="flag"> for colour. */
  children: ReactNode;
}

async function writeClipboard(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    // Insecure context or permission denied: fall back to a hidden textarea.
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    let ok = false;
    try {
      ok = document.execCommand("copy");
    } catch {
      ok = false;
    }
    document.body.removeChild(area);
    return ok;
  }
}

/** A shell command with a copy button. The "$" prompt is drawn by CSS and is never copied. */
export function CodeBlock({ text, children }: CodeBlockProps) {
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");
  const timer = useRef<number | undefined>(undefined);

  useEffect(() => () => window.clearTimeout(timer.current), []);

  const copy = async () => {
    const ok = await writeClipboard(text);
    setState(ok ? "copied" : "failed");
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setState("idle"), 1800);
  };

  const label = state === "copied" ? "Copied" : state === "failed" ? "Copy failed" : "Copy command";

  return (
    <div className="code" data-testid="landing-code">
      <span className="cm">{children}</span>
      <button
        type="button"
        className={`icon-btn copy-btn${state === "copied" ? " done" : ""}`}
        aria-label={label}
        title={label}
        onClick={() => void copy()}
      >
        {state === "copied" ? (
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="M5 12l4 4 10-10" />
          </svg>
        ) : (
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <rect x="9" y="9" width="11" height="11" rx="2" />
            <path d="M5 15V6a2 2 0 0 1 2-2h9" />
          </svg>
        )}
      </button>
      <span className="sr-only" role="status" aria-live="polite">
        {state === "copied" ? "Copied to clipboard" : state === "failed" ? "Copy failed" : ""}
      </span>
    </div>
  );
}
