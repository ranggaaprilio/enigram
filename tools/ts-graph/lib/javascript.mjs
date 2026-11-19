import fs from "node:fs";
import path from "node:path";
import { Project, ts } from "ts-morph";
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
  return { projects, skipped, truncated: all.length - projects.length };
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
  if (ok) {
    out.paths = paths;
    out.baseUrl ??= dir;
  }
  return out;
}

/**
 * One JavaScript project as a ts-morph project: its own files only, with `allowJs` and no type
 * checking. A file with a syntax error (Flow annotations, for one) is dropped and counted.
 * @param {string} root absolute repository path
 * @param {{dir: string, config: string, files: string[]}} found
 */
export function openJsProject(root, found) {
  const configFile = path.join(root, found.config);
  const project = new Project({
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
      ...(found.config.endsWith("jsconfig.json") ? jsconfigPaths(root, configFile) : {}),
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
