import { useCallback, useEffect, useRef, useState } from "react";
import "./landing.css";

/**
 * The public landing page, shown before sign-in. A port of the supplied
 * `strix-landing.html`: same copy and structure, with the hero button now
 * reading "Scan your project" and handing off to the app, the video loaded from
 * a file (it was embedded as base64, which cannot be seeked reliably), and the
 * page's two scripts (video chapters, copy buttons) as components.
 *
 * Everything on this page is static and public; nothing here touches the API.
 */

interface LandingPageProps {
  /** The user chose to scan a project: continue to sign-in (or the app, if already signed in). */
  onScan: () => void;
}

const PAGE_TITLE = "Strix: local AI pentesting for your own apps";

const CHAPTERS = [
  { label: "Scan targets", at: 9, time: "0:09" },
  { label: "AI agents", at: 22, time: "0:22" },
  { label: "Findings", at: 37, time: "0:37" },
  { label: "Local viewer", at: 52, time: "0:52" },
  { label: "Recap", at: 67, time: "1:07" },
] as const;

const VIDEO_SRC = "./strix-local-intro.mp4";
const POSTER_SRC = "./strix-landing-poster.jpg";

export function LandingPage({ onScan }: LandingPageProps) {
  // The landing page has its own title; the dashboard's is restored on the way out.
  useEffect(() => {
    const previous = document.title;
    document.title = PAGE_TITLE;
    return () => {
      document.title = previous;
    };
  }, []);

  return (
    <div className="landing" data-testid="landing-page">
      <header className="top">
        <div className="wrap">
          <a className="brand" href="#top">
            Strix<small>Internal guide</small>
          </a>
          <nav aria-label="Page sections">
            <a href="#overview">Overview video</a>
            <a href="#how">How it works</a>
            <a href="#start">Quick start</a>
            <a href="#privacy">Data and privacy</a>
          </nav>
        </div>
      </header>

      <main id="top">
        <section className="hero">
          <div className="wrap">
            <h1>Find exploitable bugs in your own app before anyone else does.</h1>
            <p className="lede">
              Strix is an AI pentesting tool. A team of agents attacks your code or running app
              inside a local Docker sandbox, proves each finding with a working exploit, and saves
              everything to your disk.
            </p>
            <div className="cta">
              <button type="button" className="btn" data-testid="landing-scan-cta" onClick={onScan}>
                Scan your project
              </button>
              <a className="textlink" href="#overview">
                Watch the 79-second overview
              </a>
            </div>
          </div>
        </section>

        <VideoStage />

        <section className="row" id="what">
          <div className="wrap">
            <div>
              <h2>What you get</h2>
            </div>
            <div className="body">
              <dl className="facts">
                <div>
                  <dt>Proven findings</dt>
                  <dd>
                    Every reported issue comes with a working proof of concept and reproduction
                    steps, so you spend time fixing bugs instead of triaging scanner noise.
                  </dd>
                </div>
                <div>
                  <dt>A team of agents</dt>
                  <dd>
                    Specialist agents split recon, exploitation and validation. They share what they
                    discover and chain findings together, the way a red team would.
                  </dd>
                </div>
                <div>
                  <dt>A real attacker’s toolkit</dt>
                  <dd>
                    An HTTP interception proxy, browser automation for XSS, CSRF and auth bypass, a
                    shell, a Python sandbox for exploit code, recon, and static plus dynamic code
                    analysis.
                  </dd>
                </div>
                <div>
                  <dt>Wide coverage</dt>
                  <dd>
                    Broken access control (IDOR, privilege escalation), injection (SQL, NoSQL,
                    command, SSTI), SSRF, XXE, RCE, XSS, CSRF, JWT and session attacks,
                    business-logic flaws, race conditions, API weaknesses and cloud
                    misconfiguration.
                  </dd>
                </div>
                <div>
                  <dt>Scored and explained</dt>
                  <dd>
                    Each finding carries a severity, a CVSS score, the affected endpoint and
                    guidance on how to fix it.
                  </dd>
                </div>
              </dl>
            </div>
          </div>
        </section>

        <section className="row" id="how">
          <div className="wrap">
            <div>
              <h2>How a scan runs</h2>
            </div>
            <div className="body">
              <ol className="steps">
                <li>
                  <div>
                    <strong>Point it at a target</strong>
                    <span>
                      A local folder, a Git repository or a running web app. Add instructions if
                      you want it to focus on something specific.
                    </span>
                  </div>
                </li>
                <li>
                  <div>
                    <strong>Agents work in a sandbox</strong>
                    <span>
                      The agents run inside a Docker container on your machine, mapping the attack
                      surface, trying exploits and validating what works.
                    </span>
                  </div>
                </li>
                <li>
                  <div>
                    <strong>Results land on your disk</strong>
                    <span>
                      Findings are written as the scan runs to <code>strix_runs/&lt;run-name&gt;</code>
                      . A headless mode exits with a non-zero code when it finds vulnerabilities,
                      which suits CI.
                    </span>
                  </div>
                </li>
                <li>
                  <div>
                    <strong>Review in the local viewer</strong>
                    <span>
                      Run <code>strix view</code> for a dashboard with an overview, validated
                      vulnerabilities, a live agent graph, mid-scan steering, run history and
                      reports.
                    </span>
                  </div>
                </li>
              </ol>
            </div>
          </div>
        </section>

        <TargetsSection />
        <QuickStartSection />
        <PrivacySection />
      </main>

      <footer>
        <div className="wrap">
          <span>
            Internal fork of{" "}
            <a href="https://github.com/usestrix/strix" rel="noopener noreferrer">
              usestrix/strix
            </a>
            , licensed under Apache 2.0.
          </span>
          <span>
            <a href="https://github.com/vankhangfet/strix" rel="noopener noreferrer">
              Source repository
            </a>{" "}
            &nbsp;&nbsp;{" "}
            <a href="https://docs.strix.ai" rel="noopener noreferrer">
              Upstream documentation
            </a>
          </span>
        </div>
      </footer>
    </div>
  );
}

/** The overview video with chapter buttons that seek and follow playback. */
function VideoStage() {
  const video = useRef<HTMLVideoElement>(null);
  const [active, setActive] = useState(-1);

  const syncChapter = useCallback(() => {
    const time = video.current?.currentTime ?? 0;
    let current = -1;
    CHAPTERS.forEach((chapter, index) => {
      if (time >= chapter.at) current = index;
    });
    setActive(current);
  }, []);

  const jump = (seconds: number) => {
    const el = video.current;
    if (!el) return;
    el.currentTime = seconds;
    // Autoplay can be refused by the browser; the user can still press play.
    void el.play().catch(() => undefined);
    syncChapter();
  };

  return (
    <section className="stage" id="overview" aria-label="Overview video">
      <div className="wrap">
        <div className="player">
          <video
            ref={video}
            controls
            preload="metadata"
            playsInline
            poster={POSTER_SRC}
            onTimeUpdate={syncChapter}
            onSeeked={syncChapter}
            data-testid="landing-video"
          >
            <source src={VIDEO_SRC} type="video/mp4" />
            Your browser can’t play this video.
          </video>
        </div>
        <ul className="chapters" aria-label="Jump to a part of the video">
          <li>Jump to</li>
          {CHAPTERS.map((chapter, index) => (
            <li key={chapter.label}>
              <button
                type="button"
                aria-current={active === index ? "true" : undefined}
                onClick={() => jump(chapter.at)}
              >
                {chapter.label}
                <time>{chapter.time}</time>
              </button>
            </li>
          ))}
        </ul>
      </div>
    </section>
  );
}

function TargetsSection() {
  return (
    <section className="row" id="targets">
      <div className="wrap">
        <div>
          <h2>What you can scan</h2>
        </div>
        <div className="body">
          <div className="tablewrap" role="region" aria-label="Scan targets" tabIndex={0}>
            <table>
              <thead>
                <tr>
                  <th scope="col">Target</th>
                  <th scope="col">Command</th>
                  <th scope="col">Good for</th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <td>Local code</td>
                  <td>
                    <code>strix --target ./app-directory</code>
                  </td>
                  <td>Source-aware (white-box) review of a project on your machine.</td>
                </tr>
                <tr>
                  <td>Very large repo</td>
                  <td>
                    <code>strix --mount ./big-repo</code>
                  </td>
                  <td>Mounts the folder read-only instead of copying it into the sandbox.</td>
                </tr>
                <tr>
                  <td>Git repository</td>
                  <td>
                    <code>strix --target https://github.com/org/repo</code>
                  </td>
                  <td>Security review of a repository’s code.</td>
                </tr>
                <tr>
                  <td>Running app</td>
                  <td>
                    <code>strix --target https://your-app.com</code>
                  </td>
                  <td>Black-box assessment of a deployed or staging app.</td>
                </tr>
                <tr>
                  <td>Logged-in testing</td>
                  <td>
                    <code>
                      --instruction "Perform authenticated testing using credentials: user:pass"
                    </code>
                  </td>
                  <td>Grey-box testing behind a login. Use a test account.</td>
                </tr>
                <tr>
                  <td>Several targets</td>
                  <td>
                    <code>strix --target-list ./targets.txt</code>
                  </td>
                  <td>One target per line in a file.</td>
                </tr>
                <tr>
                  <td>Pull request changes</td>
                  <td>
                    <code>
                      strix -n --target ./ --scan-mode quick --scope-mode diff --diff-base
                      origin/main
                    </code>
                  </td>
                  <td>Scans only what changed against a base branch.</td>
                </tr>
              </tbody>
            </table>
          </div>
          <div className="modes" role="list">
            <div role="listitem">
              <b>quick</b>
              <span>Fast checks for CI.</span>
            </div>
            <div role="listitem">
              <b>standard</b>
              <span>Routine testing.</span>
            </div>
            <div role="listitem">
              <b>deep</b>
              <span>Thorough review. This is the default.</span>
            </div>
          </div>
          <p style={{ marginTop: 16, color: "var(--slate)", fontSize: ".9375rem" }}>
            Choose a mode with <code>--scan-mode</code>. Cap spending on a run with{" "}
            <code>--max-budget-usd</code>, and continue an interrupted run with{" "}
            <code>--resume</code>.
          </p>
        </div>
      </div>
    </section>
  );
}

function QuickStartSection() {
  return (
    <section className="row" id="start">
      <div className="wrap">
        <div>
          <h2>Quick start</h2>
          <p className="note">Builds from our fork, not the public release.</p>
        </div>
        <div className="body">
          <ul className="prereq">
            <li>
              <b>Docker</b> installed and running
            </li>
            <li>
              <b>Python 3.12+</b> and <b>uv</b>
            </li>
            <li>
              <b>An LLM API key</b> from a provider your team has approved
            </li>
            <li>
              <b>Permission</b> to test whatever you point it at
            </li>
          </ul>

          <CodeBlock
            copyText={[
              "git clone https://github.com/vankhangfet/strix.git",
              "cd strix",
              "make install",
              "",
              'export STRIX_LLM="anthropic/claude-sonnet-4-6"',
              'export LLM_API_KEY="your-api-key"',
              "",
              "uv run strix --target ./app-directory",
            ].join("\n")}
          >
            {`git clone https://github.com/vankhangfet/strix.git
cd strix
make install

export STRIX_LLM="anthropic/claude-sonnet-4-6"
export LLM_API_KEY="your-api-key"

uv run strix --target ./app-directory`}
          </CodeBlock>
          <p>
            The first run pulls the sandbox Docker image, so allow a few minutes. Your model
            settings are saved to <code>~/.strix/cli-config.json</code>, so you only set them once.
          </p>

          <h3>Open the results</h3>
          <CodeBlock copyText={"uv run strix view\nuv run strix view my-run-name"}>
            {"uv run strix view                 "}
            <span className="c"># most recent run</span>
            {"\nuv run strix view my-run-name     "}
            <span className="c"># a specific run</span>
          </CodeBlock>

          <h3>Run it in a pipeline</h3>
          <CodeBlock copyText="uv run strix -n --target ./ --scan-mode quick">
            {"uv run strix -n --target ./ --scan-mode quick"}
          </CodeBlock>
          <p>
            The <code>-n</code> flag runs without the interactive screen and exits non-zero when it
            finds vulnerabilities.
          </p>
        </div>
      </div>
    </section>
  );
}

function PrivacySection() {
  return (
    <section className="row" id="privacy">
      <div className="wrap">
        <div>
          <h2>Data and privacy</h2>
        </div>
        <div className="body">
          <div className="split">
            <div>
              <h3>Stays on your machine</h3>
              <ul>
                <li>The sandbox, where the agents run and attack.</li>
                <li>
                  Every finding, log and report in <code>strix_runs/</code>.
                </li>
                <li>
                  The viewer, which listens only on <code>127.0.0.1</code> behind a private tokened
                  link.
                </li>
              </ul>
            </div>
            <div className="out">
              <h3>Goes to your model provider</h3>
              <ul>
                <li>
                  The agents’ prompts, which include the code and responses they are analysing.
                </li>
                <li>
                  This applies to any hosted model. Point <code>LLM_API_BASE</code> at an approved
                  internal endpoint or a local model such as Ollama or LM Studio if the code is
                  sensitive.
                </li>
              </ul>
            </div>
          </div>
          <h3>Before you scan</h3>
          <div className="caution">
            <p>
              Only test apps you own or have written permission to test. Strix runs real exploits
              against its target. Prefer a staging environment and a dedicated test account over
              production data and real credentials.
            </p>
          </div>
        </div>
      </div>
    </section>
  );
}

/** A code block with a Copy button. `copyText` is what lands on the clipboard (no comments). */
function CodeBlock({ children, copyText }: { children: React.ReactNode; copyText: string }) {
  const [label, setLabel] = useState<"Copy" | "Copied" | "Press Ctrl+C">("Copy");
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  useEffect(() => () => clearTimeout(timer.current), []);

  const copy = async () => {
    setLabel((await copyToClipboard(copyText)) ? "Copied" : "Press Ctrl+C");
    clearTimeout(timer.current);
    timer.current = setTimeout(() => setLabel("Copy"), 1800);
  };

  return (
    <div className="code">
      <pre>
        <code>{children}</code>
      </pre>
      <button type="button" className="copy" onClick={() => void copy()}>
        {label}
      </button>
    </div>
  );
}

/** Clipboard API where it is allowed, otherwise the legacy selection-based copy. */
async function copyToClipboard(text: string): Promise<boolean> {
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      /* fall through to the legacy path */
    }
  }
  const area = document.createElement("textarea");
  area.value = text;
  area.setAttribute("readonly", "");
  area.style.position = "fixed";
  area.style.opacity = "0";
  document.body.appendChild(area);
  area.select();
  try {
    return document.execCommand("copy");
  } catch {
    return false;
  } finally {
    document.body.removeChild(area);
  }
}

export default LandingPage;
