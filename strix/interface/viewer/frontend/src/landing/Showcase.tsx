import { useEffect, useId, useRef, useState } from "react";

/**
 * "See what a scan looks like": three real screens of the viewer, in tabs, each
 * annotated with numbered callouts and a plain-English legend.
 *
 * The screenshots are used exactly as supplied (assets/Screenshot*.png, copied
 * unchanged into public/). The legend, not the badges, carries the explanation:
 * badges are decorative and hidden from assistive technology, and the legend
 * works at any screen width. Badge positions are percentages of the image so they
 * follow it as it scales; they were measured on the full-size screenshots.
 */

interface Callout {
  /** Badge position as a percentage of the image. */
  x: number;
  y: number;
  title: string;
  text: string;
}

interface Slide {
  id: string;
  tab: string;
  heading: string;
  lede: string;
  src: string;
  width: number;
  height: number;
  alt: string;
  caption: string;
  callouts: Callout[];
}

const SLIDES: Slide[] = [
  {
    id: "running",
    tab: "While it runs",
    heading: "Watch a team of AI agents work, live.",
    lede: "This is the Agents page during a scan. Nothing to refresh: it updates every second.",
    src: "./landing-scan-running.png",
    width: 1615,
    height: 939,
    alt:
      "The Agents page of a running scan: a clock showing the scan in progress, a graph with one " +
      "Root Agent, and a live server log listing the sandbox starting up.",
    caption: "A real scan, a few seconds after it started.",
    callouts: [
      {
        x: 27.5,
        y: 4.6,
        title: "The clock",
        text: "When the scan started and how long it has been running.",
      },
      {
        x: 56.5,
        y: 34.6,
        title: "The agents",
        text: "Each box is an AI agent. The Root Agent plans the attack, then starts specialist agents as it learns about your app. Click one to read what it is doing.",
      },
      {
        x: 27.5,
        y: 61.6,
        title: "The server log",
        text: "A running diary of what is happening: the sandbox starting, the model connecting, tools running. If the scan looks quiet, look here first.",
      },
    ],
  },
  {
    id: "results",
    tab: "When it finishes",
    heading: "Read what it found.",
    lede: "When the scan ends, the Last scan page gathers the result in one place.",
    src: "./landing-scan-results.png",
    width: 1594,
    height: 943,
    alt:
      "The Last scan page of a finished scan: download buttons for the report, a panel of run " +
      "details, a bar showing the issues found by severity, and an executive summary.",
    caption: "A real finished scan.",
    callouts: [
      {
        x: 30.5,
        y: 3.1,
        title: "Take the report with you",
        text: "Download it as HTML or Markdown to read and share, or as SARIF or JSON to feed other tools.",
      },
      {
        x: 27.5,
        y: 27.5,
        title: "How the scan ran",
        text: "The target, scan mode, model, tokens used and cost, so a result is never a mystery.",
      },
      {
        x: 27.5,
        y: 58.6,
        title: "Issues by severity",
        text: "How many problems were found, from critical down to low, with a written summary below. Open Issues to see each one with proof and a suggested fix.",
      },
    ],
  },
  {
    id: "agent",
    tab: "Inside an agent",
    heading: "See how an agent thinks.",
    lede: "Click any agent on the graph to open its transcript.",
    src: "./landing-agent-notes.png",
    width: 1561,
    height: 943,
    alt:
      "An agent's transcript window showing the threat model it built for the target and a note " +
      "it read from another agent.",
    caption: "A real agent transcript.",
    callouts: [
      {
        x: 30.5,
        y: 22,
        title: "A specialist agent",
        text: "Agents have a job, such as hunting for known vulnerabilities. Each keeps its own transcript.",
      },
      {
        x: 30.5,
        y: 27.5,
        title: "Every step it takes",
        text: "Here the agent builds a threat model of the target: what is exposed and where an attacker would look.",
      },
      {
        x: 30.5,
        y: 72.1,
        title: "Notes for the team",
        text: "Agents write down what they learn so the others can build on it, the way a red team would.",
      },
    ],
  },
];
export function Showcase() {
  const uid = useId();
  const [active, setActive] = useState(0);
  const tabs = useRef<Array<HTMLButtonElement | null>>([]);
  // The slide shown full-size in the enlarge dialog, if any.
  const [zoom, setZoom] = useState<Slide | null>(null);
  const dialog = useRef<HTMLDialogElement>(null);

  // showModal() gives focus handling, Esc to close and a backdrop for free, and
  // returns focus to the screenshot button when it closes.
  useEffect(() => {
    const el = dialog.current;
    if (!el) return;
    if (zoom && !el.open) el.showModal();
    if (!zoom && el.open) el.close();
  }, [zoom]);

  const select = (index: number, focus = false) => {
    const next = (index + SLIDES.length) % SLIDES.length;
    setActive(next);
    if (focus) tabs.current[next]?.focus();
  };

  const onKeyDown = (event: React.KeyboardEvent, index: number) => {
    switch (event.key) {
      case "ArrowRight":
        event.preventDefault();
        select(index + 1, true);
        break;
      case "ArrowLeft":
        event.preventDefault();
        select(index - 1, true);
        break;
      case "Home":
        event.preventDefault();
        select(0, true);
        break;
      case "End":
        event.preventDefault();
        select(SLIDES.length - 1, true);
        break;
      default:
    }
  };

  return (
    <section className="row showcase" id="preview" data-testid="landing-showcase">
      <div className="wrap">
        <div>
          <h2>See what a scan looks like</h2>
          <p className="note">Three screens you will use.</p>
        </div>
        <div className="body">
          <div className="showcase-tabs" role="tablist" aria-label="Scan screens">
            {SLIDES.map((slide, index) => (
              <button
                key={slide.id}
                ref={(el) => {
                  tabs.current[index] = el;
                }}
                type="button"
                role="tab"
                id={`${uid}-tab-${slide.id}`}
                aria-selected={active === index}
                aria-controls={`${uid}-panel-${slide.id}`}
                tabIndex={active === index ? 0 : -1}
                onClick={() => select(index)}
                onKeyDown={(e) => onKeyDown(e, index)}
              >
                <span className="showcase-tab-n" aria-hidden="true">
                  {index + 1}
                </span>
                {slide.tab}
              </button>
            ))}
          </div>

          {SLIDES.map((slide, index) => (
            <div
              key={slide.id}
              role="tabpanel"
              id={`${uid}-panel-${slide.id}`}
              aria-labelledby={`${uid}-tab-${slide.id}`}
              hidden={active !== index}
              className="showcase-panel"
            >
              <h3>{slide.heading}</h3>
              <p className="showcase-lede">{slide.lede}</p>

              <figure className="showcase-figure">
                <div className="frame">
                  <div className="frame-bar" aria-hidden="true">
                    <i />
                    <i />
                    <i />
                    <span>Strix viewer</span>
                  </div>
                  <div className="frame-shot">
                    <button
                      type="button"
                      className="shot-zoom"
                      aria-label={`Enlarge screenshot: ${slide.tab}`}
                      aria-haspopup="dialog"
                      data-testid={`showcase-zoom-${slide.id}`}
                      onClick={() => setZoom(slide)}
                    >
                      <img
                        src={slide.src}
                        width={slide.width}
                        height={slide.height}
                        alt={slide.alt}
                        loading={index === 0 ? "eager" : "lazy"}
                        decoding="async"
                      />
                    </button>
                    {slide.callouts.map((c, i) => (
                      <span
                        key={c.title}
                        className="badge"
                        aria-hidden="true"
                        style={{ left: `${c.x}%`, top: `${c.y}%` }}
                      >
                        {i + 1}
                      </span>
                    ))}
                  </div>
                </div>
                <figcaption>
                  {slide.caption} Click the screenshot to enlarge it.
                </figcaption>
              </figure>

              <ol className="callouts">
                {slide.callouts.map((c) => (
                  <li key={c.title}>
                    <strong>{c.title}</strong>
                    <span>{c.text}</span>
                  </li>
                ))}
              </ol>
            </div>
          ))}
        </div>
      </div>

      <dialog
        ref={dialog}
        className="zoom"
        aria-label={zoom ? `Screenshot: ${zoom.tab}` : "Screenshot"}
        data-testid="showcase-zoom-dialog"
        onClose={() => setZoom(null)}
        onClick={(e) => {
          // A click on the backdrop lands on the dialog element itself.
          if (e.target === e.currentTarget) setZoom(null);
        }}
      >
        <button
          type="button"
          className="zoom-close"
          data-testid="showcase-zoom-close"
          onClick={() => setZoom(null)}
        >
          Close
        </button>
        <div className="zoom-scroll">
          {zoom && (
            <img src={zoom.src} width={zoom.width} height={zoom.height} alt={zoom.alt} />
          )}
        </div>
      </dialog>
    </section>
  );
}

export default Showcase;
