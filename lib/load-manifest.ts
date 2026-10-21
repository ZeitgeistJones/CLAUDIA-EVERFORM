// Bundled reader for the published manifest. Importing the JSON means every
// sync commit that redeploys the site ships a fresh initial state. The client
// still polls /everform/manifest.json for anything newer within a session.

import { parseManifest, type Manifest } from "@/lib/everform";
import raw from "@/public/everform/manifest.json";

export function loadManifest(): Manifest | null {
  return parseManifest(raw);
}
