"use client";

// Contribution report browser. Loads /everform/{mode}/report.json and lets you filter
// by outcome and film. Thumbnails are loaded lazily from claudia.gallery itself.

import { ArrowLeft, ArrowUpRight } from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useEffect, useMemo, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { MODES, MODE_LABELS, isMode, parseReport, type Mode, type ReportRow, type ReportStatus } from "@/lib/everform";
import { cn } from "@/lib/utils";

type Filter = "all" | ReportStatus;
type Load = { state: "loading" } | { state: "error"; message: string } | { state: "ready"; rows: ReportRow[] };

const PAGE = 120;

const STATUS_COPY: Record<ReportStatus, string> = {
  fused: "Fused",
  excluded: "Left out",
  review: "Review",
  pending: "Pending",
  error: "Error",
};

const STATUS_CLASS: Record<ReportStatus, string> = {
  fused: "border-clay/40 bg-clay/15 text-clay",
  excluded: "border-border bg-secondary text-muted-foreground",
  review: "border-amber-300/30 bg-amber-300/10 text-amber-200",
  pending: "border-border bg-transparent text-muted-foreground",
  error: "border-destructive/40 bg-destructive/10 text-destructive",
};

export function SourcesView() {
  const params = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();
  const modeParam = params.get("mode");
  const mode: Mode = isMode(modeParam) ? modeParam : "canon";
  const [loaded, setLoaded] = useState<{ mode: Mode; load: Load } | null>(null);
  const load: Load = loaded && loaded.mode === mode ? loaded.load : { state: "loading" };
  const [filter, setFilter] = useState<Filter>("all");
  const [filmChoice, setFilm] = useState<string>("all");
  const [limit, setLimit] = useState(PAGE);

  useEffect(() => {
    const ac = new AbortController();
    fetch(`/everform/${mode}/report.json?t=${Date.now()}`, { cache: "no-store", signal: ac.signal })
      .then(async (res) => {
        if (res.status === 404) throw new Error("This source hasn't been processed yet.");
        if (!res.ok) throw new Error(`The report couldn't be loaded (HTTP ${res.status}).`);
        const rows = parseReport(await res.json());
        if (!rows) throw new Error("The report file is malformed.");
        setLoaded({ mode, load: { state: "ready", rows } });
      })
      .catch((err: unknown) => {
        if (err instanceof DOMException && err.name === "AbortError") return;
        console.error("[sources] report load failed", err);
        setLoaded({
          mode,
          load: { state: "error", message: err instanceof Error ? err.message : "Unknown error" },
        });
      });
    return () => ac.abort();
  }, [mode]);

  const rows = useMemo(
    () => (loaded && loaded.mode === mode && loaded.load.state === "ready" ? loaded.load.rows : []),
    [loaded, mode],
  );
  const films = useMemo(() => {
    const seen = new Map<string, string>();
    for (const r of rows) if (!seen.has(r.film)) seen.set(r.film, r.filmTitle);
    return [...seen.entries()];
  }, [rows]);
  const film = films.some(([id]) => id === filmChoice) ? filmChoice : "all";
  const tally = useMemo(() => {
    const t: Record<Filter, number> = { all: 0, fused: 0, excluded: 0, review: 0, pending: 0, error: 0 };
    for (const r of rows) if (film === "all" || r.film === film) {
      t.all++;
      t[r.status]++;
    }
    return t;
  }, [rows, film]);
  const visible = useMemo(() => {
    const order: Record<ReportStatus, number> = { fused: 0, review: 1, excluded: 2, pending: 3, error: 4 };
    return rows
      .filter((r) => (filter === "all" || r.status === filter) && (film === "all" || r.film === film))
      .sort((a, b) => order[a.status] - order[b.status] || b.weight - a.weight || a.id.localeCompare(b.id));
  }, [rows, filter, film]);
  const totalWeight = useMemo(() => rows.reduce((s, r) => s + r.weight, 0) || 1, [rows]);

  function switchMode(m: Mode) {
    setFilter("all");
    setFilm("all");
    setLimit(PAGE);
    router.replace(`${pathname}?mode=${m}`, { scroll: false });
  }

  return (
    <div className="min-h-dvh bg-background px-5 py-6 text-foreground sm:px-8 sm:py-8">
      <header className="mx-auto flex max-w-6xl flex-col gap-5">
        <Button asChild variant="ghost" size="sm" className="w-fit -translate-x-2 text-muted-foreground">
          <Link href="/">
            <ArrowLeft />
            Back to the portrait
          </Link>
        </Button>
        <div>
          <h1 className="text-xs font-medium tracking-[0.42em]">CLAUDIA EVERFORM · SOURCES</h1>
          <p className="mt-2 max-w-2xl font-serif text-xl italic leading-snug text-muted-foreground">
            Every image of her that matches this source, and whether it shaped the face. Nothing is hidden: images
            left out stay listed with the reason.
          </p>
        </div>
        <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
          <ToggleGroup type="single" variant="outline" size="sm" value={mode} onValueChange={(v) => isMode(v) && switchMode(v)}>
            {MODES.map((m) => (
              <ToggleGroupItem key={m} value={m} className="px-3 data-[state=on]:bg-secondary">
                {MODE_LABELS[m]}
              </ToggleGroupItem>
            ))}
          </ToggleGroup>
          <ToggleGroup
            type="single"
            size="sm"
            value={filter}
            onValueChange={(v) => v && setFilter(v as Filter)}
            className="flex-wrap"
          >
            {(["all", "fused", "excluded", "review", "pending", "error"] as Filter[])
              .filter((f) => f === "all" || f === "fused" || tally[f] > 0)
              .map((f) => (
                <ToggleGroupItem key={f} value={f} className="px-2.5 data-[state=on]:bg-secondary">
                  {f === "all" ? "All" : STATUS_COPY[f]}
                  <span className="ml-1 font-mono text-[0.7rem] text-muted-foreground">{tally[f]}</span>
                </ToggleGroupItem>
              ))}
          </ToggleGroup>
        </div>
        {films.length > 1 && (
          <div className="flex flex-wrap gap-1.5">
            {[["all", "All films"] as [string, string], ...films].map(([id, title]) => (
              <button
                key={id}
                type="button"
                onClick={() => setFilm(id)}
                className={cn(
                  "rounded-full border px-3 py-1 text-xs transition-colors",
                  film === id ? "border-foreground/40 text-foreground" : "border-border text-muted-foreground hover:text-foreground",
                )}
              >
                {title}
              </button>
            ))}
          </div>
        )}
      </header>

      <main className="mx-auto mt-6 max-w-6xl">
        {load.state === "loading" && (
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
            {Array.from({ length: 10 }, (_, i) => (
              <Skeleton key={i} className="aspect-[4/5] rounded-lg" />
            ))}
          </div>
        )}
        {load.state === "error" && (
          <div className="rounded-lg border border-border p-8 text-center">
            <p className="font-serif text-xl italic">{load.message}</p>
            <p className="mt-2 text-sm text-muted-foreground">Try the other source, or come back after the next sync.</p>
          </div>
        )}
        {load.state === "ready" && visible.length === 0 && (
          <p className="rounded-lg border border-border p-8 text-center text-muted-foreground">No images match these filters.</p>
        )}
        {load.state === "ready" && visible.length > 0 && (
          <>
            <ul className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
              {visible.slice(0, limit).map((r) => (
                <SourceCard key={r.id} row={r} share={r.weight / totalWeight} />
              ))}
            </ul>
            {visible.length > limit && (
              <div className="mt-6 flex justify-center">
                <Button variant="outline" onClick={() => setLimit((l) => l + PAGE)}>
                  Show more ({visible.length - limit} left)
                </Button>
              </div>
            )}
          </>
        )}
      </main>
    </div>
  );
}

function SourceCard({ row, share }: { row: ReportRow; share: number }) {
  return (
    <li className="group overflow-hidden rounded-lg border border-border bg-card">
      <a href={row.page} target="_blank" rel="noreferrer" className="block">
        <div className="relative aspect-[4/5] overflow-hidden bg-secondary">
          {row.thumb && (
            // Remote thumbnails straight from claudia.gallery; no need to re-encode them through our server.
            // eslint-disable-next-line @next/next/no-img-element
            <img
              src={row.thumb}
              alt={`${row.filmTitle} — ${row.id}`}
              loading="lazy"
              decoding="async"
              className={cn(
                "size-full object-cover transition-transform duration-500 group-hover:scale-[1.03]",
                row.status !== "fused" && "opacity-60 grayscale-[60%]",
              )}
            />
          )}
          <Badge variant="outline" className={cn("absolute left-2 top-2 backdrop-blur", STATUS_CLASS[row.status])}>
            {STATUS_COPY[row.status]}
            {row.override && " · manual"}
          </Badge>
          <ArrowUpRight className="absolute right-2 top-2 size-3.5 text-white/0 transition-colors group-hover:text-white/80" />
        </div>
      </a>
      <div className="space-y-1 p-2.5 text-xs">
        <p className="truncate text-foreground/90">{row.filmTitle}</p>
        {row.status === "fused" ? (
          <p className="font-mono text-[0.7rem] text-muted-foreground">
            {(share * 100).toFixed(share >= 0.01 ? 1 : 2)}% of the face
            {row.duplicates.length > 0 && ` · shares a vote with ${row.duplicates.length}`}
          </p>
        ) : (
          <p className="line-clamp-2 text-muted-foreground">{row.reasons.join(", ") || "No reason recorded"}</p>
        )}
      </div>
    </li>
  );
}
