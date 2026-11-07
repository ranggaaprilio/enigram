# ts-graph

Static code graph of a TypeScript repository for enigram, built with
[ts-morph](https://ts-morph.com). It reads the repository's `tsconfig.json` and source files and
writes JSONL; it never installs, imports or runs anything from the repository, and it runs without
the repository's `node_modules` (package types stay unresolved, repository types resolve).

The Python side (`apps/api/src/enigram/adapters/driven/ts_graph_sidecar.py`) runs it once per
repository and loads the rows into Postgres. Any other parser (tree-sitter, another language) can
replace it by writing the same files.

```bash
npm ci
node index.mjs --repo <dir> --out <dir> [--tsconfig tsconfig.json]
npm test          # node --test on test/fixture, a small Nest-like project
```

Exit code 2 when the arguments are wrong or `tsconfig.json` is missing or unparsable; the
message is on stderr. On success stdout is one JSON line: the report without `diagnostics_sample`.

## Files analysed

Every file the tsconfig includes, except `node_modules/`, `dist/`, `build/`, `.git/`, `coverage/`,
files outside the repository, `*.d.ts`, tests (`*.spec.ts`, `*.e2e-spec.ts`, `*.test.ts`, `test/`,
`__tests__/`) and `*.generated.*`. Skipped files stay in the program so their types still help
the checker; they are counted in `report.files_skipped`.

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
| `exported` | own `export`, or the parent class's / interface's |
| `decorators` | decorator names, e.g. `["Post", "HttpCode"]` |
| `decorators_text` | each decorator's source text on one line, ≤ 200 chars, in source order, e.g. `["@UseGuards(AuthGuard('jwt'), RolesGuard)"]` |
| `signature` | `name(param: Type): Return` without parameter decorators, or the declaration's first line |
| `body` | the declaration text, decorators included, leading comments excluded |
| `parent` | the class or interface name for members, else `""` |

Interface method signatures are symbols (kind `method`, `parent` = the interface) so a call
through an interface has a target; the `implements` edges lead to the implementations.

### `edges.jsonl`

`{"src", "dst", "kind", "src_file", "line", "resolved"}`

| `kind` | `src` → `dst` | `resolved` |
|---|---|---|
| `calls` | caller symbol id → callee symbol id (also `new X()`) | always `true` |
| `calls_external` | caller id → the callee expression text (≤ 120 chars) | `true` when TypeScript found a declaration outside the analysed files (its lib, a skipped file); `false` when nothing declares the name, typically a package API without `node_modules` |
| `injects` | class id → class/interface id of a constructor parameter type | `true` |
| `extends`, `implements` | class/interface id → the base's id, or its text when outside the repository | `true` only for a repository target |
| `imports` | file → file (`resolved: true`) or → the bare specifier (`false`) | |
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
and parallel slots drop out of the path, `_private` folders are skipped). Framework adapters live
in `lib/entrypoints/` and export `detect({project, root, symbols, deps}) -> {entrypoints, matched}`.
When no adapter matches anything, `lib/entrypoints/generic.mjs` adds `root` rows (framework
`generic`, route = the name): exported top-level functions that nothing in the repository calls
and that call at least one repository symbol.

### `report.json`

```json
{"files": 171, "files_skipped": {"test": 13, "dts": 0, "generated": 0, "outside": 0},
 "symbols": 390, "edges": {"calls": 232, "calls_external": 729, "...": 0},
 "calls_resolved_share": 0.395, "calls_in_repo_share": 0.241, "calls_local": 0,
 "entrypoints": 25, "adapters": {"nest": 25}, "fallback": null, "unmatched_frameworks": [],
 "diagnostics_sample": ["<file>:<line> <message>", "... at most 10"], "secs": 0.3}
```

`calls_resolved_share` is the share of call sites whose callee has a declaration (repository or
TypeScript lib); `calls_in_repo_share` the share that lands on a repository symbol.
`unmatched_frameworks` lists frameworks in the repository's `package.json` dependencies (parsed,
never run) that have no adapter yet. `fallback` is `"generic"` when the `root` rows were used, else `null`.
