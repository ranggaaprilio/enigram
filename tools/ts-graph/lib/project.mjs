import fs from "node:fs";
import path from "node:path";
import { Project, ts } from "ts-morph";

export const SKIP_DIRS = ["node_modules", "dist", "build", ".git", "coverage"];
const TEST_DIRS = ["test", "__tests__"]; // a project in there is a fixture, not the repository
export const MAX_PROJECTS = 50;
const TEST_FILES = [/\.spec\.tsx?$/, /\.e2e-spec\.tsx?$/, /\.test\.tsx?$/, /(^|\/)test\//, /(^|\/)__tests__\//];
const GENERATED_FILES = [/\.generated\./];

/** Repository-relative path with forward slashes. */
export function relPath(root, file) {
  return path.relative(root, file).split(path.sep).join("/");
}

/**
 * Why a file is not analysed ("outside" | "test" | "dts" | "generated"), or null when it is.
 * @param {string} rel repository-relative path
 */
export function skipReason(rel) {
  if (rel.startsWith("../") || path.isAbsolute(rel)) return "outside";
  if (rel.split("/").some((part) => SKIP_DIRS.includes(part))) return "outside";
  if (/\.d\.[cm]?ts$/.test(rel)) return "dts";
  if (TEST_FILES.some((re) => re.test(rel))) return "test";
  if (GENERATED_FILES.some((re) => re.test(rel))) return "generated";
  return null;
}

/**
 * The repository as a ts-morph project, without node_modules: package types stay unresolved,
 * repository types resolve. Skipped files stay in the program (their types still help the checker)
 * but are not analysed.
 * @param {string} root absolute repository path
 * @param {string} tsconfig path of the tsconfig relative to root
 */
export function openProject(root, tsconfig = "tsconfig.json") {
  const configPath = path.join(root, tsconfig);
  if (!fs.existsSync(configPath)) throw new Error(`no ${tsconfig} at ${root}`);
  const project = new Project({
    tsConfigFilePath: configPath,
    skipFileDependencyResolution: true, // nothing to resolve: node_modules is absent
    compilerOptions: {
      skipLibCheck: true,
      types: [],
      typeRoots: [],
      noEmit: true,
      incremental: false,
      composite: false,
      declaration: false,
    },
  });
  const skipped = { test: 0, dts: 0, generated: 0, outside: 0 };
  const files = [];
  for (const sf of project.getSourceFiles()) {
    const rel = relPath(root, sf.getFilePath());
    const reason = skipReason(rel);
    if (reason) skipped[reason] += 1;
    else files.push(sf);
  }
  files.sort((a, b) => a.getFilePath().localeCompare(b.getFilePath()));
  return { project, files, skipped };
}

/**
 * The repository's TypeScript projects, as tsconfig paths relative to root: every
 * `tsconfig.json` outside SKIP_DIRS and test folders, and the configs their `references` name
 * (a solution-style tsconfig such as Vite's lists no files itself). Shallowest first, so a file
 * two projects include belongs to the outer one. At most MAX_PROJECTS.
 * @param {string} root absolute repository path
 * @returns {string[]}
 */
export function findProjects(root) {
  const found = new Set();
  const walk = (dir) => {
    let entries;
    try {
      entries = fs.readdirSync(dir, { withFileTypes: true });
    } catch {
      return;
    }
    for (const e of entries) {
      if (e.isDirectory() && !SKIP_DIRS.includes(e.name) && !TEST_DIRS.includes(e.name)) {
        walk(path.join(dir, e.name));
      } else if (e.isFile() && e.name === "tsconfig.json") {
        found.add(relPath(root, path.join(dir, e.name)));
      }
    }
  };
  walk(root);
  const queue = [...found];
  while (queue.length) {
    for (const ref of references(root, queue.shift())) {
      if (!found.has(ref)) {
        found.add(ref);
        queue.push(ref);
      }
    }
  }
  const depth = (p) => p.split("/").length;
  return [...found].sort((a, b) => depth(a) - depth(b) || a.localeCompare(b)).slice(0, MAX_PROJECTS);
}

/** The configs a tsconfig's `references` name, relative to root; none outside it. */
function references(root, config) {
  const file = path.join(root, config);
  const { config: json } = ts.readConfigFile(file, (f) => fs.readFileSync(f, "utf8"));
  const out = [];
  for (const ref of json?.references ?? []) {
    if (typeof ref?.path !== "string") continue;
    let target = path.resolve(path.dirname(file), ref.path);
    if (!target.endsWith(".json")) target = path.join(target, "tsconfig.json");
    const rel = relPath(root, target);
    if (!rel.startsWith("../") && fs.existsSync(target)) out.push(rel);
  }
  return out;
}
