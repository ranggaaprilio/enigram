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
 * Edge-building context: which declarations are symbols, which files are analysed. A file counts
 * as analysed when it has a symbol, or when it is one of `alsoFiles` (the JavaScript projects'
 * files, so an import of a barrel `index.js` with no symbol of its own still resolves). With
 * `commonjs` (--javascript), calls through `module.exports` objects and `require()` imports are
 * followed (exportedValue, importEdges).
 * @param {string} root
 * @param {{row: object, node: import("ts-morph").Node}[]} symbols
 * @param {string[]} alsoFiles absolute paths
 * @param {{commonjs?: boolean}} options
 */
export function edgeContext(root, symbols, alsoFiles = [], { commonjs = false } = {}) {
  const byDecl = new Map(symbols.map((s) => [declKey(s.node), s.row.id]));
  const files = new Set([...symbols.map((s) => s.node.getSourceFile().getFilePath()), ...alsoFiles]);
  return { root, byDecl, files, commonjs };
}

/**
 * The symbol id a declaration belongs to: itself, or the nearest enclosing symbol. The source
 * file is never one: its start offset is its first statement's, which may be a symbol.
 */
function owningSymbol(ctx, decl) {
  for (let node = decl; node && !Node.isSourceFile(node); node = node.getParent()) {
    const id = ctx.byDecl.get(declKey(node));
    if (id) return { id, exact: node === decl };
  }
  return null;
}

const EXPORTS_TARGET = /^(module\.exports|exports)\b/;

/**
 * A member of an object a file assigns to `module.exports` / `exports.x` (`module.exports = {
 * list, helperAlias: helper }`) stands for the value it names: the symbol of `list` / `helper`.
 * Any other symbol is returned as it is.
 */
function exportedValue(sym) {
  for (let i = 0; sym && i < 5; i += 1) {
    const decl = sym.getDeclarations()[0];
    const object = decl?.getParent();
    const assignment = object?.getParent();
    const viaExports =
      Node.isObjectLiteralExpression(object) &&
      Node.isBinaryExpression(assignment) &&
      EXPORTS_TARGET.test(assignment.getLeft().getText());
    if (!viaExports) return sym;
    let next;
    if (Node.isShorthandPropertyAssignment(decl)) {
      next = decl.getProject().getTypeChecker().getShorthandAssignmentValueSymbol(decl);
    } else if (Node.isPropertyAssignment(decl) && Node.isIdentifier(decl.getInitializer())) {
      next = decl.getInitializer().getSymbol();
    }
    if (next?.isAlias()) next = next.getAliasedSymbol() ?? next;
    if (!next) return sym;
    sym = next;
  }
  return sym;
}

/**
 * Where a name resolves to: {id} for a repository symbol, {local: true} for a parameter or local
 * of the caller itself, {declared: true} for a declaration outside the analysed files (TypeScript's
 * lib, a skipped file), or {} when nothing resolves (typically a package without node_modules).
 */
function target(ctx, node, callerId) {
  const resolved = resolveSymbol(node);
  const sym = ctx.commonjs ? exportedValue(resolved) : resolved;
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
  // <Widget /> renders (calls) the Widget component. Only repository components are edges: an
  // intrinsic tag (<div>) or a package component would be one calls_external per element.
  const elements = [
    ...node.getDescendantsOfKind(SyntaxKind.JsxOpeningElement),
    ...node.getDescendantsOfKind(SyntaxKind.JsxSelfClosingElement),
  ];
  for (const element of elements) {
    const tag = element.getTagNameNode();
    if (/^[a-z]/.test(tag.getText())) continue;
    const to = target(ctx, tag, row.id);
    if (to.id) {
      edges.push({ src: row.id, dst: to.id, kind: "calls", src_file: file, line: element.getStartLineNumber(), resolved: true });
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

const JS_SOURCE = /\.(js|jsx|mjs|cjs)$/;

/**
 * `imports` rows: every import declaration, and in a JavaScript file also every `require("x")` and
 * `import("x")` with a literal specifier (TypeScript files keep their rows as before).
 */
function importEdges(ctx, sf, edges) {
  const file = relPath(ctx.root, sf.getFilePath());
  const push = (specifier, resolved, line) => {
    const inRepo = resolved && ctx.files.has(resolved.getFilePath());
    edges.push({
      src: file, dst: inRepo ? relPath(ctx.root, resolved.getFilePath()) : specifier,
      kind: "imports", src_file: file, line, resolved: Boolean(inRepo),
    });
  };
  for (const imp of sf.getImportDeclarations()) {
    push(imp.getModuleSpecifierValue(), imp.getModuleSpecifierSourceFile(), imp.getStartLineNumber());
  }
  if (!ctx.commonjs || !JS_SOURCE.test(sf.getFilePath())) return;
  for (const call of sf.getDescendantsOfKind(SyntaxKind.CallExpression)) {
    const callee = call.getExpression();
    const isRequire = Node.isIdentifier(callee) && callee.getText() === "require";
    if (!isRequire && callee.getKind() !== SyntaxKind.ImportKeyword) continue;
    const arg = call.getArguments()[0];
    const specifier = arg ? literal(arg) : null;
    if (specifier === null) continue;
    const decl = arg.getSymbol()?.getDeclarations()?.[0];
    push(specifier, decl && Node.isSourceFile(decl) ? decl : undefined, call.getStartLineNumber());
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
