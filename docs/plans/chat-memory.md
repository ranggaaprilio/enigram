# Memori chat dengan Pydantic AI

Rencana dibuat 2026-11-09, diubah di hari yang sama: owner memilih memakai Pydantic AI. Belum
diimplementasikan.

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

**Batas arsitektur.** Pydantic AI diletakkan di adapter, bukan di application layer. Port baru
`ToolLoop` (`application/ports.py`) punya tanda tangan yang sama dengan `run_tool_loop` hari ini:
masukannya messages, tools, `run_tool`, dan batas; keluarannya `LoopResult`. Adapter
`adapters/driven/pydantic_agent.py` mengimplementasikan port itu. `answer_with_agent` hanya
mengganti pemanggilan `run_tool_loop` dengan port ini. `loop_answer`, guard, cache, dan citation
tidak berubah.

**Isi adapter:**
- Model: `OpenAIChatModel` dari Pydantic AI dengan `OpenAIProvider(base_url=cfg.llm_base_url,
  api_key=cfg.llm_api_key)`. `model_settings` membawa `temperature`, `max_tokens`, dan
  `extra_body` (thinking off), sama seperti adapter sekarang.
- Tool: dibuat dari skema JSON yang sudah ada (`Tool.from_schema`). Fungsinya hanya memanggil
  `run_tool(name, args)`, jadi `on_tool` dan `CodeAgentTools.run` tetap persis seperti sekarang.
  Bila argumen tidak valid, tool mengembalikan teks error, bukan meminta retry.
- Budget tool call: dihitung di adapter. Begitu batas tercapai, `prepare_tools` mengembalikan
  daftar tool kosong dan `BUDGET_MESSAGE` disisipkan. Model lalu menjawab sekali lagi tanpa tool,
  sama seperti sekarang. `UsageLimits` hanya dipakai sebagai pengaman (`request_limit`), bukan
  sebagai mekanisme budget, karena kalau batasnya tercapai ia melempar error, bukan memaksa
  model menjawab.
- Meter token: loop dijalankan dengan `agent.iter`. Setelah setiap respons model, usage-nya
  dicatat lewat `record_usage`. Dengan begitu budget harian tetap bisa memotong loop di tengah.
- Thinking: `ThinkingPart` diabaikan dan `strip_think` tetap diterapkan pada teks akhir.
- Error provider dari Pydantic AI (`ModelHTTPError` dan sejenisnya) dibungkus menjadi
  `EnigramError`.
- Hasil: dari pesan-pesan run, adapter membangun `LoopResult` (jawaban, token, jumlah call, trace,
  `exhausted`).

**Baseline eval `agent+repo` tetap memakai `run_tool_loop` lama.** Baseline adalah pembanding.
Kalau baseline ikut dimigrasi, kita tidak bisa membedakan apakah perubahan skor datang dari
Pydantic AI. `run_tool_loop` tetap ada, khusus untuk baseline.

**Version pin.** `pydantic-ai-slim[openai]` di-pin ke versi minor tertentu di `pyproject.toml`.
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

- **P1: adapter.** Port `ToolLoop`, adapter Pydantic AI, dan dependency yang di-pin.
  → verifikasi: unit test adapter dengan `FunctionModel`/`TestModel` milik Pydantic AI untuk
  setiap baris tabel "Kondisi sekarang":
  - urutan tool call,
  - argumen tidak valid menjadi teks,
  - budget habis → `BUDGET_MESSAGE` lalu satu jawaban tanpa tool,
  - `exhausted=True`,
  - `record_usage` dipanggil per respons,
  - meter yang melewati batas menghentikan loop,
  - `strip_think`,
  - error provider menjadi `EnigramError`,
  - `on_tool` terpanggil.
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
- **DeepSeek dan `extra_body`.** Pastikan `model_settings` benar-benar meneruskan `extra_body`
  di setiap panggilan. Kalau tidak, thinking menyala dan error 400 muncul saat
  `tool_choice=required`. Ini diuji di P1.

## Keputusan untuk owner

- **D1. Batas dan batch.** `SUMMARY_MAX_TOKENS` = 800 dan `SUMMARY_BATCH_TURNS` = 3? Apakah
  `HISTORY_MAX_TURNS` dan `HISTORY_MAX_TOKENS` tetap 6 dan 3000?
- **D2. Ringkasan sebagai evidence guard.** Usul: tidak. Ringkasan adalah kompresi buatan LLM
  yang belum dicek.
- **D3. Penanda di UI.** Perlu atau tidak?
- **D4. Eval multi-turn.** Cukup uji manual, atau perlu kasus multi-turn di golden eval?
- **D5. Cakupan migrasi.** Usul: hanya `answer_with_agent`. Baseline `agent+repo` tetap memakai
  loop lama, dan `ChatModel.complete` (rewrite, guard, LightRAG) tidak diubah.
