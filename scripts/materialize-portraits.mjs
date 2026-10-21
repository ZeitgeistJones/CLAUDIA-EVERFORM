import { existsSync, readFileSync, writeFileSync, mkdirSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(fileURLToPath(new URL(".", import.meta.url)), "..");
const assetsDir = join(root, "data/everform-assets");
const indexPath = join(assetsDir, "index.json");
if (!existsSync(indexPath)) {
  console.warn("materialize: no index.json, skipping");
  process.exit(0);
}

function loadB64(name) {
  const full = join(assetsDir, name);
  if (existsSync(full)) {
    const b64 = readFileSync(full, "utf8").trim();
    if (b64.startsWith("UklGR") && b64.length >= 1000) return b64;
  }
  // Join chunked parts: name.part00, name.part01, ...
  const parts = readdirSync(assetsDir)
    .filter((f) => f.startsWith(name + ".part"))
    .sort();
  if (!parts.length) return null;
  const joined = parts.map((f) => readFileSync(join(assetsDir, f), "utf8").trim()).join("");
  if (!joined.startsWith("UklGR") || joined.length < 1000) return null;
  return joined;
}

const index = JSON.parse(readFileSync(indexPath, "utf8"));
let ok = 0;
let skipped = 0;
for (const item of index) {
  const b64 = loadB64(item.b64);
  if (!b64) {
    console.warn("materialize: missing/stub", item.b64);
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
