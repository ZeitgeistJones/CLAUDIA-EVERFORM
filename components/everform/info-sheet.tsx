"use client";

// The discreet side panel: source mode, honest status and counts, per-film balance,
// downloads, the raw-fusion view, how the portrait is made, and credits.

import { ArrowUpRight, Download } from "lucide-react";
import Link from "next/link";

import type { StatusState } from "@/components/everform/everform";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { Switch } from "@/components/ui/switch";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import {
  MODES,
  MODE_BLURBS,
  MODE_LABELS,
  formatDate,
  formatRelative,
  isMode,
  versionLabel,
  type Manifest,
  type Mode,
  type Version,
} from "@/lib/everform";

interface InfoSheetProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  manifest: Manifest | null;
  mode: Mode;
  onModeChange: (m: Mode) => void;
  raw: boolean;
  onRawChange: (raw: boolean) => void;
  shown: Version | null;
  current: Version | null;
  status: StatusState;
  now: number;
}

const BALANCE_TEXT: Record<string, string> = {
  sqrt: "Each film's pull grows with the square root of how much usable footage it has, so a film with many close-ups counts for more, but no single film can take over the face.",
  film: "Every film pulls equally on the face, however many images it has.",
  image: "Every qualifying image counts on its own, so films with more footage pull harder.",
};

function plural(n: number, one: string, many = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`;
}

export function InfoSheet(p: InfoSheetProps) {
  const entry = p.manifest?.modes[p.mode];
  const counts = entry?.counts ?? null;
  const films = entry?.films ?? [];
  const maxShare = Math.max(...films.map((f) => f.share), 0.0001);
  const v = p.shown;
  const isCurrent = Boolean(v && p.current && v.id === p.current.id);
  const downloadHref = v ? (v.png ?? v.image) : null;
  const downloadExt = v?.png ? "png" : "webp";
  const rejectedAfterCurrent =
    entry?.lastRejected && (!p.current || entry.lastRejected.at > p.current.createdAt) ? entry.lastRejected : null;

  return (
    <Sheet open={p.open} onOpenChange={p.onOpenChange}>
      <SheetContent side="right" className="w-full gap-0 overflow-y-auto border-border/70 bg-popover/95 backdrop-blur-xl sm:max-w-md">
        <SheetHeader className="px-6 pt-6">
          <SheetTitle className="text-xs font-medium tracking-[0.36em]">CLAUDIA EVERFORM</SheetTitle>
          <SheetDescription className="font-serif text-lg italic leading-snug">
            One face, fused from her films. It changes when the archive does.
          </SheetDescription>
        </SheetHeader>

        <div className="space-y-7 px-6 pb-8 pt-5 text-sm leading-relaxed">
          <section className="space-y-3">
            <Label>Source</Label>
            <ToggleGroup
              type="single"
              variant="outline"
              value={p.mode}
              onValueChange={(val) => isMode(val) && p.onModeChange(val)}
              className="w-full"
            >
              {MODES.map((m) => (
                <ToggleGroupItem key={m} value={m} className="flex-1 data-[state=on]:bg-secondary">
                  {MODE_LABELS[m]}
                </ToggleGroupItem>
              ))}
            </ToggleGroup>
            <p className="text-muted-foreground">{MODE_BLURBS[p.mode]}</p>
          </section>

          <section className="space-y-3">
            <Label>Status</Label>
            <StatusLine status={p.status} mode={p.mode} now={p.now} />
            {p.current && (
              <p className="text-muted-foreground">
                Current portrait published {formatRelative(p.current.createdAt, p.now)}.
              </p>
            )}
            {rejectedAfterCurrent && (
              <p className="text-muted-foreground">
                The last attempted update was held back ({rejectedAfterCurrent.reason}), so the previous portrait stays up.
              </p>
            )}
          </section>

          {counts && (
            <section className="space-y-3">
              <Label>Images</Label>
              <dl className="grid grid-cols-2 gap-px overflow-hidden rounded-lg border border-border bg-border">
                <Stat label="Match this source" value={counts.matching} />
                <Stat label="Fused into the face" value={counts.fused} accent />
                <Stat label="Left out" value={counts.excluded} />
                <Stat label="Unsure, held for review" value={counts.review} />
                {counts.pending > 0 && <Stat label="Not processed yet" value={counts.pending} />}
                {counts.errors > 0 && <Stat label="Failed to process" value={counts.errors} />}
              </dl>
              <p className="text-muted-foreground">
                Not every image of her can shape a face. Side views, tiny or blurred faces, sunglasses, closed eyes,
                stylised versions (masks, holograms, avatars), crowded frames and near-duplicates are left out, and
                anything where it&apos;s unclear the face is hers goes to review instead.
              </p>
              <Link
                href={`/sources?mode=${p.mode}`}
                className="inline-flex items-center gap-1 text-foreground underline-offset-4 hover:underline"
              >
                See every image and why it was used or left out
                <ArrowUpRight className="size-3.5" />
              </Link>
            </section>
          )}

          {films.length > 0 && (
            <section className="space-y-3">
              <Label>Films in the face</Label>
              <ul className="space-y-2.5">
                {films.map((f) => (
                  <li key={f.id} className="space-y-1">
                    <div className="flex items-baseline justify-between gap-3">
                      <span className="truncate">
                        {f.title}
                        {f.released && <span className="text-muted-foreground"> · {f.released}</span>}
                      </span>
                      <span className="shrink-0 font-mono text-xs text-muted-foreground">
                        {f.fused}/{f.matching} · {Math.round(f.share * 100)}%
                      </span>
                    </div>
                    <div className="h-px w-full bg-border">
                      <div className="h-px bg-clay" style={{ width: `${(f.share / maxShare) * 100}%` }} />
                    </div>
                  </li>
                ))}
              </ul>
              <p className="text-muted-foreground">
                {BALANCE_TEXT[entry?.filmBalance ?? "sqrt"] ?? BALANCE_TEXT.sqrt} Near-identical frames share one vote.
              </p>
            </section>
          )}

          {v && (
            <section className="space-y-3">
              <Label>This portrait</Label>
              <p className="text-muted-foreground">
                {isCurrent ? "The live portrait" : versionLabel(v, p.current?.id ?? null)}
                {v.kind === "reconstructed" && " (reconstructed from the films released up to then)"} ·{" "}
                {plural(v.fused, "image")} · {plural(v.films.length, "film")}
                {v.kind === "live" && ` · ${formatDate(v.createdAt)}`}
              </p>
              <label className="flex cursor-pointer items-center justify-between gap-4 rounded-lg border border-border px-3 py-2.5">
                <span>
                  <span className="block">Show the raw fusion</span>
                  <span className="block text-xs text-muted-foreground">The plain weighted average, before refinement.</span>
                </span>
                <Switch checked={p.raw} onCheckedChange={p.onRawChange} />
              </label>
              <div className="flex flex-wrap gap-2">
                {downloadHref && (
                  <Button asChild variant="outline" size="sm">
                    <a href={downloadHref} download={`claudia-everform-${p.mode}-${v.id}.${downloadExt}`}>
                      <Download />
                      Download {downloadExt.toUpperCase()}
                    </a>
                  </Button>
                )}
                {v.contact && (
                  <Button asChild variant="ghost" size="sm">
                    <a href={v.contact} target="_blank" rel="noreferrer">
                      Contributing faces
                      <ArrowUpRight />
                    </a>
                  </Button>
                )}
                {v.details && (
                  <Button asChild variant="ghost" size="sm">
                    <a href={v.details} target="_blank" rel="noreferrer">
                      Version data
                      <ArrowUpRight />
                    </a>
                  </Button>
                )}
              </div>
              {!v.png && (
                <p className="text-xs text-muted-foreground">Full-resolution PNG is kept for the live portrait only.</p>
              )}
            </section>
          )}

          <Separator />

          <section className="space-y-3">
            <Label>How it&apos;s made</Label>
            <ol className="list-decimal space-y-2 pl-4 text-muted-foreground marker:text-muted-foreground/60">
              <li>
                <span className="text-foreground">Find.</span> Every image tagged as her on claudia.gallery. Film Canon
                keeps only finished film plates.
              </li>
              <li>
                <span className="text-foreground">Check.</span> Find the face, confirm it&apos;s her against the
                official reference faces, and measure pose, sharpness and anything covering it.
              </li>
              <li>
                <span className="text-foreground">Align.</span> 478 facial landmarks bend each face onto one shared
                face shape, so eyes, nose and mouth line up exactly.
              </li>
              <li>
                <span className="text-foreground">Fuse.</span> A weighted average, balanced across films, then a robust
                pass that restores only the detail most of her faces agree on. Nothing is generated or invented.
              </li>
              <li>
                <span className="text-foreground">Evolve.</span> New images are folded in as they appear; the face
                crossfades when it changes. The timeline below the portrait lets you scrub back through it.
              </li>
            </ol>
          </section>

          <section className="space-y-1.5 text-xs text-muted-foreground">
            <p>
              {p.manifest?.about.credit ?? "Claudia by anabology (open character)."}{" "}
              <a
                href={p.manifest?.about.gallery ?? "https://claudia.gallery"}
                target="_blank"
                rel="noreferrer"
                className="text-foreground underline-offset-4 hover:underline"
              >
                claudia.gallery
              </a>
            </p>
            <p>{p.manifest?.about.disclaimer ?? "Unofficial fan artwork. Not affiliated with Anthropic."}</p>
          </section>
        </div>
      </SheetContent>
    </Sheet>
  );
}

function Label({ children }: { children: React.ReactNode }) {
  return <h3 className="font-mono text-[0.68rem] uppercase tracking-[0.18em] text-muted-foreground">{children}</h3>;
}

function Stat({ label, value, accent }: { label: string; value: number; accent?: boolean }) {
  return (
    <div className="bg-popover px-3 py-2.5">
      <dd className={accent ? "font-mono text-lg text-clay" : "font-mono text-lg"}>{value.toLocaleString("en-US")}</dd>
      <dt className="text-xs text-muted-foreground">{label}</dt>
    </div>
  );
}

function StatusLine({ status, mode, now }: { status: StatusState; mode: Mode; now: number }) {
  if (status.state === "loading") {
    return <p className="text-muted-foreground">Checking claudia.gallery for new images…</p>;
  }
  if (status.state === "error") {
    return (
      <p className="text-muted-foreground">
        Couldn&apos;t reach claudia.gallery just now. The portrait shown is the last good one.
      </p>
    );
  }
  const m = status.data.modes[mode];
  const checked = formatRelative(status.data.checkedAt, now);
  if (!m) return <p className="text-muted-foreground">No status for this source yet.</p>;
  if (m.waiting > 0 || m.removed > 0) {
    const parts = [];
    if (m.waiting > 0) parts.push(`${plural(m.waiting, "new image")} on claudia.gallery`);
    if (m.removed > 0) parts.push(`${plural(m.removed, "image")} removed from it`);
    return (
      <p>
        <Dot className="bg-clay" />
        {parts.join(" and ")} since the last sync. The portrait will update on the next one.{" "}
        <span className="text-muted-foreground">Checked {checked}.</span>
      </p>
    );
  }
  return (
    <p>
      <Dot className="bg-emerald-400/80" />
      Up to date with all {m.inGallery.toLocaleString("en-US")} matching images on claudia.gallery.{" "}
      <span className="text-muted-foreground">Checked {checked}.</span>
    </p>
  );
}

function Dot({ className }: { className: string }) {
  return <span aria-hidden className={`mr-2 inline-block size-1.5 -translate-y-px rounded-full ${className}`} />;
}
