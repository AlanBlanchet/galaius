// Copy the catalog/benchmark data from the Python package (the source of truth at
// src/interact/data/) into the extension's src/ so resolveJsonModule can bundle it.
// Run by `npm run compile`. The copies in src/ are gitignored; refresh them with
// `npm run generate-models` / `npm run generate-benchmarks`.
const fs = require("fs");
const path = require("path");

const from = path.resolve(__dirname, "../../src/interact/data");
const to = path.resolve(__dirname, "../src");

for (const name of ["models.json", "benchmarks.json", "settings.json"]) {
  fs.cpSync(path.join(from, name), path.join(to, name));
  console.log(`synced ${name} from ${from}`);
}
