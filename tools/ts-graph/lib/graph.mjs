import fs from "node:fs";
import path from "node:path";
import { ts } from "ts-morph";
import { buildEdges, edgeContext } from "./edges.mjs";
import * as express from "./entrypoints/express.mjs";
import * as generic from "./entrypoints/generic.mjs";
import * as nest from "./entrypoints/nest.mjs";
import * as next from "./entrypoints/next.mjs";
import { findProjects, openProject, relPath } from "./project.mjs";
import { fileSymbols } from "./symbols.mjs";

const ADAPTERS = { nest, next, express };
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
 * Without `tsconfig`, every TypeScript project in it (findProjects) is analysed and the results
 * are merged: a monorepo's `apps/web/tsconfig.json` counts as much as a root one. Paths stay
 * relative to the repository root.
 * @param {string} root absolute repository path
 * @param {{tsconfig?: string, diagnostics?: boolean}} options
 */
export function buildGraph(root, { tsconfig, diagnostics = true } = {}) {
  const start = performance.now();
  const configs = tsconfig ? [tsconfig] : findProjects(root);
  if (!configs.length) throw new Error(`no tsconfig.json in ${root}`);
  const taken = new Set(); // files a shallower project analyses already
  const parts = [];
  for (const config of configs) {
    try {
      parts.push(openPart(root, config, taken));
    } catch (e) {
      if (tsconfig) throw e; // the one asked for
      parts.push({ config, error: e.message });
    }
  }
  const done = parts.filter((p) => !p.error);
  if (!done.length) throw new Error(parts.map((p) => `${p.config}: ${p.error}`).join("; "));
  // One context over every project's symbols: a call into a file another project analyses
  // (a relative import, a `paths` alias) still lands on its symbol. declKey is a path and an
  // offset, the same in every project that includes the file.
  const ctx = edgeContext(root, done.flatMap((p) => p.symbols));
  for (const part of done) finishPart(root, ctx, part, diagnostics);

  const symbols = done.flatMap((p) => p.symbols);
  const edges = done.flatMap((p) => p.edges);
  const entrypoints = done.flatMap((p) => p.entrypoints);
  const adapters = {};
  for (const p of done) {
    for (const [name, n] of Object.entries(p.adapters)) adapters[name] = (adapters[name] ?? 0) + n;
  }
  const skipped = { test: 0, dts: 0, generated: 0, outside: 0, other_project: 0 };
  for (const p of done) for (const [k, n] of Object.entries(p.skipped)) skipped[k] += n;

  const count = (kind) => edges.filter((e) => e.kind === kind).length;
  const calls = edges.filter((e) => e.kind === "calls" || e.kind === "calls_external");
  const report = {
    files: done.reduce((n, p) => n + p.files, 0),
    files_skipped: skipped,
    symbols: symbols.length,
    edges: Object.fromEntries(
      [
        "calls", "calls_external", "injects", "imports", "extends", "implements", "reads_env",
        "guarded_by", "intercepted_by", "module_imports", "provides", "provides_config",
      ].map((k) => [k, count(k)]),
    ),
    // Share of call sites whose callee has a declaration (repository or TypeScript lib); the rest
    // are names nothing declares, typically package APIs without node_modules.
    calls_resolved_share: calls.length ? Number((calls.filter((e) => e.resolved).length / calls.length).toFixed(3)) : null,
    calls_in_repo_share: calls.length ? Number((count("calls") / calls.length).toFixed(3)) : null,
    calls_local: done.reduce((n, p) => n + p.local, 0),
    entrypoints: entrypoints.length,
    adapters,
    fallback: done.some((p) => p.fallback) ? "generic" : null,
    unmatched_frameworks: [...new Set(done.flatMap((p) => p.unmatched))],
    // Each project analysed: its tsconfig, and what it gave (or why it could not be read).
    projects: parts.map((p) =>
      p.error
        ? { tsconfig: p.config, error: p.error }
        : { tsconfig: p.config, files: p.files, symbols: p.symbols.length, entrypoints: p.entrypoints.length, adapters: p.adapters },
    ),
    diagnostics_sample: done.flatMap((p) => p.diagnostics).slice(0, MAX_DIAGNOSTICS),
    secs: Number(((performance.now() - start) / 1000).toFixed(1)),
  };
  return { symbols: symbols.map((s) => s.row), edges, entrypoints, report };
}

/**
 * One TypeScript project of the repository: its files and symbols. Files `taken` by an earlier
 * project are left out (and added to it otherwise).
 * @param {string} root absolute repository path
 * @param {string} config tsconfig path relative to root
 * @param {Set<string>} taken absolute paths of files already analysed
 */
function openPart(root, config, taken) {
  const { project, files: all, skipped } = openProject(root, config);
  const files = all.filter((sf) => !taken.has(sf.getFilePath()));
  skipped.other_project = all.length - files.length;
  for (const sf of files) taken.add(sf.getFilePath());
  const dir = path.dirname(path.join(root, config));
  const deps = new Set([...dependencyNames(root), ...dependencyNames(dir)]);
  const options = { express: deps.has("express") }; // inline route handlers become symbols
  const symbols = files.flatMap((sf) => fileSymbols(root, sf, options));
  return { config, project, dir, deps, files: files.length, sourceFiles: files, skipped, symbols };
}

/**
 * A project's edges and entry points. Framework adapters look at the project's own folder: its
 * `app/` or `src/main.ts`, its package.json (and the root one).
 */
function finishPart(root, ctx, part, diagnostics) {
  const { project, symbols, sourceFiles, dir, deps } = part;
  const { edges, local } = buildEdges(ctx, symbols, sourceFiles);
  const prefix = dir === root ? "" : `${relPath(root, dir)}/`;
  const entrypoints = [];
  const adapters = {};
  for (const [name, adapter] of Object.entries(ADAPTERS)) {
    const found = adapter.detect({ project, root: dir, prefix, symbols, files: sourceFiles, deps });
    if (found.matched) adapters[name] = found.matched;
    entrypoints.push(...found.entrypoints);
  }
  // No framework recognised: exported functions nothing calls, so the project still has flows.
  let fallback = false;
  if (!Object.keys(adapters).length) {
    const found = generic.roots({ symbols, edges });
    if (found.matched) {
      adapters.generic = found.matched;
      entrypoints.push(...found.entrypoints);
      fallback = true;
    }
  }
  Object.assign(part, {
    edges,
    local,
    entrypoints,
    adapters,
    fallback,
    unmatched: KNOWN_FRAMEWORKS.filter((f) => deps.has(f) && !adapters[f]),
    diagnostics: diagnostics && sourceFiles.length ? diagnosticsSample(root, project, sourceFiles) : [],
  });
}
