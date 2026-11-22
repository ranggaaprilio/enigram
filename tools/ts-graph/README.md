# ts-graph

Static code graph of a TypeScript repository for enigram, built with
[ts-morph](https://ts-morph.com). It reads the repository's `tsconfig.json` files and source files and
writes JSONL; it never installs, imports or runs anything from the repository, and it runs without
the repository's `node_modules` (package types stay unresolved, repository types resolve).
The compiler sees the repository through a guarded file system (`lib/fsguard.mjs`): a path whose
real location is outside the repository does not exist for it, so neither a tsconfig `include`
nor an import reaches a file outside the repository, also through a symbolic link inside it. A
symbolic link that stays inside the repository works as before. A tsconfig `extends` of a file
outside the repository is ignored too, so the options it would add are missing.

The Python side (`apps/api/src/enigram/adapters/driven/ts_graph_sidecar.py`) runs it once per
repository and loads the rows into Postgres. Any other parser (tree-sitter, another language) can
replace it by writing the same files.

```bash
npm ci
node index.mjs --repo <dir> --out <dir> [--tsconfig <path>] [--javascript]
npm test          # node --test on test/fixture, a small Nest-like project
```

Exit code 2 when the arguments are wrong, the repository has no `tsconfig.json` (without
`--javascript`), or none of its projects can be read; the
message is on stderr. On success stdout is one JSON line: the report without `diagnostics_sample`.
With `--javascript`, a repository without any project is an empty graph and exit code 0.

## Projects

Without `--tsconfig`, every TypeScript project of the repository is parsed: each `tsconfig.json`
outside `node_modules/`, `dist/`, `build/`, `.git/`, `coverage/`, `test/` and `__tests__/`, plus
the configs their `references` name (a solution-style tsconfig lists no files of its own), at most
50, shallowest first. A file two projects include belongs to the first. Paths stay relative to
the repository root. Framework adapters look at each project's own folder: Next's `app/` and
`middleware.ts`, Nest's `src/main.ts`, and its `package.json` with the root one; a project no
adapter matches gets the generic `root` entry points. Calls between projects through a package
name (`@acme/ui`) stay unresolved: there is no `node_modules`.

### JavaScript projects (`--javascript`)

Off by default; without it the output is exactly what it was before. With it (and without
`--tsconfig`), the JavaScript files (`.js`, `.jsx`, `.mjs`, `.cjs`) that no TypeScript project
includes are parsed too, after the TypeScript projects:

- A file belongs to the nearest folder above it with a `jsconfig.json` or a `package.json`; each
  such folder that owns a file is one project (`config` = that file, `language` = `"js"`). A
  backend and a frontend with a `package.json` each are two projects. A folder under `test/`,
  `tests/` or `__tests__/` is never a project.
- A file a tsconfig includes stays with that TypeScript project (a tsconfig with `allowJs` keeps
  its `.js` files); a tsconfig without `allowJs` leaves its repository's `.js` files to the
  nearest `package.json`.
- Compiler options: `allowJs`, no type checking (`checkJs: false`, so `diagnostics_sample` stays
  empty for these files), JSX in `.js` files, `bundler` module resolution, and only `baseUrl` and
  `paths` from a `jsconfig.json`, and only when they stay inside the repository (a `baseUrl`
  outside it, or a `paths` value that is not a list of relative strings inside it, drops them).
  `extends` is not followed.
- All projects, TypeScript and JavaScript, count against the same limit of 50
  (`report.projects_truncated` counts the JavaScript projects left out).
- Imports reach the repository's own code without `node_modules`: every package of the
  repository by its `name` (a workspace sibling such as `@acme/util`), and every top-level folder
  of the project that is not a dependency name (`src/lib/x`, the usual Vite/webpack alias). Other
  aliases from a bundler config are not read; they stay unresolved and show in `imports_in_repo`.
- CommonJS is followed, in JavaScript projects and in the `.js` files of TypeScript projects with
  `allowJs`: a function assigned at the top level is a symbol (see `symbols.jsonl`), what
  `module.exports` / `exports.x` is set to is exported, a call through a `module.exports = { a,
  b: c }` member lands on `a` / `c`, and `require("x")` / `import("x")` are `imports` rows.
- TypeScript rows stay the same, except that a call from a TypeScript project into a JavaScript
  file that is now parsed becomes a `calls` edge to its symbol instead of `calls_external`, and
  the CommonJS rules above apply to `.js` files a tsconfig includes.
- Symbolic links are not followed.

## Files analysed

Every file the tsconfig includes, except `node_modules/`, `dist/`, `build/`, `.git/`, `coverage/`,
files outside the repository, `*.d.ts`, tests (`*.spec.ts`, `*.e2e-spec.ts`, `*.test.ts`, `test/`,
`__tests__/`) and `*.generated.*`. Skipped files stay in the program so their types still help
the checker; they are counted in `report.files_skipped`.

Every JavaScript file of a JavaScript project, except (counted in `report.files_skipped` under
the reason in brackets): tests (`*.spec.js`, `*.test.js`, `*.e2e-spec.js` and any file under
`test/`, `tests/`, `__tests__/`, `fixtures/`, `__fixtures__/` or `__mocks__/`) [`test`]; tooling
configs (`*.config.js/.cjs/.mjs`, `.eslintrc.js/.cjs`) [`config`]; `*.min.js`, `*.bundle.js` and
files whose average line is over 500 characters [`minified`]; files over 10,000 lines [`large`];
files with a syntax error, such as Flow type annotations [`syntax`]; files that cannot be read
[`unreadable`]; files with no `package.json` or `jsconfig.json` above them [`no_project`]; files
of the projects left out by the limit of 50 [`project_limit`]. `.vue` and `.svelte` files are
counted as [`unsupported`] and not parsed.

## Output

### `symbols.jsonl`

| Field | Meaning |
|---|---|
| `id` | `<file>#<qualified_name>`; a repeated name in one file gets `~2`, `~3` |
| `qualified_name` | `Class`, `Class.method`, `Class.constructor`, `Interface.method`, `function`, `variable`, `default` |
| `kind` | `class` · `interface` · `enum` · `type` · `function` · `method` · `constructor` · `accessor` · `variable` |
| `file` | repository-relative path, `/`-separated |
| `line_start`, `line_end` | 1-based, decorators included |
| `body_hash` | SHA-256 of the declaration's tokens joined by one space: whitespace and comments do not change it; literals, decorators and the signature do |
| `exported` | own `export`, the parent class's / interface's, or the name is in an export list (`export { a, b as c }`); with `--javascript` also what `module.exports` / `exports.x` is set to |
| `decorators` | decorator names, e.g. `["Post", "HttpCode"]` |
| `decorators_text` | each decorator's source text on one line, ≤ 200 chars, in source order, e.g. `["@UseGuards(AuthGuard('jwt'), RolesGuard)"]` |
| `signature` | `name(param: Type): Return` without parameter decorators, or the declaration's first line |
| `body` | the declaration text, decorators included, leading comments excluded |
| `parent` | the class or interface name for members, else `""` |

Interface method signatures are symbols (kind `method`, `parent` = the interface) so a call
through an interface has a target; the `implements` edges lead to the implementations. A class
property whose value is a function (`getUsers = async (req, res) => {...}`) is a `method` too. In a
project that depends on `express`, the inline handler of a top-level route registration
(`router.get("/articles", auth, async (req, res) => {...})`) is a `function` named
`GET /articles` (not exported), so the route has a flow. With `--javascript`, in a JavaScript
file, a function assigned at the top level to a member (not to a plain variable, whose declaration
is already a symbol) is a `function` too, unless the file already has a symbol of that name; the
row covers the whole assignment: `exports.run =
function () {...}` and `module.exports.run = ...` are `run` (exported), `module.exports =
wrap(async () => {...})` is `default` (exported), `Model.prototype.save = function () {...}` is
`Model.save`, and anything else keeps its target text (`schema.methods.check`).

### `edges.jsonl`

`{"src", "dst", "kind", "src_file", "line", "resolved"}`

| `kind` | `src` → `dst` | `resolved` |
|---|---|---|
| `calls` | caller symbol id → callee symbol id (also `new X()`) | always `true` |
| `calls_external` | caller id → the callee expression text (≤ 120 chars) | `true` when TypeScript found a declaration outside the analysed files (its lib, a skipped file); `false` when nothing declares the name, typically a package API without `node_modules` |
| `injects` | class id → class/interface id of a constructor parameter type | `true` |
| `extends`, `implements` | class/interface id → the base's id, or its text when outside the repository | `true` only for a repository target |
| `imports` | file → file (`resolved: true`) or → the bare specifier (`false`); with `--javascript` also `require("x")` and `import("x")` with a literal in `.js` files, and a JavaScript file without symbols (a barrel `index.js`) is a resolved target | |
| `reads_env` | symbol id → `process.env.X` name, or the key of `configService.get("x")` / `getOrThrow("x")` | `true` |
| `guarded_by`, `intercepted_by` | class/method id → each guard/interceptor of `@UseGuards(...)` / `@UseInterceptors(...)` (`AuthGuard('jwt')` → `AuthGuard`) | `true` only for a repository target; the text otherwise |
| `module_imports`, `provides` | `@Module` class id → each entry of `imports` / `providers`, following `X.forRoot(...)`, `cond ? A : B`, spreads, consts and `{provide, useClass/useExisting}` | `true` only for a repository target; the text otherwise |
| `provides_config` | function/variable id → the namespace string of `registerAs('<ns>', ...)` | `true` |

Calls inside decorators (`@Post('login')`, `@Body()`) are metadata and are not edges. A call to the
caller's own parameter or local function is not an edge either (counted in `report.calls_local`).
A JSX element whose tag resolves to a repository component (`<BoardList />`) is a `calls` edge to
it; intrinsic tags (`<div>`) and package components are not edges.

### `entrypoints.jsonl`

`{"symbol", "kind", "route", "framework", "version"}` with `kind` one of `http_route`
(`"POST /auth/email/login"`, controller path and method path joined), `cron` (the expression),
`event` (`@EventPattern`/`@MessagePattern` name), `job` (`<processor>:<process>`) and `cli_main`
(`bootstrap`/`main` in `src/main.ts`) for Nest; `page` (`/board/[boardId]`), `http_route`
(`"POST /api/auth"`, one row per HTTP-method export of a `route` file), `middleware` and `action`
(exported `"use server"` functions) for the Next.js App Router (`app/` or `src/app/`; route groups
and parallel slots drop out of the path, `_private` folders are skipped); `http_route`
(`"GET /api/articles"`) for Express: every `get/post/put/patch/delete/options/head/all(path, …,
handler)` on a router or app made by `express()`, `Router()` or `express.Router()`, with the
paths of the `use(path, router)` calls that mount it joined in front, across files, `.use()`
chains and `export default`, and one route per verb of a `router.route("/p").get(…).post(…)`
chain at `route()`'s path; the entry point is the last handler (an inline one is its own
symbol, `GET /p` for a chain too; a named one the symbol it resolves to; a package's handler is
skipped). With `--javascript` the CommonJS forms count as well: `require("express")` (also
`require("express").Router()` and `const { Router } = require("express")`), a router a file
exports with `module.exports = router` and another mounts through `require` (a variable or inline
`app.use("/x", require("./x"))`), a `list.forEach((r) => router.use(r.path, r.route))` over a
constant array of `{ path, route }` objects, and a named handler reached through a
`module.exports` object or a barrel (`authController.login`). Framework adapters live in
`lib/entrypoints/` and export `detect({project, root, prefix, symbols, files, deps, commonjs}) ->
{entrypoints, matched}`.
When no adapter matches anything, `lib/entrypoints/generic.mjs` adds `root` rows (framework
`generic`, route = the name): exported top-level functions that nothing in the repository calls
and that call at least one repository symbol.

### `report.json`

```json
{"files": 171, "files_skipped": {"test": 13, "dts": 0, "generated": 0, "outside": 0},
 "symbols": 390, "edges": {"calls": 232, "calls_external": 729, "...": 0},
 "calls_resolved_share": 0.395, "calls_in_repo_share": 0.241, "calls_local": 0,
 "entrypoints": 25, "adapters": {"nest": 25}, "fallback": null, "unmatched_frameworks": [],
 "projects": [{"tsconfig": "tsconfig.json", "files": 171, "symbols": 390, "entrypoints": 25, "adapters": {"nest": 25}}],
 "diagnostics_sample": ["<file>:<line> <message>", "... at most 10"], "secs": 0.3}
```

`calls_resolved_share` is the share of call sites whose callee has a declaration (repository or
TypeScript lib); `calls_in_repo_share` the share that lands on a repository symbol.
`unmatched_frameworks` lists frameworks in the repository's `package.json` dependencies (parsed,
never run) that have no adapter yet. `fallback` is `"generic"` when a project used the `root` rows, else `null`.
`projects` lists each project parsed (or `{tsconfig, error}` for one that could not be read);
`files_skipped.other_project` counts files an earlier project already took.

With `--javascript` the report has a few more keys; without it, it is unchanged:

- each `projects` entry has `config` (its `tsconfig.json`, `jsconfig.json` or `package.json`) and
  `language` (`"ts"` or `"js"`); only a TypeScript project keeps the `tsconfig` key, and a
  JavaScript project that could not be opened is `{config, language, error}`;
- `projects_truncated` counts the JavaScript projects left out by the limit of 50;
- `imports_in_repo` (`{total, resolved, unresolved_sample}`, JavaScript projects only) counts the
  imports meant for repository code: resolved ones, relative ones, and bare ones that name no
  dependency or Node built-in; imports of assets and of `.vue` / `.svelte` files are left out.
  `unresolved_sample` lists up to 10 as `<file> -> <specifier>`, typically a bundler alias;
- `files_skipped` also has `config`, `minified`, `large`, `syntax`, `unreadable`, `no_project`,
  `project_limit` and `unsupported` (see Files analysed), and JavaScript tests are added to
  `test`. Every `.js`/`.jsx`/`.mjs`/`.cjs` file outside the skipped folders that no tsconfig
  includes is either analysed or counted under exactly one reason.
