import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(fileURLToPath(new URL(".", import.meta.url)), "..");
const index = JSON.parse(
  readFileSync(join(root, "data/everform-assets/index.json"), "utf8"),
);
for (const item of index) {
  const b64 = readFileSync(join(root, "data/everform-assets", item.b64), "utf8").trim();
  const out = join(root, item.path);
  mkdirSync(dirname(out), { recursive: true });
  writeFileSync(out, Buffer.from(b64, "base64"));
  console.log("materialized", item.path, item.bytes);
}
