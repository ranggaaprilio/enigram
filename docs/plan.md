# Rencana kerja Enigram

Dibuat 2026-11-07, setelah PRD *Workspace selection* dan *Code page human annotation* selesai.
Urutan di bawah adalah urutan yang disarankan; tiap item bisa dikerjakan sebagai PR sendiri.

## Status sekarang

Per 2026-11-08 malam.

| Item | Status | PR |
|---|---|---|
| 1 Repo private | Selesai | #31 |
| 2 RBAC R1–R4 (peran, key multi-workspace, web sadar peran, login OIDC) | Selesai; Keycloak lokal untuk mencoba login | #32–#34, #36, #37, #51 |
| 3 Sesi PM/QA | **Belum** (di tangan pemilik; login per orang sudah bisa) | — |
| 4 UI U1–U4 (sidebar, pola halaman, palet ⌘K, aksesibilitas) | Selesai | #35, #39, #43, #48 |
| 5 Note A1–A3 (form baru, Knowledge + Inbox, duplikat dan bulk) | Selesai | #38, #40, #42 |
| 6 Dashboard D1–D3; Dashboard jadi halaman awal | Selesai | #44–#47, #52, #53 |
| 7 Memory M1–M2 (M3 tercakup test M1) | Selesai | #49, #50 |
| 8 Chat multi-agent | **Belum**; menunggu lima keputusan di bagian 8 | — |
| 9 Monorepo | Belum (bisa diuji pada `apps/web` repo ini) | — |
| 10 Hapus note | Selesai | #29 |
| 11 Keputusan tertunda | Selesai; bobot note per tipe di prompt diukur 2026-11-08 (lihat bagian 11) | #54 |
| 12 Perbaikan kecil Code | Selesai | #30 |
| 13 Adapter framework lain | Belum, menunggu repo nyata | — |
| 14 Fase 7 evaluasi penuh | Belum; paling akhir | — |
| 15 Rilis OSS (non-kode) | Belum, di tangan pemilik | — |

| PRD | Status |
|---|---|
| Enigram code knowledge (`enigram-code-knowledge.prd.md`) | Fase 0–6 selesai; **fase 7 (evaluasi penuh) belum** |
| Code page human annotation (`code-page-human-annotation.prd.md`) | Fase 1–6 selesai (fase 5 Mode File: PR #26) |
| Workspace selection (`workspace-selection.prd.md`) | Fase 1–4 selesai; RSS dengan 3 engine belum diukur |

Perbaikan di luar PRD: #21 (entry point Next.js dan sitasi path App Router), #22 (jawaban dokumen
mengutip `[n]` per kalimat), #23 (pertanyaan overview selalu ke kode dan dokumen).

**Cek regresi setelah #22 dan #23 (2026-11-08).**
- Agent kode, split `dev`: 11 jawaban identik dengan run 2026-11-05 (cache agent kena, prompt dan
  alur tidak berubah), jadi hasil dev 10/10 dan 0,925× token (`eval-20261105-121656`) tetap berlaku.
- Router: `golden route --split dev` target 100% (`route-20261108-065224`); `golden-docs` 29/30,
  meleset di p07 seperti sebelumnya (`route-20261108-065235`). Aturan overview #23 tidak mengubah
  rute golden.
- Jawaban dokumen (#22) **belum terukur**: dokumen Kestrel untuk `eval/golden-docs.yaml` tidak lagi
  ter-index (30/30 bukti tidak ditemukan; sumbernya hanya di `data/backup-before-phase1`).
  Diputuskan: ditunda ke fase 7 (item 14).

## Urutan yang disarankan

Nomor item tetap seperti bagian di bawah; urutan pengerjaan mengikuti gelombang (diputuskan
2026-11-08). Item 3 sengaja dipindah ke setelah form note baru (A1), dan R3 setelah kerangka UI (U1).

| Gelombang | Pekerjaan | Alasan |
|---|---|---|
| 0 | 11 keputusan dan keputusan item 1, 2, 4; cek regresi #22/#23; mulai 15 | **Selesai 2026-11-08**, kecuali 15 (non-kode, di tangan pemilik) |
| 1 | 10 hapus note, 12 perbaikan kecil Code | PR kecil |
| 2 | 1 repo private → 2 R1–R2 | Penghalang utama pemakaian nyata; sebelum RBAC, kredensial dibatasi `ANNOTATION_VERIFIERS` |
| 3 | 4 U1 → 2 R3 → 5 A1 | Halaman baru dibangun di sidebar; form note baru siap diuji |
| 4 | 3 sesi PM/QA | Menguji form baru sebagai `contributor`, bukan aktor `web` |
| 5 | 5 A2–A3, 4 U2 (U3–U4 menyusul) | Inbox dibentuk dari temuan sesi |
| 6 | 6 Dashboard, 7 Memory | D1 dan M1 (API saja) boleh mulai lebih awal |
| 7 | 9 monorepo, 13 adapter | Saat ada repo nyata yang butuh; 9 naik sebelum gelombang 4 bila repo sesi PM/QA monorepo |
| 8 | 2 R4 (OIDC) → 8 Chat | Sesi chat butuh pemilik per orang |
| 9 | 14 evaluasi penuh → rilis | Paling mahal; setelah semua stabil |

| # | Pekerjaan | Jenis | Biaya token |
|---|---|---|---|
| 1 | Index repo private | Teknis | Rendah |
| 2 | RBAC, plus API key dan MCP untuk banyak workspace | Teknis | Nol |
| 3 | Sesi uji Code page dengan PM/QA | Validasi | Nol |
| 4 | Perombakan UI/UX: sidebar, kerangka aplikasi, pola halaman | Teknis | Nol |
| 5 | Gabung halaman Code dan Annotations, form note yang intuitif | Teknis | Nol |
| 6 | Dashboard pemakaian, kualitas dan cakupan | Teknis | Nol |
| 7 | Halaman Memory: melihat dan mengelola memory agent | Teknis | Rendah |
| 8 | Chat interaktif multi-agent dengan fokus masing-masing | Teknis | **Sedang–tinggi** |
| 9 | Monorepo dan `tsconfig.json` di luar root | Teknis | Rendah |
| 10 | Endpoint hapus note | Teknis | Nol |
| 11 | Keputusan produk yang tertunda | Keputusan | Nol (**diputuskan 2026-11-08**) |
| 12 | Perbaikan kecil halaman Code | Teknis | Nol |
| 13 | Adapter framework lain | Teknis | Rendah |
| 14 | Fase 7: evaluasi penuh | Eval | **Tinggi** |
| 15 | Persiapan rilis OSS (non-kode) | Legal | Nol |

---

## 1. Index repo private

**Masalah.** `application/repo_sync.py` menolak URL dengan kredensial, jadi hanya repo publik yang
bisa di-index. Untuk pemakaian sungguhan (repo kantor), ini penghalang utama.

**Lingkup.**
- Pilih mekanisme: GitHub App, personal access token, atau SSH deploy key (lihat keputusan di bawah).
- Simpan kredensial per repo atau per workspace, tidak pernah tampil di log, job payload, trace
  atau respons API.
- Clone dan fetch (termasuk webhook dan polling) memakai kredensial itu.
- UI Documents: kolom kredensial saat index repo.

**Diputuskan 2026-11-08.** Personal access token (fine-grained, read-only) untuk clone HTTPS,
disimpan **per repo**.

**Selesai bila.** Satu repo private ter-index dari web UI, re-sync lewat webhook berjalan, dan
pencarian log/DB tidak menemukan token.

## 2. RBAC, plus API key dan MCP untuk banyak workspace

**Masalah.** Workspace sudah memisahkan *data*, tetapi belum memisahkan *akses*:

- Web UI memakai satu key (`ENIGRAM_WEB_KEY`, disuntikkan nginx). Semua orang di UI tercatat
  sebagai aktor `web` dan bisa memverifikasi note; nama `by` hanya teks bebas.
- API key (`ENIGRAM_API_KEYS`) punya nama tetapi tidak punya peran. Satu-satunya peran adalah daftar
  `ANNOTATION_VERIFIERS`.
- Setiap key bisa membuka **semua** workspace lewat header `X-Enigram-Workspace`.
- MCP (stdio dan `/mcp` HTTP) selalu memakai workspace proses (`WORKSPACE`), jadi agent hanya bisa
  memakai satu workspace per server.
- Key disimpan di `.env`; menambah atau mencabut key butuh restart.

**Tujuan.** Setiap pemanggil (orang, key, agent) punya peran per workspace; aksi ditolak bila
perannya tidak cukup; satu key dan satu server MCP bisa dipakai untuk beberapa workspace yang
memang diizinkan.

### Peran (per workspace)

| Peran | Untuk | Boleh |
|---|---|---|
| `viewer` | Pembaca | Baca dokumen, Ask, Trace, halaman Code dan Annotations |
| `contributor` | PM/QA, agent | + note sebagai *proposal*, ✓ *Looks right*, *Not right*, *Add context*, `add_memory`, `propose_annotation` |
| `maintainer` | Developer | + verifikasi/tolak note, upload dan hapus dokumen, index dan hapus repo, edit graph, deteksi konflik |
| `admin` | Pemilik workspace | + kelola anggota dan peran, kredensial repo private, secret webhook, budget workspace |

Ditambah satu peran global `owner` (buat workspace, kelola semua). Matriks lengkap per endpoint
dan tool MCP: [`rbac.md`](rbac.md). `ANNOTATION_VERIFIERS` digantikan oleh `maintainer`.

### API key untuk banyak workspace

- Key menjadi baris di DB (`principals`: nama, hash key, jenis `user`/`key`), dengan keanggotaan
  per workspace (`workspace_members`: workspace, principal, peran). Dikelola lewat API/CLI tanpa
  restart; key hanya ditampilkan sekali saat dibuat.
- Satu key bisa menjadi anggota beberapa workspace dengan peran berbeda, dan punya
  **workspace default** untuk request tanpa header.
- Request ke workspace yang bukan keanggotaannya mendapat 404 (keberadaan workspace tidak bocor);
  aksi di luar perannya mendapat 403.
- `GET /workspaces` hanya mengembalikan workspace keanggotaan pemanggil; `GET /me` mengembalikan
  principal dan perannya per workspace.

### MCP untuk banyak workspace

- MCP HTTP menerima workspace per sesi: header `X-Enigram-Workspace` pada koneksi, atau path
  `/mcp/w/<workspace>`; tanpa keduanya, workspace default milik key.
- Tool baru `list_workspaces` (workspace keanggotaan key). Tool yang ada menerima argumen opsional
  `workspace`, dicek terhadap keanggotaan dan peran yang sama seperti API.
- Engine, budget dan log query memakai workspace yang dipilih, bukan workspace proses.
- MCP stdio tetap untuk pemakaian lokal pemilik: `WORKSPACE` dari env, tanpa RBAC.

### Web UI

- `GET /me` menentukan apa yang tampil: tombol di luar peran disembunyikan atau dinonaktifkan,
  pemilih workspace hanya berisi keanggotaan, halaman anggota untuk `admin`.
- Tahap login per orang (bukan satu web key): OIDC (Google Workspace, Microsoft Entra atau GitHub)
  dengan sesi cookie; `by` pada note dan review diisi dari user yang login.

### Tahapan

| Tahap | Isi | Selesai bila |
|---|---|---|
| R1 | Tabel `principals` dan `workspace_members`, resolusi key → principal, cek peran per route, `GET /me`, CLI kelola key/anggota, migrasi | Test tabel route × peran lulus (403/404 sesuai matriks); key lama tetap jalan |
| R2 | API key multi-workspace dengan workspace default; MCP HTTP per workspace (`/mcp/w/<ws>`, `list_workspaces`, argumen `workspace`) | Satu key dan satu koneksi MCP memakai dua workspace; key tanpa keanggotaan ditolak |
| R3 | Web sadar peran: aksi mengikuti `GET /me`, pemilih workspace terfilter, halaman anggota | Contributor tidak melihat tombol verifikasi; admin bisa menambah anggota dari UI |
| R4 | Login per orang lewat OIDC menggantikan `ENIGRAM_WEB_KEY` | Note dan review mencatat user yang login; tidak ada lagi aktor `web` bersama |

**Migrasi dan kompatibilitas.**
- Key di `ENIGRAM_API_KEYS` diimpor sebagai principal di semua workspace yang ada: key di
  `ANNOTATION_VERIFIERS` sebagai `admin`, key lain sebagai `maintainer`, supaya tidak ada yang rusak
  saat deploy; peran dipersempit sesudahnya (rincian di [`rbac.md`](rbac.md)).
- Tanpa key sama sekali (mode lokal), API tetap terbuka seperti sekarang.

**Diputuskan 2026-11-08.**
- Peran default anggota baru: `viewer`.
- Workspace `default` (korpus golden eval) mengikuti peran biasa, tanpa aturan khusus.
- MCP memilih workspace dengan keduanya: header/path per koneksi sebagai default, argumen
  `workspace` per tool untuk override.

**Diputuskan untuk R4 (2026-11-08).** OIDC generik (penyedia dipilih saat deploy lewat
`OIDC_ISSUER`); login pertama otomatis menjadi `viewer` di workspace proses, hanya untuk domain
email di `OIDC_ALLOWED_DOMAINS`; `ENIGRAM_WEB_KEY` tetap pilihan lewat env (tanpa OIDC, UI tetap
memakai key bersama seperti sebelumnya).

**Kaitan dengan item lain.** Kredensial repo private (item 1) hanya boleh diatur `admin`; sesi
PM/QA (item 3) lebih bermakna setelah R1–R3, karena PM/QA masuk sebagai `contributor`, bukan
sebagai satu aktor `web`.

## 3. Sesi uji Code page dengan PM/QA

**Masalah.** Syarat sukses fase 2 PRD Code page ("PM/QA mau dan bisa menganotasi") belum diuji
dengan orang sungguhan; fiturnya sudah ada.

**Lingkup.** Satu sesi 30–45 menit dengan 1–2 PM/QA pada repo yang mereka kenal: cari fitur, baca
summary, tandai *Looks right* / *Not right*, tambah konteks. Catat di mana mereka bingung.

**Selesai bila.** Ada catatan sesi, jumlah note dan review yang dibuat, dan daftar perbaikan UI
yang muncul dari sesi itu.

## 4. Perombakan UI/UX: sidebar, kerangka aplikasi, pola halaman

**Masalah.**
- Navigasi berupa tab di header (Documents, Ask, Code, Trace, Annotations); Graph tidak punya menu.
  Rencana ini menambah Dashboard, Memory, anggota/peran dan pengaturan: tab atas tidak cukup, dan di
  ponsel tab sudah menggulir ke samping.
- Pemilih workspace dan *New workspace* menempel di header; belum ada tempat untuk user, peran,
  atau pilihan tema (tema sekarang hanya mengikuti sistem).
- `DESIGN.md` (acuan gaya web sejak 2026-11-05) adalah spesifikasi **situs marketing** Framer:
  tipografi poster dan banyak ruang kosong, bukan aplikasi kerja yang padat. File ini juga belum
  di-commit.
- Pola antar halaman belum seragam: judul dan aksi, status loading/kosong/error, umpan balik setelah
  aksi (sekarang `ErrorAlert` di dalam halaman, tanpa toast).

**Tujuan.** Kerangka aplikasi yang tumbuh bersama fitur baru, terasa seperti alat kerja, dan sama
nyamannya di layar lebar dan ponsel.

### Lingkup

**Kerangka dan sidebar.**
- Sidebar kiri, bisa diciutkan menjadi ikon (status diingat per browser), dengan kelompok:

  | Kelompok | Halaman |
  |---|---|
  | Tanya | Ask, Trace |
  | Pengetahuan | Documents, Code & Notes (gabungan, item 5), Graph, Memory |
  | Tinjau | Inbox review note (item 5), konflik dokumen-kode |
  | Pantau | Dashboard |
  | Pengaturan | Workspace, anggota dan key (setelah RBAC) |

- Pemilih workspace di atas sidebar (termasuk *New workspace*); menu user/key, peran, dan pilihan
  tema (terang/gelap/sistem) di bawahnya.
- Ponsel: sidebar menjadi drawer dari tombol menu; bar atas berisi judul halaman.
- Menu mengikuti peran (item 2): halaman yang tidak boleh dibuka tidak tampil.

**Pola halaman.**
- Header halaman seragam: breadcrumb (workspace / halaman / item), judul, aksi utama di kanan.
- Status loading (skeleton), kosong (dengan langkah berikutnya) dan error yang sama di semua halaman.
- Toast untuk hasil aksi (tersimpan, dihapus, job masuk antrean), error tetap tampil di tempatnya.
- Kepadatan "aplikasi": ukuran huruf, jarak dan tabel yang lebih rapat dari `DESIGN.md` sekarang.

**Navigasi cepat (opsional).** Command palette (⌘K / Ctrl+K): pindah halaman, ganti workspace,
cari symbol, dokumen atau trace terakhir.

**Aksesibilitas.** Landmark dan urutan fokus yang benar, navigasi keyboard di sidebar dan palette,
kontras WCAG AA di terang dan gelap.

### Lingkup teknis

- Komponen shadcn/ui yang belum ada: Sidebar, Sheet (drawer), Dialog, Tooltip, Dropdown Menu,
  Skeleton, Sonner (toast), Command (palette). Tetap Tailwind v4 dan Inter yang dibundel.
- Rute tetap `/w/<workspace>/<page>`; yang berubah hanya kerangka di `App.tsx` dan komponen bersama.
- Test web: teks dan nama tombol yang dipakai test tidak diubah tanpa alasan; test navigasi di
  `App.test.tsx` diperbarui untuk sidebar.
- Cek browser tiap tahap: terang/gelap, 1360px dan 390px, tanpa scroll horizontal.

### Tahapan

| Tahap | Isi | Selesai bila |
|---|---|---|
| U1 | Arah desain diputuskan; kerangka sidebar + drawer ponsel; pemilih workspace dan menu user/tema pindah ke sidebar | Semua halaman yang ada bisa dicapai dari sidebar di desktop dan ponsel; test web hijau |
| U2 | Pola halaman: header + breadcrumb, skeleton/kosong/error seragam, toast | Semua halaman memakai pola yang sama; tidak ada lagi status kosong buatan sendiri |
| U3 | Command palette | Pindah halaman, ganti workspace dan buka symbol tanpa mouse |
| U4 | Audit aksesibilitas dan rapikan | Kontras AA, fokus dan keyboard lulus di semua halaman |

**Diputuskan 2026-11-08.**
- Arah desain: **acuan baru bergaya alat kerja** (Linear, Vercel dashboard), tetap Inter dan
  shadcn/ui. `DESIGN.md` Framer tidak dipakai; acuan baru ditulis dan di-commit sebagai langkah
  pertama U1.
- Sidebar default terbuka; command palette (U3) nanti, setelah U1–U2.
- Aturan lama (terang sebagai default, satu kartu sorotan) **dibuka lagi**: ditentukan ulang saat
  menulis acuan baru. Pilihan tema terang/gelap/sistem di menu user tetap masuk lingkup.
- Acuan baru: [`design.md`](design.md) (U1, 2026-11-08). Tema default ikut sistem; kartu sorotan
  dihapus (netral, warna hanya untuk status); kepadatan rapat, body 14px.

**Urutan.** U1 dikerjakan sebelum Dashboard (item 6) dan Memory (item 7), supaya halaman baru
langsung dibangun di kerangka baru. U2–U4 bisa berjalan bersamaan dengan item lain.

## 5. Gabung halaman Code dan Annotations, form note yang intuitif

**Masalah.**
- Dua halaman untuk satu pekerjaan. **Code** menampilkan symbol, summary dan note-nya dengan form
  terpandu (`GuidedNoteForm`: *Not right* / *Add context*). **Annotations** berisi tabel semua note,
  filter status, riwayat, aksi review, dan form kedua (`NewNoteForm`).
- Form di Annotations teknis: pilih jenis target (`symbol`/`entity`/`document`), pilih repo, cari
  symbol dengan tombol *Find* atau ketik id target mentah, pilih tipe, isi nama, pilih `scope`
  (`deps`/`body`) tanpa penjelasan. PM/QA tidak tahu id symbol atau arti `deps`.
- Review note terpisah dari konteksnya: di tabel Annotations, reviewer tidak melihat summary atau kode
  yang dibicarakan note, jadi harus bolak-balik ke halaman Code.

**Tujuan.** Satu tempat untuk membaca pengetahuan dan menulis/meninjau note, dengan satu form yang
bisa dipakai PM/QA tanpa tahu struktur kode.

### Satu halaman, tiga tampilan

Halaman **Code & Notes** (nama final diputuskan bersama item 4):

| Tampilan | Isi | Menggantikan |
|---|---|---|
| **Jelajah** | Fitur atau file (seperti sekarang), summary, langkah, *Called by*, note pada symbol | Halaman Code |
| **Inbox** | Antrean kerja reviewer: proposal, *needs verification*, konflik dengan kode; tiap item menampilkan target, summary saat note ditulis vs sekarang, dan aksi di tempat | Bagian review di Annotations |
| **Semua note** | Tabel dengan filter (status, tipe, target, penulis, repo) dan riwayat per note | Tabel Annotations |

Note pada entity graph dan dokumen tetap didukung lewat target yang sama (lihat form di bawah).
`/annotations` dan link lama (`?id=`, `?status=`) dialihkan ke tampilan yang setara, jadi link di
Trace dan jawaban agent tetap jalan.

### Form note yang intuitif

Satu komponen **Tambah note** (panel samping), dibuka dari mana saja dengan target terisi:
Jelajah, Inbox, Trace, Graph, dan nanti Chat.

1. **Tentang apa?** Satu kotak cari untuk semua target: fitur/route (`POST /auth/login`), symbol
   (`AuthService.login`), file, entity, dokumen. Hasil dikelompokkan per jenis, dengan summary
   singkat supaya target yang benar mudah dikenali. Tidak ada lagi id mentah atau pilihan
   `symbol`/`entity`/`document`.
2. **Jenis catatan**, sebagai kartu dengan bahasa biasa dan contoh:
   - *Aturan bisnis* — "Bagaimana seharusnya, menurut bisnis"
   - *Perilaku yang diharapkan* — "Apa yang seharusnya terjadi bagi pengguna"
   - *Masalah yang diketahui* — "Apa yang salah atau belum beres"
   - *Catatan teknis* — "Hal yang perlu diketahui developer"
3. **Isi**, dengan pertanyaan pemandu per jenis (seperti `GuidedNoteForm`) dan summary target
   ditampilkan di samping, supaya catatan ditulis terhadap apa yang dilakukan kode.
4. **Opsi lanjutan (tertutup default)**: pemilik, tanggal cek ulang (pilihan cepat: 1 bulan,
   3 bulan, 6 bulan), dan cakupan cek ulang dalam bahasa biasa ("Tandai untuk dicek ulang bila
   kode ini berubah" / "... atau kode yang dipanggilnya berubah") menggantikan `body`/`deps`.

Tambahan:
- **Cek duplikat**: sebelum simpan, tampilkan note yang mirip pada target yang sama.
- **Pratinjau**: bagaimana note akan dikutip di jawaban (`note#N`, label `[human]`) dan apakah
  langsung berlaku atau menjadi proposal untuk direview.
- Nama penulis diisi otomatis (dari login setelah RBAC item 2; sebelum itu dari nama tersimpan).
- Validasi di tempat, simpan dengan Ctrl/⌘+Enter, toast setelah tersimpan.

### Lingkup teknis

- API pencarian target gabungan: `GET /targets?q=` mengembalikan route, symbol, file, entity dan
  dokumen yang cocok (memakai pencarian symbol, fitur dan graph yang sudah ada).
- Satu komponen form menggantikan `NewNoteForm` dan `GuidedNoteForm`; *Not right* dan
  *Add context* membuka form yang sama dengan jenis dan pertanyaan terisi.
- Inbox memakai filter API yang sudah ada (`status`, proposal, konflik) ditambah konteks target
  (summary) per item.
- Test web untuk Annotations dan kedua form ditulis ulang; nama aksi yang dipakai agent/MCP tidak
  berubah.

### Tahapan

| Tahap | Isi | Selesai bila |
|---|---|---|
| A1 | `GET /targets` dan form **Tambah note** baru, dipakai di halaman Code dan Annotations yang ada | PM/QA bisa menambah note pada fitur tanpa tahu id symbol; kedua form lama terhapus |
| A2 | Halaman gabungan: Jelajah, Inbox, Semua note; pengalihan dari `/annotations` | Semua aksi review bisa dilakukan dari Inbox dengan konteks; link lama tetap jalan |
| A3 | Cek duplikat, pratinjau kutipan, aksi massal di Inbox (verifikasi/tolak beberapa note) | Reviewer memproses antrean tanpa membuka tiap note satu per satu |

**Diputuskan 2026-11-08 (A2).**
- Nama halaman gabungan: **Knowledge** (tab *Explore* dan *All notes*); kelompok sidebar-nya
  menjadi *Library* (Documents, Knowledge, Graph) supaya tidak dobel nama.
- Inbox: **halaman sendiri**, *Review → Inbox*, dengan badge jumlah yang menunggu.
- Note pada entity dan dokumen tetap di halaman Knowledge (dan Inbox).

**Urutan.** Setelah U1 (item 4). Sesi PM/QA (item 3) paling bermakna bila memakai form baru, jadi
bisa dijadwalkan setelah A1.

## 6. Dashboard pemakaian, kualitas dan cakupan

**Masalah.** Angka penting tersebar di tabel dan hanya terlihat lewat SQL: berapa token terpakai
dibanding `DAILY_TOKEN_BUDGET`, berapa pertanyaan tidak terjawab, berapa kalimat jawaban berlabel
`[unsupported]`, berapa note menunggu review, repo mana yang lama tidak ter-index. Pemilik tidak
bisa melihat kesehatan sebuah workspace sekilas.

**Tujuan.** Satu halaman **Dashboard** per workspace (`/w/<ws>/dashboard`) yang menjawab: berapa
biayanya, seberapa baik jawabannya, seberapa lengkap pengetahuannya, dan apa yang perlu ditindak.

### Panel dan sumber datanya

Semua angka dihitung dari tabel yang sudah ada; tidak ada pencatatan baru.

| Panel | Isi | Sumber |
|---|---|---|
| Pemakaian token | Token per hari (30 hari) per sumber (`query`, `job:<kind>`) dan per key; sisa budget hari ini | `enigram_token_usage`, `DAILY_TOKEN_BUDGET`, `PER_KEY_DAILY_TOKENS` |
| Pertanyaan | Jumlah per hari, % tidak terjawab (`found = false`), latensi p50/p95, error | `enigram_queries` |
| Kualitas jawaban | Pembagian rute (kode, dokumen, keduanya; `rule`/`choice`/`unsure`), porsi kalimat per label asal (`[code]`, `[document]`, `[human]`, `[unsupported]`) | `enigram_queries.result` (`extra.route`, `extra.origins`) |
| Pengetahuan | Dokumen per status, repo dengan commit dan waktu index terakhir, jumlah symbol dan entry point | `lightrag_doc_status`, `code_repos`, `code_symbols`, `code_entrypoints` |
| Cakupan review | % entry point dengan ✓ *Looks right* yang masih berlaku, note per tipe dan status | `symbol_reviews`, `annotations` |
| Perlu ditindak | Proposal menunggu review, note `needs_verification`, konflik dokumen-kode, job gagal atau `budget_exceeded` 7 hari terakhir | `annotations`, `doc_code_conflicts`, `enigram_jobs` |

Setiap angka di "Perlu ditindak" menautkan ke halaman yang sudah ada (Annotations dengan filter,
Trace, Documents), jadi dashboard tidak menduplikasi daftar.

### Detail per repo dan dokumen

Panel *Pengetahuan* membuka halaman detail, supaya pertanyaan "repo ini terakhir di-update kapan dan
seberapa lengkap index-nya" terjawab tanpa SQL.

**Tabel repo** (satu baris per repo, bisa diurutkan):

| Kolom | Isi | Sumber |
|---|---|---|
| Repo dan ref | URL, branch/ref, nama | `code_repos` |
| Commit ter-index | SHA pendek dan **tanggal commit** itu | `code_repos.indexed_sha`; tanggal commit sekarang hanya ada di hasil job (`code.commit_at`), sebaiknya ikut disimpan di `code_repos` |
| Terakhir di-index | Waktu index terakhir, "x jam lalu" | `code_repos.indexed_at`, `code_index_runs` |
| Status terhadap remote | Terbaru / tertinggal (commit baru di remote belum ter-index) / tidak bisa dicek | Polling `git ls-remote` yang sudah ada (`REPO_POLL_SECS`); perlu mencatat SHA remote dan waktu cek terakhir |
| Pembaruan otomatis | Webhook terpasang, polling, atau manual | `code_repos.webhook_secret`, `REPO_POLL_SECS` |
| Index terakhir | Sukses/gagal, durasi, token, pesan error | `enigram_jobs` (`index_repo`) |
| Ukuran | File, symbol, entry point, dokumen Markdown dari repo | `code_repos.report`, `code_entrypoints`, dokumen per repo |

**Detail satu repo:**
- Riwayat index: tiap run dengan commit, waktu, durasi, token, dan file/symbol yang berubah
  (`code_index_runs`, `code_symbol_changes`, hasil job).
- Kualitas graph: adapter yang terdeteksi (Nest, Next) atau fallback generik, porsi panggilan yang
  tidak ter-resolve di flow, flow yang terpotong, file yang dilewati (test, `.d.ts`, generated),
  contoh diagnostik TypeScript (semua sudah ada di `code_repos.report` dan hasil job).
- Summary: berapa symbol dan flow punya summary, berapa dari cache.
- Dampak ke note: hasil staleness run terakhir (note yang ditandai cek ulang, yatim, dipindah).
- Aksi: **Index ulang sekarang** (antrean job yang sudah ada), link ke halaman Code repo itu.

**Tabel dokumen:** nama, asal (upload atau repo), status, jumlah chunk, terakhir diperbarui, job
terakhir dan error-nya (`GET /documents` dan `enigram_jobs`).

### Lingkup teknis

- API baca saja: `GET /stats?days=30` untuk workspace aktif, dihitung di SQL (agregasi per hari),
  bukan di browser. Satu adapter Postgres baru di belakang port, seperti tabel lain.
- Halaman web dengan kartu angka dan grafik sederhana (garis/batang per hari); mengikuti desain
  yang ada (terang/gelap, satu kartu sorotan).
- Status kosong yang jelas untuk workspace baru (belum ada pertanyaan, belum ada repo).
- Setelah RBAC (item 2): dashboard boleh dilihat `viewer`; panel pemakaian token per key hanya
  untuk `admin`. Tampilan lintas workspace untuk peran `owner`.

### Tahapan

| Tahap | Isi | Selesai bila |
|---|---|---|
| D1 | `GET /stats` dan adapter agregasi | Angka sama dengan query SQL acuan pada data `default` dan `whiteboard`; test per panel, termasuk workspace kosong |
| D2 | Halaman Dashboard per workspace | Semua panel tampil di terang/gelap dan lebar 390px; tautan "Perlu ditindak" membuka halaman yang benar |
| D2b | Detail repo dan dokumen: tabel repo, halaman detail repo, tabel dokumen; catat SHA remote dan waktu cek dari polling | Untuk white-board dan nestjs-boilerplate tampil tanggal commit, waktu index, status terhadap remote, riwayat run; repo yang tertinggal ditandai |
| D3 | Tampilan lintas workspace untuk `owner` (butuh RBAC R1) | Total token semua workspace dibanding budget per workspace dalam satu tabel |

**Keputusan yang dibutuhkan.**
- Penonton utama: pemilik/admin (biaya, kesehatan) atau juga PM/QA (cakupan review)?
- Biaya dalam token saja, atau juga rupiah/dolar? `enigram_token_usage` tidak mencatat model dan
  tidak memisahkan token input/output, jadi biaya uang butuh kolom tambahan.
- Library grafik (mis. Recharts) atau SVG sendiri yang lebih ringan.
- Rentang data: 30 hari cukup, atau perlu pilihan 7/30/90 hari.

**Di luar lingkup.** Grafana/Prometheus, alert otomatis, dan pembaruan real-time; bisa menyusul
bila dashboard terbukti dipakai.

## 7. Halaman Memory: melihat dan mengelola memory agent

**Masalah.** Agent menyimpan catatan lewat MCP `add_memory` ke tabel `enigram_memories` (teks,
embedding, key pemilik, waktu, model embedding), tetapi:

- memory hanya bisa dibaca lewat MCP `search_memory`; tidak ada endpoint HTTP dan tidak ada halaman
  web, jadi manusia tidak bisa melihat apa yang diingat agent;
- memory **tidak bisa dihapus**, termasuk yang salah atau berisi data sensitif;
- memory privat per API key: key lain tidak melihatnya. Web UI memakai key `web`, jadi tanpa aturan
  baru halaman web tidak akan melihat memory milik agent;
- memory tool MCP hanya berjalan di workspace proses (lihat item 2).

**Tujuan.** Halaman **Memory** per workspace (`/w/<ws>/memory`) untuk melihat, mencari, menghapus,
dan menjadikan memory sebagai note.

### Isi halaman

- Daftar memory terbaru dulu: teks (dipotong, bisa dibuka penuh), key pemilik, waktu, model
  embedding; filter per key.
- Pencarian dua cara: kata (teks) dan makna (embedding yang sama dengan `search_memory`, dengan
  skor kemiripan), supaya manusia melihat apa yang akan ditemukan agent.
- Detail satu memory: teks penuh, note yang dibuat darinya (`annotations.memory_id`), tanda bila
  dibuat dengan model embedding lama (tidak akan cocok di pencarian makna).
- Aksi: **Jadikan note** (proposal lewat jalur `propose_annotation` yang ada, dengan `memory_id`)
  dan **Hapus** (dengan konfirmasi).

### Lingkup teknis

- API: `GET /memories?key=&q=&mode=text|semantic&limit=`, `GET /memories/{id}`,
  `DELETE /memories/{id}`, `POST /memories/{id}/note`. Port memory yang ada mendapat `list`,
  `get` dan `delete`.
- MCP: tool `delete_memory(id)` untuk agent pemilik, menutup celah "no delete yet" di README.
- Memory yang sudah menjadi note tetap bisa dihapus; note-nya tetap ada dan mencatat
  `memory_id` yang sudah tidak ada.
- Workspace: memory mengikuti workspace aktif, termasuk lewat MCP setelah item 2 tahap R2.

### Siapa boleh melihat apa

Ini keputusan utama, karena aturan sekarang (privat per key) membuat halaman ini kosong bagi web.

| Pilihan | Akibat |
|---|---|
| A. Admin/maintainer workspace melihat memory semua key (baca saja); pemilik key melihat miliknya | Bisa diaudit; agent tidak lagi benar-benar privat dari pemilik workspace |
| B. Halaman hanya menampilkan memory milik key yang sedang dipakai | Tetap privat; web (`web`) tidak melihat memory agent sampai login per orang (item 2 R4) dan agent terikat ke orangnya |

**Diputuskan 2026-11-07: A** (pemilik), dengan aturan dicatat di README, karena tujuan halaman ini
adalah melihat apa yang diingat agent. Sebelum RBAC ada, halaman hanya aktif untuk key di
`ANNOTATION_VERIFIERS`.

### Tahapan

| Tahap | Isi | Selesai bila |
|---|---|---|
| M1 | API list/get/delete/note dan `delete_memory` MCP | Test: list per key dan workspace, pencarian teks dan makna, hapus, jadikan note; key tanpa izin mendapat 403 |
| M2 | Halaman Memory | Daftar, filter, dua jenis pencarian, detail, hapus dan jadikan note bekerja; status kosong jelas; lebar 390px |
| M3 | Ikut RBAC (item 2): siapa melihat memory siapa sesuai peran | Test tabel peran × aksi untuk memory |

**Diputuskan 2026-11-08.** Memory key lain boleh dihapus oleh `maintainer` ke atas (sama dengan
yang boleh membacanya); belum ada batas umur (retensi), hapus manual.

## 8. Chat interaktif multi-agent dengan fokus masing-masing

**Masalah.** Halaman Ask menjawab satu pertanyaan per kali: tidak ada riwayat percakapan, tidak bisa
bertanya lanjutan ("lalu bagaimana kalau token kedaluwarsa?"), dan semua orang mendapat gaya jawaban
yang sama. Padahal developer, PM dan QA menanyakan hal yang sama dengan kebutuhan berbeda: developer
ingin symbol dan alur kode, PM ingin aturan bisnis dalam bahasa biasa, QA ingin skenario dan kasus
tepi.

**Fondasi yang sudah ada.**
- Note bertipe `technical`, `business_rule`, `expected_behavior`, `known_issue`: cocok sebagai
  sumber utama tiap agent.
- Router dan dua penjawab (code agent dan dokumen), sitasi `file:line@sha` dan `[n]`, label asal
  per kalimat (`[code]`, `[document]`, `[human]`, `[unsupported]`), abstain bila tidak ada bukti.
- LightRAG sudah menerima riwayat percakapan (`QueryParam.conversation_history`); code agent dan
  `POST /query` masih stateless.

**Tujuan.** Halaman **Chat** per workspace (`/w/<ws>/chat`): percakapan bertingkat dengan beberapa
agent, masing-masing punya fokus, sumber utama dan gaya jawaban sendiri, dengan sitasi dan label
asal yang sama seperti Ask.

### Agent bawaan

| Agent | Untuk | Sumber utama | Gaya jawaban |
|---|---|---|---|
| **Technical** | Developer | Code graph (symbol, flow, summary), note `technical` dan `known_issue` | Symbol, file dan langkah alur dengan sitasi kode |
| **Business rules** | PM, analis | Dokumen, note `business_rule` dan `expected_behavior`, flow summary | Bahasa biasa tanpa jargon kode; menandai bila kode bertentangan dengan aturan (konflik dokumen-kode) |
| **QA** | Tester | Note `expected_behavior` dan `known_issue`, flow dan entry point | Skenario uji, kasus tepi, dan apa yang belum tercakup note |
| **Overview** | Semua | Dokumen dan summary | Ringkasan tingkat tinggi, menunjuk agent lain untuk detail |

Tiap agent adalah konfigurasi di atas penjawab yang sudah ada (prompt fokus, bobot sumber, tool yang
boleh dipakai, tipe note yang diutamakan), bukan model atau pipeline terpisah.

### Cara kerja multi-agent

- **Tanya satu agent**: pilih agent untuk percakapan, atau sebut di pesan (`@business`).
- **Panel**: satu pertanyaan dijawab beberapa agent sekaligus, tampil berdampingan (atau tab di
  ponsel); berguna saat developer dan PM membahas fitur yang sama.
- **Lanjutan**: setiap agent melihat riwayat percakapan, termasuk jawaban agent lain, sehingga
  "@qa buat skenario dari jawaban technical di atas" bisa dijawab.
- Satu jawaban = satu trace (halaman Trace yang ada), jadi sumber dan rute tetap bisa diperiksa.

### Lingkup teknis

- Tabel `chat_sessions` (workspace, pemilik, judul, waktu) dan `chat_messages` (sesi, peran
  user/agent, nama agent, teks, id query/trace, token).
- API: buat/daftar/hapus sesi, kirim pesan ke satu atau beberapa agent, jawaban di-*stream* (SSE)
  supaya teks muncul bertahap.
- Riwayat dikirim ke penjawab: `conversation_history` untuk dokumen, pesan sebelumnya untuk code
  agent, dipangkas ke batas token.
- Konfigurasi agent bawaan di kode (seperti `decisions.yaml`); tahap berikutnya: agent kustom per
  workspace yang diatur `admin` (nama, fokus, sumber, tipe note).
- Batas biaya: token chat dihitung ke `DAILY_TOKEN_BUDGET` dan per key seperti `/query`; mode panel
  memakai token sebanyak jumlah agent yang menjawab, jadi ada batas agent per panel.
- Dari pesan bisa langsung **Jadikan note** (jalur proposal yang ada), supaya jawaban yang
  dikonfirmasi manusia masuk ke pengetahuan.

### Tahapan

| Tahap | Isi | Selesai bila |
|---|---|---|
| C1 | Sesi dan pesan tersimpan, satu agent (Technical) dengan riwayat, streaming | Pertanyaan lanjutan dijawab dengan konteks sebelumnya; tiap jawaban punya trace |
| C2 | Empat agent bawaan dan pilihan agent / `@mention` | Pertanyaan yang sama memberi jawaban berbeda fokus; test per agent memeriksa sumber yang dipakai |
| C3 | Mode panel (beberapa agent paralel) dan batas biaya per panel | Panel tiga agent tampil berdampingan; token tercatat per agent |
| C4 | Agent kustom per workspace (butuh RBAC) | Admin membuat agent baru dari UI dan bisa dipakai di chat |

**Kualitas.** Sebelum C2 dianggap selesai, ukur di beberapa pertanyaan golden: apakah agent
Business rules benar-benar memakai note dan dokumen, dan apakah fokus tidak menurunkan ketepatan
dibanding Ask (porsi `[unsupported]`, abstain yang benar). Biaya eval kecil ini dicatat terpisah
dari fase 7.

**Keputusan yang dibutuhkan.**
- Interaksi utama: pilih satu agent per percakapan, panel, atau keduanya sejak awal.
- Daftar agent bawaan (empat di atas) dan namanya di UI.
- Siapa boleh melihat sesi chat: hanya pemiliknya, atau anggota workspace (berbagi percakapan).
- Retensi riwayat chat.
- Halaman Ask tetap ada di samping Chat, atau digantikan Chat.

## 9. Monorepo dan `tsconfig.json` di luar root

**Masalah.** Code graph hanya dibuat bila ada `tsconfig.json` di root repo; monorepo
(`apps/web/tsconfig.json`, `packages/*/tsconfig.json`) hanya mendapat index Markdown.

**Lingkup.** Deteksi semua `tsconfig.json` yang bukan turunan dari yang lain, jalankan sidecar per
project, gabungkan hasilnya dalam satu graph repo dengan path relatif ke root. Bahasa selain
TypeScript/JavaScript tetap di luar lingkup.

**Selesai bila.** Repo enigram sendiri (`apps/web`) mendapat entry point dan flow summary.

## 10. Endpoint hapus note

**Masalah.** Note hanya bisa di-`rejected`. Validasi fase 4 workspace meninggalkan note #458 di
workspace `whiteboard`.

**Lingkup.** `DELETE /annotations/{id}` dengan aturan izin yang sama seperti verifikasi (pembuat
atau `ANNOTATION_VERIFIERS`), event dicatat, tombol di halaman Annotations.

**Selesai bila.** Note terhapus tidak muncul di daftar, jawaban maupun agent; test izin lulus.

## 11. Keputusan produk yang tertunda

Diputuskan 2026-11-08. Hanya baris pertama yang butuh kode (perubahan prompt, ukur di split `dev`).

| Pertanyaan | Keputusan | Dampak |
|---|---|---|
| Bobot note per tipe di prompt agent: aturan bisnis = fakta, known issue = petunjuk? | Ya, bertingkat: `business_rule`/`expected_behavior` = fakta, `known_issue`/`technical` = petunjuk | **Diterapkan 2026-11-08** (lihat di bawah) |
| Tanda ✓ *Looks right* dikirim ke agent? | Tidak di v1 | — |
| Batas token global selain budget per workspace? | Belum, pantau lewat Dashboard (item 6) | — |
| Branch yang dilacak: default saja atau juga branch PR? | Default saja | — |
| Daftar fitur memuat symbol ber-summary `flow` yang bukan entry point? | Tidak perlu: semua summary `flow` sudah entry point (nestjs-boilerplate 25/25, white-board 7/7) | — |

**Bobot note per tipe, diterapkan 2026-11-08.** Baris note di prompt kini memuat tipenya
(`note#7 [business rule, verified by …]`) dan kedua penjawab (code agent dan jalur dokumen)
mendapat aturan `NOTE_WEIGHT_RULE`: aturan bisnis dan expected behavior adalah keputusan tim
(jawab dengannya, tandai kode yang berbeda), known issue dan technical adalah petunjuk yang kalah
oleh kode. Prompt code agent naik 35 token (299 → 334), `AGENT_VERSION` 3.4.
- Split `dev` (`answers-enigram-agent-20261108-130801`): 10/10 benar menurut jawaban golden,
  a01 benar, 47/47 sitasi valid, token 1,016× run 2026-11-05. Label oleh Claude, bukan blind.
- Spike anotasi (semua note `technical`): s01–s03 dan s05 mengikuti note; s05 menyatakan kode
  sebagai jawaban dan tetap menampilkan konflik. s04 tidak mengutip note#54 (jawaban kodenya
  benar); di `main` pertanyaan yang sama mengutipnya 1/3 kali, sama dengan prompt baru, jadi bukan
  regresi.
- Note#55 (s05) diubah sementara ke `business_rule`: jawabannya berubah menjadi "per aturan bisnis
  tim: ya, tapi kode belum menerapkannya", sesuai tujuan; tipe dikembalikan ke `technical`.

## 12. Perbaikan kecil halaman Code

- **Called by lewat interface**: sekarang hanya panggilan langsung; panggilan lewat member
  interface belum dihitung ke implementasinya.
- **Daftar Features** belum scroll otomatis ke fitur yang dibuka (daftar Files sudah).

## 13. Adapter framework lain

Sekarang ada Nest, Next.js App Router dan fallback generik. Kandidat berikutnya: Express
(`router.*`), BullMQ (worker), Convex (query/mutation), Next.js Pages Router. Pilih berdasarkan repo
yang benar-benar akan dipakai.

**Selesai bila.** Repo contoh untuk tiap adapter mendapat entry point dengan kind yang benar,
dan `ENTRYPOINT_KINDS` di Python ikut diperbarui (pelajaran dari #21).

## 14. Fase 7: evaluasi penuh

**Tujuan.** Vonis Key Hypothesis PRD utama: PASS, FAIL, atau INSUFFICIENT DATA.

**Lingkup (dari PRD).** Index tag `eval-v1`; split `verdict` (40 pertanyaan `behavior`) dijawab oleh
sistem penuh, `agent+repo`, `mix` dan ablasi `--scope code`; judge per jawaban dan κ antar judge;
pertanyaan `dev` dilaporkan terpisah; `reserve` hanya dibuka bila INSUFFICIENT DATA.

**Sebelum mulai.** Perkirakan biaya token dari run eval sebelumnya dan setujui anggarannya. Eval
ini juga mengukur dampak #22 dan #23 terhadap kualitas jawaban. Untuk `eval/golden-docs.yaml`,
index ulang dulu keempat dokumen Kestrel (sumber di `data/backup-before-phase1`) dan pastikan
`golden check --file eval/golden-docs.yaml` lulus.

**Kenapa terakhir.** Paling mahal, dan hasilnya lebih bermakna setelah item 1–13 stabil.

## 15. Persiapan rilis OSS (non-kode)

- Pernyataan tertulis dari Hubexo soal kepemilikan kode.
- Entitas penerima CLA (pribadi atau badan usaha) dan tes CLA dengan akun kedua.
- Review lawyer atas lisensi dan CLA.
- Pendaftaran merek "Enigram", atau cukup `TRADEMARK.md` dulu.

## Di luar rencana ini

- Bahasa selain TypeScript/JavaScript untuk code graph.
- Hapus/rename workspace (keputusan v1: tidak ada).
- Flag `--workspace` di CLI (cukup `WORKSPACE=x uv run enigram ...`).
