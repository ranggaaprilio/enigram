# Dukungan repo JavaScript di code graph

Rencana dibuat 2026-11-19. Revisi kedua 2026-11-19: cakupan diubah dari "repo seperti
proshop_mern" menjadi repo JavaScript secara umum (keputusan owner). Belum diimplementasikan.

Tanda di dokumen ini: **(diuji)** berarti dijalankan pada fungsi sidecar yang ada
(`fileSymbols`, `buildEdges`, `express.detect`, `generic.roots`) dengan project ts-morph tanpa
tsconfig, pada proshop_mern `1d20b5b` atau fixture kecil. **(dinalar)** berarti disimpulkan dari
membaca kode. **(belum diuji)** berarti belum ada bukti sama sekali.

Survei J0 sudah dijalankan pada 9 repo publik (2026-11-19, tanpa LLM). Angkanya ada di bagian
Hasil, dan desain di bawah sudah disesuaikan. Tanda **(J0)** berarti didukung angka survei itu.
Kode produk belum diubah.

## Masalah

Repo JavaScript hanya ter-index Markdown-nya. Kodenya tidak masuk code graph, dan tidak ada pesan
yang menjelaskan sebabnya. Contoh yang memicu plan ini: `bradtraversy/proshop_mern`.

Penyebabnya ada di dua tempat:

1. **API.** `repo_sync.py` hanya memanggil `index_code` kalau `is_ts_repo(root)` true. Fungsi itu
   (`application/code_index.py:40`) mencari `tsconfig.json`.
2. **Sidecar.** `tools/ts-graph` hanya membuka project lewat `tsconfig.json` (`findProjects`,
   `openProject` di `lib/project.mjs`), dan `buildGraph` melempar "no tsconfig.json"
   (`lib/graph.mjs:51`).

## Cakupan: bentuk repo JS

"Semua repo JS" tidak bisa dijamin. Yang dijamin plan ini: setiap bentuk di tabel punya status
yang jelas, dan setiap file atau import yang tidak tertangani dihitung di report, sehingga
kegagalan terlihat dan tidak diam-diam.

| Sumbu | Bentuk | Status di plan | Bukti sekarang |
|---|---|---|---|
| Sistem modul | ESM (`import`/`export`) | Didukung, J2 | Diuji pada proshop; `export { a, b }` perlu perbaikan |
| | CommonJS (`require`, `module.exports`, `exports.x`) | Didukung, J2 (wajib) | Diuji pada fixture: symbol hilang, tanpa edge `imports` |
| | Campuran ESM dan CJS dalam satu repo | Didukung, J2 | Belum diuji |
| Susunan repo | Satu `package.json` | Didukung, J1 | Diuji |
| | Beberapa `package.json` (backend + frontend) | Didukung, J1 | Dinalar; proshop berbentuk ini |
| | Workspace (npm/yarn/pnpm), import antar paket dengan nama paket | Didukung, J2 | J0: create-react-app, 30 import antar paket, 0 ter-resolve |
| | Campuran dengan TS (sebagian paket punya tsconfig) | Didukung, J1; hasil project TS tidak berubah | Dinalar |
| | Repo JS dengan `tsconfig.json` ber-`allowJs` (untuk type check) | Sudah lewat jalur TS hari ini; limit dan baseline file diperbaiki di J4 | J0: webpack, 1.022 file ter-index, tetapi limit hanya menghitung 9 file `.ts` |
| | Repo JS dengan `tsconfig.json` tanpa `allowJs` | Didukung, J1: file JS yang tidak dimasukkan tsconfig menjadi project JS | J0: axios, hari ini hanya 13 file `.ts` ter-index, 92 file JS terlewat |
| | Tanpa `package.json` (kumpulan skrip) | Tidak didukung; pesan di hasil sync | — |
| Resolusi import | Relatif, dengan atau tanpa ekstensi | Didukung | Diuji |
| | `jsconfig.json` `paths`/`baseUrl` | Didukung | J0: repo Next, 17 dari 17 import alias ter-resolve |
| | `imports` (`#x`) dan `exports` di `package.json` | Tidak dikerjakan; dihitung sebagai import tak ter-resolve | J0: 0 import `#x` di 9 repo |
| | Alias di config build yang menunjuk folder di root project (`src/x`) | Didukung, J2, lewat tebakan folder | J0: coreui, 53 dari 53 alias ter-resolve dengan tebakan |
| | Alias lain di config webpack/Vite/Babel | Tidak didukung (config repo tidak pernah dijalankan); dihitung | J0: create-react-app, 9 alias `@docusaurus/*` |
| Dialek | JSX di `.js`/`.jsx` | Didukung | Diuji |
| | Anotasi Flow, sintaks non-standar | Tidak didukung; file dilewati dan dihitung | J0: 37 file di create-react-app, 3 di webpack |
| | `.vue`, `.svelte`, template lain | Tidak didukung; dihitung | J0: vue-element-admin, 131 file `.vue` di samping 76 file JS |
| Gaya kode | Fungsi dan class yang dideklarasikan | Didukung | Diuji |
| | Fungsi yang ditugaskan ke properti (`exports.x = fn`, `Foo.prototype.x = fn`, `schema.methods.x = fn`) | Didukung, J2 | Diuji: sekarang tidak menjadi symbol |
| | Kode top-level di luar fungsi (skrip, pendaftaran handler) | Tidak masuk graph di v1 (usulan D9) | J0: sisanya 0–16% baris kode setelah aturan J2 |
| Framework | Express (ESM dan CJS) | Didukung, J3 | J0: proshop 4 dari 25 route; node-express-boilerplate (CJS) 0 dari 14 |
| | Next.js dalam JS | Didukung tanpa perubahan | J0: 11 entry point ditemukan |
| | NestJS dalam JS | Tidak diukur | Belum diuji (tidak ada repo ukur) |
| | Fastify, Koa, Hono, React, Vue | Tanpa adapter; generic fallback; nama framework dilaporkan di `unmatched_frameworks` | — |
| Ukuran | Sampai `CODE_MAX_FILES`/`CODE_MAX_LOC` | Didukung | J0: webpack (1.022 file, 390 ribu baris, 7,8× limit baris) 33,7 detik dan 1,3 GB di sidecar |

**Bukan tujuan:** adapter framework baru, bahasa selain JS/TS, inferensi tipe JSDoc (`checkJs`),
membaca config build untuk alias, route yang didaftarkan di dalam `if/else` atau fungsi.

## Kondisi sekarang

Dicek terhadap kode pada 2026-11-19 (path API relatif ke `apps/api/src/enigram/`).
`code_agent.py` punya perubahan yang belum di-commit di branch lain, jadi nomor barisnya bisa
bergeser.

### Logika yang menganggap TypeScript

| Bagian | Lokasi | Asumsi |
|---|---|---|
| Deteksi repo | `application/code_index.py:40` `is_ts_repo` | Harus ada `tsconfig.json` |
| Batas ukuran | `code_index.py:54` `repo_limits`, `_is_ts` | Hanya `.ts/.tsx/.mts/.cts` yang dihitung |
| Baseline per commit | `code_index.py:96` `git_blob_shas` | Hanya blob dengan `TS_SUFFIXES` |
| Storage | `repo_sync.py:244-248` | Repo kode tanpa Postgres langsung error |
| Project sidecar | `tools/ts-graph/lib/project.mjs`; `lib/graph.mjs:51` | Satu project per `tsconfig.json`; tanpa tsconfig sidecar keluar dengan kode 2 (`index.mjs:46-48`) |
| Filter test | `project.mjs:8` `TEST_FILES` | Regex hanya `.tsx?` |
| Flag `exported` | `lib/symbols.mjs:132` | `export { a, b }` di akhir file memberi `exported: false` **(diuji)**. Berlaku juga untuk repo TS |
| Symbol | `lib/symbols.mjs:129-138` | Hanya deklarasi dan `export default`. Penugasan fungsi ke properti tidak menjadi symbol **(diuji)** |
| Edge import | `lib/edges.mjs:233-243` | Hanya `ImportDeclaration`. `require()` tidak memberi edge `imports` **(diuji)** |
| Adapter Express | `lib/entrypoints/express.mjs:14-20` `fromExpress` | Hanya `import ... from "express"` |
| Route Express | `express.mjs:35-41,96`, `symbols.mjs:161-171` | Chain `router.route(path).get(...)` tidak dikenali; handler inline hanya di top level |
| Eval | `evaluation/golden.py:263-269` `read_ts_files` | Hanya membaca `*.ts` |

Jalur update per commit (`application/code_changes.py`) dan `repo_store.py` tidak punya filter
ekstensi. Dockerfile menyalin seluruh sidecar, jadi image tidak perlu diubah.

### Teks yang menyebut TypeScript

- API: `repo_sync.py:247,262,264`; `code_index.py:67,71,156`; `config.py:199`;
  `code_agent.py:153,196`; `evaluation/agent_repo.py:83`; `mcp_server.py:353`; `cli.py:61,67`.
- Sidecar: `index.mjs:8-13,47`; `graph.mjs:102-105` (kunci `projects[*].tsconfig`);
  `tools/ts-graph/README.md`.
- Web: `apps/web/src/pages/RepoDetail.tsx:221` (label "TypeScript diagnostics").
- Docs: `README.md:41,44,73,77,85`; `docs/intro.md:12,53,57,65`;
  `docs/guides/repositories.md:22,30,55`; `docs/reference/http-api.md:66`;
  `docs/evaluation/golden-set.md:13`.

### Yang sudah berjalan tanpa perubahan (diuji)

- Project ts-morph tanpa tsconfig, dibangun dari daftar file dengan `allowJs` (proshop: 61 file,
  253 symbol, 0 error sintaks).
- JSX di file `.js` ter-parse, dan edge ke `<Component />` ter-resolve.
- `jsconfig.json` bisa dibuka lewat `openProject` yang ada; import lewat `baseUrl` ter-resolve.
- `const x = wrap(async ...)` menjadi symbol variabel dengan edge panggilan, dan ter-resolve
  lewat `export { ... }` dan `import`.
- `module.exports.helper = helper` dan `module.exports = run` ter-resolve ke symbol-nya.

### Satu titik data: proshop_mern (SHA `1d20b5b`)

- ESM, 61 file JS, 5.158 baris (backend 18 file, frontend 43 file, semuanya `.js` dengan JSX).
- `package.json` di root (backend, punya `express`) dan di `frontend/`.
- 25 pendaftaran route: adapter sekarang menemukan 4; 19 memakai chain `route()`; 2 di dalam
  `if/else`.
- Sebagai satu project tanpa aturan skip: `calls_resolved_share` 0,425, `calls_in_repo_share`
  0,217.

## Tujuan

Repo JavaScript dari bentuk yang berstatus "Didukung" di tabel cakupan ter-index ke code graph
yang sama dengan repo TypeScript: sitasi `path:line@sha`, entry point, flow summary, dan update
per commit.

Untuk bentuk yang tidak didukung, hasil sync dan report menyebut apa yang terlewat dan berapa
banyak.

Hasil project TypeScript yang sudah ter-index tidak boleh berubah, kecuali perbaikan `exported`
yang disebut di J2.

### Ukuran berhasil

Diukur pada **kumpulan repo ukur** (9 repo di bagian Hasil), bukan pada satu repo. Angka "J0"
adalah keadaan sebelum implementasi.

| Ukuran | Cara ukur | J0 | Target | Fase |
|---|---|---|---|---|
| Setiap repo ukur menghasilkan graph | `files` > 0 dan `symbols` > 0 per project yang punya file JS | — | 9 dari 9 repo | J1 |
| Tidak ada file yang hilang tanpa jejak | File JS di repo = dianalisis + `files_skipped` (semua alasan) | — | Sama persis di 9 repo | J1 |
| Symbol hilang | Fungsi yang ditugaskan di top level tanpa symbol | 156 di 9 repo (express 78, webpack 41, create-react-app 22) | 0 | J2 |
| Edge `imports` di repo CJS | Jumlah edge `imports` | 0 di node-express-boilerplate dan express | Sama dengan jumlah `require` relatif yang ter-resolve (81 dan 54) | J2 |
| Import workspace | Import antar paket yang ter-resolve | 0 dari 30 (create-react-app) | 30 dari 30 | J2 |
| Alias folder | Alias yang ter-resolve | 0 dari 53 (coreui) | 53 dari 53 | J2 |
| Entry point repo CJS | Entry point per repo | 0 di node-express-boilerplate | Lebih dari 0 di tiap repo yang punya kode | J2–J3 |
| Route Express | Daftar route yang diharapkan ditulis di test | proshop 4 dari 25; node-express-boilerplate 0 dari 14 | proshop 23 dari 25; node-express-boilerplate 14 dari 14 (daftar dicek tangan di J3) | J3 |
| Update per commit | `affected_files` menemukan pemakai sebuah file di repo ESM dan di repo CJS | Tidak bekerja di CJS (0 edge `imports`) | Bekerja di keduanya | J2 |
| Panggilan | `calls_in_repo_share` per repo | 2,6% sampai 32,4% | Tidak ada target; dicatat sebelum dan sesudah | J5 |
| Mini check | 5 pertanyaan per repo yang di-index di J5 | — | Ambang lulus di D6 | J5 |
| Regresi TS | Fixture TS lama; golden TS split `dev` | 10/10 | Fixture lulus; `dev` tidak turun | J1–J5 |

## Desain

### Flag `CODE_JAVASCRIPT`

Dukungan JS ada di belakang env `CODE_JAVASCRIPT`, mati secara default sampai J5 selesai (pola
yang sama dengan `VERIFY_ANSWERS`). Alasannya:

- Repo JS yang sudah ter-index (Markdown saja) akan membuat code graph dan flow summary pada sync
  atau webhook berikutnya, dengan biaya token.
- Repo Markdown yang kebetulan punya `package.json` dan satu file JS akan dianggap repo kode.
- Repo TS yang punya file JS di luar project TS-nya akan mendapat project JS tambahan.

Dengan flag mati, perilaku sekarang tidak berubah sama sekali.

### Project dan kepemilikan file

1. Project adalah folder yang punya `tsconfig.json`, `jsconfig.json`, atau `package.json`.
   Project `tsconfig.json` dibuka persis seperti sekarang.
2. Folder dengan `package.json` tanpa `tsconfig.json`/`jsconfig.json` menjadi project JS virtual.
3. **File JS yang sudah dimasukkan sebuah project TS tetap di sana.** File JS lain dimiliki oleh
   `package.json` atau `jsconfig.json` terdekat di atasnya, dan project virtual tidak masuk ke
   subfolder yang punya config sendiri. Akibatnya:
   - backend dan frontend dengan `package.json` masing-masing menjadi dua project (proshop: 18
     dan 43 file **(J0)**);
   - repo JS yang tsconfig-nya ber-`allowJs` tidak berubah: file-nya sudah milik project TS
     (webpack: 883 file **(J0)**);
   - repo JS yang tsconfig-nya tanpa `allowJs` mendapat project JS untuk file yang terlewat
     (axios: 92 file di root **(J0)**). Versi plan sebelumnya memberi file itu ke tsconfig, dan
     hasilnya `lib/` axios tidak ter-index sama sekali;
   - file JS di luar semua project TS, misalnya `scripts/*.js` di root monorepo TS, masuk ke
     project virtual root. Ini penambahan, bukan perubahan pada project TS (D5).
4. Project virtual memakai `allowJs: true`, `checkJs: false`, `jsx: preserve`,
   `module`/`moduleResolution: bundler`, `noEmit`. `nodenext`, `bundler` dan `commonjs` memberi
   hasil yang sama pada proshop **(diuji)**; `bundler` dipilih karena paling longgar.
5. Workspace: kalau `package.json` root punya `workspaces` (atau ada `pnpm-workspace.yaml`), nama
   tiap paket dipetakan ke foldernya lewat `paths`, supaya `import x from "@repo/utils"` mengarah
   ke kode di repo. Dibutuhkan: di create-react-app 30 import antar paket tidak ter-resolve
   **(J0)**. Pemetaannya sendiri belum diuji.
6. **Tebakan folder untuk alias.** Import bukan-relatif yang bukan dependency, dan segmen
   pertamanya adalah folder di root project (`src/components/x`), di-resolve dari root project.
   Ini menutup alias Vite/webpack yang paling umum tanpa membaca config build: coreui 53 dari 53
   **(J0)**. Alias bentuk lain (`@/x` tanpa `jsconfig.json`) tidak ditebak, karena tidak ada
   kasusnya di 9 repo.
7. File dengan error sintaks (Flow, sintaks non-standar) dilewati dan dihitung di
   `files_skipped.syntax` **(J0: 37 file di create-react-app, 3 di webpack)**.
8. Kalau tidak ada project sama sekali, sidecar mengembalikan graph kosong dengan kode keluar 0.
9. `MAX_PROJECTS` (50) tetap berlaku. Repo dengan banyak contoh bisa melewatinya (webpack punya
   204 folder ber-config **(J0)**); project yang terpotong dihitung di report.

Aturan API:

- `is_ts_repo` tetap ada. Fungsi baru `is_js_repo`: flag menyala, ada `package.json` atau
  `jsconfig.json`, dan ada minimal satu file `.js/.jsx/.mjs/.cjs` di luar `SKIP_DIRS`. Ini hanya
  pre-check murah; sidecar yang menentukan project dan file.
- Repo yang hanya punya project JS, tanpa Postgres: code index dilewati dengan pesan, bukan error.
  Markdown-nya tetap ter-index. Error "needs STORAGE_BACKEND=postgres" tetap untuk repo TS.
- Graph kosong dari sidecar: hasil sync membawa pesan "code not indexed" beserta alasannya (tidak
  ada project, atau semua file dilewati dan jumlah per alasan).

### File yang dilewati

- `*.min.js`, `*.bundle.js`, dan file dengan rata-rata panjang baris di atas 500 karakter
  (`files_skipped.minified`).
- Config tooling menurut nama file, di kedalaman mana pun: `*.config.js/.cjs/.mjs`,
  `.eslintrc.js/.cjs`, `babel.config.js` (`files_skipped.config`). Hanya untuk ekstensi JS.
- Regex `TEST_FILES` diperluas ke `.jsx?`, `.mjs`, `.cjs`, dan ke folder `fixtures/`,
  `__fixtures__/`, `__mocks__/`.
- File di atas 10.000 baris (`files_skipped.large`, usulan D11). Dua bundle fixture yang tidak
  di-minify di create-react-app (41 ribu baris masing-masing) lolos dari aturan minified dan
  mengisi 82,7 ribu dari 91 ribu baris kode repo itu **(J0)**.

Folder `examples/` dan `docs/` tidak dilewati. Di repo library isinya bisa lebih banyak daripada
kode utamanya (express: 43 dari 50 file yang dianalisis ada di `examples/`; webpack: 412 file di luar
tsconfig, sebagian besar contoh **(J0)**), dan ini dicatat sebagai
risiko, bukan aturan skip.

Folder bernama `public`, `static`, `vendor`, `config`, `data` **tidak** dilewati, karena nama itu
juga dipakai kode aplikasi. Boilerplate framework (misalnya `serviceWorker.js` dari CRA) juga
tidak dilewati.

**Limit dan baseline untuk jalur TS yang ada (D10).** `repo_limits` dan `git_blob_shas` hanya
melihat ekstensi TS, padahal sidecar sudah meng-index file JS dari tsconfig ber-`allowJs`. Di
webpack limit menghitung 9 file `.ts`, sedangkan sidecar meng-index 1.022 file dan 14.725 symbol
**(J0)**. Jadi `CODE_MAX_LOC` tidak berlaku untuk repo seperti itu, dan `files_changed` serta
`files_affected` tidak melihat perubahan file JS. Graph-nya sendiri tetap benar karena diganti
utuh tiap index. Memperbaiki ini mengubah perilaku yang ada: repo seperti webpack akan mulai
ditolak limit.

`repo_limits` di Python hanya menerapkan ekstensi, `SKIP_DIRS`, dan `*.min.js`. Bundle besar
tanpa nama `.min.js` tetap dihitung, jadi limit bisa tercapai walau sidecar akan melewatinya.
Error limit menyebut nama env-nya.

### Symbol dan edge

Satu aturan umum menggantikan daftar kasus per pola:

- **Penugasan fungsi di top level menjadi symbol.** Kalau sisi kanan sebuah penugasan top-level
  adalah fungsi, atau panggilan yang membungkus fungsi, sisi kirinya menjadi nama symbol. Ini
  mencakup `exports.x = fn`, `module.exports.x = fn`, `module.exports = wrap(fn)`,
  `Foo.prototype.x = fn`, dan `schema.methods.x = fn`.
- **`exported`** true untuk nama di `export { a, b }`, `export { a as b }`, dan untuk anggota
  `module.exports`/`exports`. Perbaikan `export { }` juga mengenai repo TS: fixture TS dijalankan
  ulang dan perubahannya dicatat di PR.
- **Panggilan ke anggota `module.exports = { a, b: c }`** menjadi edge dalam repo. Sekarang
  tercatat `calls_external` dengan `resolved: true`, yang menaikkan `calls_resolved_share` secara
  semu **(diuji)**.
- **Edge `imports` dari `require("./x")`** dan dari `import()` dinamis dengan string literal,
  supaya `affected_files` di update per commit bekerja untuk CJS.
- **`imports_resolved_share`** di report: bagian import non-paket yang mengarah ke file di repo.
  Import relatif atau alias yang tidak ter-resolve dihitung, dengan contoh di report. Ini yang
  membuat alias webpack/Vite terlihat sebagai kekurangan, bukan hilang diam-diam.
- Satu edge salah terlihat di fixture CJS: panggilan ke `exports.other` tercatat ke fungsi pertama
  di file tujuan. Dugaan penyebab: `owningSymbol` naik sampai `SourceFile` (`edges.mjs:30-35`).
  Belum diisolasi; diperiksa di J2.

**Kode top-level di luar fungsi (D9).** Skrip tanpa fungsi, `schema.pre('save', ...)`, dan
pendaftaran handler tidak masuk graph. J0 mengukur bagian baris kode di luar symbol, sekarang dan
perkiraan setelah aturan di atas (penugasan fungsi, daftar ekspor, handler route inline):

| Repo | Sekarang | Setelah aturan J2/J3 |
|---|---|---|
| proshop, axios, coreui, repo Next | 0–3,4% | 0–2,0% |
| vue-element-admin | 18,7% | 3,6% |
| node-express-boilerplate | 21,7% | 8,5% |
| express (termasuk `examples/`) | 73,9% | 16,0% |
| webpack, hanya file di luar tsconfig (contoh dan skrip) | 39,9% | 28,1% |

Sisanya terutama skrip dan contoh. Usulan: tidak ada symbol `<module>` per file di v1, karena
symbol itu juga mengubah graph repo TS dan sisanya kecil pada repo aplikasi. Angka "setelah"
adalah perkiraan dari bentuk statement, bukan hasil implementasi.

### Entry point

- **Express, J3:** chain `router.route(path).get(h).post(m, h)`, dan bentuk CJS
  (`require('express')`, `module.exports = router`, `app.use('/api', require('./routes/x'))`).
- **Next dalam JS:** adapter yang ada bekerja tanpa perubahan (11 entry point di repo ukur
  **(J0)**). Nest dalam JS tidak diukur.
- **Repo CJS tanpa adapter sekarang tidak punya entry point sama sekali.** Generic fallback
  butuh `exported`, dan di CJS hampir tidak ada symbol yang `exported` (node-express-boilerplate:
  2 dari 209 symbol, 0 entry point **(J0)**). Perbaikan `exported` di J2 yang menutup ini.
- **Tanpa adapter:** generic fallback (fungsi yang diekspor dan tidak dipanggil). Framework yang
  dikenal tapi tanpa adapter tetap dilaporkan di `unmatched_frameworks`.
- Middleware tidak menjadi bagian flow: entry point adalah handler terakhir **(dinalar)**. Ini
  batasan yang sama dengan Express TS sekarang, dan tidak diubah di plan ini.

### Report

- `report.projects[*]` mendapat `config` (path config-nya) dan `language: "ts" | "js"`. Kunci
  `tsconfig` tetap ada untuk project TS.
- `files_skipped` mendapat kunci `config`, `minified`, `syntax`, dan `unsupported` (jumlah file
  `.vue`/`.svelte` yang ada di repo).
- `imports_resolved_share` dan contoh import yang tidak ter-resolve.
- `diagnostics_sample` kosong untuk project JS (`checkJs: false`) **(diuji)**. Label
  "TypeScript diagnostics" di `RepoDetail.tsx:221` menjadi "Diagnostics".

### Prompt agent

Kata "TypeScript repos" di prompt agent kode, MCP dan CLI diubah menjadi
"TypeScript/JavaScript repos" di **J5**, bersama bump `AGENT_VERSION` (sekarang "3.8"). Bump itu
membatalkan cache eval golden TS, jadi dilakukan di fase yang memang menjalankan eval.

## Fase dan PR

Satu PR per fase dari `main`, tidak bertumpuk. J0 tidak mengubah kode produk.

| Fase | Isi | Verifikasi (tanpa LLM kecuali J5) |
|---|---|---|
| J0 Survei | **Selesai 2026-11-19.** Fungsi sidecar yang ada dijalankan pada 9 repo publik di SHA yang dipatok | Tabel di bagian Hasil. Target angka sudah diisi; pemetaan workspace dan tebakan folder masuk J2; perbaikan Next dicoret; D9 punya angka |
| J1 Project | Project JS virtual dengan aturan kepemilikan (file yang sudah milik project TS tetap di sana), `jsconfig.json`, aturan skip termasuk `fixtures/` dan file besar, graph kosong tanpa error, `config`/`language`/`files_skipped` di report | `npm test`: fixture `fixture-js` (dua `package.json`, JSX di `.js`, file config, `.min.js`, file test, satu file dengan sintaks Flow) dan `fixture-mixed` (root `package.json` tanpa tsconfig, satu paket TS, `scripts/x.js`): keluaran project TS identik dengan sebelum perubahan. Fixture TS lama lulus. Fixture `fixture-tsjs` (tsconfig tanpa `allowJs` di samping `lib/*.js`, bentuk axios): file JS masuk project JS. Pada tiap repo ukur: jumlah file cocok dengan dianalisis + dilewati; axios memberi project root dengan 92 file; keluaran webpack tidak berubah |
| J2 Modul | Aturan penugasan fungsi, `exported` (ESM dan CJS), edge untuk `module.exports = { }`, edge `imports` dari `require`, pemetaan workspace, tebakan folder untuk alias, `imports_resolved_share`, pemeriksaan `owningSymbol` | `npm test`: `fixture-js` (ESM, `export { a, b }`) dan `fixture-cjs` (`exports.x`, `module.exports = { }`, `prototype`, `require`). Fixture workspace (dua paket yang saling import dengan nama paket). Pada repo ukur: angka target di tabel Ukuran berhasil; `affected_files` diuji pada proshop (ESM) dan node-express-boilerplate (CJS) |
| J3 Entry point | Chain `route()`, Express CJS | `npm test` pada fixture ESM dan CJS. Daftar route proshop dan node-express-boilerplate ditulis di test dan ditemukan |
| J4 API | Flag `CODE_JAVASCRIPT`, `is_js_repo`, ekstensi JS di `repo_limits` dan `git_blob_shas` (termasuk untuk jalur TS, D10), repo JS tanpa Postgres dilewati dengan pesan, pesan untuk graph kosong, teks error dan docs (termasuk tabel cakupan) | pytest: deteksi (TS saja, JS saja, campuran, Markdown + `package.json` tanpa JS, flag mati), `git_blob_shas` dengan `.js`, repo JS tanpa graph backend tidak error |
| J5 Ukur | Index tiap repo ukur dengan LLM, mini check, prompt + `AGENT_VERSION`, perluas `read_ts_files` ke JS, jalankan ulang golden TS split `dev` | Angka per repo di bagian Hasil. Golden TS `dev` tidak turun dari 10/10 |

## Kumpulan repo ukur (J0)

Dipilih supaya tiap sumbu di tabel cakupan diwakili minimal satu repo. Semuanya repo publik,
di-clone dangkal pada 2026-11-19 dan hanya dibaca; tidak ada kode repo yang dijalankan.

| # | Repo | SHA | Bentuk yang diwakili |
|---|---|---|---|
| 1 | `bradtraversy/proshop_mern` | `1d20b5b` | Express ESM + frontend React, dua `package.json` |
| 2 | `hagopj13/node-express-boilerplate` | `179ae84` | Express CommonJS, satu paket |
| 3 | `expressjs/express` | `43fca52` | Library CommonJS gaya lama (`app.x = function`), dengan `examples/` |
| 4 | `axios/axios` | `9f0f4dd` | Library ESM dengan `tsconfig.json` tanpa `allowJs` |
| 5 | `adrianhajdin/project_next_14_ai_prompt_sharing` | `29d79a7` | Next.js dalam JS, alias lewat `jsconfig.json` |
| 6 | `coreui/coreui-free-react-admin-template` | `2609f59` | Frontend React + Vite, alias hanya di config build |
| 7 | `PanJiaChen/vue-element-admin` | `6858a9a` | Vue: sebagian besar kode di file `.vue` |
| 8 | `facebook/create-react-app` | `6254386` | Monorepo workspace, CJS dan ESM campur, Flow |
| 9 | `webpack/webpack` | `789b7e35` | Repo besar, CJS, `tsconfig.json` ber-`allowJs` |

Bentuk yang tidak terwakili: NestJS dalam JS, Fastify/Koa/Hono, pnpm workspace, `.svelte`,
`imports` (`#x`) di `package.json`, dan repo milik owner sendiri. Repo milik owner lebih baik
daripada repo contoh, karena itulah yang nanti benar-benar di-index (D3).

## Mini check (J5)

- 5 pertanyaan per repo yang di-index di J5 (D12), ditulis sebelum index dijalankan, masing-masing dengan file dan
  symbol yang diharapkan muncul di sitasi.
- Pertanyaan tidak boleh bergantung pada batasan yang sudah dinyatakan (middleware, bentuk yang
  tidak didukung).
- Label draft oleh Claude, dicek owner (D6).
- Lulus per repo: minimal 4 dari 5. Kalau kurang, penyebab tiap kegagalan dicatat sebelum ada
  perubahan kode lagi.

## Biaya

- J0–J4 tanpa LLM.
- J5: index repo pilihan D12 dengan flow summary dan embedding, 5 pertanyaan agent per repo, dan
  run ulang golden TS split `dev` (11 pertanyaan). Empat repo usulan D12 berjumlah 225 file dan
  sekitar 17 ribu baris kode **(J0)**. Belum ada angka token. Perlu persetujuan owner (D8).

## Risiko

| Risiko | Dampak | Mitigasi |
|---|---|---|
| Kumpulan repo ukur tetap tidak mewakili repo yang nanti di-index | Lulus ukuran, tetapi repo berikutnya buruk | Report menghitung file dilewati, import tak ter-resolve, dan framework tanpa adapter, jadi kekurangan terlihat per repo |
| Tanpa tipe, banyak panggilan tidak ter-resolve (proshop: 21,7% di dalam repo) | Flow pendek, sitasi sedikit | Diukur per repo di J0 dan J5; mini check menentukan apakah cukup |
| Aturan penugasan fungsi menghasilkan terlalu banyak symbol (misalnya handler kecil `x.onclick = ...`) | Graph berisik | Hanya penugasan top-level; jumlah symbol per repo dibandingkan sebelum dan sesudah di J2 |
| Perbaikan `exported` mengubah entry point generic di repo TS | Flow repo TS berubah pada sync berikutnya | Fixture TS dijalankan ulang, perubahan dicatat di PR |
| Project virtual root di monorepo TS menambah symbol dari `scripts/` | Graph repo TS bertambah | Hanya saat flag menyala; `fixture-mixed` memastikan project TS sendiri tidak berubah |
| Alias di config build tidak ter-resolve | Edge antar file hilang di frontend | Dihitung di `imports_resolved_share`; `jsconfig.json` `paths` sebagai jalan keluar yang didokumentasikan |
| Bundle besar tanpa nama `.min.js` | Limit `CODE_MAX_*` tercapai sebelum sidecar melewatinya | Error menyebut env; limit bisa dinaikkan |
| Waktu dan memori pada repo JS besar | Sync lambat atau gagal | J0: webpack 33,7 detik dan 1,3 GB untuk 390 ribu baris, yaitu 7,8× limit baris default. Repo di dalam limit belum diukur di ukuran itu |
| `examples/` dan `docs/` ikut ter-index di repo library | Entry point dan flow dari contoh, bukan dari kode utama (express: 43 dari 50 file yang dianalisis ada di `examples/`, hanya 6 di `lib/`) | Tidak dilewati di v1; terlihat di `report.projects` dan daftar entry point. Ditinjau lagi setelah J5 |
| Tebakan folder me-resolve import ke file yang salah | Edge salah | Hanya untuk import yang bukan dependency di `package.json` mana pun dan yang file tujuannya ada |
| Perbaikan limit (D10) menolak repo yang sekarang ter-index | Sync yang tadinya jalan menjadi gagal | Keputusan owner; error menyebut env dan limit bisa dinaikkan |

## Keputusan owner

Sudah diputuskan:

- **Cakupan (2026-11-19).** Proshop hanya contoh; plan dibuat untuk repo JS secara umum.

Terbuka:

- **D1 Aturan deteksi.** Usulan: `package.json` atau `jsconfig.json` plus file JS.
- **D2 Project tanpa adapter.** Usulan: tetap di-index dengan generic fallback.
- **D3 Repo ukur.** J0 memakai 9 repo publik pilihan Claude (bagian Hasil). Owner boleh menambah
  atau mengganti dengan repo yang memang ingin di-index; skrip survei bisa dijalankan ulang.
- **D4 Limit.** Usulan: TS dan JS memakai `CODE_MAX_FILES`/`CODE_MAX_LOC` yang sama.
- **D5 Repo campuran TS dan JS.** Usulan: config terdekat memiliki file, sehingga kode JS di luar
  project TS ikut ter-index saat flag menyala. Alternatif: repo yang punya tsconfig tidak
  disentuh sama sekali, dan repo campuran tetap setengah ter-index.
- **D6 Mini check.** Usulan: 5 pertanyaan per repo, label draft Claude dicek owner, lulus 4 dari
  5 per repo.
- **D7 Default flag.** Usulan: `CODE_JAVASCRIPT` mati sampai J5 lulus, lalu owner memutuskan.
- **D8 Biaya J5.** Persetujuan sebelum index dengan LLM dan run ulang golden `dev`.
- **D9 Kode top-level.** Usulan setelah J0: tidak ada symbol `<module>` per file di v1. Sisa kode
  di luar symbol diperkirakan 0–3,6% pada repo aplikasi, 8,5–16% pada dua repo CJS.
- **D10 Limit untuk repo JS ber-tsconfig.** Hari ini `CODE_MAX_FILES`/`CODE_MAX_LOC` tidak
  menghitung file JS yang di-index lewat `allowJs`. Usulan: perbaiki di J4, tidak di belakang
  flag, dengan catatan di changelog. Alternatif: biarkan sampai flag dinyalakan.
- **D11 Batas ukuran per file.** Usulan: file di atas 10.000 baris dilewati dan dihitung. Angka
  10.000 adalah pilihan, bukan hasil ukur; file terbesar yang ingin dilewati di J0 adalah 41
  ribu baris.
- **D12 Repo untuk J5.** Index dengan LLM pada 9 repo terlalu mahal untuk sebuah cek. Usulan: 4
  repo yang kecil dan berbeda bentuk: proshop (Express ESM + React), node-express-boilerplate
  (Express CJS), repo Next (jsconfig), axios (library, tsconfig tanpa `allowJs`).

## Hasil

### J0 Survei (2026-11-19)

**Cara ukur.** Skrip sementara memanggil fungsi sidecar yang ada (`fileSymbols`, `buildEdges`,
adapter Express/Next/Nest, `generic.roots`) pada project ts-morph tanpa tsconfig, dengan aturan
project dan skip dari plan ini. Jadi angkanya menunjukkan apa yang dihasilkan kode symbol dan
edge **sekarang** kalau file JS sampai ke sana. Untuk axios dan webpack, sidecar asli juga
dijalankan (`node index.mjs`), karena keduanya punya `tsconfig.json`. Tanpa LLM, tanpa Postgres.
Skripnya ada di folder scratchpad sesi dan belum disimpan di repo.

Batas cara ukur:

- "Route ada" dihitung dari pola panggilan (`x.get('/path', ...)` dan chain `route()`), belum
  dicek tangan, kecuali proshop.
- "Setelah aturan" pada kolom kode di luar symbol adalah perkiraan dari bentuk statement.
- Import ke aset (CSS, gambar, JSON) dan ke file `.vue` tidak dihitung sebagai import kode.

**Per repo.** "Dianalisis" adalah file JS setelah aturan skip. "Di luar symbol" adalah bagian
baris kode top-level yang tidak masuk symbol mana pun, sekarang → perkiraan setelah aturan J2/J3.

| Repo | File JS dianalisis / ada | Baris kode | Modul | Symbol | Fungsi tanpa symbol | Di luar symbol | Edge `imports` | Panggilan dalam repo | Entry point sekarang |
|---|---|---|---|---|---|---|---|---|---|
| proshop_mern | 61 / 61 | 4.867 | ESM | 253 | 1 | 1,8% → 1,8% | 262 | 21,7% | 4 dari 25 route; 17 generic di frontend |
| node-express-boilerplate | 42 / 50 | 1.260 | CJS | 209 | 2 | 21,7% → 8,5% | 0 | 16,1% | 0 (14 route terlihat) |
| express | 50 / 142 | 2.835 | CJS | 234 | 78 | 73,9% → 16,0% | 0 | 2,6% | 2 generic |
| axios | 101 / 254 | 10.305 | ESM | 507 | 7 | 3,4% → 2,0% | 251 | 32,4% | 10 generic |
| repo Next | 21 / 24 | 863 | ESM | 43 | 0 | 0% → 0% | 47 | 13,0% | 11 dari adapter Next |
| coreui | 81 / 83 | 13.950 | ESM | 216 | 0 | 0,3% → 0,2% | 292 | 13,9% | 12 generic |
| vue-element-admin | 76 / 89 | 4.387 | ESM + CJS | 211 | 8 | 18,7% → 3,6% | 77 | 14,2% | 24 generic |
| create-react-app | 126 / 273 | 91.072 | CJS + ESM | 588 | 25 | 93,5% (dua bundle) | 101 | 9,1% | 11 generic |
| webpack, di luar tsconfig | 414 / 25.461 | 3.586 | CJS + ESM | 423 | 42 | 39,9% → 28,1% | 192 | 13,0% | 19 generic |

**File yang dilewati.**

| Repo | Test | Config | Minified | Error sintaks | Sudah milik project TS |
|---|---|---|---|---|---|
| node-express-boilerplate | 7 | 1 | 0 | 0 | 0 |
| express | 92 | 0 | 0 | 0 | 0 |
| axios | 147 | 6 | 0 | 0 | 0 |
| repo Next | 0 | 3 | 0 | 0 | 0 |
| coreui | 0 | 2 | 0 | 0 | 0 |
| vue-element-admin | 6 | 6 | 1 | 0 | 0 |
| create-react-app | 105 | 5 | 0 | 37 | 0 |
| webpack | 24.050 | 91 | 20 | 3 | 883 |

**Import kode yang bukan paket** (ter-resolve / jumlah).

| Repo | Relatif | Alias | Workspace |
|---|---|---|---|
| proshop_mern | 129 / 129 | — | — |
| node-express-boilerplate | 81 / 81 | — | — |
| express | 54 / 54 | 0 / 3 | — |
| axios | 196 / 196 | — | 1 / 2 |
| repo Next | 2 / 2 | 17 / 17 (`jsconfig.json`) | — |
| coreui | 76 / 79 | 0 / 53; 53 dengan tebakan folder | — |
| vue-element-admin | 33 / 33 | 24 / 88; contoh yang gagal menunjuk file `.vue` | — |
| create-react-app | 82 / 84 | 1 / 10 | 0 / 30 |
| webpack, di luar tsconfig | 266 / 277 | 0 / 76 (antara lain import URL `https://`) | 0 / 3 |

**Sidecar asli pada repo ber-tsconfig (perilaku hari ini).**

| Repo | tsconfig | File ter-index | Symbol | Panggilan dalam repo | Edge `imports` | Entry point | Waktu | Memori puncak |
|---|---|---|---|---|---|---|---|---|
| axios | Tanpa `allowJs` | 13 (hanya `.ts`); 92 file JS di `lib/` dan root terlewat | 193 | 5,6% | 9 | 0 | 0,3 detik | 228 MB |
| webpack | `allowJs` + `checkJs` | 1.022 | 14.725 | 45,2% | 8 | 9 generic | 33,7 detik | 1,3 GB |

Di webpack `repo_limits` hanya akan menghitung 9 file `.ts`, padahal folder `lib/` saja berisi
828 file JS dan 390 ribu baris (limit default 50 ribu baris).

**Waktu dan memori skrip survei.** Di bawah 3 detik dan di bawah 750 MB untuk delapan repo;
webpack 38,8 detik dan 1,35 GB, sebagian besar untuk membaca 25 ribu file.

**Temuan yang mengubah plan.**

1. Repo JS ber-tsconfig adalah bentuk tersendiri, dengan dua sub-bentuk yang perilakunya
   berlawanan hari ini (axios hampir tidak ter-index; webpack ter-index penuh tanpa limit).
   Aturan kepemilikan file dan D10 berasal dari sini.
2. Repo CJS hari ini tidak memberi edge `imports` sama sekali, hampir tidak ada symbol
   `exported`, dan node-express-boilerplate tidak punya satu pun entry point. CommonJS wajib.
3. Aturan penugasan fungsi menutup sebagian besar kode di luar symbol (express 73,9% → 16,0%).
   Sisanya kecil pada repo aplikasi, jadi symbol `<module>` tidak diusulkan (D9).
4. Import antar paket workspace tidak ter-resolve (create-react-app 0 dari 30).
5. Alias yang menunjuk folder root project bisa ditebak tanpa membaca config build (coreui 53
   dari 53).
6. Next dalam JS sudah bekerja; `jsconfig.json` `paths` sudah bekerja.
7. Aturan minified tidak menangkap bundle yang tidak di-minify di `fixtures/` (create-react-app).
8. Tanpa tipe, panggilan dalam repo 2,6–32,4%. Dengan `checkJs` dan JSDoc (webpack) 45,2%.
9. Repo Vue: 131 file `.vue` tidak terbaca, dan flow-nya akan sangat tidak lengkap. Statusnya
   tetap "tidak didukung, dihitung".

### J1–J5

Belum ada.
