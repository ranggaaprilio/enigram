import { Node } from "ts-morph";

/** A string literal's value; the first element of an array literal; else null. */
export function literal(node) {
  if (!node) return null;
  if (Node.isStringLiteral(node) || Node.isNoSubstitutionTemplateLiteral(node)) return node.getLiteralValue();
  if (Node.isArrayLiteralExpression(node)) return literal(node.getElements()[0]);
  return null;
}

export function property(obj, name) {
  const prop = obj.getProperty(name);
  return prop && Node.isPropertyAssignment(prop) ? prop.getInitializer() : undefined;
}

/** "/a//b/" + ":id" → "/a/b/:id"; the root stays "/". */
export function joinRoute(...parts) {
  const joined = `/${parts.filter(Boolean).join("/")}`.replace(/\/+/g, "/");
  return joined.length > 1 ? joined.replace(/\/$/, "") : joined;
}

/** Text of the first decorator argument: its literal value, else its source text. */
export function argText(decorator) {
  const arg = decorator.getArguments()[0];
  return arg ? (literal(arg) ?? arg.getText()) : "";
}

/** A function, or a variable whose initialiser is an arrow function or function expression. */
export function isFunctionValued({ row, node }) {
  if (row.kind === "function") return true;
  if (row.kind !== "variable" || !Node.isVariableDeclaration(node)) return false;
  const init = node.getInitializer();
  return Boolean(init) && (Node.isArrowFunction(init) || Node.isFunctionExpression(init));
}

/**
 * The symbol a file exports by default, from that file's symbols: `export default function F()`
 * or `export default class C` itself; `const F = ...; export default F` → F; any other
 * `export default <expression>` → the `default` symbol, whose body holds the expression.
 * @param {{row: object, node: import("ts-morph").Node}[]} fileSymbols symbols of one file
 */
export function defaultExportSymbol(fileSymbols) {
  for (const sym of fileSymbols) {
    const { node } = sym;
    if ((Node.isFunctionDeclaration(node) || Node.isClassDeclaration(node)) && node.isDefaultExport()) return sym;
  }
  const assignment = fileSymbols.find((s) => s.row.qualified_name === "default" && Node.isExportAssignment(s.node));
  if (!assignment) return null;
  const expr = assignment.node.getExpression();
  if (Node.isIdentifier(expr)) {
    const named = fileSymbols.find((s) => s.row.qualified_name === expr.getText() && !s.row.parent);
    if (named) return named;
  }
  return assignment;
}
