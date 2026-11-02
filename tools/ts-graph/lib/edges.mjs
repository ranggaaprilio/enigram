import { Node, SyntaxKind } from "ts-morph";
import { relPath } from "./project.mjs";
import { declKey } from "./symbols.mjs";

const MAX_EXTERNAL_TEXT = 120;
const FUNCTION_KINDS = new Set(["function", "method", "constructor", "accessor", "variable"]);
const CONFIG_GETTERS = new Set(["get", "getOrThrow"]);
const GUARD_DECORATORS = { UseGuards: "guarded_by", UseInterceptors: "intercepted_by" };
const MODULE_LISTS = [["imports", "module_imports"], ["providers", "provides"]];
const CONFIG_KINDS = new Set(["function", "variable"]);

function resolveSymbol(node) {
  let sym = node.getSymbol();
  if (sym?.isAlias()) sym = sym.getAliasedSymbol() ?? sym;
  return sym;
}

/**
 * Edge-building context: which declarations are symbols, which files are analysed.
 * @param {string} root
 * @param {{row: object, node: import("ts-morph").Node}[]} symbols
 */
export function edgeContext(root, symbols) {
  const byDecl = new Map(symbols.map((s) => [declKey(s.node), s.row.id]));
  const files = new Set(symbols.map((s) => s.node.getSourceFile().getFilePath()));
  return { root, byDecl, files };
}

/** The symbol id a declaration belongs to: itself, or the nearest enclosing symbol. */
function owningSymbol(ctx, decl) {
  for (let node = decl; node; node = node.getParent()) {
    const id = ctx.byDecl.get(declKey(node));
    if (id) return { id, exact: node === decl };
  }
  return null;
}

/**
 * Where a name resolves to: {id} for a repository symbol, {local: true} for a parameter or local
 * of the caller itself, {declared: true} for a declaration outside the analysed files (TypeScript's
 * lib, a skipped file), or {} when nothing resolves (typically a package without node_modules).
 */
function target(ctx, node, callerId) {
  const sym = resolveSymbol(node);
  const decls = sym?.getDeclarations() ?? [];
  if (!decls.length) return {};
  for (const decl of decls) {
    if (!ctx.files.has(decl.getSourceFile().getFilePath())) continue;
    const owner = owningSymbol(ctx, decl);
    if (!owner) continue;
    if (owner.id === callerId && !owner.exact) return { local: true };
    return { id: owner.id };
  }
  return { declared: true };
}

function isInDecorator(node, stop) {
  for (let n = node.getParent(); n && n !== stop; n = n.getParent()) {
    if (n.getKind() === SyntaxKind.Decorator) return true;
  }
  return false;
}

function literal(node) {
  if (Node.isStringLiteral(node) || Node.isNoSubstitutionTemplateLiteral(node)) {
    return node.getLiteralValue();
  }
  return null;
}

function property(obj, name) {
  const prop = obj.getProperty(name);
  return prop && Node.isPropertyAssignment(prop) ? prop.getInitializer() : undefined;
}

/** Identifiers a decorator or module list entry refers to: X, X.forRoot(...), c ? A : B, ...xs. */
function moduleRefs(expr) {
  if (!expr) return [];
  if (Node.isIdentifier(expr)) return [expr];
  if (Node.isCallExpression(expr) || Node.isPropertyAccessExpression(expr)) return moduleRefs(expr.getExpression());
  if (Node.isConditionalExpression(expr)) return [...moduleRefs(expr.getWhenTrue()), ...moduleRefs(expr.getWhenFalse())];
  if (Node.isSpreadElement(expr) || Node.isParenthesizedExpression(expr)) return moduleRefs(expr.getExpression());
  return [];
}

function refEdge(ctx, row, file, kind, ref) {
  const to = target(ctx, ref, row.id);
  return {
    src: row.id, dst: to.id ?? ref.getText().slice(0, MAX_EXTERNAL_TEXT), kind, src_file: file,
    line: ref.getStartLineNumber(), resolved: Boolean(to.id),
  };
}

/** @UseGuards(AuthGuard('jwt'), RolesGuard) / @UseInterceptors(...) on classes and methods. */
function guardEdges(ctx, sym, file, edges) {
  if (!Node.isDecoratable(sym.node)) return;
  for (const decorator of sym.node.getDecorators()) {
    const kind = GUARD_DECORATORS[decorator.getName()];
    if (!kind) continue;
    for (const arg of decorator.getArguments()) {
      for (const ref of moduleRefs(arg)) edges.push(refEdge(ctx, sym.row, file, kind, ref));
    }
  }
}

/** @Module({ imports: [...], providers: [...] }); `{ provide, useClass }` points at the class. */
function moduleEdges(ctx, sym, file, edges) {
  if (!Node.isClassDeclaration(sym.node)) return;
  const arg = sym.node.getDecorator("Module")?.getArguments()[0];
  if (!arg || !Node.isObjectLiteralExpression(arg)) return;
  for (const [prop, kind] of MODULE_LISTS) {
    const init = property(arg, prop);
    if (!init || !Node.isArrayLiteralExpression(init)) continue;
    for (const el of init.getElements()) {
      const expr = Node.isObjectLiteralExpression(el) ? (property(el, "useClass") ?? property(el, "useExisting")) : el;
      for (const ref of moduleRefs(expr)) edges.push(refEdge(ctx, sym.row, file, kind, ref));
    }
  }
}

/** registerAs('auth', () => ...) → provides_config "auth" (only on functions and variables). */
function configEdges(sym, file, edges) {
  if (!CONFIG_KINDS.has(sym.row.kind)) return;
  for (const call of sym.node.getDescendantsOfKind(SyntaxKind.CallExpression)) {
    const ns = call.getArguments()[0];
    if (call.getExpression().getText() === "registerAs" && ns && literal(ns) !== null) {
      edges.push({ src: sym.row.id, dst: literal(ns), kind: "provides_config", src_file: file, line: call.getStartLineNumber(), resolved: true });
    }
  }
}

/** Whether `receiver` (in `receiver.get("x")`) is a ConfigService, by its type or declared type. */
function isConfigService(receiver) {
  const decl = resolveSymbol(receiver)?.getValueDeclaration();
  // The declared type text works without @nestjs/config installed; the checked type with it.
  const declared = decl && Node.isTyped(decl) ? (decl.getTypeNode()?.getText() ?? "") : "";
  return [declared, receiver.getType().getText(receiver)].some((t) => /ConfigService\b/.test(t));
}

function callEdges(ctx, sym, file, edges, counts) {
  const { row, node } = sym;
  const calls = [
    ...node.getDescendantsOfKind(SyntaxKind.CallExpression),
    ...node.getDescendantsOfKind(SyntaxKind.NewExpression),
  ];
  for (const call of calls) {
    if (isInDecorator(call, node)) continue; // @Post('x'), @Body(): metadata, not behaviour
    const callee = call.getExpression();
    const line = call.getStartLineNumber();
    const to = target(ctx, callee, row.id);
    if (to.local) {
      counts.local += 1;
    } else if (to.id) {
      edges.push({ src: row.id, dst: to.id, kind: "calls", src_file: file, line, resolved: true });
    } else {
      const text = callee.getText().replace(/\s+/g, " ").slice(0, MAX_EXTERNAL_TEXT);
      edges.push({
        src: row.id, dst: text, kind: "calls_external", src_file: file, line,
        resolved: Boolean(to.declared),
      });
    }
    // configService.get("db.type") and friends
    if (Node.isCallExpression(call) && Node.isPropertyAccessExpression(callee)) {
      const key = call.getArguments()[0];
      if (CONFIG_GETTERS.has(callee.getName()) && key && literal(key) !== null && isConfigService(callee.getExpression())) {
        edges.push({ src: row.id, dst: literal(key), kind: "reads_env", src_file: file, line, resolved: true });
      }
    }
  }
}

function envEdges(sym, file, edges) {
  const { row, node } = sym;
  for (const access of node.getDescendantsOfKind(SyntaxKind.PropertyAccessExpression)) {
    if (access.getExpression().getText() === "process.env") {
      edges.push({ src: row.id, dst: access.getName(), kind: "reads_env", src_file: file, line: access.getStartLineNumber(), resolved: true });
    }
  }
  for (const access of node.getDescendantsOfKind(SyntaxKind.ElementAccessExpression)) {
    const key = access.getArgumentExpression();
    if (access.getExpression().getText() === "process.env" && key && literal(key) !== null) {
      edges.push({ src: row.id, dst: literal(key), kind: "reads_env", src_file: file, line: access.getStartLineNumber(), resolved: true });
    }
  }
}

function heritageEdges(ctx, sym, file, edges) {
  const { row, node } = sym;
  const clauses = [];
  if (Node.isClassDeclaration(node)) {
    if (node.getExtends()) clauses.push(["extends", node.getExtends()]);
    for (const impl of node.getImplements()) clauses.push(["implements", impl]);
  } else if (Node.isInterfaceDeclaration(node)) {
    for (const ext of node.getExtends()) clauses.push(["extends", ext]);
  }
  for (const [kind, clause] of clauses) {
    const to = target(ctx, clause.getExpression(), row.id);
    edges.push({
      src: row.id, dst: to.id ?? clause.getExpression().getText(), kind, src_file: file,
      line: clause.getStartLineNumber(), resolved: Boolean(to.id),
    });
  }
}

function injectEdges(ctx, sym, file, edges) {
  const { row, node } = sym;
  if (!Node.isConstructorDeclaration(node)) return;
  const owner = row.parent ? `${file}#${row.parent}` : row.id;
  for (const param of node.getParameters()) {
    const typeNode = param.getTypeNode();
    if (!typeNode || !Node.isTypeReference(typeNode)) continue;
    const to = target(ctx, typeNode.getTypeName(), row.id);
    if (to.id) {
      edges.push({ src: owner, dst: to.id, kind: "injects", src_file: file, line: param.getStartLineNumber(), resolved: true });
    }
  }
}

function importEdges(ctx, sf, edges) {
  const file = relPath(ctx.root, sf.getFilePath());
  for (const imp of sf.getImportDeclarations()) {
    const resolved = imp.getModuleSpecifierSourceFile();
    const inRepo = resolved && ctx.files.has(resolved.getFilePath());
    edges.push({
      src: file, dst: inRepo ? relPath(ctx.root, resolved.getFilePath()) : imp.getModuleSpecifierValue(),
      kind: "imports", src_file: file, line: imp.getStartLineNumber(), resolved: Boolean(inRepo),
    });
  }
}

/**
 * calls / calls_external / injects / extends / implements / imports / reads_env /
 * guarded_by / intercepted_by / module_imports / provides / provides_config rows.
 * @returns {{edges: object[], local: number}} local = calls to the caller's own parameters/locals
 */
export function buildEdges(ctx, symbols, files) {
  const edges = [];
  const counts = { local: 0 };
  for (const sym of symbols) {
    const file = sym.row.file;
    if (FUNCTION_KINDS.has(sym.row.kind)) {
      callEdges(ctx, sym, file, edges, counts);
      envEdges(sym, file, edges);
    }
    heritageEdges(ctx, sym, file, edges);
    injectEdges(ctx, sym, file, edges);
    guardEdges(ctx, sym, file, edges);
    moduleEdges(ctx, sym, file, edges);
    configEdges(sym, file, edges);
  }
  for (const sf of files) importEdges(ctx, sf, edges);
  return { edges, local: counts.local };
}
