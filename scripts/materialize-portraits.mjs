import { existsSync, readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(fileURLToPath(new URL(".", import.meta.url)), "..");
const indexPath = join(root, "data/everform-assets/index.json");
if (!existsSync(indexPath)) {
  console.warn("materialize: no index.json, skipping");
  process.exit(0);
}
const index = JSON.parse(readFileSync(indexPath, "utf8"));
let ok = 0;
let skipped = 0;
for (const item of index) {
  const src = join(root, "data/everform-assets", item.b64);
  if (!existsSync(src)) {
    console.warn("materialize: missing", item.b64);
    skipped++;
    continue;
  }
  const b64 = readFileSync(src, "utf8").trim();
  if (!b64.startsWith("UklGR") || b64.length < 1000) {
    console.warn("materialize: invalid/stub", item.b64, b64.length);
    skipped++;
    continue;
  }
  const out = join(root, item.path);
  mkdirSync(dirname(out), { recursive: true });
  writeFileSync(out, Buffer.from(b64, "base64"));
  console.log("materialized", item.path, item.bytes);
  ok++;
}
console.log(`materialize done: ${ok} ok, ${skipped} skipped`);
