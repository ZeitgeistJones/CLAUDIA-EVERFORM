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
  released: string | null;
  matching: number;
  fused: number;
  share: number;
}

export interface Version {
  id: string;
  kind: "live" | "reconstructed";
  label: string;
  createdAt: string;
  fused: number;
  films: string[];
  filmTitles: string[];
  image: string;
  png: string | null;
  raw: string;
  contact: string;
  details: string;
}

export interface ModeEntry {
  label: string;
  current: string | null;
  versions: Version[];
  counts: Counts | null;
  films: FilmEntry[];
  filmBalance: string | null;
  updatedAt: string | null;
  lastRejected: { at: string; reason: string } | null;
}

export interface About {
  title: string;
  credit: string;
  disclaimer: string;
  gallery: string;
}

export interface Manifest {
  modes: Partial<Record<Mode, ModeEntry>>;
  about: About;
}

export type ReportStatus = "fused" | "excluded" | "review" | "pending" | "error";

export interface ReportRow {
  id: string;
  film: string;
  filmTitle: string;
  page: string;
  thumb: string;
  framing: string | null;
  status: ReportStatus;
  reasons: string[];
  quality: number | null;
  identity: number | null;
  weight: number;
  duplicates: string[];
  override: string | null;
}

export interface GalleryStatus {
  ok: boolean;
  checkedAt: string;
  modes: Partial<Record<Mode, { inGallery: number; waiting: number; removed: number }>>;
  error?: string;
}

// ---------------------------------------------------------------- narrowing

type Obj = Record<string, unknown>;

function isObj(v: unknown): v is Obj {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}
function str(v: unknown, fallback = ""): string {
  return typeof v === "string" ? v : fallback;
}
function strOrNull(v: unknown): string | null {
  return typeof v === "string" && v.length > 0 ? v : null;
}
function num(v: unknown, fallback = 0): number {
  return typeof v === "number" && Number.isFinite(v) ? v : fallback;
}
function numOrNull(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}
function strList(v: unknown): string[] {
  return Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : [];
}

export function isMode(v: unknown): v is Mode {
  return typeof v === "string" && (MODES as readonly string[]).includes(v);
}

function parseVersion(v: unknown): Version | null {
  if (!isObj(v)) return null;
  const id = str(v.id);
  const image = str(v.image);
  if (!id || !image.startsWith("/everform/")) return null;
  return {
    id,
    kind: v.kind === "reconstructed" ? "reconstructed" : "live",
    label: str(v.label, "Portrait"),
    createdAt: str(v.createdAt),
    fused: num(v.fused),
    films: strList(v.films),
    filmTitles: strList(v.filmTitles),
    image,
    png: strOrNull(v.png),
    raw: str(v.raw, image),
    contact: str(v.contact),
    details: str(v.details),
  };
}

function parseCounts(v: unknown): Counts | null {
  if (!isObj(v)) return null;
  return {
    matching: num(v.matching),
    fused: num(v.fused),
    excluded: num(v.excluded),
    review: num(v.review),
    pending: num(v.pending),
    errors: num(v.errors),
  };
}

function parseFilm(v: unknown): FilmEntry | null {
  if (!isObj(v) || !str(v.id)) return null;
  return {
    id: str(v.id),
    title: str(v.title, str(v.id)),
    released: strOrNull(v.released),
    matching: num(v.matching),
    fused: num(v.fused),
    share: num(v.share),
  };
}

function parseModeEntry(mode: Mode, v: unknown): ModeEntry | null {
  if (!isObj(v)) return null;
  const versions = (Array.isArray(v.versions) ? v.versions : [])
    .map(parseVersion)
    .filter((x): x is Version => x !== null);
  const rej = isObj(v.lastRejected) ? v.lastRejected : null;
  const gate = rej && isObj(rej.gate) ? rej.gate : null;
  return {
    label: str(v.label, MODE_LABELS[mode]),
    current: strOrNull(v.current),
    versions,
    counts: parseCounts(v.counts),
    films: (Array.isArray(v.films) ? v.films : []).map(parseFilm).filter((x): x is FilmEntry => x !== null),
    filmBalance: strOrNull(v.filmBalance),
    updatedAt: strOrNull(v.updatedAt),
    lastRejected: rej ? { at: str(rej.at), reason: str(gate?.reason, "failed the quality check") } : null,
  };
}

const DEFAULT_ABOUT: About = {
  title: "CLAUDIA EVERFORM",
  credit: "Claudia by anabology (open character). Source images: claudia.gallery.",
  disclaimer: "Unofficial fan artwork. Not affiliated with Anthropic or claudia.gallery.",
  gallery: "https://claudia.gallery",
};

export function parseManifest(raw: unknown): Manifest | null {
  if (!isObj(raw) || !isObj(raw.modes)) return null;
  const modes: Partial<Record<Mode, ModeEntry>> = {};
  for (const m of MODES) {
    const entry = parseModeEntry(m, raw.modes[m]);
    if (entry) modes[m] = entry;
  }
  const a = isObj(raw.about) ? raw.about : {};
  return {
    modes,
    about: {
      title: str(a.title, DEFAULT_ABOUT.title),
      credit: str(a.credit, DEFAULT_ABOUT.credit),
      disclaimer: str(a.disclaimer, DEFAULT_ABOUT.disclaimer),
      gallery: str(a.gallery, DEFAULT_ABOUT.gallery),
    },
  };
}

const STATUSES: ReportStatus[] = ["fused", "excluded", "review", "pending", "error"];

export function parseReport(raw: unknown): ReportRow[] | null {
  if (!isObj(raw) || !Array.isArray(raw.images)) return null;
  const rows: ReportRow[] = [];
  for (const r of raw.images) {
    if (!isObj(r) || !str(r.id)) continue;
    const status = STATUSES.includes(r.status as ReportStatus) ? (r.status as ReportStatus) : "pending";
    rows.push({
      id: str(r.id),
      film: str(r.film),
      filmTitle: str(r.filmTitle, str(r.film)),
      page: str(r.page),
      thumb: str(r.thumb),
      framing: strOrNull(r.framing),
      status,
      reasons: strList(r.reasons),
      quality: numOrNull(r.quality),
      identity: numOrNull(r.identity),
      weight: num(r.weight),
      duplicates: strList(r.duplicates),
      override: strOrNull(r.override),
    });
  }
  return rows;
}

// ---------------------------------------------------------------- display

export function currentVersion(entry: ModeEntry | undefined): Version | null {
  if (!entry) return null;
  return entry.versions.find((v) => v.id === entry.current) ?? null;
}

/** Reconstructed history first, then every published live state, oldest to newest. */
export function timeline(entry: ModeEntry | undefined): Version[] {
  if (!entry) return [];
  const recon = entry.versions.filter((v) => v.kind === "reconstructed");
  const live = entry.versions
    .filter((v) => v.kind === "live")
    .sort((a, b) => a.createdAt.localeCompare(b.createdAt));
  return [...recon, ...live];
}

export function versionLabel(v: Version, currentId: string | null): string {
  if (v.kind === "reconstructed") return v.label;
  if (v.id === currentId) return "Now";
  return `Published ${formatDate(v.createdAt)}`;
}

export function formatDate(iso: string | null): string {
  if (!iso) return "unknown";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "unknown";
  return d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });
}

export function formatRelative(iso: string | null, now: number): string {
  if (!iso) return "unknown";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "unknown";
  const s = Math.max(0, Math.round((now - t) / 1000));
  if (s < 60) return "just now";
  const m = Math.round(s / 60);
  if (m < 60) return `${m} min ago`;
  const h = Math.round(m / 60);
  if (h < 48) return `${h} h ago`;
  return formatDate(iso);
}

/** Same rules as the worker (worker/everform/gallery.py): flags must be literally true. */
export function inMode(item: Obj, mode: Mode): boolean {
  if (item.her !== true) return false;
  return mode === "archive" || item.plate === true;
}

export function extractItems(data: unknown): { items: Obj[]; next: string | null } {
  if (Array.isArray(data)) return { items: data.filter(isObj), next: null };
  if (!isObj(data)) return { items: [], next: null };
  for (const key of ["items", "images", "data", "results"]) {
    const v = data[key];
    if (Array.isArray(v)) {
      const links = isObj(data.links) ? data.links : {};
      const next = strOrNull(data.next) ?? strOrNull(data.next_page) ?? strOrNull(links.next);
      return { items: v.filter(isObj), next };
    }
  }
  return { items: [], next: null };
}
