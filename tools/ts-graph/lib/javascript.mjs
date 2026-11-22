import fs from "node:fs";
import { builtinModules } from "node:module";
import path from "node:path";
import { Project, ts } from "ts-morph";
import { guardedFileSystem } from "./fsguard.mjs";
import { relPath, SKIP_DIRS } from "./project.mjs";

const JS_FILE = /\.(js|jsx|mjs|cjs)$/;
const UNSUPPORTED_FILE = /\.(vue|svelte)$/;
const TEST_DIRS = ["test", "tests", "__tests__"]; // a package.json in there is a fixture, not a project
const TEST_FILES = [
  /\.(spec|test|e2e-spec)\.(jsx?|mjs|cjs)$/,
  /(^|\/)(test|tests|__tests__|fixtures|__fixtures__|__mocks__)\//,
];
const CONFIG_FILES = [/\.config\.(js|cjs|mjs)$/, /(^|\/)\.eslintrc\.(js|cjs)$/];
const MINIFIED_FILES = /\.(min|bundle)\.(js|mjs|cjs)$/;
const MAX_AVG_LINE = 500; // a longer average line is a minified or generated file
export const MAX_LINES = 10_000;
export const BUILTINS = new Set(builtinModules);
export const MAX_PACKAGES = 1000;

/** Skip counters only JavaScript files have (tests are counted with TypeScript's), in report order. */
export function emptyJsSkips() {
  return { config: 0, minified: 0, large: 0, syntax: 0, unreadable: 0, no_project: 0, project_limit: 0, unsupported: 0 };
}

/**
 * Why a JavaScript file is not analysed, or null when it is. Reads the file for the size checks.
 * @param {string} rel repository-relative path
 * @param {string} file absolute path
 */
function skipReason(rel, file) {
  if (TEST_FILES.some((re) => re.test(rel))) return "test";
  if (CONFIG_FILES.some((re) => re.test(rel))) return "config";
  if (MINIFIED_FILES.test(rel)) return "minified";
  let text;
  try {
    text = fs.readFileSync(file, "utf8");
  } catch {
    return "unreadable"; // no permission, or gone since the walk
  }
  const lines = text.split("\n").length - (text.endsWith("\n") ? 1 : 0) || 1;
  if (lines > MAX_LINES) return "large";
  if (text.length / lines > MAX_AVG_LINE) return "minified";
  return null;
}

/**
 * The repository's JavaScript projects: every folder with a `package.json` or `jsconfig.json`
 * (outside SKIP_DIRS and test folders) that owns at least one JavaScript file. A file belongs to
 * the nearest such folder above it, unless a TypeScript project already includes it (`tsFiles`).
 * Symbolic links are not followed. Shallowest first, at most `max`.
 * @param {string} root absolute repository path
 * @param {Set<string>} tsFiles absolute paths every TypeScript project includes
 * @param {number} max
 * @returns {{projects: {dir: string, config: string, files: string[]}[], skipped: object, truncated: number}}
 */
export function findJsProjects(root, tsFiles, max) {
  const skipped = { test: 0, ...emptyJsSkips() };
  const owned = new Map(); // project dir -> files
  const configOf = new Map(); // project dir -> its config file name
  const named = []; // [package name, its folder], for workspace imports by name
  const walk = (dir, owner, inTestDir) => {
    let entries;
    try {
      entries = fs.readdirSync(dir, { withFileTypes: true });
    } catch {
      return;
    }
    const names = new Set(entries.filter((e) => e.isFile()).map((e) => e.name));
    if (!inTestDir && (names.has("jsconfig.json") || names.has("package.json"))) {
      owner = dir;
      configOf.set(dir, names.has("jsconfig.json") ? "jsconfig.json" : "package.json");
    }
    if (!inTestDir && names.has("package.json")) {
      const name = packageName(path.join(dir, "package.json"));
      if (name) named.push([name, dir]);
    }
    for (const e of entries) {
      const file = path.join(dir, e.name);
      if (e.isDirectory()) {
        if (!SKIP_DIRS.includes(e.name)) walk(file, owner, inTestDir || TEST_DIRS.includes(e.name));
        continue;
      }
      if (!e.isFile()) continue; // symbolic links and the rest
      if (UNSUPPORTED_FILE.test(e.name)) skipped.unsupported += 1;
      if (!JS_FILE.test(e.name) || tsFiles.has(file)) continue;
      const reason = skipReason(relPath(root, file), file);
      if (reason) skipped[reason] += 1;
      else if (!owner) skipped.no_project += 1;
      else {
        if (!owned.has(owner)) owned.set(owner, []);
        owned.get(owner).push(file);
      }
    }
  };
  walk(root, null, false);
  const depth = (dir) => (dir === root ? 0 : relPath(root, dir).split("/").length);
  const all = [...owned.keys()]
    .sort((a, b) => depth(a) - depth(b) || a.localeCompare(b))
    .map((dir) => ({
      dir,
      config: relPath(root, path.join(dir, configOf.get(dir))),
      files: owned.get(dir).sort(),
    }));
  const projects = all.slice(0, Math.max(0, max));
  for (const left of all.slice(projects.length)) skipped.project_limit += left.files.length;
  // A name two package.json share goes to the shallower folder (a package over its example copy);
  // at most MAX_PACKAGES names, each becoming two `paths` entries of every project.
  const packages = new Map();
  named.sort(([, a], [, b]) => depth(a) - depth(b) || a.localeCompare(b));
  for (const [name, dir] of named) {
    if (packages.size >= MAX_PACKAGES) break;
    if (!packages.has(name)) packages.set(name, dir);
  }
  return { projects, skipped, truncated: all.length - projects.length, packages };
}

/** The `name` of a package.json (parsed, never run), or null. */
function packageName(file) {
  try {
    const name = JSON.parse(fs.readFileSync(file, "utf8")).name;
    return typeof name === "string" && /^(@[\w.-]+\/)?[\w.-]+$/.test(name) ? name : null;
  } catch {
    return null;
  }
}

/**
 * `paths` entries that let imports reach repository code without node_modules: every package of
 * the repository by its name (a workspace sibling, `@acme/util` → its folder), and every top-level
 * folder of the project that is not a dependency name (`src/lib/x` → `<project>/src/lib/x`, the
 * usual Vite/webpack alias). Entries the jsconfig already has win. Targets are absolute, so they
 * need no `baseUrl` (which would also send every bare import, dependencies included, to the
 * project folder first).
 * @param {{dir: string}} found
 * @param {Map<string, string>} packages package name -> folder
 * @param {Set<string>} deps dependency names of the project
 */
function repositoryPaths(found, packages, deps, own = {}) {
  const paths = { ...own };
  const add = (key, dir) => {
    if (!Object.hasOwn(paths, key)) paths[key] = [key.endsWith("/*") ? path.join(dir, "*") : dir];
  };
  for (const [name, dir] of packages) {
    add(name, dir);
    add(`${name}/*`, dir);
  }
  let entries = [];
  try {
    entries = fs.readdirSync(found.dir, { withFileTypes: true });
  } catch {
    // an unreadable folder adds no aliases
  }
  for (const e of entries) {
    if (!e.isDirectory() || e.name.startsWith(".") || SKIP_DIRS.includes(e.name) || deps.has(e.name)) continue;
    if (BUILTINS.has(e.name)) continue;
    add(`${e.name}/*`, path.join(found.dir, e.name));
  }
  return paths;
}

const inside = (root, p) => {
  const rel = path.relative(root, p);
  return rel === "" || (!rel.startsWith("..") && !path.isAbsolute(rel));
};

/**
 * `baseUrl` and `paths` of a jsconfig.json; nothing else of it is used. The file comes from the
 * repository, so both are dropped unless they stay inside it: a `baseUrl` outside the repository,
 * a `paths` value that is not a list of strings, or a target with `..` or an absolute path.
 */
function jsconfigPaths(root, file) {
  const { config } = ts.readConfigFile(file, (f) => fs.readFileSync(f, "utf8"));
  const raw = config?.compilerOptions ?? {};
  const dir = path.dirname(file);
  const baseUrl = typeof raw.baseUrl === "string" ? path.resolve(dir, raw.baseUrl) : undefined;
  if (baseUrl && !inside(root, baseUrl)) return {};
  const out = baseUrl ? { baseUrl } : {};
  const paths = raw.paths;
  const ok =
    paths && typeof paths === "object" && !Array.isArray(paths) &&
    Object.values(paths).every((targets) =>
      Array.isArray(targets) &&
      targets.every((t) => typeof t === "string" && !path.isAbsolute(t) && inside(root, path.resolve(baseUrl ?? dir, t))),
    );
  // Without a baseUrl, `paths` targets are relative to the jsconfig's folder (pathsBasePath).
  if (ok) out.paths = paths;
  return out;
}

/**
 * One JavaScript project as a ts-morph project: its own files only, with `allowJs` and no type
 * checking. A file with a syntax error (Flow annotations, for one) is dropped and counted.
 * Imports reach the repository's other packages by name and the project's own top-level folders
 * (repositoryPaths).
 * @param {string} root absolute repository path
 * @param {{dir: string, config: string, files: string[]}} found
 * @param {{packages?: Map<string, string>, deps?: Set<string>}} context
 */
export function openJsProject(root, found, { packages = new Map(), deps = new Set() } = {}) {
  const configFile = path.join(root, found.config);
  const own = found.config.endsWith("jsconfig.json") ? jsconfigPaths(root, configFile) : {};
  // A jsconfig's own baseUrl is kept (bare imports then try it first, as the editor does).
  const resolution = {
    ...(own.baseUrl ? { baseUrl: own.baseUrl } : { pathsBasePath: found.dir }),
    paths: repositoryPaths(found, packages, deps, own.paths),
  };
  const project = new Project({
    fileSystem: guardedFileSystem(root), // nothing outside the repository, symbolic links included
    skipAddingFilesFromTsConfig: true,
    skipFileDependencyResolution: true,
    compilerOptions: {
      allowJs: true,
      checkJs: false,
      noEmit: true,
      jsx: ts.JsxEmit.Preserve,
      module: ts.ModuleKind.ESNext,
      moduleResolution: ts.ModuleResolutionKind.Bundler,
      skipLibCheck: true,
      types: [],
      typeRoots: [],
      ...resolution,
    },
  });
  let unreadable = 0;
  const added = [];
  for (const file of found.files) {
    try {
      added.push(project.addSourceFileAtPath(file));
    } catch {
      unreadable += 1; // gone or changed since the walk read it
    }
  }
  // One program for all files; JavaScript-only syntax errors (type annotations) come from it.
  const program = project.getProgram().compilerObject;
  const broken = new Set(added.filter((sf) => program.getSyntacticDiagnostics(sf.compilerNode).length));
  for (const sf of broken) project.removeSourceFile(sf);
  const files = added.filter((sf) => !broken.has(sf));
  return { project, files, syntax: broken.size, unreadable };
}
