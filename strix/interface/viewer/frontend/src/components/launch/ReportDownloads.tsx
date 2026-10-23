import { useEffect, useState } from "react";
import { Download } from "lucide-react";
import {
  ARTIFACT_KEYS,
  artifactUrl,
  fetchArtifacts,
  type ArtifactKey,
} from "@/data/serverSource";

/**
 * Download links for a run's report artifacts (frontend-components §ReportDownloads).
 *
 * - Queries `/api/run/artifacts?run=` and renders a link only for files the
 *   server reports present; the four keys are fixed (BR-32). Re-queries once
 *   when the run finishes.
 * - Each link is a plain `<a href=... download>` so the browser handles the
 *   download and the session cookie is sent automatically.
 * - For `report.html`, if the issue-html-export affordance already exists on
 *   the page (`htmlHandledElsewhere`), this hides its own HTML link (BR-33).
 */

export interface ReportDownloadsProps {
  runName: string;
  finished: boolean;
  /** Set by the call site when an existing HTML-export route owns report.html. */
  htmlHandledElsewhere?: boolean;
}

const LABELS: Record<ArtifactKey, string> = {
  "report.html": "Report (HTML)",
  "penetration_test_report.md": "Report (Markdown)",
  "findings.sarif": "SARIF",
  "vulnerabilities.json": "Findings (JSON)",
};

export function ReportDownloads({ runName, finished, htmlHandledElsewhere }: ReportDownloadsProps) {
  const [available, setAvailable] = useState<ArtifactKey[]>([]);

  // Load on mount and re-query once the run finishes (new artifacts appear as
  // the run settles). `finished` in the deps gives exactly that second query.
  useEffect(() => {
    let cancelled = false;
    void fetchArtifacts(runName).then((keys) => {
      if (!cancelled) setAvailable(keys);
    });
    return () => {
      cancelled = true;
    };
  }, [runName, finished]);

  const links = ARTIFACT_KEYS.filter((key) => {
    if (!available.includes(key)) return false;
    if (key === "report.html" && htmlHandledElsewhere) return false;
    return true;
  });

  if (links.length === 0) return null;

  return (
    <div
      className="flex flex-wrap items-center gap-2"
      data-testid="report-downloads"
      aria-label="Report downloads"
    >
      {links.map((key) => (
        <a
          key={key}
          href={artifactUrl(runName, key)}
          download
          data-testid={`report-download-${key}`}
          className="inline-flex items-center gap-1.5 rounded-lg border border-[#2a2a2a] bg-[rgba(255,255,255,0.03)] px-3 py-1.5 text-xs font-medium text-[#ddd] transition-colors hover:border-[#555] hover:text-white"
        >
          <Download className="h-3.5 w-3.5" aria-hidden="true" />
          {LABELS[key]}
        </a>
      ))}
    </div>
  );
}

export default ReportDownloads;
