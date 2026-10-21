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

function isPlaceholder(s) {
  return (
    !s ||
    s.includes("PLACEHOLDER") ||
    s.includes("__LOAD_FROM__") ||
    s.includes("LOAD_FROM")
  );
}

function loadB64(name) {
  const full = join(assetsDir, name);
  if (existsSync(full)) {
    const b64 = readFileSync(full, "utf8").trim();
    if (!isPlaceholder(b64) && b64.startsWith("UklGR") && b64.length >= 1000) {
      return b64;
    }
  }
  // Join chunked parts: name.part00, name.part01, ... (skip stub/placeholder parts)
  // Middle parts are raw base64 fragments — they do NOT start with UklGR.
  const parts = readdirSync(assetsDir)
    .filter((f) => f.startsWith(name + ".part"))
    .sort();
  if (!parts.length) return null;
  const chunks = [];
  for (const f of parts) {
    const chunk = readFileSync(join(assetsDir, f), "utf8").trim();
    if (isPlaceholder(chunk) || chunk.length < 100) {
      console.warn("materialize: skipping stub part", f, chunk.length);
      continue;
    }
    chunks.push(chunk);
  }
  if (!chunks.length) return null;
  const joined = chunks.join("");
  if (isPlaceholder(joined) || !joined.startsWith("UklGR") || joined.length < 1000) {
    return null;
  }
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
  const buf = Buffer.from(b64, "base64");
  if (buf.length < 1000 || buf.subarray(0, 4).toString("ascii") !== "RIFF") {
    console.warn("materialize: decoded not RIFF/too small", item.b64, buf.length);
    skipped++;
    continue;
  }
  // Reject truncated joins (must be within ~2% of expected bytes when known)
  if (item.bytes && Math.abs(buf.length - item.bytes) > Math.max(512, item.bytes * 0.02)) {
    console.warn(
      "materialize: size mismatch",
      item.path,
      "got",
      buf.length,
      "expected",
      item.bytes,
    );
    skipped++;
    continue;
  }
  const out = join(root, item.path);
  mkdirSync(dirname(out), { recursive: true });
  writeFileSync(out, buf);
  console.log("materialized", item.path, buf.length);
  ok++;
}
console.log(`materialize done: ${ok} ok, ${skipped} skipped`);
