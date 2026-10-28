import { AlertTriangle, Check, Pencil, Plus, Trash2 } from "lucide-react";
import { cn } from "@/lib/utils";
import type { LlmProfile, LlmPreset } from "@/data/serverSource";

/**
 * The profiles list at the top of the Settings page (llm-profiles-spec.md
 * §UI). One row per saved profile: name, preset label, model, base host, key
 * set / not set, and an ACTIVE badge on the active one. Each row (except for a
 * read-only viewer) carries Activate / Edit / Delete buttons; Delete is
 * disabled for the active profile ("switch first"). A "+ New profile" button
 * sits above the list.
 *
 * Accessibility: action buttons carry an `aria-label` naming the profile (the
 * icon alone is not a label), the ACTIVE badge is text, and the delete
 * confirmation is an inline `role="dialog"`.
 */

/** The host of a base URL string, or "" when it cannot be parsed. */
function hostOf(base: string | null): string {
  const trimmed = (base ?? "").trim();
  if (!trimmed) return "";
  try {
    return new URL(trimmed).host;
  } catch {
    return trimmed;
  }
}

function presetLabel(presets: LlmPreset[], id: string): string {
  return presets.find((p) => p.id === id)?.label ?? id;
}

export interface ProfileListProps {
  profiles: LlmProfile[];
  presets: LlmPreset[];
  /** True on a loopback bind; read-only viewers see rows without actions. */
  editable: boolean;
  /** The profile id a delete confirmation is open for, or null. */
  pendingDeleteId: string | null;
  busy: boolean;
  onNew: () => void;
  onActivate: (id: string) => void;
  onEdit: (id: string) => void;
  onRequestDelete: (id: string) => void;
  onConfirmDelete: (id: string) => void;
  onCancelDelete: () => void;
}

export function ProfileList(props: ProfileListProps) {
  const {
    profiles,
    presets,
    editable,
    pendingDeleteId,
    busy,
    onNew,
    onActivate,
    onEdit,
    onRequestDelete,
    onConfirmDelete,
    onCancelDelete,
  } = props;

  return (
    <div className="space-y-4" data-testid="profile-list">
      <div className="flex items-center justify-between gap-3">
        <h2 className="text-lg font-semibold text-white">Profiles</h2>
        {editable && (
          <button
            type="button"
            data-testid="profile-new"
            onClick={onNew}
            disabled={busy}
            className="inline-flex items-center gap-1.5 rounded-lg bg-white px-3 py-1.5 text-sm font-semibold text-black transition-colors hover:bg-neutral-200 disabled:cursor-not-allowed disabled:opacity-60"
          >
            <Plus className="h-4 w-4" aria-hidden="true" />
            New profile
          </button>
        )}
      </div>

      <ul className="space-y-2">
        {profiles.map((profile) => (
          <li key={profile.id} data-testid={`profile-row-${profile.id}`}>
            <div
              className={cn(
                "rounded-lg border px-4 py-3",
                profile.active
                  ? "border-emerald-500/40 bg-emerald-500/[0.04]"
                  : "border-[#222] bg-[rgba(255,255,255,0.02)]"
              )}
            >
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-sm font-medium text-white">{profile.name}</span>
                    <span className="inline-flex items-center rounded-full border border-white/10 bg-[rgba(255,255,255,0.05)] px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide text-[#999]">
                      {presetLabel(presets, profile.preset)}
                    </span>
                    {profile.active && (
                      <span
                        data-testid={`profile-active-${profile.id}`}
                        className="inline-flex items-center gap-1 rounded-full border border-emerald-500/40 bg-emerald-500/10 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-emerald-300"
                      >
                        <Check className="h-3 w-3" aria-hidden="true" />
                        Active
                      </span>
                    )}
                  </div>
                  <dl className="mt-1 flex flex-wrap gap-x-4 gap-y-0.5 text-xs text-[#888]">
                    <div className="flex gap-1">
                      <dt className="text-[#666]">Model:</dt>
                      <dd className="font-mono text-[#aaa]">{profile.model || "—"}</dd>
                    </div>
                    <div className="flex gap-1">
                      <dt className="text-[#666]">Base:</dt>
                      <dd className="font-mono text-[#aaa]">{hostOf(profile.api_base) || "provider default"}</dd>
                    </div>
                    <div className="flex gap-1">
                      <dt className="text-[#666]">Key:</dt>
                      <dd className={profile.api_key_set ? "text-emerald-300" : "text-amber-300"}>
                        {profile.api_key_set ? "set" : "not set"}
                      </dd>
                    </div>
                  </dl>
                </div>

                {editable && (
                  <div className="flex flex-shrink-0 items-center gap-1.5">
                    {!profile.active && (
                      <button
                        type="button"
                        data-testid={`profile-activate-${profile.id}`}
                        onClick={() => onActivate(profile.id)}
                        disabled={busy}
                        className="rounded-md border border-[#333] px-2.5 py-1 text-xs font-medium text-[#ddd] transition-colors hover:border-[#555] hover:text-white disabled:cursor-not-allowed disabled:opacity-60"
                      >
                        Activate
                      </button>
                    )}
                    <button
                      type="button"
                      data-testid={`profile-edit-${profile.id}`}
                      onClick={() => onEdit(profile.id)}
                      disabled={busy}
                      aria-label={`Edit ${profile.name}`}
                      className="inline-flex items-center gap-1 rounded-md border border-[#333] px-2.5 py-1 text-xs text-[#ddd] transition-colors hover:border-[#555] hover:text-white disabled:cursor-not-allowed disabled:opacity-60"
                    >
                      <Pencil className="h-3.5 w-3.5" aria-hidden="true" />
                      Edit
                    </button>
                    <button
                      type="button"
                      data-testid={`profile-delete-${profile.id}`}
                      onClick={() => onRequestDelete(profile.id)}
                      disabled={busy || profile.active}
                      aria-label={
                        profile.active
                          ? `Delete ${profile.name} (switch to another profile first)`
                          : `Delete ${profile.name}`
                      }
                      title={profile.active ? "Switch to another profile first" : undefined}
                      className="inline-flex items-center gap-1 rounded-md border border-[#333] px-2.5 py-1 text-xs text-[#ddd] transition-colors hover:border-red-500/50 hover:text-red-300 disabled:cursor-not-allowed disabled:opacity-40"
                    >
                      <Trash2 className="h-3.5 w-3.5" aria-hidden="true" />
                      Delete
                    </button>
                  </div>
                )}
              </div>

              {pendingDeleteId === profile.id && (
                <div
                  role="dialog"
                  aria-modal="false"
                  aria-label={`Delete profile ${profile.name}`}
                  data-testid={`profile-delete-confirm-${profile.id}`}
                  className="mt-3 rounded-lg border border-amber-500/30 bg-amber-500/5 px-4 py-3"
                >
                  <div className="flex items-start gap-3">
                    <AlertTriangle className="mt-0.5 h-5 w-5 flex-shrink-0 text-amber-400" aria-hidden="true" />
                    <p className="flex-1 text-sm text-amber-100">
                      Delete the profile <span className="font-medium">{profile.name}</span>? This
                      cannot be undone.
                    </p>
                  </div>
                  <div className="mt-3 flex items-center gap-2">
                    <button
                      type="button"
                      data-testid={`profile-delete-confirm-button-${profile.id}`}
                      onClick={() => onConfirmDelete(profile.id)}
                      disabled={busy}
                      className="rounded-lg bg-white px-3 py-1.5 text-xs font-semibold text-black transition-colors hover:bg-neutral-200 disabled:cursor-not-allowed disabled:opacity-60"
                    >
                      {busy ? "Deleting…" : "Delete"}
                    </button>
                    <button
                      type="button"
                      data-testid={`profile-delete-cancel-button-${profile.id}`}
                      onClick={onCancelDelete}
                      disabled={busy}
                      className="rounded-lg border border-[#333] px-3 py-1.5 text-xs text-[#aaa] transition-colors hover:border-[#555] hover:text-white disabled:opacity-60"
                    >
                      Cancel
                    </button>
                  </div>
                </div>
              )}
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}

export default ProfileList;
