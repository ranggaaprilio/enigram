import fs from "node:fs";
import path from "node:path";
import { Project } from "ts-morph";

export const SKIP_DIRS = ["node_modules", "dist", "build", ".git", "coverage"];
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
