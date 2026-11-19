import { Node, SyntaxKind } from "ts-morph";
import { bodyHash } from "./hash.mjs";
import { relPath } from "./project.mjs";

const MAX_SIGNATURE_CHARS = 300;
const MAX_DECORATOR_CHARS = 200;
// Express route registrations whose handler is written inline get a symbol of their own.
export const HTTP_VERBS = ["get", "post", "put", "patch", "delete", "options", "head", "all"];

/** Map key of a declaration node: its file and start offset. */
export function declKey(node) {
  return `${node.getSourceFile().getFilePath()}:${node.getStart()}`;
}

function decoratorNames(node) {
  return Node.isDecoratable(node) ? node.getDecorators().map((d) => d.getName()) : [];
}

/** Each decorator's source text on one line, clipped: `@UseGuards(AuthGuard('jwt'), RolesGuard)`. */
function decoratorTexts(node) {
  return Node.isDecoratable(node)
    ? node.getDecorators().map((d) => d.getText().replace(/\s+/g, " ").slice(0, MAX_DECORATOR_CHARS))
    : [];
}

function headLine(node) {
  const text = node.getText();
  const brace = text.indexOf("{");
  const head = (brace >= 0 ? text.slice(0, brace) : text.split("\n")[0]).replace(/\s+/g, " ").trim();
  return head.slice(0, MAX_SIGNATURE_CHARS);
}

/** `name(a: T, b?: U): R` without parameter decorators, or the declaration head. */
function signature(node, name) {
  if (!Node.isParametered(node)) return headLine(node);
  const params = node.getParameters().map((p) => {
    const type = p.getTypeNode();
    const rest = p.isRestParameter() ? "..." : "";
    const optional = p.hasQuestionToken() ? "?" : "";
    return `${rest}${p.getNameNode().getText()}${optional}${type ? `: ${type.getText()}` : ""}`;
  });
  const ret = Node.isReturnTyped(node) ? node.getReturnTypeNode() : undefined;
  const text = `${name}(${params.join(", ")})${ret ? `: ${ret.getText()}` : ""}`;
  return text.replace(/\s+/g, " ").slice(0, MAX_SIGNATURE_CHARS);
}

/**
 * Declarations of one file as symbol rows: classes and their methods, constructors and accessors,
 * interfaces and their method signatures, enums, type aliases, functions, top-level variables and
 * `export default` expressions. A class property whose value is a function (`getUsers = async
 * (req, res) => {...}`) is a method. With `express`, an inline handler of a top-level route
 * registration (`router.get("/articles", auth, async (req, res) => {...})`) is a function named
 * `GET /articles`, so the route has a flow (the express adapter makes it the entry point).
 * A name in an export list (`export { a, b as c }`) is exported. With `commonjs` (--javascript),
 * a function assigned at the top level (`exports.run = function () {...}`, `module.exports =
 * wrap(async () => {...})`, `Model.prototype.save = function () {...}`) is a function too (see
 * assignedName), and a name `module.exports` / `exports.x` is set to (`module.exports = { a, b: c
 * }`) is exported.
 * @param {{express?: boolean, commonjs?: boolean}} options
 * @returns {{row: object, node: import("ts-morph").Node}[]}
 */
export function fileSymbols(root, sf, { express = false, commonjs = false } = {}) {
  const file = relPath(root, sf.getFilePath());
  const out = [];
  // CommonJS rules are for JavaScript files only, also inside a TypeScript project with allowJs.
  const cjs = commonjs && JS_FILE.test(file);
  const listed = exportedNames(sf, cjs);
  const isExported = (own, name) => own || listed.has(name);
  const seen = new Set();
  const add = (node, qualifiedName, kind, exported, parent = "") => {
    let id = `${file}#${qualifiedName}`;
    // Overloads and get/set pairs share a name; keep ids unique within the file.
    for (let n = 2; seen.has(id); n += 1) id = `${file}#${qualifiedName}~${n}`;
    seen.add(id);
    const short = qualifiedName.split(".").pop();
    out.push({
      node,
      row: {
        id,
        qualified_name: qualifiedName,
        kind,
        file,
        line_start: node.getStartLineNumber(),
        line_end: node.getEndLineNumber(),
        body_hash: bodyHash(node.getText()),
        exported,
        decorators: decoratorNames(node),
        decorators_text: decoratorTexts(node),
        signature: signature(node, short),
        body: node.getText(),
        parent,
      },
    });
  };

  for (const cls of sf.getClasses()) {
    const name = cls.getName() ?? "default";
    const exported = isExported(cls.isExported(), name);
    add(cls, name, "class", exported);
    for (const ctor of cls.getConstructors()) {
      if (ctor.isOverload()) continue;
      add(ctor, `${name}.constructor`, "constructor", exported, name);
    }
    for (const method of cls.getMethods()) {
      // ts-morph calls every method without a body an overload, `abstract foo(): T;` included.
      // An abstract method is the only declaration a call through the abstract class finds, so it
      // must be a symbol; abstract overloads after the first would repeat its id.
      if (method.isOverload() && !(method.isAbstract() && method === cls.getMethod(method.getName()))) {
        continue;
      }
      add(method, `${name}.${method.getName()}`, "method", exported, name);
    }
    for (const accessor of [...cls.getGetAccessors(), ...cls.getSetAccessors()]) {
      add(accessor, `${name}.${accessor.getName()}`, "accessor", exported, name);
    }
    for (const prop of cls.getProperties()) {
      const init = prop.getInitializer();
      if (init && (Node.isArrowFunction(init) || Node.isFunctionExpression(init))) {
        add(prop, `${name}.${prop.getName()}`, "method", exported, name);
      }
    }
  }
  for (const iface of sf.getInterfaces()) {
    const name = iface.getName();
    add(iface, name, "interface", iface.isExported());
    // Method signatures are symbols so a call through an interface has a target; the
    // summariser follows `implements` edges from there.
    for (const method of iface.getMethods()) {
      add(method, `${name}.${method.getName()}`, "method", iface.isExported(), name);
    }
  }
  for (const en of sf.getEnums()) add(en, en.getName(), "enum", en.isExported());
  for (const alias of sf.getTypeAliases()) add(alias, alias.getName(), "type", alias.isExported());
  for (const fn of sf.getFunctions()) {
    if (fn.isOverload()) continue;
    const name = fn.getName() ?? "default";
    add(fn, name, "function", isExported(fn.isExported() || fn.isDefaultExport(), name));
  }
  for (const statement of sf.getVariableStatements()) {
    for (const decl of statement.getDeclarations()) {
      if (!Node.isIdentifier(decl.getNameNode())) continue; // destructuring: no single name
      add(decl, decl.getName(), "variable", isExported(statement.isExported(), decl.getName()));
    }
  }
  for (const assignment of sf.getExportAssignments()) {
    if (assignment.isExportEquals()) continue;
    add(assignment, "default", "variable", true);
  }
  for (const statement of cjs ? sf.getStatements() : []) {
    const assigned = assignedFunction(statement);
    // A name the file already declares (`function x` beside `exports.x = function`) keeps its row.
    if (!assigned || out.some((s) => s.row.qualified_name === assigned.name)) continue;
    add(assigned.node, assigned.name, "function", assigned.exported);
  }
  if (express) {
    for (const { handler, verb, path } of inlineRouteHandlers(sf)) {
      add(handler, `${verb.toUpperCase()} ${path}`, "function", false);
    }
  }
  return out;
}

const JS_FILE = /\.(js|jsx|mjs|cjs)$/;
const isFunctionNode = (n) => Boolean(n) && (Node.isArrowFunction(n) || Node.isFunctionExpression(n));

/** `x = ...` at the top level: the assignment, or null. */
function topLevelAssignment(statement) {
  if (!Node.isExpressionStatement(statement)) return null;
  const expr = statement.getExpression();
  return Node.isBinaryExpression(expr) && expr.getOperatorToken().getKind() === SyntaxKind.EqualsToken ? expr : null;
}

/**
 * The symbol name of an assignment target, and whether it is exported: `module.exports` →
 * `default`; `module.exports.x` / `exports.x` → `x`; `Foo.prototype.x` → `Foo.x`; anything else
 * (`schema.methods.x`) its own text.
 */
export function assignedName(left) {
  const text = left.getText().replace(/\s+/g, "");
  if (text === "module.exports") return { name: "default", exported: true };
  const member = /^(?:module\.exports|exports)\.(.+)$/.exec(text);
  if (member) return { name: member[1], exported: true };
  const proto = /^(.+)\.prototype\.([^.]+)$/.exec(text);
  if (proto) return { name: `${proto[1]}.${proto[2]}`, exported: false };
  return { name: text, exported: false };
}

/**
 * A top-level assignment to a member (`exports.x`, `Foo.prototype.y`, `api.get`) whose value is a
 * function, or a call with a function argument (`wrap(async () => {...})`): {node, name,
 * exported}, the node being the assignment. Null otherwise; an assignment to a plain variable
 * (`handler = () => {}`) is not one, the variable's declaration is already a symbol.
 */
function assignedFunction(statement) {
  const assignment = topLevelAssignment(statement);
  if (!assignment || !Node.isPropertyAccessExpression(assignment.getLeft())) return null;
  const right = assignment.getRight();
  const fn = isFunctionNode(right) || (Node.isCallExpression(right) && right.getArguments().some(isFunctionNode));
  if (!fn) return null;
  return { node: assignment, ...assignedName(assignment.getLeft()) };
}

/**
 * Local names a file exports without an `export` keyword on the declaration: `export { a, b as c }`
 * (without `from`), and, with `commonjs`, the identifiers `module.exports` / `module.exports.x` /
 * `exports.x` are set to (`= a`, `= { a, b: c }`).
 */
function exportedNames(sf, commonjs) {
  const names = new Set();
  for (const decl of sf.getExportDeclarations()) {
    if (decl.getModuleSpecifier()) continue; // a re-export names another file's declarations
    for (const spec of decl.getNamedExports()) names.add(spec.getNameNode().getText());
  }
  for (const statement of commonjs ? sf.getStatements() : []) {
    const assignment = topLevelAssignment(statement);
    if (!assignment || !/^(module\.exports|exports)\b/.test(assignment.getLeft().getText())) continue;
    const right = assignment.getRight();
    if (Node.isIdentifier(right)) names.add(right.getText());
    if (!Node.isObjectLiteralExpression(right)) continue;
    for (const prop of right.getProperties()) {
      if (Node.isShorthandPropertyAssignment(prop)) names.add(prop.getName());
      else if (Node.isPropertyAssignment(prop) && Node.isIdentifier(prop.getInitializer())) {
        names.add(prop.getInitializer().getText());
      }
    }
  }
  return names;
}

/** A string literal's value, or null. */
function pathLiteral(node) {
  return node && (Node.isStringLiteral(node) || Node.isNoSubstitutionTemplateLiteral(node))
    ? node.getLiteralValue()
    : null;
}

/**
 * Route registrations at the top level of a file (`x.get("/p", ..., (req, res) => {...})`, also
 * inside a `.use(...).get(...)` chain) whose last argument is a function written inline.
 * @returns {{handler: import("ts-morph").Node, verb: string, path: string, call: import("ts-morph").CallExpression}[]}
 */
export function inlineRouteHandlers(sf) {
  const out = [];
  for (const statement of sf.getStatements()) {
    if (!Node.isExpressionStatement(statement) && !Node.isExportAssignment(statement)) continue;
    const calls = [statement.getExpression(), ...statement.getExpression().getDescendantsOfKind(SyntaxKind.CallExpression)];
    for (const call of calls) {
      if (!Node.isCallExpression(call)) continue;
      const callee = call.getExpression();
      if (!Node.isPropertyAccessExpression(callee) || !HTTP_VERBS.includes(callee.getName())) continue;
      const args = call.getArguments();
      const path = pathLiteral(args[0]);
      const handler = args.at(-1);
      if (path === null || args.length < 2 || !(Node.isArrowFunction(handler) || Node.isFunctionExpression(handler))) continue;
      out.push({ handler, verb: callee.getName(), path, call });
    }
  }
  return out;
}
