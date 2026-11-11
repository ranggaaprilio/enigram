# Memori chat dengan Pydantic AI

Rencana dibuat 2026-11-09, diubah di hari yang sama: owner memilih memakai Pydantic AI. PR 1
(migrasi loop) diimplementasikan 2026-11-11; Bagian A sudah disesuaikan. PR 2 belum.

## Masalah

Agent hanya melihat sebagian percakapan. `history_of` (`application/chat.py`) mengirim pasangan
tanya-jawab terbaru, maksimal `HISTORY_MAX_TURNS` (6) pasang dan `HISTORY_MAX_TOKENS` (3000)
token (`domain/chat.py`). Giliran yang lebih lama dibuang tanpa jejak. Akibatnya, di percakapan
panjang agent "lupa" hal yang masih terlihat di layar. Contoh: nama service yang disebut di
giliran 1, lalu ditanyakan lagi di giliran 10.

Menaikkan batasnya saja tidak cukup. History dikirim ulang di setiap tool call dalam satu jawaban
(lihat komentar di `domain/chat.py`). Jadi biayanya berlipat sesuai jumlah tool call, dan suatu
saat batasnya tetap tercapai.

## Tujuan

1. **Migrasi agent loop ke Pydantic AI.** Item ini memang dijadwalkan setelah B4 (PR #64, sudah
   merged), dan owner sudah setuju bahwa migrasinya harus lolos gate akurasi.
2. **Memori chat lewat ringkasan berjalan.** Giliran yang keluar dari jendela history tidak hilang,
   tetapi diringkas. Yang dikirim ke agent adalah **ringkasan giliran lama + giliran terbaru utuh**,
   lewat capability `ProcessHistory` milik Pydantic AI. Cara ini sama dengan "compact" di ChatGPT
   dan Claude.

Urutannya wajib: (1) dulu, baru (2). `ProcessHistory` hanya berlaku untuk loop yang dijalankan
oleh Pydantic AI. Saat ini Pydantic AI belum ada di dependency sama sekali.

Bukan tujuan rencana ini:
- memori lintas session (seperti mem0, Zep, Letta),
- retrieval atas history,
- mengubah tool, prompt, guard, atau cara citation diproses.

## Kondisi sekarang

Semua loop agent ada di satu fungsi: `run_tool_loop` (`application/agent_loop.py`). Fungsi ini
dipanggil dari dua tempat:

- `answer_with_agent` (`code_agent.py`). Ini dipakai oleh chat, `/query`, MCP, dan route `agent`.
- baseline eval `agent+repo` (`evaluation/agent_repo.py`).

Perilaku loop dan adapter yang harus tetap sama:

| Perilaku | Di mana sekarang |
|---|---|
| Tool memakai skema function OpenAI (`TOOLS`, `DOC_TOOLS`), dijalankan oleh `CodeAgentTools.run` | `code_agent.py` |
| Tool tidak pernah melempar error ke model; masalah dikembalikan sebagai teks, termasuk argumen JSON yang tidak valid | `run_tool_loop` |
| Budget: maksimal 30 tool call atau 120k token input kumulatif. Setelah itu, pesan `BUDGET_MESSAGE` dikirim lalu model diminta menjawab sekali lagi **tanpa tool** (bukan dihentikan dengan error) | `run_tool_loop` |
| `temperature=0.0` | `run_tool_loop` |
| `on_tool` dipanggil sebelum setiap tool jalan (langkah yang tampil di SSE chat) | `answer_with_agent` |
| Token setiap panggilan provider dicatat ke meter budget harian (`record_usage`). Meter melempar error begitu batas terlewati, **di tengah loop** | `openai_llm.py` |
| DeepSeek: `extra_body` untuk mematikan thinking, `max_tokens = cfg.llm_max_tokens`, `strip_think` pada jawaban | `openai_llm.py` |
| Error provider dibungkus menjadi `EnigramError` | `openai_llm.py` |
| `LoopResult` (jawaban, token in/out, jumlah call, trace, `exhausted`) dibaca oleh `loop_answer` dan diagnostics | `agent_loop.py` |

## Desain

### Bagian A: migrasi loop

Diubah 2026-11-11 saat implementasi (owner memilih opsi ini di Gate 1). Rencana awal memakai
`OpenAIChatModel` milik Pydantic AI di adapter. Itu ditinggalkan karena extra `[openai]` Pydantic
AI butuh `openai>=3.31`, sedangkan repo memakai 3.3, dan karena metering, `extra_body`,
`strip_think`, serta pembungkusan error harus disalin ulang.

**Desain yang dipakai.** `application/pydantic_loop.py` berisi `run_agent_loop`, dengan tanda
tangan dan `LoopResult` yang sama dengan `run_tool_loop`. Di dalamnya ada `Model` Pydantic AI
buatan sendiri yang membungkus port `ChatModel`. Pydantic AI menjalankan loop, tetapi setiap
panggilan model tetap lewat `OpenAIChatModel.chat`, jadi `record_usage` per respons,
`provider_extra_body`, `strip_think`, dan `EnigramError` tidak berubah dan tidak disalin. Pydantic
AI tidak melakukan I/O sendiri di sini, jadi modul ini ada di application layer (seperti
networkx atau yaml). Port `ToolLoop` dan wiring lewat bootstrap tidak diperlukan.
`answer_with_agent` cukup memanggil `run_agent_loop`.

**Perilaku Pydantic AI yang dikunci supaya sama dengan loop lama:**
- Tool dibuat dengan `Tool.from_schema(..., sequential=True)`. Defaultnya, Pydantic AI menjalankan
  tool secara paralel.
- Argumen yang bukan JSON object dan nama tool yang tidak dikenal dikirim ke satu tool tersembunyi
  yang menjawab dengan teks. Defaultnya, Pydantic AI meminta retry dan run gagal setelah
  `max_retries`.
- Balasan kosong diganti penanda internal. Defaultnya, Pydantic AI meminta retry.
- Argumen mentah setiap tool call disimpan sesuai urutan. Pydantic AI menulis ulang JSON-nya,
  padahal request berikutnya harus mengirim teks yang sama persis.
- Budget: setelah satu putaran tool, bila batas call atau token input tercapai, `BUDGET_MESSAGE`
  ditambahkan ke request berikutnya (lewat `agent.iter`) dan capability `PrepareTools`
  mengosongkan daftar tool. `UsageLimits(request_limit=max_tool_calls + 2)` hanya pengaman.
- `messages` diisi dengan pesan request terakhir, sama seperti loop lama, karena guard membaca
  tool output dari situ (`tool_evidence`).

`tests/test_pydantic_loop.py` menjalankan loop lama dan baru pada skrip yang sama, lalu
membandingkan setiap request, urutan tool, `messages`, dan `LoopResult`.

**Baseline eval `agent+repo` tetap memakai `run_tool_loop` lama.** Baseline adalah pembanding.
Kalau baseline ikut dimigrasi, kita tidak bisa membedakan apakah perubahan skor datang dari
Pydantic AI. `run_tool_loop` tetap ada, khusus untuk baseline.

**Version pin.** `pydantic-ai-slim==2.67.0` (tanpa extra) di-pin di `apps/api/pyproject.toml`.
API `capabilities` / `ProcessHistory` / `CompactionPart` masih baru, jadi pin ini mencegah
perubahan diam-diam.

**`AGENT_VERSION`** dinaikkan. Ini wajib, karena kalau tidak, cache eval akan menyembunyikan
perubahan loop.

### Bagian B: memori chat

**Data.** Dua kolom baru di `chat_sessions`, ditambahkan dengan `ALTER TABLE ... ADD COLUMN IF
NOT EXISTS` sesuai pola di `postgres_jobs.py`:

| Kolom | Tipe | Isi |
|---|---|---|
| `summary` | `TEXT NOT NULL DEFAULT ''` | Ringkasan giliran lama |
| `summary_upto` | `BIGINT` | `id` pesan terakhir yang sudah tercakup ringkasan; `NULL` = belum ada |

`ChatSession` dan `ChatRepository.update_session` mendapat field yang sama. Session yang sudah ada
otomatis berisi ringkasan kosong, jadi perilakunya tetap seperti sekarang.

**History tetap disimpan sebagai teks.** Tool call dan tool output tidak ikut disimpan. History
teks diubah menjadi `ModelRequest`/`ModelResponse` di adapter. Format seluruh pesan Pydantic AI
(`ModelMessagesTypeAdapter`) sengaja tidak disimpan, karena tool output membuat history membengkak
dan mengikat skema database ke format internal satu library.

**Membuat ringkasan.** Ringkasan dibuat oleh `Agent` Pydantic AI kecil (`summarize_agent`, model
yang sama) di `ask`, **setelah** semua jawaban giliran itu tersimpan dan dikirim lewat `emit`.
Jadi pengguna tidak menunggu proses ringkasan.

1. Ambil giliran yang keluar dari jendela history dan `id`-nya lebih besar dari `summary_upto`.
2. Kalau jumlahnya kurang dari `SUMMARY_BATCH_TURNS` (usul: 3), lewati. Batching ini menjaga
   biaya.
3. Kalau cukup, ringkas: ringkasan lama + giliran yang baru keluar → ringkasan baru, maksimal
   `SUMMARY_MAX_TOKENS` (usul: 800).
4. Simpan `summary` dan `summary_upto`. Kalau gagal, catat di log dan pakai ringkasan lama.
   Giliran pengguna tidak ikut gagal, dan ringkasan dicoba lagi di giliran berikutnya. Token
   ringkasan dihitung ke budget harian dengan source `chat`.

Kenapa ringkasan tidak dibuat di dalam processor? `ProcessHistory` dijalankan **sebelum setiap
panggilan model**, termasuk setiap putaran tool. Kalau ringkasan dibuat di sana, LLM dipanggil
berkali-kali dalam satu jawaban. Contoh di dokumentasi Pydantic AI memang meringkas di processor,
tetapi itu untuk run pendek tanpa ringkasan yang disimpan.

**Memakai ringkasan: `ProcessHistory`.** Processor-nya deterministik dan murah:
- bagian history dari giliran sebelumnya diganti dengan `CompactionPart(summary)` + giliran
  terbaru di dalam jendela (`HISTORY_MAX_TURNS` dan `HISTORY_MAX_TOKENS`, logika `history_of`
  dipindah ke sini),
- pesan dari run saat ini (pertanyaan, tool call, tool output) **tidak disentuh**, supaya
  pasangan tool call dan tool return tidak terputus.

Ringkasan juga masuk ke `standalone_question`. Contoh: pertanyaan "service yang tadi itu" bisa
ditulis ulang menjadi nama service yang benar walaupun giliran aslinya sudah di luar jendela.

**Guard dan cache.**
- Guard abstain mengambil `assistant` dari `history` sebagai evidence. Ringkasan **tidak**
  ikut dihitung sebagai evidence. Lihat D2.
- Cache key `answer_with_agent` ditambah `summary` (bila tidak kosong), sama seperti `history`.
- LightRAG tetap tidak menerima history. Ringkasan sampai ke sana lewat pertanyaan standalone.

**UI.** Tidak ada perubahan wajib. Opsional: penanda "Pesan sebelum ini diringkas untuk agent"
(D3).

## Fase dan PR

Dua PR berurutan, **tidak bertumpuk**. PR 2 dibuat dari `main` setelah PR 1 merged.

**PR 1: migrasi loop (P1-P2).**

- **P1: loop.** `run_agent_loop` di atas `ChatModel` dan dependency yang di-pin.
  → verifikasi: test paritas dengan `run_tool_loop` (request yang sama, tool yang sama, `messages`
  dan `LoopResult` yang sama) untuk: jawaban langsung, beberapa putaran, argumen tidak valid,
  tool tidak dikenal, teks bersama tool call, jawaban kosong, budget call, budget token, tool call
  setelah budget. Ditambah: tool berjalan berurutan, dan error provider atau meter diteruskan apa
  adanya. Metering, `extra_body`, `strip_think`, dan `EnigramError` tetap di `OpenAIChatModel`
  dan test-nya yang sudah ada.
- **P2: dipakai `answer_with_agent`.** Naikkan `AGENT_VERSION`. `run_tool_loop` tetap untuk
  baseline.
  → verifikasi:
  - Seluruh test API yang ada lulus.
  - **Gate akurasi** dengan runner B4 (scratchpad `run_b4.sh`): code `--split dev` (11),
    code unanswerable+injection (15), docs (35), untuk sistem `agent-auto`, `agent-business`,
    dan `enigram-agent`. Lulus bila skor baru ≥ skor lama per set. Token dan latensi dilaporkan,
    bukan di-gate (keputusan owner: akurasi dulu).
  - Uji manual di Docker: satu pertanyaan chat dengan langkah SSE tampil.

**PR 2: memori chat (M1-M3).**

- **M1: penyimpanan.** Kolom, field, `update_session`, dan `turns_to_summarize(messages,
  summary_upto)`.
  → verifikasi: unit test fungsi murni, test PG untuk kolom baru dan session lama.
- **M2: membuat ringkasan.** `summarize_agent`, `SUMMARY_PROMPT`, dan pemanggilannya di akhir
  `ask`.
  → verifikasi: test dengan `FunctionModel`:
  - ringkasan tersimpan setelah batch tercapai,
  - tidak ada panggilan sebelum batch,
  - kegagalan membiarkan ringkasan lama,
  - jawaban tetap tersimpan.
- **M3: memakai ringkasan.** Processor `ProcessHistory`, `standalone_question`, cache key, dan
  `AGENT_VERSION`.
  → verifikasi:
  - unit test processor: ringkasan + jendela, pesan run saat ini utuh, pasangan tool tidak
    terputus,
  - guard tidak memakai ringkasan sebagai evidence,
  - uji manual di Docker: percakapan 10 giliran, lalu pertanyaan yang merujuk ke giliran 1 tanpa
    menyebut namanya. Tanpa ringkasan agent gagal, dengan ringkasan agent berhasil.

**(Opsional) M4: penanda di UI**, bila D3 = ya.

## Biaya

- **PR 1:** tidak ada perubahan biaya yang disengaja, karena prompt, tool, dan batasnya sama.
  Tetap diukur di gate.
- **PR 2:**
  - Per batch: satu panggilan LLM dengan input ≤ ~3,5k token dan output ≤ 800 token.
  - Per jawaban: tambahan ≤ 800 token di setiap putaran tool.
  - Session dengan 6 giliran atau kurang tidak bertambah biayanya.
- **Gate eval PR 1:** satu putaran runner B4 untuk 3 sistem × 61 pertanyaan. Biayanya sekitar
  biaya B4 sebelumnya.

## Risiko

- **Perilaku loop bergeser walaupun prompt sama.** Contohnya: format pesan tool, cara tool
  dipanggil paralel, atau penanganan teks yang muncul bersamaan dengan tool call. Gate akurasi
  dipasang untuk menangkap ini. Kalau gate gagal, PR 1 tidak di-merge, dan memori chat bisa
  dibuat dengan rencana tanpa framework (versi sebelumnya ada di git history file ini).
- **API Pydantic AI masih bergerak.** `capabilities` dan `CompactionPart` masih baru. Versi
  di-pin dan API-nya dicek lagi di dokumentasi saat P1 dimulai.
- **DeepSeek dan `extra_body`.** Tidak lagi menjadi risiko: setiap panggilan tetap lewat
  `OpenAIChatModel.chat`, yang menambahkan `extra_body` seperti sebelumnya.

## Keputusan untuk owner

- **D1. Batas dan batch.** `SUMMARY_MAX_TOKENS` = 800 dan `SUMMARY_BATCH_TURNS` = 3? Apakah
  `HISTORY_MAX_TURNS` dan `HISTORY_MAX_TOKENS` tetap 6 dan 3000?
- **D2. Ringkasan sebagai evidence guard.** Usul: tidak. Ringkasan adalah kompresi buatan LLM
  yang belum dicek.
- **D3. Penanda di UI.** Perlu atau tidak?
- **D4. Eval multi-turn.** Cukup uji manual, atau perlu kasus multi-turn di golden eval?
- **D5. Cakupan migrasi.** Usul: hanya `answer_with_agent`. Baseline `agent+repo` tetap memakai
  loop lama, dan `ChatModel.complete` (rewrite, guard, LightRAG) tidak diubah.
- Dukungan banyak penyedia (endpoint yang kompatibel dengan OpenAI, model lokal, embedding selain
  Voyage) ada di rencana terpisah: [llm-providers](llm-providers.md). Helper `extra_body` di
  Bagian A dibuat di PR 1 rencana itu.
- **D6. Migrasi lanjutan `ChatModel.complete`.** Setelah PR 2, apakah `ChatModel.complete`
  (rewrite, guard, judge) juga dipindah ke Pydantic AI?
  - Ya: semua panggilan LLM lewat satu jalur, dan dukungan banyak penyedia dari Pydantic AI
    (OpenAI, Anthropic, Gemini, Ollama) benar-benar terpakai. Perlu rencana dan gate akurasi
    sendiri.
  - Tidak: D5 tetap berlaku, dan fleksibilitas penyedia hanya ada di agent loop.
  - Di luar cakupan keduanya: LightRAG dan embedding Voyage, karena keduanya memakai klien
    sendiri. Mengganti model embedding juga berarti reindex, karena `EMBED_DIM` terikat ke kolom
    pgvector.
