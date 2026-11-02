import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { buildGraph } from "../lib/graph.mjs";
import { joinRoute } from "../lib/entrypoints/nest.mjs";

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
  assert.equal(graph.report.files, 11);
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
