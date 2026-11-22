# API dan worker tanpa disk bersama

Rencana dibuat 2026-11-22 dari peta kode saat ini. S1, S2, S3 dan S4 dibangun 2026-11-22 (lihat
bagiannya); fase lain belum.
Path kode relatif ke `apps/api/src/enigram/`.

## Masalah

API dan worker berbagi folder `DATA_DIR` (`./data:/app/data`, `docker-compose.yml:21-22`).
Data utama (index LightRAG, code graph, job, chat, user) sudah di Postgres: API menolak jalan
tanpa `STORAGE_BACKEND=postgres` (`adapters/driving/http_api.py:484-488`). Tapi empat hal masih
berupa file, dan API membaca semuanya:

1. **Manifest dokumen** `index_manifest.json`: status, sha256, error, dan setting chunk atau
   embedding tiap dokumen. Ditulis worker, dibaca API untuk daftar dokumen, dashboard, upload,
   dan delete (`application/indexing.py:42-52`, `application/documents.py:22`,
   `application/dashboard.py:167`, `http_api.py:792`, `:824`).
2. **File sumber** `books/`: upload ditulis API (`http_api.py:778-805`), dibaca worker
   (`adapters/driving/worker.py:295`). Markdown dari repo juga disalin ke sini
   (`application/repo_sync.py:319-328`).
3. **Hasil parse** `parsed/`: `<book>.chunks.jsonl` dipakai lagi saat insert dan delete
   (`application/indexing.py:169-170`, `:275`, `:297`), cache Docling `*.docling.json`
   (`adapters/driven/docling_parser.py:119`).
4. **Bare clone git** `repos/`: dibuat worker (`application/repo_store.py:89-123`), dibaca API
   saat menjawab pertanyaan kode (read_file, grep, list_dir, cek sitasi:
   `application/code_agent.py:381-389`). Kalau clone tidak ada, `_git` mengembalikan `None` dan
   agent jalan tanpa akses source.

Selain itu:

5. **Worker tunggal** untuk semua workspace, lewat advisory lock global
   (`adapters/driven/postgres_jobs.py:363-373`). Alasannya: dua worker akan menulis manifest dan
   index yang sama.
6. **Jawaban chat berjalan sebagai task di proses API** (`http_api.py:1551-1553`). Kalau pod
   dimatikan di tengah jawaban, jawaban itu hilang.

Akibatnya API tidak bisa dijalankan lebih dari satu replika di mesin berbeda, dan tidak bisa
di-deploy di platform tanpa persistent disk yang bisa dipakai bersama.

## Tujuan

- API tidak butuh disk persisten. Dua replika API di belakang load balancer melihat data yang
  sama, dan pod API bisa dibuang kapan saja.
- Worker hanya butuh disk sementara (cache). Kalau disknya hilang, worker membangunnya ulang
  dari Postgres, object storage, atau remote git.
- `docker compose up` tetap jalan tanpa layanan tambahan secara default.

**Bukan tujuan:**
- CLI dengan `STORAGE_BACKEND=file` tetap memakai file lokal. Mode ini untuk satu orang di satu
  mesin.
- `eval/` dan `reports/` tetap file lokal. Hanya dipakai CLI.
- State Keycloak dan Postgres. Keduanya layanan terpisah dan punya cara backup sendiri.
- Cache `Workspaces.open` (`adapters/driving/workspaces.py:59`) dan volume `model-cache`.
  Keduanya cache murni dan aman kalau hilang.

## Keputusan owner

| # | Pertanyaan | Pilihan | Saran |
|---|---|---|---|
| D1 | Targetnya apa? | (a) beberapa replika API, (b) platform tanpa persistent disk (Cloud Run, ECS Fargate), (c) keduanya | (c). Fase S1-S4 dibutuhkan keduanya; S5 hanya untuk throughput worker. **Diputuskan (c)** oleh pemilik 2026-11-22 |
| D2 | Di mana file sumber disimpan? | (a) S3-compatible (S3, GCS, MinIO lokal), (b) kolom `bytea` di Postgres | (a). PDF bisa puluhan MB; Postgres tetap kecil dan backup cepat. **Diputuskan (a)** oleh pemilik saat S2 dikerjakan (2026-11-22) |
| D3 | Di mana chunk hasil parse disimpan? | (a) tabel Postgres, (b) object storage seperti file sumber | (a). Ukurannya kecil, dan delete atau re-insert tidak perlu unduh file. **Diputuskan (a)** saat S3 dikerjakan (2026-11-22) |
| D4 | Bagaimana API membaca source kode? | (a) clone lokal di tiap pod API, dibuat saat pertama dipakai, (b) isi file disimpan di Postgres saat index, (c) volume RWX bersama (EFS, Filestore) | (a) dulu. Paling sedikit perubahan, dan `ensure_store` sudah ada. (b) kalau cold start terlalu lambat. **Diputuskan (a)** oleh pemilik 2026-11-22 |
| D5 | Perlu lebih dari satu worker? | ya / tidak sekarang | Tidak sekarang. Antrean FIFO cukup selama belum diukur lambat. **Diputuskan: tidak sekarang** oleh pemilik 2026-11-22 |

## Fase

Setiap fase bisa di-merge sendiri, dan perilaku compose default tidak berubah sampai S6.

### S1: Manifest ke Postgres

- Port `ManifestRepository` (`load`, `save_entry`, `delete_entry`, `settings`) dengan dua
  adapter: `FileManifest` (JSON sekarang, untuk CLI mode `file`) dan `PostgresManifest`.
- Tabel `enigram_documents (workspace, book, status, sha256, error, chunks, updated_at, ...)` dan
  `enigram_index_settings (workspace, key, value)` untuk setting chunk dan embedding yang
  sekarang ada di root manifest (`application/indexing.py:62-122`).
- Saat start, kalau tabel kosong dan `index_manifest.json` ada, impor sekali lalu ganti nama
  file menjadi `index_manifest.json.imported`.
- Semua pemanggil `load_manifest`/`save_manifest` pindah ke port.

**Cek:** test yang ada lulus dengan kedua adapter; test impor dari JSON lama; daftar dokumen di
UI sama sebelum dan sesudah impor pada data lokal.

**Dibangun (2026-11-22), berbeda dari rencana di atas:**
- Port `IndexState` (`load_manifest`, `save_manifest`, `read/write/delete_chunks`) memegang manifest
  dan chunk sekaligus (S1 + S3), dengan `FileIndexState` dan `PostgresIndexState`.
- Entry dokumen dan setting disimpan sebagai jsonb (`enigram_documents.entry`,
  `enigram_index_settings.settings`), bukan satu kolom per field. Isinya dibaca dan ditulis utuh.
- `save_manifest` tetap mengganti seluruh manifest dalam satu transaksi, sama seperti menimpa file.
- Impor berjalan di bootstrap (`_new_index_state`) di bawah `pg_advisory_xact_lock`, hanya kalau
  workspace belum punya baris settings. Sesudah commit, file diganti nama menjadi `*.imported`.
- Test: `tests/test_index_state.py`. Kontrak pada kedua adapter lulus pada Postgres lokal.

### S2: File sumber ke blob store

- Port `BlobStore` (`put`, `get_to(path)`, `delete`, `list(prefix)`) dengan `LocalBlobStore`
  (folder `books/`, default) dan `S3BlobStore` (`BLOB_URL=s3://bucket/prefix`, kredensial dari
  env standar AWS).
- Upload di API ditulis ke blob, bukan ke disk. Worker mengunduh ke folder sementara sebelum
  parse. Salinan Markdown dari repo juga ditulis ke blob.
- Key: `<workspace>/books/<nama file>`.

**Cek:** test dengan `LocalBlobStore` dan MinIO; upload, index, dan delete dokumen lewat UI
dengan `BLOB_URL` ke MinIO.

**Dibangun (2026-11-22), berbeda dari rencana di atas:**
- Port `BlobStore` (`names`, `put`, `get_to`, `delete`); `list(prefix)` diganti `names()`, karena
  satu store sudah per workspace. `S3BlobStore` memakai boto3 di thread; endpoint MinIO lewat env
  standar `AWS_ENDPOINT_URL`, bukan variabel baru.
- Worker menyalin file satu dokumen ke folder sementara, lalu `run_index` jalan di folder itu.
  Sync repo menulis Markdown ke store, lalu menyalinnya kembali ke folder sementara (clone sudah
  dihapus saat index jalan).
- Upload di API ditulis ke folder sementara lokal, lalu `put` ke store.
- File lama di `data/books` tidak dipindah otomatis saat `BLOB_URL` diisi; docs memberi perintah
  `aws s3 sync`.
- Test: `tests/test_blob_store.py`, kontrak pada kedua adapter (S3 dengan marker `s3`,
  `ENIGRAM_TEST_S3_URL`).

### S3: Chunk ke Postgres, cache Docling jadi sementara

- Tabel `enigram_chunks (workspace, book, position, chunk_id, text, meta)` menggantikan
  `<book>.chunks.jsonl`. `_insert_book` dan `delete_document` membaca dari tabel.
- Cache `*.docling.json` tetap di disk worker tapi diperlakukan sebagai cache: hilang berarti
  parse ulang.
- Impor sekali dari `parsed/*.chunks.jsonl` yang sudah ada, seperti S1.

**Cek:** hapus folder `parsed/` lalu re-index satu dokumen yang tidak berubah: tidak ada chunk
yang di-insert ulang (`plan_inserts` melihat semua sudah ada).

**Dibangun (2026-11-22)** bersama S1, lihat di atas. Tabel `enigram_chunks` menyimpan satu baris
per chunk (`chunk` jsonb). `delete_document` menghapus baris itu dan cache Docling di disk.
`golden check` membaca chunk lewat port.

### S4: Source kode di API tanpa disk bersama

Dengan D4 (a):
- `CodeAgent._git` memanggil `ensure_store` saat clone tidak ada, dengan token repo dari
  Postgres, lalu menyimpannya di cache lokal pod (`REPO_STORE` di disk sementara).
- Satu lock per `(url, ref)` di proses supaya dua pertanyaan tidak meng-clone bersamaan.
- Log waktu clone pertama, untuk memutuskan apakah perlu D4 (b).

**Cek:** jalankan API dengan `REPO_STORE` kosong, tanya soal kode umami: jawaban punya sitasi
dan log menunjukkan satu clone. Ulangi pertanyaan: tidak ada clone kedua.

**Dibangun (2026-11-22):**
- `CodeAgentTools.ensure_git` memanggil `repo_store.prepare_store` sekali per pertanyaan per repo:
  tidak ada apa-apa kalau store sudah memegang commit yang di-index, clone kalau store tidak ada,
  fetch kalau worker sudah meng-index commit yang lebih baru. Cek commit memakai
  `GIT_NO_LAZY_FETCH=1`, karena partial clone akan mengambil commit yang hilang diam-diam.
- Lock per path store (asyncio, satu proses). Sebelum cek sitasi, store repo yang dikutip juga
  disiapkan, karena agent bisa mengutip baris dari graph tanpa membaca file.
- Hasil cek (umami 182k LOC, `REPO_STORE=/tmp/enigram-repos` di container API): pertanyaan pertama
  clone dalam **1,4 s**, sitasi valid; pertanyaan kedua (berbeda) membaca 2 file tanpa clone kedua.
  Dengan angka ini D4 (b) belum perlu.
- Test: `tests/test_store_on_demand.py` (repo `file://` lokal). Unit test lain memakai
  `prepare_store` palsu (conftest), supaya tidak ada git atau jaringan.

### S5: Worker lebih dari satu (hanya kalau D5 = ya)

- Ganti lock global dengan klaim job `SELECT ... FOR UPDATE SKIP LOCKED` dan advisory lock per
  workspace, karena index LightRAG satu workspace tetap harus ditulis satu per satu.
- Urutan FIFO tetap berlaku di dalam satu workspace.

**Cek:** dua worker, job di dua workspace berjalan paralel; dua job di satu workspace berjalan
berurutan.

### S6: Compose, shutdown, dan dokumentasi

- Saat SIGTERM, API menunggu `app.state.chat_turns` selesai sampai batas waktu
  (`SHUTDOWN_GRACE_SECS`) sebelum keluar.
- Compose: `api` tidak lagi me-mount `./data`; `worker` memakai volume cache. Service `minio`
  opsional di profile `s3`.
- Uji akhir: `docker compose up --scale api=2` di belakang nginx web, lalu upload, index, chat,
  dan tanya kode melalui keduanya.
- Update `docs/` (deployment dan env baru: `BLOB_URL`, `REPO_STORE`, `SHUTDOWN_GRACE_SECS`).

## Risiko

- **Migrasi data lama.** S1 dan S3 mengimpor file yang ada. Backup `data/` sebelum menjalankan
  versi baru, karena impor mengganti nama file.
- **Cold start S4.** Clone repo besar saat pertanyaan pertama bisa makan puluhan detik. Clone
  partial (`--filter`) mengurangi ini, tapi belum diukur.
- **Token repo privat di pod API.** Sekarang hanya worker yang memakai token untuk clone. Dengan
  S4 (a), API juga memakainya, jadi API butuh `REPO_CREDENTIALS_KEY` juga.
