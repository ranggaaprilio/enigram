import { Node } from "ts-morph";
import { bodyHash } from "./hash.mjs";
import { relPath } from "./project.mjs";

const MAX_SIGNATURE_CHARS = 300;
const MAX_DECORATOR_CHARS = 200;

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
 * `export default` expressions.
 * @returns {{row: object, node: import("ts-morph").Node}[]}
 */
export function fileSymbols(root, sf) {
  const file = relPath(root, sf.getFilePath());
  const out = [];
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
    const exported = cls.isExported();
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
    add(fn, fn.getName() ?? "default", "function", fn.isExported() || fn.isDefaultExport());
  }
  for (const statement of sf.getVariableStatements()) {
    for (const decl of statement.getDeclarations()) {
      if (!Node.isIdentifier(decl.getNameNode())) continue; // destructuring: no single name
      add(decl, decl.getName(), "variable", statement.isExported());
    }
  }
  for (const assignment of sf.getExportAssignments()) {
    if (assignment.isExportEquals()) continue;
    add(assignment, "default", "variable", true);
  }
  return out;
}
