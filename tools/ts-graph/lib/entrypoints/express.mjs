import { Node, SyntaxKind } from "ts-morph";
import { exportedValue } from "../edges.mjs";
import { declKey, HTTP_VERBS, routeChain } from "../symbols.mjs";
import { joinRoute } from "./common.mjs";

const MAX_MOUNT_DEPTH = 10; // a mount cycle (a router mounted inside itself) stops here

// Set per detect() call: CommonJS forms (require, module.exports, forEach mounts) are followed
// only with --javascript.
let commonjs = false;

function resolved(node) {
  let sym = node.getSymbol();
  if (sym?.isAlias()) sym = sym.getAliasedSymbol() ?? sym;
  if (commonjs) sym = exportedValue(sym);
  return sym?.getDeclarations()?.[0];
}

/** `require("<name>")`, with a literal. */
function isRequireOf(node, name) {
  if (!Node.isCallExpression(node)) return false;
  const callee = node.getExpression();
  const arg = node.getArguments()[0];
  return Node.isIdentifier(callee) && callee.getText() === "require" && Node.isStringLiteral(arg) && arg.getLiteralValue() === name;
}

/**
 * Whether a name comes from the `express` package (its default import, `Router`, a namespace;
 * with CommonJS also `require("express")`, a variable or destructured name holding it).
 */
function fromExpress(node) {
  if (Node.isPropertyAccessExpression(node)) return fromExpress(node.getExpression());
  if (commonjs && isRequireOf(node, "express")) return true;
  if (!Node.isIdentifier(node)) return false;
  const decl = node.getSymbol()?.getDeclarations()?.[0];
  const imported = decl?.getFirstAncestorByKind(SyntaxKind.ImportDeclaration);
  if (imported) return imported.getModuleSpecifierValue() === "express";
  if (!commonjs || !decl) return false;
  const variable = Node.isVariableDeclaration(decl) ? decl : decl.getFirstAncestorByKind(SyntaxKind.VariableDeclaration);
  return Boolean(variable) && isRequireOf(unwrap(variable.getInitializer()), "express");
}

/** The value a file assigns to `module.exports` (its last top-level assignment), or null. */
function moduleExportsValue(sf) {
  let value = null;
  for (const statement of sf.getStatements()) {
    const expr = Node.isExpressionStatement(statement) ? statement.getExpression() : null;
    if (Node.isBinaryExpression(expr) && expr.getOperatorToken().getKind() === SyntaxKind.EqualsToken && expr.getLeft().getText() === "module.exports") {
      value = expr.getRight();
    }
  }
  return value;
}

/** The file a `require("./x")` call loads, when it is a repository file. */
function requiredFile(node) {
  if (!Node.isCallExpression(node)) return null;
  const callee = node.getExpression();
  const arg = node.getArguments()[0];
  if (!Node.isIdentifier(callee) || callee.getText() !== "require" || !Node.isStringLiteral(arg)) return null;
  const decl = arg.getSymbol()?.getDeclarations()?.[0];
  return decl && Node.isSourceFile(decl) ? decl : null;
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
    // app.use("/health", require("./routes/health")): the router that file exports.
    const file = commonjs ? requiredFile(node) : null;
    const value = file && moduleExportsValue(file);
    return value ? routerKey(value, depth + 1) : null;
  }
  if (Node.isIdentifier(node)) {
    const decl = resolved(node);
    if (Node.isVariableDeclaration(decl)) {
      const init = decl.getInitializer();
      return init ? routerKey(init, depth + 1) : null;
    }
    if (Node.isExportAssignment(decl)) return routerKey(decl.getExpression(), depth + 1);
    // const routes = require("./routes"): the module, whose `module.exports = router` is the router.
    if (commonjs && Node.isSourceFile(decl)) {
      const value = moduleExportsValue(decl);
      return value ? routerKey(value, depth + 1) : null;
    }
    if (commonjs && Node.isBinaryExpression(decl)) return routerKey(decl.getRight(), depth + 1);
    return null;
  }
  return null;
}

/**
 * `list.forEach((r) => router.use(r.path, r.route))` over a constant array of object literals:
 * one {child, path} per element, read from the properties the call names. Null when `call` is not
 * such a `use`.
 */
function forEachMounts(call, depth) {
  const [pathArg, childArg] = call.getArguments();
  if (!Node.isPropertyAccessExpression(pathArg) || !Node.isPropertyAccessExpression(childArg)) return null;
  const param = pathArg.getExpression();
  if (!Node.isIdentifier(param) || childArg.getExpression().getText() !== param.getText()) return null;
  const fn = call.getFirstAncestor((a) => Node.isArrowFunction(a) || Node.isFunctionExpression(a));
  const loop = fn?.getParent();
  if (!fn || fn.getParameters()[0]?.getName() !== param.getText() || !Node.isCallExpression(loop)) return null;
  if (loop.getArguments()[0] !== fn) return null; // the callback, not another argument
  const loopCallee = loop.getExpression();
  if (!Node.isPropertyAccessExpression(loopCallee) || loopCallee.getName() !== "forEach") return null;
  const listDecl = resolved(loopCallee.getExpression());
  const list = Node.isVariableDeclaration(listDecl) ? unwrap(listDecl.getInitializer()) : null;
  if (!Node.isArrayLiteralExpression(list)) return null;
  const out = [];
  for (const el of list.getElements()) {
    if (!Node.isObjectLiteralExpression(el)) continue;
    const value = (name) => {
      const prop = el.getProperty(name);
      return Node.isPropertyAssignment(prop) ? unwrap(prop.getInitializer()) : null;
    };
    const path = value(pathArg.getName());
    const child = value(childArg.getName()) && routerKey(value(childArg.getName()), depth + 1);
    if (child && path && (Node.isStringLiteral(path) || Node.isNoSubstitutionTemplateLiteral(path))) {
      out.push({ child, path: path.getLiteralValue() });
    }
  }
  return out;
}

/**
 * The symbol a handler belongs to: the inline function's own, or what a name resolves to. The
 * source file is never one (its start offset is its first statement's).
 */
function handlerSymbol(handler, byDecl) {
  let node = Node.isArrowFunction(handler) || Node.isFunctionExpression(handler) ? handler : resolved(handler);
  for (; node && !Node.isSourceFile(node); node = node.getParent()) {
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
export function detect({ symbols, files = [], deps, commonjs: cjs = false }) {
  const entrypoints = [];
  if (!deps.has("express")) return { entrypoints, matched: 0 };
  commonjs = cjs;
  const byDecl = new Map(symbols.map((s) => [declKey(s.node), s.row]));
  const routes = []; // {router, verb, path, handler}
  const mounts = []; // {parent, child, path}
  for (const sf of files) {
    for (const call of sf.getDescendantsOfKind(SyntaxKind.CallExpression)) {
      const callee = call.getExpression();
      if (!Node.isPropertyAccessExpression(callee)) continue;
      const name = callee.getName();
      if (name !== "use" && !HTTP_VERBS.includes(name)) continue;
      const args = call.getArguments();
      const router = routerKey(callee.getExpression());
      if (!router) {
        // router.route("/:id").get(h).delete(m, h): one route per verb, at route()'s path.
        const chain = name === "use" ? null : routeChain(callee.getExpression());
        const chained = chain && routerKey(chain.router);
        if (chained && args.length >= 1) routes.push({ router: chained, verb: name, path: chain.path, handler: unwrap(args.at(-1)) });
        continue;
      }
      const first = unwrap(args[0]);
      const path = first && (Node.isStringLiteral(first) || Node.isNoSubstitutionTemplateLiteral(first)) ? first.getLiteralValue() : null;
      if (name === "use") {
        const listed = commonjs && path === null ? forEachMounts(call, 0) : null;
        for (const m of listed ?? []) if (m.child !== router) mounts.push({ parent: router, child: m.child, path: m.path });
        if (listed?.length) continue;
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
