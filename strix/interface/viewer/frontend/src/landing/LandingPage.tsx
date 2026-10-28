import { useCallback, useEffect, useRef, useState } from "react";
import { Showcase } from "./Showcase";
import "./landing.css";

/**
 * The public landing page, shown before sign-in. It has one job: get a new user
 * to their first scan. So it says what Strix does in a sentence, shows the three
 * steps of the scan form, lists what must be in place first, and keeps the
 * overview video for anyone who wants to see it working.
 *
 * Everything here describes the browser flow (New scan). The form accepts web
 * targets only (a URL, domain or IP); source code and repositories go through the
 * command line, which is mentioned once rather than documented here.
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
            <a href="#get">What you get</a>
            <a href="#preview">What a scan looks like</a>
            <a href="#how">First scan in 3 steps</a>
            <a href="#before">Before you start</a>
            <a href="#overview">Watch the video</a>
          </nav>
        </div>
      </header>

      <main id="top">
        <section className="hero">
          <div className="wrap">
            <h1>Find the security bugs in your web app before attackers do.</h1>
            <p className="lede">
              Strix is an AI pentester. Give it the address of your app and a team of AI agents
              tries to break in, proves every problem with a working exploit, and shows you the
              results right here.
            </p>
            <div className="cta">
              <button type="button" className="btn" data-testid="landing-scan-cta" onClick={onScan}>
                Scan your project
              </button>
              <a className="textlink" href="#how">
                See how it works
              </a>
            </div>
            <ul className="trust" aria-label="What Strix gives you">
              <li>Runs on your machine</li>
              <li>Every finding proven with an exploit</li>
              <li>CLI-first, pipeline-ready</li>
            </ul>
          </div>
        </section>

        <section className="band" aria-label="Strix at a glance">
          <div className="wrap">
            <dl className="metrics">
              <div>
                <dt>100%</dt>
                <dd>runs on your machine</dd>
              </div>
              <div>
                <dt>44</dt>
                <dd>tools in the agent sandbox</dd>
              </div>
              <div>
                <dt>PoC</dt>
                <dd>request + response per finding</dd>
              </div>
              <div>
                <dt>
                  <code>exit 2</code>
                </dt>
                <dd>non-zero when it finds bugs</dd>
              </div>
            </dl>
          </div>
        </section>

        <section className="row" id="get">
          <div className="wrap">
            <div>
              <h2>What you get</h2>
              <p className="note">A pentest team, not a scanner.</p>
            </div>
            <div className="body">
              <ul className="prereq stack">
                <li>
                  <b>Every finding is proven.</b> Agents build a working exploit and save the
                  request and response, so you triage facts instead of guesses.
                </li>
                <li>
                  <b>It runs in a local sandbox.</b> The agents attack from inside a Docker
                  container on your machine — nothing deploys to a vendor cloud.
                </li>
                <li>
                  <b>The viewer explains itself.</b> A live agent graph, validated vulnerabilities,
                  run history and reports — all on this screen.
                </li>
                <li>
                  <b>It drops into your pipeline.</b> Run headless with <code>-n</code>; it exits
                  non-zero on findings and writes Markdown, HTML and <code>findings.sarif</code>{" "}
                  (SARIF 2.1.0) into <code>strix_runs/</code>.
                </li>
              </ul>
            </div>
          </div>
        </section>

        <Showcase />

        <section className="row" id="how">
          <div className="wrap">
            <div>
              <h2>Your first scan in 3 steps</h2>
              <p className="note">Check the list below first.</p>
            </div>
            <div className="body">
              <ol className="steps">
                <li>
                  <div>
                    <strong>Enter your web address</strong>
                    <span>
                      Sign in, then type the address of the app to test, for example{" "}
                      <code>https://staging.example.com</code>. Leave the mode on{" "}
                      <b>Quick</b> for a first run.
                    </span>
                  </div>
                </li>
                <li>
                  <div>
                    <strong>Confirm you may test it</strong>
                    <span>
                      Tick “I am authorized to test this target”, then press <b>Start scan</b>.
                    </span>
                  </div>
                </li>
                <li>
                  <div>
                    <strong>Watch it work, then read the results</strong>
                    <span>
                      Follow the agents live on the <b>Agents</b> page. When the scan ends, open{" "}
                      <b>Last scan</b> and <b>Issues</b> to see what it found, each with proof and a
                      suggested fix.
                    </span>
                  </div>
                </li>
              </ol>
              <p className="aside">
                This form scans web apps and APIs (a URL, domain or IP address). To scan a source
                folder or a Git repository, use the command line:{" "}
                <code>strix --target ./app-directory</code>
              </p>
            </div>
          </div>
        </section>

        <section className="row" id="before">
          <div className="wrap">
            <div>
              <h2>Before you start</h2>
              <p className="note">Three things to have ready.</p>
            </div>
            <div className="body">
              <ul className="prereq stack">
                <li>
                  <b>Docker is running.</b> The agents work inside a Docker sandbox on your
                  machine. The first scan downloads it, so allow a few minutes.
                </li>
                <li>
                  <b>A model is set up.</b> Open <b>Settings</b> in the sidebar, choose your AI
                  provider (or a local model), add the key if it needs one, and press the connection
                  test.
                </li>
                <li>
                  <b>You have permission.</b> Only test apps you own or have written permission to
                  test. Strix runs real exploits, so prefer a staging copy and a test account.
                </li>
              </ul>
              <p className="aside">
                Your findings and logs stay on this machine. The agents’ prompts go to your model
                provider, so use a local model such as Ollama if the code is sensitive.
              </p>
              <div className="cta">
                <button type="button" className="btn" onClick={onScan}>
                  Scan your project
                </button>
              </div>
            </div>
          </div>
        </section>

        <section className="row" id="privacy">
          <div className="wrap">
            <div>
              <h2>Data &amp; privacy</h2>
              <p className="note">You know where your data goes.</p>
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
                      This viewer, which listens only on <code>127.0.0.1</code> behind a private
                      tokened link.
                    </li>
                  </ul>
                </div>
                <div className="out">
                  <h3>Goes to your model provider</h3>
                  <ul>
                    <li>The agents’ prompts, which include the code and responses they analyse.</li>
                    <li>This applies to any hosted model you configure.</li>
                    <li>
                      Point <code>LLM_API_BASE</code> at an approved internal endpoint or a local
                      model such as Ollama if the code is sensitive.
                    </li>
                  </ul>
                </div>
              </div>
              <p className="caution">
                Only test apps you own or have written permission to test. Strix runs{" "}
                <b>real exploits</b> against its target — prefer a staging environment and a
                dedicated test account over production data and real credentials.
              </p>
            </div>
          </div>
        </section>

        <VideoStage />
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
    <section className="stage row" id="overview" aria-label="Overview video">
      <div className="wrap">
        <div>
          <h2>See it in action</h2>
          <p className="note">79 seconds.</p>
        </div>
        <div className="body">
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
      </div>
    </section>
  );
}

export default LandingPage;
