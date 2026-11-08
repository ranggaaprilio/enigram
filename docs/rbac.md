# Matriks peran (RBAC)

Disetujui 2026-11-08 untuk item 2 di [`plan.md`](plan.md), sebelum implementasi R1. Matriks ini
menjadi tabel test R1: setiap baris diuji untuk peran tepat di bawah minimum (403) dan untuk
peran minimum (lolos).

## Peran

Bertingkat: setiap peran mendapat semua hak peran di bawahnya.

| Peran | Lingkup | Untuk |
|---|---|---|
| `viewer` | per workspace | Pembaca: dokumen, Ask, Trace, halaman Code dan Annotations |
| `contributor` | per workspace | PM/QA dan agent: proposal note, ✓ *Looks right*, memory |
| `maintainer` | per workspace | Developer: verifikasi note, dokumen, repo, graph, konflik |
| `admin` | per workspace | Pemilik workspace: anggota, token repo private, secret webhook |
| `owner` | global | Membuat workspace; dianggap `admin` di semua workspace |

Anggota baru mendapat `viewer` (diputuskan 2026-11-08). Workspace `default` mengikuti aturan yang
sama.

## Aturan umum

- **Status HTTP.** Tanpa key yang valid → 401 (seperti sekarang). Bukan anggota workspace → **404**,
  supaya keberadaan workspace tidak bocor. Anggota dengan peran kurang → **403**, pesan menyebut
  peran yang dibutuhkan.
- **Workspace.** Header `X-Enigram-Workspace`; tanpa header, workspace default milik principal
  (bukan lagi workspace proses).
- **Mode lokal.** Tanpa `ENIGRAM_API_KEYS` API tetap terbuka dan RBAC mati, seperti sekarang.
- **MCP stdio.** Tetap tanpa RBAC: `WORKSPACE` dari env, dipakai pemilik di mesinnya sendiri.
- **Kepemilikan.** Beberapa aksi boleh untuk "pembuat" walau perannya di bawah minimum. Pembuat
  tetap harus anggota dengan peran minimal `contributor`; pembuat yang turun ke `viewer` kehilangan
  hak itu.

## Route HTTP

### Tanpa RBAC

| Route | Akses | Catatan |
|---|---|---|
| `GET /health` | terbuka | Tidak menyebut nama workspace lain |
| `POST /repos/{id}/webhook` | tanda tangan HMAC | Bukan Bearer key; secret per repo |

### Workspace dan principal

| Route | Minimal | Catatan |
|---|---|---|
| `GET /workspaces` | key valid | Hanya workspace keanggotaan pemanggil (semua untuk `owner`) |
| `POST /workspaces` | `owner` | Pembuat otomatis `admin` di workspace baru |
| `GET /me` (baru, R1) | key valid | Principal, workspace default, peran per workspace |

### Dokumen dan job

| Route | Minimal | Catatan |
|---|---|---|
| `GET /documents` | `viewer` | |
| `POST /documents` | `maintainer` | Upload memakai token (index) |
| `DELETE /documents/{book}` | `maintainer` | |
| `GET /jobs/{id}` | `viewer` | Job workspace itu saja |
| `GET /stats` | `viewer` | Angka Dashboard (item 6); `tokens.by_key` hanya untuk `admin`, peran lain mendapat `null` |
| `GET /stats/repos`, `/stats/repos/{id}` | `viewer` | Detail repo di Dashboard (D2b) |

### Repo dan Code page

| Route | Minimal | Catatan |
|---|---|---|
| `GET /repos` | `viewer` | |
| `POST /repos` tanpa `token` | `maintainer` | Index dan re-sync |
| `POST /repos` dengan `token` | `admin` | Token repo private (item 1); menggantikan syarat `ANNOTATION_VERIFIERS` |
| `DELETE /repos/{id}` | `maintainer` | |
| `POST /repos/{id}/secret` | `admin` | Membuat/memutar secret webhook |
| `GET /repos/{id}/symbols`, `/features`, `/tree`, `/symbols/{symbol}` | `viewer` | |
| `POST /repos/{id}/symbols/{symbol}/review` | `contributor` | ✓ *Looks right* |
| `DELETE /repos/{id}/symbols/{symbol}/review` | `contributor` (pembuat tanda) / `maintainer` (tanda siapa pun) | Menggantikan "aktor yang sama atau `ANNOTATION_VERIFIERS`" |

### Note (annotations)

| Route | Minimal | Catatan |
|---|---|---|
| `GET /annotations`, `GET /annotations/{id}` | `viewer` | |
| `POST /annotations` | `contributor` | `contributor`: selalu menjadi `proposed`. `maintainer`: langsung `verified` kecuali `proposed: true` |
| `DELETE /annotations/{id}` | `contributor` (pembuat) / `maintainer` (note siapa pun) | Menggantikan "pembuat atau `ANNOTATION_VERIFIERS`" |
| `PATCH /annotations/{id}` | per field, tabel di bawah | |

`PATCH /annotations/{id}` per field:

| Field | Minimal | Catatan |
|---|---|---|
| `text` | `contributor` (pembuat) / `maintainer` | Diubah `contributor` → kembali `proposed`; diubah `maintainer` → tetap/kembali `verified` |
| `by` | `contributor` (pembuat) / `maintainer` | Nama tampilan; setelah R4 diisi dari login |
| `status: verified` / `rejected` | `maintainer` | Proposal agent (`via = mcp`): tidak boleh oleh key pengusulnya sendiri, seperti sekarang |
| `status: needs_verification` | `contributor` | Menandai note untuk dicek ulang |
| `owner`, `verify_by` | `maintainer` | |
| `conflict` | `maintainer` | |
| `target_ref` (relink note yatim) | `maintainer` | Hasilnya `needs_verification` |

`ANNOTATION_VERIFIERS` dihapus; aturan "pemilik note boleh memverifikasi" (`may_verify`) diganti
peran `maintainer`.

### Konflik, pertanyaan, graph

| Route | Minimal | Catatan |
|---|---|---|
| `GET /conflicts` | `viewer` | |
| `POST /conflicts/detect` | `maintainer` | Memakai token |
| `POST /query` | `viewer` | Memakai token; dihitung ke budget per key dan workspace |
| `GET /queries`, `GET /queries/{id}` | `viewer` | Trace berisi pertanyaan semua key di workspace itu |
| `GET /graph/search` | `viewer` | |
| `POST /graph/entity/edit`, `/relation/edit`, `/entity/merge` | `maintainer` | |
| `DELETE /graph/entity/{name}`, `DELETE /graph/relation` | `maintainer` | |

### Route baru dari tahap RBAC (R1–R3)

| Route | Minimal | Catatan |
|---|---|---|
| `GET /members` | `admin` | Anggota workspace dan perannya |
| `PUT /members/{name}` `{role}` | `admin` | Menambah key yang sudah ada atau mengubah perannya; admin terakhir tidak bisa diturunkan |
| `DELETE /members/{name}` | `admin` | Admin terakhir tidak bisa dikeluarkan |
| `POST /keys` | `admin` | Key baru, langsung anggota workspace ini; key ditampilkan sekali |
| `DELETE /keys/{name}` | `admin` di semua workspace key itu / `owner` | Mencabut tanpa restart; key `owner` hanya oleh `owner` |

CLI (`enigram principals ...`) bekerja langsung ke DB dan setara `owner`.

### Route item lain (dicatat supaya konsisten)

| Route | Minimal | Catatan |
|---|---|---|
| `GET /stats` (item 6) | `viewer` | Panel token per key hanya untuk `admin`; lintas workspace untuk `owner` |
| `GET /memories`, `GET /memories/{id}` (item 7) | `contributor` (memory sendiri) / `maintainer` (semua key) | Keputusan A, 2026-11-07 |
| `DELETE /memories/{id}` (item 7) | `contributor` (memory sendiri) / `maintainer` (key lain: belum diputuskan) | |
| `POST /memories/{id}/note` (item 7) | `contributor` | Menjadi proposal |
| Chat (item 8) | `viewer` | Siapa boleh melihat sesi orang lain: belum diputuskan |

## Tool MCP (HTTP `/mcp`)

Workspace per koneksi (`/mcp/w/<ws>` atau header), bisa ditimpa argumen `workspace` per tool;
keduanya dicek terhadap keanggotaan yang sama dengan API.

| Tool | Minimal | Catatan |
|---|---|---|
| `list_workspaces` (baru, R2) | key valid | Workspace keanggotaan key |
| `query_knowledge_base` | `viewer` | Memakai token |
| `list_documents` | `viewer` | |
| `what_changed_since` | `viewer` | |
| `get_annotations` | `viewer` | |
| `get_conflicts` | `viewer` | |
| `search_memory` | `contributor` | Memory key sendiri saja, seperti sekarang |
| `add_memory` | `contributor` | |
| `delete_memory` (item 7) | `contributor` | Memory key sendiri |
| `propose_annotation` | `contributor` | Selalu `proposed` |

## Migrasi (R1)

- Setiap key di `ENIGRAM_API_KEYS` menjadi principal jenis `key`, dengan workspace default = workspace
  proses.
- Peran awal, supaya tidak ada yang rusak saat deploy (keputusan 1): key di
  `ANNOTATION_VERIFIERS` → `admin` di semua workspace yang ada; key lain → `maintainer`.
- Satu principal ditandai `owner` lewat CLI saat deploy.
- `ENIGRAM_API_KEYS` dan `ANNOTATION_VERIFIERS` tetap dibaca sampai R1 terpasang di semua deployment,
  lalu dihapus dari dokumentasi; tabel `principals` menjadi sumbernya.

## Keputusan (2026-11-08)

Pemilik mengikuti semua usulan:

1. **Peran awal key lama.** Key di `ANNOTATION_VERIFIERS` → `admin`; key lain → `maintainer`. Akibat
   yang diterima: key non-verifier kini bisa memverifikasi note.
2. **Key `web` sebelum R4** mengikuti aturan 1: `admin` bila ada di `ANNOTATION_VERIFIERS`, selain itu
   `maintainer`. Jadi semua pengguna UI bisa memverifikasi sampai login per orang (R4).
3. **Trace:** `viewer` melihat trace semua key di workspace-nya.
4. **`status: needs_verification`** boleh oleh `contributor`.
5. **Upload dokumen:** minimal `maintainer`.

## Login per orang (R4, 2026-11-08)

- OIDC generik (authorization code + PKCE): `GET /auth/login`, `GET /auth/callback`,
  `POST /auth/logout`, `GET /auth/config` (terbuka). ID token dicek penuh (tanda tangan JWKS,
  issuer, audience, kedaluwarsa, nonce); email harus dari `OIDC_ALLOWED_DOMAINS`.
- Login pertama: principal jenis `user` bernama email, `viewer` di workspace proses. Nama yang
  sama dengan key ditolak; user yang dicabut ditolak.
- Sesi: cookie `enigram_session` (HttpOnly, SameSite=Lax, Secure bila redirect https), hash-nya di
  tabel `sessions`, `SESSION_DAYS` hari. Tulis lewat cookie wajib header `X-Requested-With`.
- `by` pada note dan review: nama tampilan user dari penyedia, tidak bisa diisi orang lain.
- Key API tetap berlaku di samping login (agent, skrip).
