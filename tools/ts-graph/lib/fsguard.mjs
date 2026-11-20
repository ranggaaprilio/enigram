import fs from "node:fs";
import path from "node:path";
import { Project } from "ts-morph";

/**
 * The real path of `p`, or, when it does not exist, the real path of its nearest existing folder
 * with the rest joined on: the place a read of `p` would end up.
 */
function landing(p) {
  const rest = [];
  for (let dir = path.resolve(p); ; dir = path.dirname(dir)) {
    try {
      return path.join(fs.realpathSync(dir), ...rest.reverse());
    } catch {
      if (dir === path.dirname(dir)) return path.resolve(p);
      rest.push(path.basename(dir));
    }
  }
}

/**
 * Whether `p` really lies inside the repository `root` (symbolic links resolved). For reads the
 * sidecar does with node:fs itself (package.json, jsconfig.json).
 */
export function isInside(root, p) {
  const rel = path.relative(fs.realpathSync(root), landing(p));
  return rel === "" || (!rel.startsWith("..") && !path.isAbsolute(rel));
}

/**
 * The project's file system, limited to the repository: a path whose real location (symbolic
 * links resolved) is outside `root` does not exist for the compiler. Module resolution, tsconfig
 * `include` globs and `paths` then never load a file outside the repository, however a symbolic
 * link inside it points. The TypeScript lib files are served from memory by ts-morph and are not
 * read through it. Everything else is the real file system.
 * @param {string} root absolute repository path
 * @returns {import("ts-morph").FileSystemHost}
 */
export function guardedFileSystem(root) {
  const real = new Project({ skipAddingFilesFromTsConfig: true }).getFileSystem();
  const realRoot = fs.realpathSync(root);
  // Reads of an outside path go to a path that does not exist, so the real host raises its own
  // "not found" error, the only kind ts-morph treats as a missing file.
  let missing = path.join(realRoot, ".ts-graph-outside");
  while (fs.existsSync(missing)) missing += "_";
  const cache = new Map();
  const inside = (p) => {
    if (!cache.has(p)) {
      const rel = path.relative(realRoot, landing(p));
      cache.set(p, rel === "" || (!rel.startsWith("..") && !path.isAbsolute(rel)));
    }
    return cache.get(p);
  };
  const guard = {
    fileExists: async (p) => inside(p) && real.fileExists(p),
    fileExistsSync: (p) => inside(p) && real.fileExistsSync(p),
    directoryExists: async (p) => inside(p) && real.directoryExists(p),
    directoryExistsSync: (p) => inside(p) && real.directoryExistsSync(p),
    readFile: async (p, encoding) => real.readFile(inside(p) ? p : missing, encoding),
    readFileSync: (p, encoding) => real.readFileSync(inside(p) ? p : missing, encoding),
    readDirSync: (p) => (inside(p) ? real.readDirSync(p).filter((entry) => inside(entry.name)) : []),
    glob: async (patterns) => (await real.glob(patterns)).filter(inside),
    globSync: (patterns) => real.globSync(patterns).filter(inside),
  };
  return new Proxy(real, {
    get(target, prop) {
      if (Object.hasOwn(guard, prop)) return guard[prop];
      const value = Reflect.get(target, prop, target);
      return typeof value === "function" ? value.bind(target) : value;
    },
  });
}
