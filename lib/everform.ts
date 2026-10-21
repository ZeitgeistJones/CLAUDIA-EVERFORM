// Shared types and helpers for the Everform website.
// The Python worker writes public/everform/manifest.json and report.json; this file
// describes those shapes and narrows untrusted JSON before the UI uses it.

export const MODES = ["canon", "archive"] as const;
export type Mode = (typeof MODES)[number];

export const MODE_LABELS: Record<Mode, string> = {
  canon: "Film Canon",
  archive: "Full Archive",
};

export const MODE_BLURBS: Record<Mode, string> = {
  canon: "Only finished film plates that feature her.",
  archive: "Every image of her in the archive, including tests and character sheets.",
};

export interface Counts {
  matching: number;
  fused: number;
  excluded: number;
  review: number;
  pending: number;
  errors: number;
}

export interface FilmEntry {
  id: string;
  title: string;
  released: string;
  matching: number;
  fused: number;
  share: number;
}

export type VersionKind = "live" | "reconstructed";

export interface VersionEntry {
  id: string;
  kind: VersionKind;
  label: string;
  createdAt: string;
  hash: string;
  fused: number;
  films: string[];
  filmTitles: string[];
  change: number | null;
  identity: number;
  image: string;
  png: string | null;
  raw: string;
  contact: string;
  details: string;
}

export interface ModeBlock {
  label: string;
  current: string;
  versions: VersionEntry[];
  counts: Counts;
  films: FilmEntry[];
  filmBalance: string;
  updatedAt: string;
  historyKey?: string;
}

export interface Manifest {
  schema: number;
  modes: Record<Mode, ModeBlock>;
  about: {
    title: string;
    credit: string;
    disclaimer: string;
    gallery: string;
  };
  generatedAt: string;
}

export type ReportStatus = "fused" | "excluded" | "review" | "pending" | "error";

export interface ReportRow {
  id: string;
  film: string;
  filmTitle: string;
  page: string;
  thumb: string | null;
  status: ReportStatus;
  reasons: string[];
  weight: number;
  duplicates: string[];
  override: boolean | null;
}

export function isMode(value: string | null): value is Mode {
  return value === "canon" || value === "archive";
}

export function parseManifest(raw: unknown): Manifest | null {
  if (!raw || typeof raw !== "object") return null;
  const m = raw as Manifest;
  if (m.schema !== 1 || !m.modes?.canon || !m.modes?.archive) return null;
  return m;
}

export function parseReport(raw: unknown): ReportRow[] | null {
  if (!raw || typeof raw !== "object") return null;
  const r = raw as { images?: unknown };
  if (!Array.isArray(r.images)) return null;
  const rows: ReportRow[] = [];
  for (const item of r.images) {
    if (!item || typeof item !== "object") continue;
    const o = item as Record<string, unknown>;
    if (typeof o.id !== "string" || typeof o.film !== "string") continue;
    rows.push({
      id: o.id,
      film: o.film,
      filmTitle: String(o.filmTitle ?? o.film),
      page: String(o.page ?? ""),
      thumb: typeof o.thumb === "string" ? o.thumb : null,
      status: (o.status as ReportStatus) ?? "error",
      reasons: Array.isArray(o.reasons) ? o.reasons.map(String) : [],
      weight: typeof o.weight === "number" ? o.weight : 0,
      duplicates: Array.isArray(o.duplicates) ? o.duplicates.map(String) : [],
      override: typeof o.override === "boolean" ? o.override : null,
    });
  }
  return rows;
}
