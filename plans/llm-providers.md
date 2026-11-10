# Banyak penyedia LLM dan embedding

Draft 2026-11-10. Belum diimplementasikan. Keputusan owner di bagian akhir masih terbuka.

## Masalah

Enigram hanya bisa memakai DeepSeek untuk chat dan Voyage untuk embedding. Untuk proyek open
source yang self-hosted, ini penghalang adopsi:

- Tidak bisa memakai model lokal (Ollama, vLLM, LM Studio). Jadi kode dan dokumen selalu dikirim ke
  API luar, walaupun index dan catatan tersimpan di Postgres sendiri.
- Banyak tim tidak boleh mengirim kode ke penyedia tertentu, dan tidak punya pilihan lain.
- Orang yang sudah punya akun OpenAI, OpenRouter, atau penyedia lain tetap harus mendaftar ke
  DeepSeek dan Voyage dulu.

## Kondisi sekarang

| Bagian | Keterikatan | Di mana |
|---|---|---|
| Daftar penyedia chat | `LLM_PROVIDERS = ("deepseek",)`. `LLM_PROVIDER` dan `JUDGE_PROVIDER` lain ditolak | `config.py:23`, `config.py:196-210` |
| Endpoint chat | `endpoint()` selalu mengembalikan `DEEPSEEK_BASE_URL` dan `DEEPSEEK_API_KEY` | `config.py:200-202` |
| Flag thinking | `extra_body={"thinking": {"type": "disabled"}}` dikirim setiap kali `THINKING_MODE=off` | `openai_llm.py:23-35`, `openai_llm.py:124-125`, `ragas_scorer.py:12-13` |
| Cek `tool_choice=required` | Pesan error menyebut DeepSeek | `openai_llm.py:118-122` |
| Embedding dokumen, memori, Ragas | Hanya Voyage (`voyageai` SDK) | `voyage_embedding.py` |
| Embedding kode | Hanya Voyage (`VoyageCodeEmbedder`, dengan `input_type`) | `voyage_embedding.py`, `bootstrap.py:27` |
| `VOYAGE_API_KEY` | Wajib, walaupun nanti tidak dipakai | `config.py:260` |
| Cek penyedia (`doctor`) | Mengecek `/models`, completion, JSON mode, dimensi embedding | `openai_llm.py:186-245` |
| Halaman Setup, docs, README | Menyebut DeepSeek dan Voyage | `application/setup.py`, `docs/setup.md`, `README.md` |

Yang sudah mendukung perubahan ini:

- Semua panggilan chat sudah lewat SDK `openai` atau `openai_complete_if_cache` milik LightRAG
  dengan `base_url` dan `api_key` dari `Config`. Membuka endpoint lain tidak perlu klien baru.
- `guard_llm_model` dan `guard_embed_model` (`application/indexing.py:100-124`) sudah menolak
  mencampur index dari model berbeda. `PostgresCodeGraph` juga menolak `CODE_EMBED_DIM` yang beda
  dari kolom `vector(n)` yang tersimpan.
- `strip_think` aman untuk model yang tidak menulis blok `<think>`.

## Tujuan

1. **Chat lewat endpoint apa pun yang kompatibel dengan OpenAI**, untuk generator dan judge:
   OpenAI, OpenRouter, Ollama, vLLM, LM Studio, dan penyedia lain yang menyediakan endpoint
   `/v1/chat/completions`.
2. **Embedding lewat endpoint `/v1/embeddings` yang kompatibel dengan OpenAI**, untuk embedding
   dokumen dan embedding kode, selain Voyage.
3. **Akurasi di satu model selain DeepSeek diukur** dan hasilnya ditulis di docs, supaya README
   tidak menjanjikan hal yang belum dicek.

DeepSeek dan Voyage tetap menjadi default. Konfigurasi yang ada sekarang tetap jalan tanpa
perubahan.

Bukan tujuan rencana ini:

- SDK native non-OpenAI (Anthropic, Gemini). Keduanya bisa dicoba lewat endpoint kompatibel
  OpenAI masing-masing, tapi tidak diuji di sini. Jalur native ada di D6 rencana
  [chat-memory](chat-memory.md), lewat Pydantic AI.
- Reindex otomatis saat model diganti. Guard yang ada tetap meminta reindex manual.
- Memilih penyedia dari web UI. Halaman Setup tetap hanya menampilkan konfigurasi.
- Mengganti klien LLM milik LightRAG.

## Desain

### Bagian A: chat

**Config.** `LLM_PROVIDERS = ("deepseek", "openai-compatible")`. Nama nilai kedua menunggu P1.

| Variabel | `deepseek` | `openai-compatible` |
|---|---|---|
| `LLM_BASE_URL` | Diabaikan, tetap `DEEPSEEK_BASE_URL` | Wajib, misalnya `http://ollama:11434/v1` |
| API key | `DEEPSEEK_API_KEY` (wajib) | `LLM_API_KEY` (boleh kosong untuk server lokal) |
| `LLM_MODEL` | Default `deepseek-flash` | Wajib |

Judge memakai variabel yang sama dengan awalan `JUDGE_` (`JUDGE_BASE_URL`, `JUDGE_API_KEY`). Bila
`JUDGE_PROVIDER` kosong, judge tetap mengikuti generator seperti sekarang.

**Satu helper untuk parameter khusus penyedia.** `provider_extra_body(cfg)` di `openai_llm.py`:

- `deepseek` dan `THINKING_MODE=off`: `DEEPSEEK_NO_THINKING`.
- Selain itu: kosong. `THINKING_MODE` hanya berlaku untuk DeepSeek, dan `doctor` mencetak
  peringatan bila diisi untuk penyedia lain.

Helper ini menggantikan tiga tempat yang sekarang menulis flag itu sendiri: `prepare_request`,
`OpenAIChatModel.chat`, dan `_api_thinking` di `ragas_scorer.py`. Adapter Pydantic AI di rencana
chat-memory memakai helper yang sama. Penolakan `tool_choice=required` di
`OpenAIChatModel.chat` hanya berlaku untuk DeepSeek dengan thinking menyala.

**Syarat model.** Model yang dipilih harus mendukung:

- tool calling (agent loop menjawab lewat tool call, dengan `tool_choice` default `auto`;
  `tool_choice="required"` tidak dipakai di mana pun),
- JSON mode (`response_format={"type": "json_object"}`, dipakai LightRAG dan judge),
- konteks yang cukup untuk budget agent (120k token input kumulatif, dibagi ke beberapa panggilan).

`doctor` menambah satu cek: satu panggilan yang menawarkan satu tool, tanpa memaksa
`tool_choice`, sama seperti agent loop. Bila model tidak memanggil tool itu, `doctor` gagal dengan
pesan yang menyebut syarat ini. `doctor` juga memberi peringatan bila endpoint tidak melaporkan
`usage`. Cek JSON mode yang sudah ada tetap.
Bila `/models` tidak ada di server (404), `doctor` mencetak peringatan, bukan gagal.

**Ganti model pada index yang sudah ada.** `guard_llm_model` tetap menolak. Ini disengaja: entity
dari dua model extraction tidak boleh bercampur. Docs menjelaskan langkah reindex.

### Bagian B: embedding

**Config.** `EMBED_PROVIDER` dengan nilai `voyage` (default) dan `openai-compatible`, plus
`EMBED_BASE_URL` dan `EMBED_API_KEY`. `VOYAGE_API_KEY` hanya wajib bila `EMBED_PROVIDER=voyage`.
`EMBED_MODEL`, `EMBED_DIM`, `CODE_EMBED_MODEL`, dan `CODE_EMBED_DIM` tetap. Untuk
`openai-compatible`, model dan dimensi wajib diisi, karena default Voyage tidak berlaku.

**Adapter.** `openai_embedding.py` baru, dengan tiga bagian yang sama seperti
`voyage_embedding.py`: fungsi embedding LightRAG, `CodeEmbedder`, dan embedding Ragas.
`bootstrap.py`, `lightrag_engine.py`, `ragas_scorer.py`, dan `check_providers` memilih adapter
berdasarkan `EMBED_PROVIDER`.

Perbedaan dengan Voyage yang harus ditangani:

| Hal | Voyage | `openai-compatible` |
|---|---|---|
| `input_type` (query atau document) | Dikirim untuk embedding kode | Tidak ada. Diabaikan |
| Teks terlalu panjang | `truncation=True` | Tidak ada standar. Teks dipotong di sisi Enigram ke `EMBED_MAX_TOKENS` (tiktoken) sebelum dikirim |
| Token terpakai | `total_tokens` | `usage.prompt_tokens`, dicatat ke `record_usage` dengan cara yang sama |
| Ukuran batch | 128 | Konstanta sendiri, lebih kecil untuk server lokal |

**Dimensi dan reindex.** Guard yang sudah ada menangani pergantian model. Docs menjelaskan: ganti
model embedding berarti hapus index dokumen dan embedding kode, lalu index ulang.

### Bagian C: ukur, lalu dokumentasikan

- Jalankan runner B4 (code dev 11, unanswerable+injection 15, docs 35) untuk `enigram-agent` dengan
  satu model non-DeepSeek (P2). Untuk embedding, pakai satu model embedding non-Voyage (P4).
- Tulis hasilnya di `docs/evaluation.md`, berdampingan dengan skor DeepSeek. Ini **pengukuran,
  bukan gate**: skor lebih rendah tidak memblokir merge, tapi harus tertulis.
- Perbarui `.env.example` (contoh Ollama dan OpenAI, dalam komentar), `docs/setup.md`,
  `docs/deployment.md`, tabel "Supported today" di `README.md`, baris di halaman Setup
  (`application/setup.py`), dan teks bantuan `doctor` di `cli.py`.

## Fase dan PR

Tiga PR berurutan dari `main`, **tidak bertumpuk**.

**PR 1: chat (L1).** Config, `provider_extra_body`, tiga tempat pemanggil, cek tool calling di
`doctor`, `.env.example`.
→ verifikasi:
- Test config: `deepseek` tanpa variabel baru tetap lolos (konfigurasi lama tidak berubah);
  `openai-compatible` tanpa `LLM_BASE_URL` atau `LLM_MODEL` ditolak dengan pesan jelas; judge
  mengikuti generator bila `JUDGE_PROVIDER` kosong.
- Test `provider_extra_body`: flag terkirim untuk `deepseek` + off, tidak terkirim untuk penyedia
  lain, di ketiga pemanggil.
- Seluruh test API lulus.
- Manual: `doctor` lolos untuk DeepSeek, dan untuk satu endpoint lain (P2). Satu pertanyaan chat
  di Docker dengan endpoint itu, langkah SSE tampil.

**PR 2: embedding (L2).** `EMBED_PROVIDER`, `openai_embedding.py`, pemilihan adapter.
→ verifikasi:
- Unit test adapter dengan server palsu (`httpx.MockTransport`): batching, pemotongan teks,
  `record_usage`, error menjadi `EnigramError`, dimensi salah ditolak.
- `VOYAGE_API_KEY` tidak wajib bila `EMBED_PROVIDER=openai-compatible`.
- Manual: index satu repo kecil dan satu folder docs dengan embedding non-Voyage, lalu pencarian
  semantik (`search_code`, query docs) mengembalikan hasil.

**PR 3: ukur dan docs (L3).** Runner B4 dengan model dari P2 dan P4, `docs/evaluation.md`,
README, docs setup.
→ verifikasi: angka tertulis di docs, link README benar, CI hijau.

## Hubungan dengan rencana chat-memory

- PR 1 rencana ini membuat `provider_extra_body`, yang dipakai adapter Pydantic AI di
  [chat-memory](chat-memory.md). Urutan yang paling sederhana: **PR 1 rencana ini dulu**, lalu
  PR 1 chat-memory. Kalau chat-memory jalan lebih dulu, adapter-nya memakai `DEEPSEEK_NO_THINKING`
  sementara, lalu diganti di PR 1 rencana ini (P6).
- Gate akurasi chat-memory tetap memakai DeepSeek, supaya hasilnya bisa dibandingkan dengan B4.
- D6 di chat-memory (memindah `ChatModel.complete` ke Pydantic AI) adalah jalan menuju SDK native
  seperti Anthropic dan Gemini. Rencana ini cukup dengan endpoint yang kompatibel dengan OpenAI.

## Biaya

- PR 1 dan PR 2: tidak ada biaya LLM tambahan, selain uji manual.
- PR 3: satu putaran runner B4 untuk satu sistem × 61 pertanyaan dengan model P2, ditambah
  indexing ulang dokumen dan repo eval dengan embedding P4. Bila P2 adalah model lokal, biaya API
  nol, tapi waktunya lebih lama.

## Risiko

- **Model lain lebih lemah di tool calling atau JSON mode.** Model lokal kecil sering gagal di
  `tool_choice="required"` atau menulis JSON yang rusak. Cek di `doctor` menangkap yang paling
  parah, dan PR 3 mengukur sisanya. Docs harus menyebut syarat model dengan jelas.
- **Prompt di-tuning untuk DeepSeek.** Skor di model lain bisa lebih rendah tanpa ada bug. Ini
  alasan PR 3 mengukur dan mencatat, bukan menjadikan skor DeepSeek sebagai gate.
- **Endpoint "kompatibel OpenAI" tidak semuanya sama.** Contohnya: `/models` tidak ada,
  `response_format` diabaikan, atau `usage` tidak dikirim. Bila `usage` kosong, token dicatat 0 dan
  budget harian tidak membatasi. `doctor` memberi peringatan untuk kasus ini.
- **LightRAG.** Indexing dokumen memanggil `openai_complete_if_cache` dengan `base_url`. Perilakunya
  dengan server lokal (timeout, concurrency `LLM_MAX_ASYNC`) perlu dicek di uji manual PR 1.

## Keputusan owner (2026-11-10)

- **P1. Nama penyedia:** `openai-compatible`, dengan `LLM_BASE_URL` wajib.
- **P2. Model chat kedua:** OpenCode, sebagai endpoint `openai-compatible`. Base URL dan model
  diisi owner saat uji manual L1 dan pengukuran L3.
- **P3. Ollama:** tidak ditambahkan ke `docker-compose.yml`. Cukup contoh di docs.
- **P4. Embedding kedua:** tetap Voyage untuk sekarang. L2 dibangun dan diverifikasi dengan unit
  test saja. Uji manual L2 dengan model embedding sungguhan dan pengukuran embedding di L3
  ditunda sampai ada sumber embedding non-Voyage.
- **P5. Embedding kode:** tetap dua variabel terpisah (`EMBED_MODEL` dan `CODE_EMBED_MODEL`).
- **P6. Urutan:** PR 1 rencana ini dulu, baru PR 1 chat-memory.
- **L3:** tanya owner dulu, dengan estimasi biaya, sebelum runner B4 dijalankan.
