// GET /api/status — honest "is the portrait up to date?" check.
// Compares what claudia.gallery lists right now with the IDs the worker has
// already taken in (public/everform/known.json, written on every sync).
// The gallery lookup is cached for 10 minutes so visitors never hammer the source.

import { readFile } from "node:fs/promises";
import path from "node:path";

import { cacheLife } from "next/cache";
import { connection } from "next/server";

import { MODES, extractItems, inMode, type GalleryStatus, type Mode } from "@/lib/everform";

const GALLERY = (process.env.CLAUDIA_GALLERY_URL || "https://claudia.gallery").replace(/\/+$/, "");
const MAX_PAGES = 20;
const KNOWN_PATH = path.join(process.cwd(), "public", "everform", "known.json");

async function knownIds(mode: Mode): Promise<Set<string>> {
  try {
    const raw = JSON.parse(await readFile(KNOWN_PATH, "utf8")) as Record<string, unknown>;
    const list = raw[mode];
    return new Set(Array.isArray(list) ? list.filter((x): x is string => typeof x === "string") : []);
  } catch {
    return new Set();
  }
}

async function fetchGalleryIds(): Promise<Record<Mode, Set<string>>> {
  const out: Record<Mode, Set<string>> = { canon: new Set(), archive: new Set() };
  let url: string | null = `${GALLERY}/api/images.json`;
  const seen = new Set<string>();
  for (let page = 0; page < MAX_PAGES && url; page++) {
    if (seen.has(url)) break;
    seen.add(url);
    const res: Response = await fetch(url, {
      headers: { accept: "application/json", "user-agent": "claudia-everform/1 (+status check)" },
      signal: AbortSignal.timeout(15_000),
      cache: "no-store",
    });
    if (!res.ok) throw new Error(`images.json returned HTTP ${res.status}`);
    const { items, next } = extractItems(await res.json());
    for (const item of items) {
      if (typeof item.id !== "string") continue;
      for (const m of MODES) if (inMode(item, m)) out[m].add(item.id);
    }
    url = next ? new URL(next, GALLERY).toString() : null;
  }
  return out;
}

async function galleryStatus(): Promise<GalleryStatus> {
  "use cache";
  cacheLife({ stale: 60, revalidate: 600, expire: 3600 });
  const ids = await fetchGalleryIds();
  const modes: GalleryStatus["modes"] = {};
  for (const m of MODES) {
    const have = await knownIds(m);
    const now = ids[m];
    let waiting = 0;
    for (const id of now) if (!have.has(id)) waiting++;
    let removed = 0;
    for (const id of have) if (!now.has(id)) removed++;
    modes[m] = { inGallery: now.size, waiting, removed };
  }
  return { ok: true, checkedAt: new Date().toISOString(), modes };
}

export async function GET(): Promise<Response> {
  await connection();
  try {
    return Response.json(await galleryStatus());
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    console.error("[api/status] gallery check failed:", message);
    const body: GalleryStatus = { ok: false, checkedAt: new Date().toISOString(), modes: {}, error: message };
    return Response.json(body, { status: 503 });
  }
}
