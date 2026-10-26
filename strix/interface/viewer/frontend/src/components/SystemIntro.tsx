import type { ReactNode } from "react";
import { Bot, Cloud, Code, Crosshair, FileText, Globe, Radar, Search, Server } from "lucide-react";

/**
 * Introduction card for the Overview tab. Static, run-independent content that
 * explains what P-hacker Strix Agents is and how its AI agents carry out an
 * assessment. It sits above the current run's details and findings so the
 * Overview both introduces the system and shows the run's results.
 */

interface Step {
  icon: ReactNode;
  title: string;
  body: string;
}

const STEPS: Step[] = [
  {
    icon: <Radar className="h-4 w-4" aria-hidden="true" />,
    title: "Scope & plan",
    body: "Reads the target and the chosen scan mode (quick, standard, or deep), then plans the assessment.",
  },
  {
    icon: <Search className="h-4 w-4" aria-hidden="true" />,
    title: "Recon & map",
    body: "Explores the attack surface inside an isolated sandbox using a browser, an HTTP proxy, and terminal tools.",
  },
  {
    icon: <Crosshair className="h-4 w-4" aria-hidden="true" />,
    title: "Test & validate",
    body: "Spawns specialized sub-agents that probe for vulnerabilities and safely confirm the ones that are real.",
  },
  {
    icon: <FileText className="h-4 w-4" aria-hidden="true" />,
    title: "Report",
    body: "Consolidates confirmed findings with severity, evidence, and remediation guidance.",
  },
];

const CAPABILITIES: { icon: ReactNode; label: string }[] = [
  { icon: <Globe className="h-3.5 w-3.5" aria-hidden="true" />, label: "Web applications" },
  { icon: <Server className="h-3.5 w-3.5" aria-hidden="true" />, label: "APIs (OpenAPI / Postman)" },
  { icon: <Code className="h-3.5 w-3.5" aria-hidden="true" />, label: "Source code repositories" },
  { icon: <Radar className="h-3.5 w-3.5" aria-hidden="true" />, label: "Domains & IPs" },
  { icon: <Cloud className="h-3.5 w-3.5" aria-hidden="true" />, label: "Cloud resources" },
];

export function SystemIntro() {
  return (
    <section
      data-testid="system-intro"
      className="animate-card-in rounded-xl border border-[#222] bg-[rgba(255,255,255,0.02)] p-5"
    >
      <div className="flex items-center gap-2">
        <Bot className="h-4 w-4 text-emerald-400" aria-hidden="true" />
        <h2 className="text-sm font-semibold text-white">About P-hacker Strix Agents</h2>
      </div>
      <p className="mt-2 max-w-3xl text-sm leading-relaxed text-[#bbb]">
        P-hacker Strix Agents is an autonomous AI penetration-testing system. Point it at a target
        and a coordinated team of AI agents plans the assessment, runs it inside an isolated
        sandbox, and writes up what it finds — no manual tooling required.
      </p>

      <h3 className="mb-3 mt-5 text-[11px] font-semibold uppercase tracking-wide text-[#555]">
        How it runs a test
      </h3>
      <ol className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        {STEPS.map((step, i) => (
          <li
            key={step.title}
            className="flex gap-3 rounded-lg border border-[#1e1e1e] bg-[rgba(255,255,255,0.02)] p-3"
          >
            <span className="mt-0.5 flex h-7 w-7 flex-none items-center justify-center rounded-full bg-[rgba(255,255,255,0.05)] text-emerald-400">
              {step.icon}
            </span>
            <span className="min-w-0">
              <span className="flex items-center gap-2 text-sm font-medium text-white">
                <span className="text-[11px] tabular-nums text-[#555]">{i + 1}</span>
                {step.title}
              </span>
              <span className="mt-0.5 block text-xs leading-relaxed text-[#888]">{step.body}</span>
            </span>
          </li>
        ))}
      </ol>

      <h3 className="mb-3 mt-5 text-[11px] font-semibold uppercase tracking-wide text-[#555]">
        What it can test
      </h3>
      <div className="flex flex-wrap gap-2">
        {CAPABILITIES.map((c) => (
          <span
            key={c.label}
            className="inline-flex items-center gap-1.5 rounded-full border border-[#2a2a2a] bg-[rgba(255,255,255,0.03)] px-3 py-1 text-xs text-[#bbb]"
          >
            <span className="text-emerald-400">{c.icon}</span>
            {c.label}
          </span>
        ))}
      </div>

      <p className="mt-5 text-xs leading-relaxed text-[#666]">
        Start an assessment from <span className="text-[#aaa]">New scan</span>, watch the agents
        work in real time under <span className="text-[#aaa]">Agents</span>, and review confirmed
        findings under <span className="text-[#aaa]">Issues</span>.
      </p>
    </section>
  );
}

export default SystemIntro;
