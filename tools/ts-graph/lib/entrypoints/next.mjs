import fs from "node:fs";
import path from "node:path";
import { Node } from "ts-morph";
import { defaultExportSymbol, isFunctionValued, joinRoute } from "./common.mjs";

const HTTP_METHODS = new Set(["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]);
const CONFIGS = ["next.config.js", "next.config.mjs", "next.config.ts"];
const APP_DIRS = ["app", "src/app"];
const MIDDLEWARE = new Set(["middleware.ts", "middleware.js", "src/middleware.ts", "src/middleware.js"]);
const PAGE = /^page\.[jt]sx?$/;
const ROUTE = /^route\.[jt]s$/;

/**
 * URL path of an App Router folder: route groups `(x)` and parallel slots `@x` drop out,
 * intercepting prefixes `(.)`, `(..)`, `(...)` are stripped, dynamic segments stay as written.
 * @param {string[]} dirs folders between the app directory and the file
 */
export function appRoute(dirs) {
  const parts = dirs
    .filter((d) => !(d.startsWith("(") && d.endsWith(")")) && !d.startsWith("@"))
    .map((d) => d.replace(/^(\(\.{1,3}\))+/, ""));
  return joinRoute(...parts);
}

/** Whether a statement list starts with the "use server" directive. */
function usesServer(statements) {
  const first = statements?.[0];
  if (!first || !Node.isExpressionStatement(first)) return false;
  const expr = first.getExpression();
  return Node.isStringLiteral(expr) && expr.getLiteralValue() === "use server";
}

function bodyStatements(node) {
  const fn = Node.isVariableDeclaration(node) ? node.getInitializer() : node;
  const body = fn && typeof fn.getBody === "function" ? fn.getBody() : undefined;
  return body && Node.isBlock(body) ? body.getStatements() : [];
}

/**
 * Next.js App Router entry points, by file convention: `page` (a page's default export),
 * `http_route` (each HTTP-method export of a `route` file), `middleware`, and `action` (exported
 * server actions). Runs only when the repository depends on `next` or has a next.config file.
 * @param {{root: string, symbols: {row: object, node: import("ts-morph").Node}[], deps: Set<string>}} input
 */
export function detect({ root, symbols, deps }) {
  const entrypoints = [];
  if (!deps.has("next") && !CONFIGS.some((c) => fs.existsSync(path.join(root, c)))) {
    return { entrypoints, matched: 0 };
  }
  const byFile = new Map();
  for (const sym of symbols) {
    if (!byFile.has(sym.row.file)) byFile.set(sym.row.file, []);
    byFile.get(sym.row.file).push(sym);
  }
  const appDirs = APP_DIRS.filter((d) => fs.existsSync(path.join(root, d)));
  const add = (sym, kind, route) =>
    entrypoints.push({ symbol: sym.row.id, kind, route, framework: "next", version: "" });

  for (const [file, fileSymbols] of byFile) {
    const appDir = appDirs.find((d) => file.startsWith(`${d}/`));
    if (appDir) {
      const segments = file.slice(appDir.length + 1).split("/");
      const name = segments.pop();
      if (segments.some((d) => d.startsWith("_"))) continue; // private folders: never routed
      if (PAGE.test(name)) {
        const sym = defaultExportSymbol(fileSymbols);
        if (sym) add(sym, "page", appRoute(segments));
        continue;
      }
      if (ROUTE.test(name)) {
        for (const sym of fileSymbols) {
          if (!sym.row.parent && sym.row.exported && HTTP_METHODS.has(sym.row.qualified_name) && isFunctionValued(sym)) {
            add(sym, "http_route", `${sym.row.qualified_name} ${appRoute(segments)}`);
          }
        }
        continue;
      }
    }
    if (MIDDLEWARE.has(file)) {
      const sym = fileSymbols.find((s) => s.row.qualified_name === "middleware" && s.row.exported) ?? defaultExportSymbol(fileSymbols);
      if (sym) add(sym, "middleware", "middleware");
      continue;
    }
    const fileIsServer = usesServer(fileSymbols[0]?.node.getSourceFile().getStatements());
    for (const sym of fileSymbols) {
      if (sym.row.parent || !sym.row.exported || !isFunctionValued(sym)) continue;
      if (fileIsServer || usesServer(bodyStatements(sym.node))) add(sym, "action", sym.row.qualified_name);
    }
  }
  return { entrypoints, matched: entrypoints.length };
}
