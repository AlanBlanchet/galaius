const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "..");
const schemas = fs.readFileSync(process.argv[2], "utf8").trim();
const runtime = fs.readFileSync(path.join(__dirname, "runtime-decoders.ts"), "utf8")
  .replace("__CONVERSATION_SCHEMAS__", schemas);
fs.appendFileSync(
  path.join(root, "src", "generated", "types.ts"),
  runtime,
);
