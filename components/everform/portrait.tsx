"use client";

// Crossfading portrait. The outgoing image stays put; the incoming one fades in
// over it once the browser has decoded the pixels (works for cached images too).

import { useRef, useState } from "react";

import { cn } from "@/lib/utils";

interface PortraitProps {
  src: string | null;
  alt: string;
  className?: string;
}

const FADE_MS = 1400;

export function Portrait({ src, alt, className }: PortraitProps) {
  // Derive the crossfade pair from the prop during render so we don't need an effect
  // just to notice that `src` changed (and so React Strict Mode can't wipe the base).
  const [base, setBase] = useState<string | null>(src);
  const [incoming, setIncoming] = useState<string | null>(null);
  const fadeTimer = useRef<number | null>(null);

  if (src && src !== base && src !== incoming) {
    if (!base) {
      setBase(src);
    } else {
      setIncoming(src);
    }
  }

  function settle(next: string) {
    if (fadeTimer.current) window.clearTimeout(fadeTimer.current);
    fadeTimer.current = window.setTimeout(() => {
      setBase(next);
      setIncoming(null);
      fadeTimer.current = null;
    }, FADE_MS + 40);
  }

  return (
    <div className={cn("relative aspect-[4/5]", className)}>
      {!base && !incoming && (
        <div className="absolute inset-0 animate-pulse rounded-[50%] bg-white/[0.025] blur-2xl" aria-hidden />
      )}
      {base && (
        // Local versioned assets under /everform — no need to re-encode through next/image.
        // eslint-disable-next-line @next/next/no-img-element
        <img
          src={base}
          alt={incoming ? "" : alt}
          aria-hidden={incoming ? true : undefined}
          draggable={false}
          className="absolute inset-0 size-full select-none object-cover"
        />
      )}
      {incoming && (
        <Incoming
          key={incoming}
          src={incoming}
          alt={alt}
          onReady={() => settle(incoming)}
        />
      )}
    </div>
  );
}

function Incoming({ src, alt, onReady }: { src: string; alt: string; onReady: () => void }) {
  const [show, setShow] = useState(false);
  const started = useRef(false);

  function reveal() {
    if (started.current) return;
    started.current = true;
    setShow(true);
    onReady();
  }

  return (
    // eslint-disable-next-line @next/next/no-img-element
    <img
      src={src}
      alt={alt}
      draggable={false}
      onLoad={reveal}
      ref={(el) => {
        if (el?.complete && el.naturalWidth > 0) reveal();
      }}
      className="absolute inset-0 size-full select-none object-cover"
      style={{ opacity: show ? 1 : 0, transition: `opacity ${FADE_MS}ms ease-in-out` }}
    />
  );
}
