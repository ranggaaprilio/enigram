import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { buildGraph } from "../lib/graph.mjs";
import { joinRoute } from "../lib/entrypoints/nest.mjs";
import { defaultExportSymbol } from "../lib/entrypoints/common.mjs";
import { appRoute } from "../lib/entrypoints/next.mjs";
import { findProjects, openProject } from "../lib/project.mjs";
import { fileSymbols } from "../lib/symbols.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const FIXTURE = path.join(HERE, "fixture");
const graph = buildGraph(FIXTURE);

const symbol = (name) => graph.symbols.find((s) => s.qualified_name === name);
const edge = (kind, src, dst) => graph.edges.find((e) => e.kind === kind && e.src.endsWith(`#${src}`) && (e.dst === dst || e.dst.endsWith(`#${dst}`)));

function copyFixture() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "ts-graph-"));
  fs.cpSync(FIXTURE, dir, { recursive: true });
  return dir;
}

test("finds the route with the controller path joined", () => {
  const routes = graph.entrypoints.filter((e) => e.kind === "http_route");
  assert.deepEqual(routes, [
    { symbol: "src/app.controller.ts#AppController.find", kind: "http_route", route: "GET /users/:id", framework: "nest", version: "" },
  ]);
  assert.deepEqual(graph.report.adapters, { nest: 2 });
  assert.deepEqual(graph.report.unmatched_frameworks, ["express"]);
});

test("bootstrap in src/main.ts is a cli_main entry point", () => {
  assert.ok(graph.entrypoints.some((e) => e.kind === "cli_main" && e.symbol === "src/main.ts#bootstrap"));
});

test("a call through an injected service resolves to its method", () => {
  const e = edge("calls", "AppController.find", "AppService.find");
  assert.ok(e, "AppController.find -> AppService.find");
  assert.equal(e.resolved, true);
  assert.equal(e.src_file, "src/app.controller.ts");
  assert.ok(edge("calls", "AppService.find", "AppService.helper"));
});

test("a call through an interface resolves to the interface member; implements is recorded", () => {
  assert.ok(edge("calls", "AppService.find", "UserRepo.load"));
  assert.equal(symbol("UserRepo.load").kind, "method");
  assert.equal(symbol("UserRepo.load").parent, "UserRepo");
  assert.ok(edge("implements", "SqlUserRepo", "UserRepo"));
  assert.ok(edge("injects", "AppService", "UserRepo"));
  assert.ok(edge("injects", "AppService", "ConfigService"));
});

test("a package call without node_modules is an unresolved calls_external", () => {
  const e = graph.edges.find((x) => x.kind === "calls_external" && x.dst === "bcrypt.compare");
  assert.ok(e);
  assert.equal(e.resolved, false);
  // Number(...) is declared in TypeScript's lib: external but resolved.
  assert.equal(graph.edges.find((x) => x.dst === "Number").resolved, true);
  // Decorator calls are metadata, not behaviour.
  assert.ok(!graph.edges.some((x) => ["Get", "Param", "Controller", "UseGuards", "Roles"].includes(x.dst)));
});

test("env and config reads", () => {
  assert.ok(edge("reads_env", "AppService.find", "LIMIT"));
  assert.ok(edge("reads_env", "AppService.find", "db.type"));
});

test("imports are file to file inside the repository, bare specifiers outside", () => {
  assert.ok(graph.edges.some((e) => e.kind === "imports" && e.src === "src/app.service.ts" && e.dst === "src/repo.ts" && e.resolved));
  assert.ok(graph.edges.some((e) => e.kind === "imports" && e.dst === "bcrypt" && !e.resolved));
});

test("spec files are skipped and counted", () => {
  assert.ok(!graph.symbols.some((s) => s.file.endsWith(".spec.ts")));
  assert.equal(graph.report.files_skipped.test, 1);
  assert.equal(graph.report.files, 13);
});

test("symbol rows carry lines, decorators, signature and body", () => {
  const find = symbol("AppController.find");
  assert.deepEqual([find.line_start, find.line_end], [13, 16]);
  assert.deepEqual(find.decorators, ["Get"]);
  assert.equal(find.signature, "find(id: string): string");
  assert.ok(find.body.startsWith("@Get(':id')"));
  assert.equal(find.exported, true);
  assert.equal(symbol("AppService.helper").exported, true); // the parent class is exported
  assert.match(find.body_hash, /^[0-9a-f]{64}$/);
});

test("body_hash ignores whitespace and comments but not literals", () => {
  const dir = copyFixture();
  const file = path.join(dir, "src", "app.service.ts");
  const original = fs.readFileSync(file, "utf8");
  fs.writeFileSync(file, original.replace("return 42;", "// the answer\n    return   42 ; /* inline */"));
  const reformatted = buildGraph(dir, { diagnostics: false }).symbols.find((s) => s.qualified_name === "AppService.helper");
  assert.equal(reformatted.body_hash, symbol("AppService.helper").body_hash);
  fs.writeFileSync(file, original.replace("return 42;", "return 43;"));
  const changed = buildGraph(dir, { diagnostics: false }).symbols.find((s) => s.qualified_name === "AppService.helper");
  assert.notEqual(changed.body_hash, symbol("AppService.helper").body_hash);
  fs.rmSync(dir, { recursive: true });
});

test("non-TypeScript files such as .env are never read", () => {
  const dir = copyFixture();
  fs.writeFileSync(path.join(dir, ".env"), "SECRET_TOKEN=do-not-leak-1234567890\n");
  const copy = buildGraph(dir, { diagnostics: false });
  assert.ok(!JSON.stringify(copy).includes("do-not-leak"));
  fs.rmSync(dir, { recursive: true });
});

test("joinRoute collapses slashes", () => {
  assert.equal(joinRoute("auth", "email/login"), "/auth/email/login");
  assert.equal(joinRoute("/users/", "/"), "/users");
  assert.equal(joinRoute("", ""), "/");
});

test("the CLI writes the three JSONL files and report.json", () => {
  const out = fs.mkdtempSync(path.join(os.tmpdir(), "ts-graph-out-"));
  const stdout = execFileSync(process.execPath, [path.join(HERE, "..", "index.mjs"), "--repo", FIXTURE, "--out", out], { encoding: "utf8" });
  assert.equal(JSON.parse(stdout).symbols, graph.symbols.length);
  for (const name of ["symbols.jsonl", "edges.jsonl", "entrypoints.jsonl", "report.json"]) {
    assert.ok(fs.existsSync(path.join(out, name)), name);
  }
  const rows = fs.readFileSync(path.join(out, "symbols.jsonl"), "utf8").trim().split("\n").map((l) => JSON.parse(l));
  assert.equal(rows.length, graph.symbols.length);
  fs.rmSync(out, { recursive: true });
});

test("the CLI exits 2 without a tsconfig.json", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "ts-graph-empty-"));
  assert.throws(
    () => execFileSync(process.execPath, [path.join(HERE, "..", "index.mjs"), "--repo", dir, "--out", dir], { stdio: "pipe" }),
    (e) => e.status === 2 && /tsconfig\.json/.test(e.stderr.toString()),
  );
  fs.rmSync(dir, { recursive: true });
});

test("an abstract class method is a symbol, so a call through the abstract class resolves to it", () => {
  assert.equal(symbol("SessionRepo.findById").parent, "SessionRepo");
  assert.ok(edge("calls", "SessionService.get", "SessionRepo.findById"));
  assert.ok(edge("extends", "SqlSessionRepo", "SessionRepo"));
});

test("decorators_text keeps each decorator's source text in order", () => {
  assert.deepEqual(symbol("AppController").decorators_text, [
    "@Roles('admin')",
    "@UseGuards(AuthGuard('jwt'), RolesGuard)",
    "@Controller('users')",
  ]);
  assert.deepEqual(symbol("AppController.find").decorators_text, ["@Get(':id')"]);
  assert.deepEqual(symbol("AppService.helper").decorators_text, []);
});

test("@UseGuards gives guarded_by edges: repository guards resolved, package guards as text", () => {
  const roles = edge("guarded_by", "AppController", "RolesGuard");
  assert.equal(roles.dst, "src/roles.guard.ts#RolesGuard");
  assert.equal(roles.resolved, true);
  const auth = edge("guarded_by", "AppController", "AuthGuard");
  assert.equal(auth.resolved, false);
  assert.equal(graph.report.edges.guarded_by, 2);
  assert.equal(graph.report.edges.intercepted_by, 0);
});

test("@Module imports follow forRoot calls and consts; providers follow useClass", () => {
  const db = edge("module_imports", "AppModule", "database");
  assert.equal(db.dst, "src/app.module.ts#database");
  assert.equal(db.resolved, true);
  const config = edge("module_imports", "AppModule", "ConfigModule");
  assert.equal(config.resolved, false);
  assert.equal(edge("provides", "AppModule", "AppService").dst, "src/app.service.ts#AppService");
  assert.equal(edge("provides", "AppModule", "SqlUserRepo").dst, "src/repo.ts#SqlUserRepo");
  assert.equal(graph.report.edges.module_imports, 2);
  assert.equal(graph.report.edges.provides, 2);
  // Controllers are not wiring edges.
  assert.ok(!edge("provides", "AppModule", "AppController"));
});

test("registerAs gives a provides_config edge to the namespace", () => {
  const e = edge("provides_config", "default", "auth");
  assert.equal(e.src, "src/auth.config.ts#default");
  assert.equal(e.resolved, true);
  assert.equal(graph.report.edges.provides_config, 1);
  assert.ok(edge("reads_env", "default", "AUTH_SECRET"));
  assert.ok(edge("reads_env", "database", "DB"));
});

test("a JSX element is a call to the repository component; intrinsic tags are not edges", () => {
  const e = edge("calls", "View", "Widget");
  assert.equal(e.dst, "src/widget.tsx#Widget");
  assert.equal(e.resolved, true);
  assert.equal(e.line, 6);
  assert.ok(!graph.edges.some((x) => x.src_file.endsWith(".tsx") && ["div", "span"].includes(x.dst)));
});

test("defaultExportSymbol resolves `export default X` to X, and keeps declarations and expressions", () => {
  const { files } = openProject(FIXTURE);
  const symbolsOf = (name) => fileSymbols(FIXTURE, files.find((f) => f.getFilePath().endsWith(name)));
  assert.equal(defaultExportSymbol(symbolsOf("widget.tsx")).row.id, "src/widget.tsx#Widget");
  assert.equal(defaultExportSymbol(symbolsOf("auth.config.ts")).row.id, "src/auth.config.ts#default");
  assert.equal(defaultExportSymbol(symbolsOf("view.tsx")), null);
});

const FIXTURE_NEXT = path.join(HERE, "fixture-next");
const nextGraph = buildGraph(FIXTURE_NEXT, { diagnostics: false });

test("Next: pages, route handlers, middleware and server actions, by file convention", () => {
  const rows = (symbol, kind, route) => ({ symbol, kind, route, framework: "next", version: "" });
  assert.deepEqual(nextGraph.entrypoints, [
    rows("app/(marketing)/page.tsx#LandingPage", "page", "/"),
    rows("app/@modal/(.)photo/page.tsx#PhotoModal", "page", "/photo"),
    rows("app/actions.ts#createBoard", "action", "createBoard"),
    rows("app/api/auth/route.ts#GET", "http_route", "GET /api/auth"),
    rows("app/api/auth/route.ts#POST", "http_route", "POST /api/auth"),
    rows("app/board/[boardId]/page.tsx#BoardPage", "page", "/board/[boardId]"),
    rows("middleware.ts#middleware", "middleware", "middleware"),
  ]);
  assert.deepEqual(nextGraph.report.adapters, { next: 7 });
  assert.deepEqual(nextGraph.report.unmatched_frameworks, []);
});

test("Next: a page's flow reaches the components it renders", () => {
  const e = nextGraph.edges.find((x) => x.kind === "calls" && x.src === "app/board/[boardId]/page.tsx#BoardPage");
  assert.equal(e.dst, "app/board/[boardId]/_components/canvas.tsx#Canvas");
});

test("Next: the adapter stays off without next in package.json or a next.config", () => {
  assert.ok(!graph.entrypoints.some((e) => e.framework === "next"));
});

test("appRoute drops groups and slots, strips intercepting prefixes, keeps dynamic segments", () => {
  assert.equal(appRoute([]), "/");
  assert.equal(appRoute(["(shop)", "cart"]), "/cart");
  assert.equal(appRoute(["@modal", "(..)(..)photo", "[id]"]), "/photo/[id]");
  assert.equal(appRoute(["sign-in", "[[...sign-in]]"]), "/sign-in/[[...sign-in]]");
});

test("fallback: with no adapter, exported functions nothing calls (and that call something) are roots", () => {
  const plain = buildGraph(path.join(HERE, "fixture-plain"), { diagnostics: false });
  assert.deepEqual(plain.entrypoints, [
    { symbol: "src/cli.ts#main", kind: "root", route: "main", framework: "generic", version: "" },
    { symbol: "src/worker.ts#tick", kind: "root", route: "tick", framework: "generic", version: "" },
  ]);
  assert.deepEqual(plain.report.adapters, { generic: 2 });
  assert.equal(plain.report.fallback, "generic");
});

test("fallback: never runs when an adapter matched", () => {
  assert.equal(graph.report.fallback, null);
  assert.ok(!graph.entrypoints.some((e) => e.kind === "root"));
  assert.equal(nextGraph.report.fallback, null);
});

const MONO = path.join(HERE, "fixture-mono");
const mono = buildGraph(MONO, { diagnostics: false });

test("monorepo: every project, references followed, test folders left out, shallowest first", () => {
  assert.deepEqual(findProjects(MONO), [
    "tsconfig.json",
    "apps/site/tsconfig.app.json", // only through the root's references
    "packages/lib/tsconfig.json",
  ]);
  assert.deepEqual(
    mono.report.projects.map((p) => [p.tsconfig, p.files, p.entrypoints]),
    [
      ["tsconfig.json", 0, 0], // solution-style: no files of its own
      ["apps/site/tsconfig.app.json", 3, 2],
      ["packages/lib/tsconfig.json", 1, 1], // format.ts is apps/site's already
    ],
  );
  assert.equal(mono.report.files_skipped.other_project, 1);
  assert.equal(mono.symbols.filter((s) => s.qualified_name === "format").length, 1);
});

test("monorepo: adapters per project folder, paths from the repository root", () => {
  const rows = (symbol, kind, route, framework) => ({ symbol, kind, route, framework, version: "" });
  assert.deepEqual(mono.entrypoints, [
    rows("apps/site/app/page.tsx#Home", "page", "/", "next"),
    rows("apps/site/middleware.ts#middleware", "middleware", "middleware", "next"),
    rows("packages/lib/src/index.ts#run", "root", "run", "generic"), // no framework there
  ]);
  assert.deepEqual(mono.report.adapters, { next: 2, generic: 1 });
  assert.equal(mono.report.fallback, "generic");
});

test("monorepo: a call into a file another project analyses still resolves", () => {
  const calls = mono.edges.filter((e) => e.kind === "calls").map((e) => `${e.src} -> ${e.dst}`);
  assert.deepEqual(calls.sort(), [
    "apps/site/app/page.tsx#Home -> packages/lib/src/format.ts#format",
    "packages/lib/src/index.ts#clean -> packages/lib/src/format.ts#format",
    "packages/lib/src/index.ts#run -> packages/lib/src/index.ts#clean",
  ]);
});

test("monorepo: --tsconfig still parses only the project asked for", () => {
  const one = buildGraph(MONO, { tsconfig: "packages/lib/tsconfig.json", diagnostics: false });
  assert.deepEqual(one.report.projects.map((p) => p.tsconfig), ["packages/lib/tsconfig.json"]);
  assert.equal(one.report.files, 2);
});

const EXPRESS = buildGraph(path.join(HERE, "fixture-express"), { diagnostics: false });

test("Express: routes with their mount paths, across files, chains and default exports", () => {
  const rows = (symbol, route) => ({ symbol, kind: "http_route", route, framework: "express", version: "" });
  assert.deepEqual(EXPRESS.entrypoints, [
    rows("src/articles.ts#DELETE /articles/:slug", "DELETE /api/articles/:slug"),
    rows("src/main.ts#GET /", "GET /"),
    rows("src/articles.ts#GET /articles", "GET /api/articles"), // Router().use("/api", api), api = Router().use(articles)
    rows("src/users/userController.ts#UserController.getUser", "GET /users/:id"),
    rows("src/users/userController.ts#UserController.create", "POST /users"),
  ]);
  assert.deepEqual(EXPRESS.report.adapters, { express: 5 });
  assert.equal(EXPRESS.report.fallback, null);
});

test("Express: an inline handler is a symbol, so the route has a flow", () => {
  const handler = EXPRESS.symbols.find((s) => s.id === "src/articles.ts#GET /articles");
  assert.equal(handler.kind, "function");
  assert.equal(handler.exported, false);
  const calls = (src) => EXPRESS.edges.filter((e) => e.kind === "calls" && e.src === src).map((e) => e.dst);
  assert.deepEqual(calls("src/articles.ts#GET /articles"), ["src/service.ts#list"]);
  // A class property holding a function is a method, and a named handler resolves to it.
  const getUser = EXPRESS.symbols.find((s) => s.id === "src/users/userController.ts#UserController.getUser");
  assert.equal(getUser.kind, "method");
  assert.deepEqual(calls(getUser.id), ["src/service.ts#list"]);
});

test("Express: the adapter stays off, and inline handlers are no symbols, without express", () => {
  assert.ok(!graph.entrypoints.some((e) => e.framework === "express"));
  assert.ok(!nextGraph.symbols.some((s) => /^[A-Z]+ \//.test(s.qualified_name)));
});
