import fs from "node:fs";
import path from "node:path";
import { ts } from "ts-morph";
import { buildEdges, edgeContext } from "./edges.mjs";
import * as nest from "./entrypoints/nest.mjs";
import { openProject, relPath } from "./project.mjs";
import { fileSymbols } from "./symbols.mjs";

const ADAPTERS = { nest };
// Frameworks with entry points but no adapter yet; reported so a miss is visible.
const KNOWN_FRAMEWORKS = ["express", "next", "fastify", "koa", "hono"];
const MAX_DIAGNOSTICS = 10;

/** Dependency names from the repository's package.json (parsed, never run). */
function dependencyNames(root) {
  try {
    const pkg = JSON.parse(fs.readFileSync(path.join(root, "package.json"), "utf8"));
    return new Set([...Object.keys(pkg.dependencies ?? {}), ...Object.keys(pkg.devDependencies ?? {})]);
  } catch {
    return new Set();
  }
}

function diagnosticsSample(root, project, files) {
  const program = project.getProgram().compilerObject;
  const out = [];
  for (const sf of files) {
    for (const d of program.getSemanticDiagnostics(sf.compilerNode)) {
      const line = d.file && d.start !== undefined ? d.file.getLineAndCharacterOfPosition(d.start).line + 1 : 0;
      out.push(`${relPath(root, sf.getFilePath())}:${line} ${ts.flattenDiagnosticMessageText(d.messageText, " ")}`);
      if (out.length >= MAX_DIAGNOSTICS) return out;
    }
  }
  return out;
}

/**
 * Static analysis of one repository. Never runs, imports or installs anything from it.
 * @param {string} root absolute repository path
 * @param {{tsconfig?: string, diagnostics?: boolean}} options
 */
export function buildGraph(root, { tsconfig = "tsconfig.json", diagnostics = true } = {}) {
  const start = performance.now();
  const { project, files, skipped } = openProject(root, tsconfig);
  const symbols = files.flatMap((sf) => fileSymbols(root, sf));
  const ctx = edgeContext(root, symbols);
  const { edges, local } = buildEdges(ctx, symbols, files);

  const deps = dependencyNames(root);
  const entrypoints = [];
  const adapters = {};
  for (const [name, adapter] of Object.entries(ADAPTERS)) {
    const found = adapter.detect({ project, root, symbols });
    if (found.matched) adapters[name] = found.matched;
    entrypoints.push(...found.entrypoints);
  }

  const count = (kind) => edges.filter((e) => e.kind === kind).length;
  const calls = edges.filter((e) => e.kind === "calls" || e.kind === "calls_external");
  const report = {
    files: files.length,
    files_skipped: skipped,
    symbols: symbols.length,
    edges: Object.fromEntries(
      ["calls", "calls_external", "injects", "imports", "extends", "implements", "reads_env"].map((k) => [k, count(k)]),
    ),
    // Share of call sites whose callee has a declaration (repository or TypeScript lib); the rest
    // are names nothing declares, typically package APIs without node_modules.
    calls_resolved_share: calls.length ? Number((calls.filter((e) => e.resolved).length / calls.length).toFixed(3)) : null,
    calls_in_repo_share: calls.length ? Number((count("calls") / calls.length).toFixed(3)) : null,
    calls_local: local,
    entrypoints: entrypoints.length,
    adapters,
    unmatched_frameworks: KNOWN_FRAMEWORKS.filter((f) => deps.has(f) && !adapters[f]),
    diagnostics_sample: diagnostics ? diagnosticsSample(root, project, files) : [],
    secs: Number(((performance.now() - start) / 1000).toFixed(1)),
  };
  return { symbols: symbols.map((s) => s.row), edges, entrypoints, report };
}
