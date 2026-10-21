"use client";

// The whole Everform room: one portrait, quiet metadata, a history scrubber and the
// info panel. Polls the published manifest so a new version crossfades in on its own.

import { Info } from "lucide-react";
import { useCallback, useEffect, useMemo, useState, useSyncExternalStore } from "react";

import { InfoSheet } from "@/components/everform/info-sheet";
import { Portrait } from "@/components/everform/portrait";
import { Button } from "@/components/ui/button";
import { Slider } from "@/components/ui/slider";
import {
  MODE_LABELS,
  currentVersion,
  formatDate,
  isMode,
  parseManifest,
  timeline,
  versionLabel,
  type GalleryStatus,
  type Manifest,
  type Mode,
} from "@/lib/everform";

const MANIFEST_POLL_MS = 60_000;
const STATUS_POLL_MS = 5 * 60_000;
const MODE_KEY = "everform-mode";

export type StatusState = { state: "loading" } | { state: "ready"; data: GalleryStatus } | { state: "error" };

function subscribeStorage(onChange: () => void): () => void {
  window.addEventListener("storage", onChange);
  return () => window.removeEventListener("storage", onChange);
}

function readSavedMode(): string | null {
  try {
    return window.localStorage.getItem(MODE_KEY);
  } catch {
    return null;
  }
}

function subscribeClock(onChange: () => void): () => void {
  const id = window.setInterval(onChange, 60_000);
  return () => window.clearInterval(id);
}

export function Everform({ initial }: { initial: Manifest | null }) {
  const [manifest, setManifest] = useState<Manifest | null>(initial);
  const savedMode = useSyncExternalStore(subscribeStorage, readSavedMode, () => null);
  const [chosenMode, setChosenMode] = useState<Mode | null>(null);
  const mode: Mode = chosenMode ?? (isMode(savedMode) ? savedMode : "canon");
  const [raw, setRaw] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [status, setStatus] = useState<StatusState>({ state: "loading" });
  const [panelOpen, setPanelOpen] = useState(false);
  // Tick once a minute so "N min ago" stays honest without prerendering Date.now().
  const now = useSyncExternalStore(subscribeClock, () => Date.now(), () => 0);

  const setMode = useCallback((m: Mode) => {
    setChosenMode(m);
    setSelectedId(null);
    try {
      window.localStorage.setItem(MODE_KEY, m);
    } catch (err) {
      console.error("[everform] could not remember the source mode", err);
    }
  }, []);

  useEffect(() => {
    let alive = true;
    async function poll() {
      try {
        const res = await fetch(`/everform/manifest.json?t=${Date.now()}`, { cache: "no-store" });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const next = parseManifest(await res.json());
        if (alive && next) setManifest(next);
      } catch (err) {
        console.error("[everform] manifest refresh failed; keeping the current portrait", err);
      }
    }
    const id = window.setInterval(poll, MANIFEST_POLL_MS);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
  }, []);

  useEffect(() => {
    let alive = true;
    async function check() {
      try {
        const res = await fetch("/api/status", { cache: "no-store" });
        const data = (await res.json()) as GalleryStatus;
        if (alive) setStatus(data && data.ok ? { state: "ready", data } : { state: "error" });
      } catch (err) {
        console.error("[everform] status check failed", err);
        if (alive) setStatus({ state: "error" });
      }
    }
    check();
    const id = window.setInterval(check, STATUS_POLL_MS);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
  }, []);

  const entry = manifest?.modes[mode];
  const current = currentVersion(entry);
  const versions = useMemo(() => timeline(entry), [entry]);
  const selected = versions.find((v) => v.id === selectedId) ?? null;
  const shown = selected ?? current;
  const src = shown ? (raw ? shown.raw : shown.image) : null;
  const index = shown ? versions.findIndex((v) => v.id === shown.id) : -1;
  const counts = entry?.counts ?? null;
  const filmCount = shown?.films.length ?? 0;
  const viewingPast = Boolean(selected && selected.id !== current?.id);

  const onScrub = useCallback(
    (value: number[]) => {
      const v = versions[value[0]];
      if (!v) return;
      setSelectedId(v.id === current?.id ? null : v.id);
    },
    [versions, current],
  );

  return (
    <div className="relative flex min-h-dvh flex-col overflow-hidden bg-background text-foreground">
      {src && (
        <div aria-hidden className="pointer-events-none absolute inset-0 opacity-30 blur-[90px] saturate-50">
          <Portrait src={src} alt="" className="absolute left-1/2 top-1/2 h-[120%] -translate-x-1/2 -translate-y-1/2" />
        </div>
      )}

      <header className="relative z-10 flex items-start justify-between gap-4 px-5 pt-5 sm:px-8 sm:pt-7">
        <div>
          <h1 className="text-[0.7rem] font-medium tracking-[0.42em] text-foreground/90 sm:text-xs">CLAUDIA EVERFORM</h1>
          <p className="mt-1.5 font-serif text-lg italic leading-snug text-muted-foreground sm:text-xl">
            A portrait that changes every time Claudia does.
          </p>
        </div>
        <Button
          variant="ghost"
          size="sm"
          onClick={() => setPanelOpen(true)}
          className="gap-1.5 text-muted-foreground hover:text-foreground"
          aria-label="About this portrait and settings"
        >
          <Info />
          <span className="hidden sm:inline">About</span>
        </Button>
      </header>

      <main className="relative z-10 flex flex-1 items-center justify-center px-4 py-4 sm:py-2">
        {shown ? (
          <Portrait
            src={src}
            alt={`Fused portrait of Claudia from ${shown.fused} film images (${MODE_LABELS[mode]}, ${versionLabel(shown, entry?.current ?? null)})`}
            className="portrait-feather w-[min(100%,calc((100dvh-17rem)*0.8))] lg:w-[min(100%,calc((100dvh-11rem)*0.8))]"
          />
        ) : (
          <EmptyState mode={mode} hasAny={Boolean(manifest && Object.keys(manifest.modes).length)} onSwitch={setMode} />
        )}
      </main>

      <footer className="relative z-10 flex flex-col gap-4 px-5 pb-5 sm:px-8 sm:pb-6 lg:flex-row lg:items-end lg:justify-between">
        <div className="space-y-1.5 font-mono text-[0.68rem] uppercase tracking-[0.14em] text-muted-foreground">
          {shown && counts ? (
            <p className="flex flex-wrap gap-x-3 gap-y-1">
              <span className="text-foreground/85">{shown.fused} images fused</span>
              <span>
                {filmCount} {filmCount === 1 ? "film" : "films"}
              </span>
              {viewingPast ? (
                <span className="text-clay">{versionLabel(shown, entry?.current ?? null)}</span>
              ) : (
                <span>updated {formatDate(entry?.updatedAt ?? null)}</span>
              )}
              <button
                type="button"
                onClick={() => setPanelOpen(true)}
                className="uppercase underline-offset-4 hover:foreground hover:underline"
              >
                {MODE_LABELS[mode]}
              </button>
              {raw && <span className="text-clay">raw fusion</span>}
            </p>
          ) : null}
          <p className="normal-case tracking-normal text-muted-foreground/70">
            Claudia by anabology · unofficial, not affiliated with Anthropic
          </p>
        </div>

        {versions.length > 1 && index >= 0 && (
          <div className="w-full max-w-sm space-y-2 lg:w-80">
            <div className="flex items-baseline justify-between gap-3 font-mono text-[0.68rem] uppercase tracking-[0.14em] text-muted-foreground">
              <span className="truncate">{versionLabel(shown!, entry?.current ?? null)}</span>
              {viewingPast ? (
                <button type="button" onClick={() => setSelectedId(null)} className="shrink-0 text-clay hover:underline">
                  back to now
                </button>
              ) : (
                <span className="shrink-0">evolution</span>
              )}
            </div>
            <Slider
              min={0}
              max={versions.length - 1}
              step={1}
              value={[index]}
              onValueChange={onScrub}
              aria-label="Scrub through the portrait's history"
            />
          </div>
        )}
      </footer>

      <InfoSheet
        open={panelOpen}
        onOpenChange={setPanelOpen}
        manifest={manifest}
        mode={mode}
        onModeChange={setMode}
        raw={raw}
        onRawChange={setRaw}
        shown={shown}
        current={current}
        status={status}
        now={now}
      />
    </div>
  );
}

function EmptyState({ mode, hasAny, onSwitch }: { mode: Mode; hasAny: boolean; onSwitch: (m: Mode) => void }) {
  const other: Mode = mode === "canon" ? "archive" : "canon";
  return (
    <div className="max-w-md text-center">
      <p className="font-serif text-2xl italic text-foreground/90">
        {hasAny ? `The ${MODE_LABELS[mode]} portrait hasn't been made yet.` : "No portrait has been published yet."}
      </p>
      <p className="mt-3 text-sm leading-relaxed text-muted-foreground">
        {hasAny
          ? "The worker hasn't finished processing this set of images. It will appear here automatically."
          : "The portrait appears here as soon as the worker finishes its first run over claudia.gallery."}
      </p>
      {hasAny && (
        <Button variant="outline" size="sm" className="mt-5" onClick={() => onSwitch(other)}>
          View {MODE_LABELS[other]}
        </Button>
      )}
    </div>
  );
}
