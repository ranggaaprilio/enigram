import fs from "node:fs";
import path from "node:path";
import { ts } from "ts-morph";
import { buildEdges, edgeContext } from "./edges.mjs";
import * as express from "./entrypoints/express.mjs";
import * as generic from "./entrypoints/generic.mjs";
import * as nest from "./entrypoints/nest.mjs";
import * as next from "./entrypoints/next.mjs";
import { BUILTINS, emptyJsSkips, findJsProjects, openJsProject } from "./javascript.mjs";
import { findProjects, MAX_PROJECTS, openProject, relPath } from "./project.mjs";
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
 * With `javascript` (and no `tsconfig`), JavaScript files no TypeScript project includes are
 * analysed too, one project per nearest `package.json` / `jsconfig.json` (findJsProjects); a
 * repository with no project at all is then an empty graph instead of an error.
 * @param {string} root absolute repository path
 * @param {{tsconfig?: string, diagnostics?: boolean, javascript?: boolean}} options
 */
export function buildGraph(root, { tsconfig, diagnostics = true, javascript = false } = {}) {
  const start = performance.now();
  const withJs = javascript && !tsconfig;
  const configs = tsconfig ? [tsconfig] : findProjects(root);
  if (!configs.length && !withJs) throw new Error(`no tsconfig.json in ${root}`);
  const taken = new Set(); // files a shallower project analyses already
  const parts = [];
  for (const config of configs) {
    try {
      parts.push(openPart(root, config, taken, withJs));
    } catch (e) {
      if (tsconfig) throw e; // the one asked for
      parts.push({ config, error: e.message });
    }
  }
  const jsSkipped = withJs ? { test: 0, ...emptyJsSkips() } : null;
  let truncated = 0;
  if (withJs) {
    // Every file a TypeScript project includes stays its own, skipped ones (tests, .d.ts) too.
    const tsFiles = new Set(
      parts.filter((p) => !p.error).flatMap((p) => p.project.getSourceFiles().map((sf) => sf.getFilePath())),
    );
    const found = findJsProjects(root, tsFiles, MAX_PROJECTS - configs.length);
    Object.assign(jsSkipped, found.skipped);
    truncated = found.truncated;
    for (const project of found.projects) {
      try {
        parts.push(openJsPart(root, project, jsSkipped, found.packages));
      } catch (e) {
        parts.push({ config: project.config, language: "js", error: e.message });
      }
    }
  }
  const done = parts.filter((p) => !p.error);
  if (!done.length && parts.length) throw new Error(parts.map((p) => `${p.config}: ${p.error}`).join("; "));
  // One context over every project's symbols: a call into a file another project analyses
  // (a relative import, a `paths` alias) still lands on its symbol. declKey is a path and an
  // offset, the same in every project that includes the file.
  const jsFiles = done.filter((p) => p.language === "js").flatMap((p) => p.sourceFiles.map((sf) => sf.getFilePath()));
  const ctx = edgeContext(root, done.flatMap((p) => p.symbols), jsFiles, { commonjs: withJs });
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
  if (jsSkipped) {
    // JavaScript skip reasons appear only with --javascript, so the report without it is unchanged.
    const { test: jsTests, ...jsOther } = jsSkipped;
    skipped.test += jsTests;
    Object.assign(skipped, jsOther);
  }

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
    // Each project analysed: its config, and what it gave (or why it could not be read). A
    // TypeScript project keeps its `tsconfig` key; a JavaScript one has none. `config` and
    // `language` only with --javascript, so the report without it keeps its old shape.
    projects: parts.map((p) => {
      let head = { tsconfig: p.config };
      if (p.language === "js") head = { config: p.config, language: "js" };
      else if (withJs) head = { tsconfig: p.config, config: p.config, language: "ts" };
      return p.error
        ? { ...head, error: p.error }
        : { ...head, files: p.files, symbols: p.symbols.length, entrypoints: p.entrypoints.length, adapters: p.adapters };
    }),
    ...(withJs ? { projects_truncated: truncated, imports_in_repo: importsInRepo(done) } : {}),
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
function openPart(root, config, taken, commonjs) {
  const { project, files: all, skipped } = openProject(root, config);
  const files = all.filter((sf) => !taken.has(sf.getFilePath()));
  skipped.other_project = all.length - files.length;
  for (const sf of files) taken.add(sf.getFilePath());
  const dir = path.dirname(path.join(root, config));
  const deps = new Set([...dependencyNames(root), ...dependencyNames(dir)]);
  // express: inline route handlers become symbols; commonjs: JavaScript's CommonJS rules.
  const options = { express: deps.has("express"), commonjs };
  const symbols = files.flatMap((sf) => fileSymbols(root, sf, options));
  return { config, project, dir, deps, files: files.length, sourceFiles: files, skipped, symbols };
}

/**
 * One JavaScript project (findJsProjects): its files, minus those with a syntax error (counted
 * in `jsSkipped.syntax`), and their symbols.
 */
function openJsPart(root, found, jsSkipped, packages) {
  const deps = new Set([...dependencyNames(root), ...dependencyNames(found.dir)]);
  const { project, files, syntax, unreadable } = openJsProject(root, found, { packages, deps });
  jsSkipped.syntax += syntax;
  jsSkipped.unreadable += unreadable;
  const symbols = files.flatMap((sf) => fileSymbols(root, sf, { express: deps.has("express"), commonjs: true }));
  return {
    config: found.config,
    language: "js",
    project,
    dir: found.dir,
    deps,
    files: files.length,
    sourceFiles: files,
    skipped: {},
    symbols,
  };
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
    const found = adapter.detect({ project, root: dir, prefix, symbols, files: sourceFiles, deps, commonjs: ctx.commonjs });
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
    ...(part.language === "js" ? { repoImports: repoImports(root, edges, deps) } : {}),
  });
}

/** repoImports of every JavaScript project, summed, with a sample of the unresolved ones. */
function importsInRepo(parts) {
  const all = parts.filter((p) => p.repoImports).map((p) => p.repoImports);
  return {
    total: all.reduce((n, r) => n + r.total, 0),
    resolved: all.reduce((n, r) => n + r.resolved, 0),
    unresolved_sample: all.flatMap((r) => r.unresolved).slice(0, MAX_UNRESOLVED_SAMPLE),
  };
}

const COMPONENT = /\.(vue|svelte)$/;

/**
 * Whether a relative import names a `.vue` / `.svelte` file, with or without the extension. Only
 * paths inside the repository are looked at.
 */
function isComponentImport(root, fromFile, spec) {
  if (COMPONENT.test(spec)) return true;
  if (!spec.startsWith(".")) return false;
  const base = path.resolve(root, path.dirname(fromFile), spec);
  if (relPath(root, base).startsWith("../")) return false;
  return [".vue", ".svelte", "/index.vue", "/index.svelte"].some((ext) => fs.existsSync(base + ext));
}

const MAX_SPECIFIER_CHARS = 200;

const ASSET = /\.(css|scss|sass|less|styl|svg|png|jpe?g|gif|webp|avif|ico|woff2?|ttf|eot|json|md|html|txt|graphql|gql|wasm)(\?.*)?$/;
const MAX_UNRESOLVED_SAMPLE = 10;
const packageOf = (spec) => (spec.startsWith("@") ? spec.split("/").slice(0, 2).join("/") : spec.split("/")[0]);

/**
 * The `imports` rows of a JavaScript project that are meant for repository code: resolved ones,
 * relative ones, and bare ones that name no dependency or Node built-in (an alias). Imports of
 * assets (`./app.css`) and of single-file components (`./App.vue`, or `./App` when only an
 * `App.vue` / `App.svelte` is there) are left out. An unresolved one is an alias or path the graph
 * cannot follow.
 */
function repoImports(root, edges, deps) {
  const out = { total: 0, resolved: 0, unresolved: [] };
  for (const e of edges) {
    if (e.kind !== "imports") continue;
    if (e.resolved) {
      out.total += 1;
      out.resolved += 1;
      continue;
    }
    const spec = e.dst;
    if (ASSET.test(spec) || isComponentImport(root, e.src_file, spec)) continue;
    if (!spec.startsWith(".") && (spec.startsWith("node:") || BUILTINS.has(packageOf(spec)) || deps.has(packageOf(spec)))) continue;
    out.total += 1;
    out.unresolved.push(`${e.src_file} -> ${spec.slice(0, MAX_SPECIFIER_CHARS)}`);
  }
  return out;
}
