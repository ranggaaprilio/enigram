#!/usr/bin/env node
// ts-graph: static TypeScript code graph as JSONL (see README.md for the contract).
import fs from "node:fs";
import path from "node:path";
import { parseArgs } from "node:util";
import { buildGraph } from "./lib/graph.mjs";

const USAGE = `usage: node index.mjs --repo <dir> --out <dir> [--tsconfig tsconfig.json]

Parses a TypeScript repository with ts-morph (no node_modules, nothing from the repository is
run) and writes symbols.jsonl, edges.jsonl, entrypoints.jsonl and report.json into --out.`;

function writeJsonl(file, rows) {
  fs.writeFileSync(file, rows.map((r) => JSON.stringify(r)).join("\n") + (rows.length ? "\n" : ""));
}

function main() {
  let args;
  try {
    ({ values: args } = parseArgs({
      options: {
        repo: { type: "string" },
        out: { type: "string" },
        tsconfig: { type: "string", default: "tsconfig.json" },
        help: { type: "boolean", short: "h" },
      },
    }));
  } catch (e) {
    console.error(`${e.message}\n${USAGE}`);
    return 2;
  }
  if (args.help) {
    console.log(USAGE);
    return 0;
  }
  if (!args.repo || !args.out) {
    console.error(USAGE);
    return 2;
  }
  const root = path.resolve(args.repo);
  let graph;
  try {
    graph = buildGraph(root, { tsconfig: args.tsconfig });
  } catch (e) {
    console.error(`ts-graph: cannot read ${args.tsconfig} in ${root}: ${e.message}`);
    return 2;
  }
  fs.mkdirSync(args.out, { recursive: true });
  writeJsonl(path.join(args.out, "symbols.jsonl"), graph.symbols);
  writeJsonl(path.join(args.out, "edges.jsonl"), graph.edges);
  writeJsonl(path.join(args.out, "entrypoints.jsonl"), graph.entrypoints);
  fs.writeFileSync(path.join(args.out, "report.json"), JSON.stringify(graph.report, null, 2));
  const { diagnostics_sample: _, ...summary } = graph.report;
  console.log(JSON.stringify(summary));
  return 0;
}

process.exitCode = main();
