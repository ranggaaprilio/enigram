# Akurasi retrieval dan graph

Rencana dibuat 2026-11-22 dari riset best practice 2024-2026 dan peta kode saat ini.
R0, R1 dan R2a dibangun 2026-11-22; angka ada di bagian Hasil. Fase lain
belum.

## Masalah

Kita tidak tahu seberapa baik retrieval enigram, dan sisi dokumen tertinggal dari praktik umum.

1. **Retrieval tidak diukur terpisah.** Eval kode hanya menilai jawaban. Tidak ada recall@k atau
   MRR terhadap simbol yang benar, jadi kalau jawaban salah kita tidak tahu apakah retrieval
   meleset atau LLM salah membaca. Split `dev` (10 soal) sudah 10/10, jadi tidak bisa
   menunjukkan perbaikan.
2. **Eval dokumen tidak bisa jalan.** Dokumen Kestrel untuk `eval/golden-docs.yaml` tidak lagi
   di-index (`plans/plan.md:43`).
3. **Dokumen hanya dicari secara dense.** LightRAG `mix` mencari lewat vektor saja, tanpa
   full-text, tanpa RRF, tanpa reranker (`enable_rerank=False`,
   `adapters/driven/lightrag_engine.py:187`). Chunk hanya diberi heading path sebagai konteks.
4. **Full-text kode tidak mengerti identifier.** Semua indeks dan query memakai
   `to_tsvector('english', ...)` (`adapters/driven/postgres_code_graph.py:96-99`, `:497-524`).
   `getUserById` menjadi satu token, jadi query "user" tidak menemukannya, dan stemming bahasa
   Inggris dipakai pada nama kode.
5. **Entity graph dokumen hanya di-merge berdasarkan nama persis.** Alias dan duplikat harus
   di-merge manual (`application/graph_edits.py`). Belum pernah diukur apakah graph (`mix`)
   lebih baik dari `naive` untuk dokumen kita.

## Tujuan

- Retrieval kode dan dokumen punya angka sendiri (recall@k, MRR), terpisah dari skor jawaban.
- Setiap perubahan retrieval di bawah hanya di-merge kalau angkanya naik di set pengukuran dan
  skor jawaban tidak turun.

**Bukan tujuan:**
- Mengganti graph kode statis (ts-morph) dengan graph hasil ekstraksi LLM. Untuk kode, analisis
  statis lebih lengkap dan tidak berhalusinasi (RepoGraph, LocAgent memakai pendekatan yang sama).
- Semantic chunking. Qu dkk. (NAACL 2025, arXiv 2410.13070) menemukan chunk berukuran tetap
  lebih baik di data nyata.
- HyDE untuk kode. LLM cenderung mengarang nama identifier di codebase privat.
- HNSW untuk `code_embeddings`. Scan exact yang sekarang paling akurat; HNSW hanya soal
  kecepatan.
- Community summary ala Microsoft GraphRAG. Biaya indexing sangat besar, dan dengan budget token
  yang sama selisihnya dengan vector RAG hampir nol (arXiv 2502.11371: 71,01% vs 71,17% di
  MultiHop-RAG).

## Kondisi sekarang

Dicek terhadap kode pada 2026-11-22 (path relatif ke `apps/api/src/enigram/`).

| Bagian | Kondisi | Lokasi |
|---|---|---|
| Chunk dokumen | Docling `HybridChunker`, maks 800 token, overlap 0, prefix heading path | `adapters/driven/docling_parser.py:68-77`, `:161` |
| Embedding dokumen | `voyage-4-lite`, tanpa `input_type` | `adapters/driven/voyage_embedding.py:47-50` |
| Retrieval dokumen | LightRAG `mix`, default `top_k=40`, `chunk_top_k=20`, tanpa reranker | `adapters/driven/lightrag_engine.py:166-195` |
| Hook reranker LightRAG | `rerank_model_func`, contoh di `lightrag/rerank.py` (Cohere, Jina, generic API) | lightrag-hku 1.5.7 |
| Graph dokumen | Ekstraksi LLM, tipe entity tetap, gleaning mati, merge manual | `adapters/driven/lightrag_engine.py:43-60`, `:87` |
| Simbol kode | ts-morph, satu simbol per unit, ringkasan LLM bottom-up | `application/code_index.py`, `application/code_summary.py` |
| `search_code` | full-text (`english`) + cosine exact, RRF k=60, nama persis di depan, 6 hit | `application/code_embeddings.py:16-46`, `adapters/driven/postgres_code_graph.py:492-636` |
| Gold evidence kode | `evidence: - symbol:` per soal | `eval/golden.yaml` |
| Metrik konteks dokumen | `context_recall`, `context_precision` | `domain/stats.py:15-31` |

## Desain

Urutan berdasarkan dampak per usaha. R0 wajib duluan; fase lain diukur dengan R0.

### R0: ukur retrieval

**Kode.** Perintah baru `enigram golden retrieval --split <s>` yang, untuk setiap soal:

1. Memanggil `search_code` dengan pertanyaan apa adanya, top-k 6 dan 20 (k=20 hanya untuk
   pengukuran).
2. Membandingkan hit dengan `evidence.symbol` soal itu.
3. Mencatat recall@6, recall@20 dan MRR per soal dan rata-ratanya.

Selain itu, dari trace run agent yang sudah ada: berapa persen simbol evidence yang dibaca atau
dikutip agent. Ini menunjukkan apakah agent menemukan bukti lewat tool lain (grep, find_symbol)
walaupun `search_code` meleset.

Tanpa LLM untuk menjawab, jadi murah (hanya embedding query).

**Dokumen.** Perintah yang sama pada golden set dokumen: `context_recall` dan
`context_precision` chunk evidence untuk `mix` dan `naive`, tanpa membuat jawaban. Dokumen
Kestrel sudah ter-index di workspace `eval-docs` (2026-11-09, untuk L3), jadi tidak perlu index
ulang:

```bash
DATA_DIR=./data/workspaces/eval-docs WORKSPACE=eval-docs \
  uv run --project apps/api enigram golden retrieval --file eval/golden-docs.yaml
```

**Set pengukuran (K1).** Tuning hanya di `dev`. Setiap fase yang di-merge dijalankan sekali di
semua split (`--allow-verdict`) sebagai konfirmasi, tanpa tuning sesudahnya, seperti
`golden route`.

### R1: ranking `search_code` (dibangun 2026-11-22)

Rencana awal (tokenizer identifier untuk full-text) diuji dulu dan **tidak dipakai**: di umami
kanal nama + path yang dipecah per kata hanya menjadi kanal terbaik untuk 3 dari 84 evidence,
dan menurunkan MRR. Diagnosis per kanal menunjukkan masalahnya di fusion: kanal vektor sudah
menaruh 59 dari 84 evidence paling atas, tapi RRF dengan full-text (`ts_rank` OR-query tanpa BM25,
didominasi tipe hasil generate dan duplikat `clickhouseQuery`) mendorongnya keluar dari top 6.

Yang dibangun (keputusan pemilik 2026-11-22), hanya untuk `search_code` (`search_ranking`):

- Simbol yang namanya disebut persis di pertanyaan (`getWebsiteStats`, `Class.method`) dipin
  di atas; nama yang lebih panjang yang memuatnya tidak.
- Sisanya urutan vektor. Full-text hanya dipakai bila tidak ada vektor atau embedding gagal.
- `doc_conflicts` tetap memakai `rank_search` lama (belum diukur); sistem `code` yang dibekukan
  tidak berubah.
- `AGENT_VERSION` 3.10.

Risiko: kata persis yang bukan nama simbol (teks pesan error, env `UPPER_SNAKE`) tidak lagi
dibantu full-text di `search_code`; agent masih punya `grep`. Full-text dengan BM25 yang layak
(R5, atau pg_search) bisa diuji lagi sebagai kanal hybrid nanti.

### R2a: reranker untuk `search_code` (dibangun 2026-11-22, gate tidak lulus)

Fase baru, dikerjakan sebelum R2 karena bisa diukur di umami tanpa korpus dokumen.

- 20 kandidat teratas dari ranking R1 di-rerank oleh `CODE_RERANK_MODEL` (default `rerank-2.5`,
  `off` mematikannya; tidak ada di provider embedding OpenAI-compatible). Sesudah rerank,
  `search_code` menampilkan 6 teratas.
- Query diberi instruksi ("Find the TypeScript function, route handler or constant whose code
  answers this question about the codebase: "). Dokumen per simbol berisi nama, jenis dan file,
  ringkasannya, serta signature, tanpa body.
- Kalau panggilan rerank gagal, urutan R1 dipakai.
- `doc_conflicts` tidak ikut. `AGENT_VERSION` 3.11.

**Gate tidak lulus**: recall@6 naik 1,9 poin, tetapi MRR@20 turun 3,4 poin. Varian ini juga
percobaan keempat di 43 soal yang sama, jadi bisa overfit. Pemilik memutuskan tetap
memakainya (2026-11-22). Biaya per `search_code`: satu panggilan Voyage (sekitar 4,5k token; 9k dengan
dokumen penuh) dan latensi p50 sekitar 0,45 detik.

### R2: reranker untuk dokumen (ditunda, K6)

- Isi `rerank_model_func` LightRAG dengan Voyage `rerank-2.5` (kunci Voyage sudah ada),
  lalu `enable_rerank=True`.
- Naikkan `chunk_top_k` agar reranker punya kandidat lebih banyak (mulai 60), dan biarkan
  hasil rerank dipotong oleh budget token yang ada.
- Matikan kalau provider embedding bukan Voyage dan tidak ada reranker lain yang diatur.

Bukti: di uji Anthropic Contextual Retrieval, reranker menurunkan kegagalan top-20 dari 2,9% ke
1,9% (sekitar -34% relatif). Tambahan latensi sekitar 100-300 ms per query.

### R3: `input_type` untuk embedding dokumen

Kirim `input_type="document"` saat index dan `"query"` saat mencari, sama seperti jalur kode
(`application/code_embeddings.py:76`). Vektor dokumen berubah, jadi manifest akan meminta
re-index penuh; gabungkan re-index ini dengan R4 kalau R4 disetujui. Belum ada angka publik
yang saya temukan, jadi hasilnya harus diukur sendiri.

### R4: contextual retrieval untuk chunk dokumen

- Sebelum insert, LLM menulis 50-100 token konteks per chunk: posisi chunk dalam dokumen dan
  apa yang dibahasnya. Konteks ditambahkan di depan teks chunk (setelah heading path).
- Cache konteks per hash (teks chunk + id dokumen), karena perubahan satu dokumen sekarang
  memicu re-index penuh (`application/indexing.py:156-161`).
- Masukkan setting ini ke manifest agar perubahan prompt memaksa re-index.

Bukti: Anthropic melaporkan kegagalan top-20 turun 35% dengan contextual embedding, 49% kalau
digabung dengan contextual BM25. Biaya mereka sekitar $1 per juta token dokumen (Claude Haiku
dengan prompt caching); dengan DeepSeek harus dihitung ulang.

Efek samping: teks chunk yang lebih panjang juga dibaca saat ekstraksi entity LightRAG, jadi
biaya ekstraksi ikut naik.

### R5: full-text + RRF untuk dokumen (bersyarat)

LightRAG tidak punya kanal BM25. Kalau setelah R2-R4 `context_recall` dokumen masih rendah
untuk soal yang memakai istilah persis (nama fitur, kode error, angka), tambahkan:

- tabel full-text atas chunk dokumen di Postgres,
- merge hasilnya dengan chunk LightRAG lewat RRF k=60 sebelum masuk prompt.

Hanya berlaku untuk `STORAGE_BACKEND=postgres`. Elastic melaporkan RRF +18% nDCG@10 di atas
BM25 saja di BEIR, dan hasilnya tidak pernah lebih buruk dari BM25.

### R6: graph dokumen (bersyarat)

Mulai dari ablation di R0: `mix` vs `naive` di golden-docs.

- Kalau `mix` tidak lebih baik, jangan tambah fitur graph. Pertimbangkan default `naive` +
  reranker (lebih murah, tanpa ekstraksi keyword per query).
- Kalau `mix` lebih baik, urutannya:
  1. Entity resolution otomatis: kandidat dari kemiripan embedding nama + normalisasi string,
     keputusan merge lewat decision layer (pertanyaan baru di `decisions.yaml`), hasilnya
     berupa usulan di Inbox, bukan merge langsung.
  2. Coba gleaning pass, ukur entity yang bertambah vs biaya ekstraksi.

### R7: retrieval ulang saat bukti lemah

Pola CRAG (arXiv 2401.15884, +7 poin di PopQA): kalau `answerable` atau verifikasi per kalimat
menyatakan bukti lemah, agent mencari lagi dengan query yang diubah, bukan hanya mencatat
verdict. Bergantung pada pengukuran di `plans/answer-verification.md` (gate sebelum V4), jadi
dikerjakan setelah gate itu lulus.

## Fase dan PR

| PR | Isi | Gate merge |
|---|---|---|
| R0 | perintah `golden retrieval`, metrik dari trace agent, re-index Kestrel, baseline tercatat | angka baseline tertulis di rencana ini |
| R1 | ranking `search_code`: nama persis + vektor | recall@6 dan MRR@20 umami (43 soal) naik: LULUS 0,674→0,779, 0,494→0,772 |
| R2a | reranker `search_code` | recall@6 dan MRR@20 umami naik: TIDAK LULUS (0,779→0,798, 0,772→0,738); dipakai atas keputusan pemilik |
| R2 | reranker dokumen | `context_recall`/`context_precision` dokumen naik, latensi p50 dicatat (ditunda, K6) |
| R3+R4 | `input_type` + contextual retrieval, satu re-index | metrik dokumen naik dibanding R2, biaya re-index dicatat |
| R5 | full-text + RRF dokumen | hanya kalau R2-R4 belum cukup |
| R6 | entity resolution | hanya kalau `mix` > `naive` di R0 |
| R7 | retrieval ulang | setelah gate answer-verification |

Satu PR per baris, tidak di-stack. Setiap PR mencatat run id eval di bagian "Hasil" di bawah.

## Risiko

- **Set pengukuran kecil.** 30 soal kode dan 35 soal dokumen; selisih 1-2 soal bisa noise.
  Catat per soal, jangan hanya rata-rata.
- **Gold evidence tidak lengkap.** Agent bisa menemukan simbol lain yang juga benar. Recall
  terhadap evidence adalah batas bawah, bukan kebenaran mutlak.
- **Angka riset dari vendor.** Voyage, Cursor dan Anthropic melaporkan angka sendiri di data
  mereka. Gate di atas memakai data kita.
- **Re-index dokumen mahal.** R3 dan R4 memicu re-index penuh termasuk ekstraksi entity LLM.

## Keputusan untuk pemilik

- **K1 (diputuskan 2026-11-22).** Tuning di `dev` saja; satu run konfirmasi di semua split
  setelah setiap fase di-merge. `reserve` tidak dipakai untuk tuning, karena ia cadangan
  `verdict` di Phase 7 (`eval/README.md:30`).
- **K2.** Reranker: `rerank-2.5` atau `rerank-2.5-lite` (lebih murah dan cepat)?
- **K3 (diputuskan 2026-11-22).** Eval dokumen di workspace terpisah. Workspace `eval-docs`
  yang sudah ada dipakai, tanpa index ulang.
- **K6 (baru).** Korpus dokumen evaluasi terlalu kecil untuk R2-R6 (lihat Hasil). Pilihan:
  tambah dokumen pengecoh ke `eval-docs`, pakai korpus lain dengan soal baru, atau tunda fase
  dokumen.
- **K4 (diputuskan 2026-11-22).** Soal retrieval kode ditambah: Claude membuat draf soal dan
  evidence, pemilik me-review (label tidak blind). Setiap kunci jawaban dicek ulang terhadap
  kode oleh pemeriksa kedua; yang salah atau kurang diputuskan pemilik.
  - **Diubah 2026-11-22 (pemilik): eval retrieval hanya memakai umami** (Next.js App Router,
    MIT, `eval/golden-umami.yaml`, 43 soal `dev`). nestjs-boilerplate tidak dipakai lagi untuk
    retrieval: kecil (190 file, 9,6k LOC, 408 simbol) dan namanya rapi seperti buku teks.
  - Gate setiap fase kode: recall@6 dan MRR@20 umami naik, dibandingkan per soal (soal yang
    membaik dan memburuk), bukan hanya rata-rata.

  Kandidat diukur 2026-11-22 dengan `repo_limits` dan parse sidecar (tanpa LLM). Tidak ada
  aplikasi nyata di bawah `CODE_MAX_LOC` 50k:

  | Repo | File / LOC | Simbol | Route HTTP | Call ter-resolve |
  |---|---|---|---|---|
  | linkwarden | 678 / 88,8k | 1.555 | 0 | 49% |
  | ghostfolio | 1.021 / 114,6k | 3.339 | 135 | 49% |
  | umami | 1.507 / 182,8k | 3.799 | 210 | 71% |

  Perkiraan biaya index umami 2,3-3,2 juta token, dari rasio repo yang sudah di-index (sekitar
  32% simbol diringkas, 1,9-2,6k token per ringkasan); belum diukur.

  **Celah cakupan parser yang ditemukan di linkwarden** (dikerjakan terpisah):
  - File TS di `packages/` (46 file `lib`, `router`, `filesystem`) tidak ter-index karena tidak
    ada `tsconfig.json` yang mencakupnya; hanya 492 dari 678 file yang ter-parse.
  - Route Next.js di `apps/web/pages/api` (66 file) tidak dikenali sebagai `http_route` di
    monorepo; semua 145 entry point jatuh ke `root`.
- **K5.** Pertanyaan berbahasa Indonesia: perlu full-text dengan config selain `english`?

## Sumber

- Anthropic, Contextual Retrieval: https://www.anthropic.com/news/contextual-retrieval
- Qu dkk., Is Semantic Chunking Worth the Computational Cost?: https://arxiv.org/abs/2410.13070
- cAST, AST chunking untuk kode: https://arxiv.org/abs/2506.15655
- Elastic, hybrid search dan RRF: https://www.elastic.co/search-labs/blog/improving-information-retrieval-elastic-stack-hybrid
- Voyage rerank-2.5: https://www.mongodb.com/company/blog/product-release-announcements/rerank-2-5-and-rerank-2-5-lite-instruction-following-rerankers
- Cursor, semantic search + grep: https://cursor.com/blog/semsearch
- RepoGraph: https://arxiv.org/abs/2410.14684, LocAgent: https://arxiv.org/abs/2503.09089
- RAG vs GraphRAG: https://arxiv.org/abs/2502.11371
- HippoRAG 2: https://arxiv.org/abs/2502.14802
- LazyGraphRAG: https://www.microsoft.com/en-us/research/blog/lazygraphrag-setting-a-new-standard-for-quality-and-cost/
- CRAG: https://arxiv.org/abs/2401.15884

## Hasil

### R2a (2026-11-22, umami 43 soal, 20 kandidat dari R1)

| Varian | recall@6 | MRR@20 | Membaik | Memburuk |
|---|---|---|---|---|
| R1 tanpa rerank | 0,779 | **0,772** | - | - |
| rerank-2.5, dokumen penuh (dengan body) | 0,775 | 0,761 | 6 | 4 |
| rerank-2.5, nama persis tetap di atas | 0,775 | 0,744 | 6 | 4 |
| rerank-2.5 + instruksi | 0,775 | 0,767 | 6 | 4 |
| rerank-2.5, dokumen ringkas | 0,748 | 0,684 | 4 | 4 |
| **rerank-2.5 + instruksi + dokumen ringkas (dibangun)** | **0,798** | 0,738 | 6 (u25, u30, u31, u32, u37, u38) | 2 (u17, u34) |
| rerank-2.5-lite, dokumen penuh | 0,717 | 0,656 | 5 | 7 |

- recall@20 tetap 0,868 (kandidatnya sama).
- Implementasi mereproduksi varian yang dibangun: `data/reports/retrieval-20261122-133006.md`.
- Eksperimen memakai sekitar 1,94 juta token rerank Voyage.

### R1 (2026-11-22, umami 43 soal)

| Varian `search_code` | recall@6 | recall@20 | MRR@20 |
|---|---|---|---|
| Sebelum (RRF full-text + vektor) | 0,674 | 0,775 | 0,494 |
| + nama persis diurutkan dulu | 0,674 | 0,787 | 0,500 |
| + kanal nama/path per kata | 0,674 | 0,787 | 0,483 |
| + normalisasi panjang `ts_rank` | 0,651 | 0,787 | 0,523 |
| RRF, bobot full-text 0,25 | 0,744 | 0,833 | 0,596 |
| **Nama persis + vektor (dibangun)** | **0,779** | **0,868** | **0,772** |

- Per soal (recall@6): 9 membaik (u02, u07, u08, u12, u15, u17, u21, u27, u33), 0 memburuk.
- Laporan: `data/reports/retrieval-20261122-122410.md`.

**Agent dengan R1 pada 43 soal** (`answers-enigram-agent-20261122-124859`, DeepSeek,
`data/reports/retrieval-20261122-124956.md`). Baseline agent untuk fase berikutnya; tidak ada run
sebelum R1 (keputusan pemilik), dan benar-salah jawaban belum dinilai.

| Metrik | Nilai |
|---|---|
| Evidence dibaca agent | 0,907 |
| Evidence dikutip agent | 0,798 |
| Kutipan valid | 223/223 (43/43 jawaban) |
| Kalimat tanpa kutipan | 36 |
| Token | 1.030.318 (rata-rata 24k, median 14k, maks 118k per soal) |
| Tool call per soal | 8,1 (read_file 179, search_code 80, grep 39, find_symbol 33, entrypoint_flow 16) |
| Latensi p50 / maks | 8,9 / 24,1 detik |

- 0 error, 0 abstain.
- Evidence terlewat atau tidak dikutip di 15 soal. Terburuk u23 (revenue: 1 dari 3 dibaca, 0
  dikutip; `search_code` juga 0). u28 dan u29 membaca evidence tapi mengutip baris lain.

### R0 baseline (2026-11-22)

**Kode, split `dev`** (`data/reports/retrieval-20261122-093232.md`, golden `acfec67e`,
`voyage-code-4`):

| Metrik | Nilai |
|---|---|
| recall@6 `search_code` | 0,758 |
| recall@20 | 0,833 |
| MRR@20 | 0,49 |
| evidence dibaca agent (run `answers-enigram-agent-20261108-130801`) | 0,833 |
| evidence dikutip agent | 0,788 |

- 11 soal; satu soal sekitar 9 poin.
- b63 (`AppModule`, `UsersModule`): recall@6 0, evidence pertama di posisi 20. Search lemah
  untuk pertanyaan wiring modul.
- a01: `search_code` menemukan evidence di posisi 3, tapi agent tidak membacanya. Ini masalah
  agent, bukan retrieval.
- Agent membaca lebih banyak evidence daripada yang ditemukan `search_code`, lewat `grep` dan
  `find_symbol`.

**Kode, umami `eval-v1`** (`eval/golden-umami.yaml`, workspace `default`). Ini baseline yang
dipakai untuk gate fase kode:

| Set | Laporan | recall@6 | recall@20 | MRR@20 |
|---|---|---|---|---|
| 18 soal pertama | `retrieval-20261122-112125` | 0,685 | 0,778 | 0,534 |
| **43 soal (u01-u43)** | `retrieval-20261122-114152` | **0,674** | **0,775** | **0,494** |

- Index umami: 3.799 simbol, 1.392 ringkasan, 5.191 embedding, 2.259.955 token (termasuk 8
  Markdown), sekitar 595 token per simbol.
- u08 menyebut `getWebsiteStats`, tapi implementasinya (`src/queries/sql/getWebsiteStats.ts`)
  tidak masuk top-20: nama yang sama di `packages/api-client` (client hasil generate) dan
  `packages/mcp` mengisi hasil name match.
- u02 dan u07: hasil teratas diisi kode hasil generate (`packages/api-client/src/generated`)
  dan konstanta ClickHouse.

Baseline nestjs di atas hanya catatan sejarah: database lokal di-reset 2026-11-22 dan nestjs
tidak dipakai lagi untuk eval retrieval (K4).

**Dokumen, `eval/golden-docs.yaml`, workspace `eval-docs`**
(`data/workspaces/eval-docs/reports/retrieval-20261122-095340.md`, 30 soal berevidence):

| Mode | context_recall | context_precision |
|---|---|---|
| mix | 1,0 | 0,854 |
| naive | 1,0 | 0,889 |

Korpusnya hanya 29 chunk. `mix` selalu mengambil 20 chunk (`chunk_top_k`), `naive` rata-rata
17,3. Recall 1,0 karena hampir seluruh korpus terambil, bukan karena retrieval bagus. Set ini
tidak bisa menunjukkan perbaikan R2-R6; precision hanya memberi sinyal lemah. `naive` tidak
lebih buruk dari `mix` di sini, tapi korpus terlalu kecil untuk menyimpulkan R6. Lihat K6.
