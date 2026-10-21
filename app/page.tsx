// Home: the portrait room.

import type { Metadata } from "next";

import { Everform } from "@/components/everform/everform";
import { currentVersion } from "@/lib/everform";
import { loadManifest } from "@/lib/load-manifest";

const manifest = loadManifest();

export function generateMetadata(): Metadata {
  const live = currentVersion(manifest?.modes.canon);
  return live ? { openGraph: { images: [{ url: live.image, width: 1024, height: 1280 }] } } : {};
}

export default function Home() {
  return <Everform initial={manifest} />;
}
