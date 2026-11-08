import { Node, SyntaxKind } from "ts-morph";
import { declKey, HTTP_VERBS } from "../symbols.mjs";
import { joinRoute } from "./common.mjs";

const MAX_MOUNT_DEPTH = 10; // a mount cycle (a router mounted inside itself) stops here

function resolved(node) {
  let sym = node.getSymbol();
  if (sym?.isAlias()) sym = sym.getAliasedSymbol() ?? sym;
  return sym?.getDeclarations()?.[0];
}

/** Whether a name comes from the `express` package (its default import, `Router`, a namespace). */
function fromExpress(node) {
  if (Node.isPropertyAccessExpression(node)) return fromExpress(node.getExpression());
  if (!Node.isIdentifier(node)) return false;
  const decl = node.getSymbol()?.getDeclarations()?.[0];
  const imported = decl?.getFirstAncestorByKind(SyntaxKind.ImportDeclaration);
  return imported?.getModuleSpecifierValue() === "express";
}

function unwrap(node) {
  while (node && (Node.isParenthesizedExpression(node) || Node.isAsExpression(node))) node = node.getExpression();
  return node;
}

/**
 * The router an expression stands for, as a key: `express()`, `Router()`, `express.Router()`
 * itself; a variable or `export default` holding one, through imports; a `.use(...)` chain on
 * one (it returns the same router). Null for anything else.
 */
function routerKey(node, depth = 0) {
  node = unwrap(node);
  if (!node || depth > MAX_MOUNT_DEPTH) return null;
  if (Node.isCallExpression(node)) {
    const callee = node.getExpression();
    if (fromExpress(callee)) return declKey(node);
    if (Node.isPropertyAccessExpression(callee) && ["use", ...HTTP_VERBS].includes(callee.getName())) {
      return routerKey(callee.getExpression(), depth + 1);
    }
    return null;
  }
  if (Node.isIdentifier(node)) {
    const decl = resolved(node);
    if (Node.isVariableDeclaration(decl)) {
      const init = decl.getInitializer();
      return init ? routerKey(init, depth + 1) : null;
    }
    if (Node.isExportAssignment(decl)) return routerKey(decl.getExpression(), depth + 1);
    return null;
  }
  return null;
}

/** The symbol a handler belongs to: the inline function's own, or what a name resolves to. */
function handlerSymbol(handler, byDecl) {
  let node = Node.isArrowFunction(handler) || Node.isFunctionExpression(handler) ? handler : resolved(handler);
  for (; node; node = node.getParent()) {
    const row = byDecl.get(declKey(node));
    if (row) return row;
  }
  return null;
}

/**
 * Express entry points: every `get/post/put/patch/delete/options/head/all(path, ..., handler)`
 * on a router or app, with the paths of the `use(path, router)` calls that mount it joined in
 * front, across files through imports. A router is known by the `express()` / `Router()` call
 * that made it, whichever variable, import or `export default` reaches it. The entry point is
 * the last handler: an inline one is its own symbol (`GET /articles`, see fileSymbols), a named
 * one the symbol it resolves to. Runs only when the project depends on `express`.
 * @param {{symbols: {row: object, node: import("ts-morph").Node}[], files?: import("ts-morph").SourceFile[], deps: Set<string>}} input
 */
export function detect({ symbols, files = [], deps }) {
  const entrypoints = [];
  if (!deps.has("express")) return { entrypoints, matched: 0 };
  const byDecl = new Map(symbols.map((s) => [declKey(s.node), s.row]));
  const routes = []; // {router, verb, path, handler}
  const mounts = []; // {parent, child, path}
  for (const sf of files) {
    for (const call of sf.getDescendantsOfKind(SyntaxKind.CallExpression)) {
      const callee = call.getExpression();
      if (!Node.isPropertyAccessExpression(callee)) continue;
      const name = callee.getName();
      if (name !== "use" && !HTTP_VERBS.includes(name)) continue;
      const router = routerKey(callee.getExpression());
      if (!router) continue;
      const args = call.getArguments();
      const first = unwrap(args[0]);
      const path = first && (Node.isStringLiteral(first) || Node.isNoSubstitutionTemplateLiteral(first)) ? first.getLiteralValue() : null;
      if (name === "use") {
        for (const arg of path === null ? args : args.slice(1)) {
          const child = routerKey(arg);
          if (child && child !== router) mounts.push({ parent: router, child, path: path ?? "" });
        }
      } else if (path !== null && args.length >= 2) {
        routes.push({ router, verb: name, path, handler: unwrap(args.at(-1)) });
      }
    }
  }

  const prefixes = (router, depth = 0) => {
    const parents = mounts.filter((m) => m.child === router);
    if (!parents.length || depth > MAX_MOUNT_DEPTH) return [""];
    return parents.flatMap((m) => prefixes(m.parent, depth + 1).map((p) => joinRoute(p, m.path)));
  };
  const seen = new Set();
  for (const r of routes) {
    const row = handlerSymbol(r.handler, byDecl);
    if (!row) continue; // a package's handler (swagger-ui, ...): nothing to follow
    for (const prefix of prefixes(r.router)) {
      const route = `${r.verb.toUpperCase()} ${joinRoute(prefix, r.path)}`;
      if (seen.has(`${row.id} ${route}`)) continue;
      seen.add(`${row.id} ${route}`);
      entrypoints.push({ symbol: row.id, kind: "http_route", route, framework: "express", version: "" });
    }
  }
  entrypoints.sort((a, b) => a.route.localeCompare(b.route) || a.symbol.localeCompare(b.symbol));
  return { entrypoints, matched: entrypoints.length };
}
