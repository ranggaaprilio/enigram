import { Node } from "ts-morph";

const HTTP = { Get: "GET", Post: "POST", Put: "PUT", Patch: "PATCH", Delete: "DELETE", Options: "OPTIONS", Head: "HEAD", All: "ALL" };
const EVENTS = new Set(["EventPattern", "MessagePattern"]);

function literal(node) {
  if (!node) return null;
  if (Node.isStringLiteral(node) || Node.isNoSubstitutionTemplateLiteral(node)) return node.getLiteralValue();
  if (Node.isArrayLiteralExpression(node)) return literal(node.getElements()[0]);
  return null;
}

function property(obj, name) {
  const prop = obj.getProperty(name);
  return prop && Node.isPropertyAssignment(prop) ? prop.getInitializer() : undefined;
}

/** `@Controller('users')` or `@Controller({ path: 'auth', version: '1' })` → {path, version}. */
function controllerPath(decorator) {
  const arg = decorator.getArguments()[0];
  if (!arg) return { path: "", version: "" };
  if (Node.isObjectLiteralExpression(arg)) {
    return { path: literal(property(arg, "path")) ?? "", version: literal(property(arg, "version")) ?? "" };
  }
  return { path: literal(arg) ?? "", version: "" };
}

/** "/a//b/" + ":id" → "/a/b/:id"; the root stays "/". */
export function joinRoute(...parts) {
  const joined = `/${parts.filter(Boolean).join("/")}`.replace(/\/+/g, "/");
  return joined.length > 1 ? joined.replace(/\/$/, "") : joined;
}

/** Text of the first decorator argument: its literal value, else its source text. */
function argText(decorator) {
  const arg = decorator.getArguments()[0];
  return arg ? (literal(arg) ?? arg.getText()) : "";
}

/**
 * Nest entry points: routes, @Cron, @EventPattern/@MessagePattern, @Process in a @Processor and
 * bootstrap()/main() in src/main.ts. Reads decorators syntactically: no @nestjs types needed.
 * @param {{symbols: {row: object, node: import("ts-morph").Node}[]}} input
 */
export function detect({ symbols }) {
  const entrypoints = [];
  const classes = new Map(symbols.filter((s) => s.row.kind === "class").map((s) => [s.row.id, s.node]));
  for (const { row, node } of symbols) {
    if (row.kind === "function" && row.file === "src/main.ts" && ["bootstrap", "main"].includes(row.qualified_name)) {
      entrypoints.push({ symbol: row.id, kind: "cli_main", route: row.qualified_name, framework: "nest", version: "" });
      continue;
    }
    if (row.kind !== "method" || !Node.isMethodDeclaration(node)) continue;
    const cls = classes.get(`${row.file}#${row.parent}`);
    if (!cls) continue;
    const controller = cls.getDecorator("Controller");
    const processor = cls.getDecorator("Processor");
    for (const decorator of node.getDecorators()) {
      const name = decorator.getName();
      if (controller && HTTP[name]) {
        const { path, version } = controllerPath(controller);
        const route = `${HTTP[name]} ${joinRoute(path, literal(decorator.getArguments()[0]) ?? "")}`;
        entrypoints.push({ symbol: row.id, kind: "http_route", route, framework: "nest", version });
      } else if (name === "Cron") {
        entrypoints.push({ symbol: row.id, kind: "cron", route: argText(decorator), framework: "nest", version: "" });
      } else if (EVENTS.has(name)) {
        entrypoints.push({ symbol: row.id, kind: "event", route: argText(decorator), framework: "nest", version: "" });
      } else if (name === "Process" && processor) {
        const job = [argText(processor), argText(decorator)].filter(Boolean).join(":");
        entrypoints.push({ symbol: row.id, kind: "job", route: job, framework: "nest", version: "" });
      }
    }
  }
  return { entrypoints, matched: entrypoints.length };
}
