# Verifikasi jawaban per kalimat

Rencana dibuat 2026-11-09. Belum diimplementasikan.

## Masalah

Tag asal (`[code]`, `[document]`, `[summary]`, `[human]`, `[unsupported]`) dibuat oleh kode,
bukan LLM (`domain/origin.py`, `tag_origins`). Tag hanya menunjukkan sumber yang dikutip sebuah
kalimat. Tag tidak membuktikan bahwa sumber itu benar-benar mengatakan isi kalimat tersebut.
Celahnya:

1. **Kutipan salah sasaran.** LLM bisa menulis kalimat dengan `[3]`, padahal chunk 3 tidak
   mengatakannya. Kalimat itu tetap mendapat `[document]`.
2. **Kutipan kode hanya dicek bentuknya.** Teks apa pun yang cocok dengan `path:line@sha`
   (`domain/code.py`, `CITATION`) mendapat `[code]`, walaupun file atau barisnya tidak ada.
3. **Tag tulisan LLM dianggap sah.** Kalimat yang sudah berakhiran tag dilewati karena
   `_tag` bersifat idempotent, jadi `[code]` yang ditulis LLM sendiri akan lolos.
4. **Kalimat pendek tidak ditandai.** Kalimat di bawah `MIN_CLAIM_WORDS` tidak diberi tag,
   termasuk tidak diberi `[unsupported]`.

## Tujuan

Setiap kalimat klaim dalam jawaban dicek terhadap teks sumber yang dikutipnya. Jawaban membawa
skor confidence yang bisa dijelaskan, yaitu dengan menunjuk kalimat mana yang menurunkannya.

Halusinasi tidak bisa dijamin nol. Model penulis jawaban maupun Jev sama-sama bisa salah. Yang
dijamin rencana ini:

- Setiap kutipan menunjuk ke sesuatu yang benar-benar ada.
- Setiap kalimat punya verdict yang tercatat di trace.
- Tingkat kesalahan verifier diukur di split `dev` sebelum hasilnya dipakai untuk mengubah
  jawaban.

**Bukan tujuan:** meminta Jev memberi satu skor untuk seluruh jawaban. Jev adalah pengambil
keputusan yes/no dan pilihan ganda (`DecisionProvider`), bukan penilai teks bebas. Skor jawaban
dihitung dari verdict per kalimat.

## Bahan yang sudah ada

| Bahan | Lokasi | Dipakai untuk |
|---|---|---|
| `claim_relation` (`supports` / `contradicts` / `says_nothing`, threshold 0,8) | `application/decisions.yaml:4` | verdict per kalimat |
| `judge_claims_decision`: satu request untuk banyak klaim | `application/evaluation/judges.py:59` | contoh pemanggilan; saat ini konteksnya dipakai bersama |
| `decide()` beserta trace dan fallback | `application/decisions.py:91` | memanggil Jev, mencatat `extra.decisions` |
| Aturan "hanya Jev yang boleh mengubah jawaban" | `application/abstain.py` (`REPLACING_PROVIDERS`) | pola yang sama untuk verifikasi |
| `RepoStore.read_file(path, start, end)` pada sha | `application/repo_store.py:157` | teks sumber untuk kutipan kode |
| `ContextChunk(ref, text)` dan `tools.doc_citations` | `domain/answer.py`, `application/code_agent.py:273` | teks sumber untuk `[n]` |
| Pemecahan kalimat | `domain/claims.py`, `domain/origin.py` | unit yang diverifikasi |

## Desain

Alur setelah jawaban selesai ditulis, di kedua jalur (`querying.answer_question` dan agent
`code_agent`):

```
jawaban LLM
  → V1 cek kutipan tanpa LLM (kutipan tidak valid dibuang, tag tulisan LLM dihapus)
  → tag_origins (seperti sekarang)
  → abstain guard (seperti sekarang)
  → V2 cari teks sumber tiap kalimat
  → V3 claim_relation per kalimat (satu request ke Jev)
  → V4 verdict, badge, confidence
```

### V1: cek kutipan tanpa LLM

- `[n]` harus ada di `result.sources`. Kalau tidak ada, kutipan itu dihapus dan dicatat.
- `path:start-end@sha` harus memenuhi tiga syarat: sha-nya milik repo yang diindeks
  (`extra.repos`), file-nya ada pada sha itu, dan `end` tidak melebihi jumlah baris file.
  Pengecekannya lewat `RepoStore.read_file`.
- `note#N` harus termasuk note yang memang diberikan ke model (`notes_seen` atau `found`). Ini
  sudah berlaku sekarang.
- Tag asal yang ditulis LLM dihapus sebelum `tag_origins` dijalankan, jadi tag selalu buatan
  kode.
- Kalimat yang kehilangan semua kutipannya menjadi `[unsupported]`.
- Trace: `extra.citations = {"invalid": [{"sentence": i, "citation": "...", "reason": "..."}]}`.

Tidak ada panggilan model di tahap ini, jadi tambahan biayanya nol. Tahap ini bisa di-merge
sendiri.

### V2: teks sumber per kalimat

Untuk setiap kalimat yang punya kutipan valid, kumpulkan teks dari kutipannya saja, tidak dari
seluruh evidence:

| Kutipan | Teks |
|---|---|
| `[n]` dokumen | teks `ContextChunk` dengan `ref == n`; di jalur agent, chunk dari `doc_citations[n]` |
| `[n]` flow summary | teks ringkasan flow pada source itu |
| `path:a-b@sha` | `read_file(path, a, b)` pada sha itu |
| `note#N` | teks note |

Teks dipotong per kalimat (misalnya 4.000 karakter) dan total per request dibatasi
`STATE_MAX_CHARS`. Kalau terpotong, dicatat `truncated: true`.

### V3: verdict per kalimat

- Satu panggilan `decide()` dengan purpose `answer_verification`. State-nya
  `{"claims": {i: kalimat}, "contexts": {i: teks}}` dan ada satu pertanyaan `claim_relation`
  per kalimat, dengan konteks milik kalimat itu sendiri.
  - Perlu dicek di awal V3: apakah `question("claim_relation", claim=..., context=...)` bisa
    menunjuk `contexts.i`. Kalau `claim_relation` hanya mendukung konteks bersama, tambahkan
    key baru di `decisions.yaml`. Jangan mengubah yang lama, karena eval memakainya.
- Kalimat tanpa kutipan valid tidak dikirim. Verdict-nya langsung `unsupported`.
- Verdict hanya mengubah jawaban kalau provider-nya Jev, sama seperti abstain guard. Kalau yang
  menjawab judge fallback (yang hanya memberi 0 atau 1), verdict tetap dicatat dengan
  `reason: uncalibrated provider` tapi tidak dipakai untuk badge maupun confidence.
- Jawaban yang sudah abstain (`found: false`) tidak diverifikasi.
- Trace: `extra.verification = {"provider", "model", "sentences": [{"i", "text", "origins",
  "citations", "verdict", "p_supports", "truncated"}], "confidence", "level"}`.

### V4: badge, mode strict, confidence

**Verdict per kalimat:**

| Kondisi | Verdict |
|---|---|
| `supports` dan `p_supports` ≥ threshold | `verified` |
| `contradicts` | `contradicted` |
| `says_nothing`, atau `supports` di bawah threshold | `unverified` |
| tidak ada kutipan valid | `unsupported` |

**Web.** Badge asal tetap ditampilkan. Di sebelahnya ada tanda verdict: centang untuk
`verified`, merah untuk `contradicted`, abu-abu untuk `unverified`. Tooltip menampilkan
`p_supports` dan potongan teks sumbernya (`components/Answer.tsx`).

**Mode strict** (opsional, per request: `verify: "strict"`). Kalimat `contradicted` dibuang
dari jawaban. Kalau tidak ada satu pun kalimat `verified`, jawaban diganti dengan abstain. Versi
aslinya disimpan di trace.

**Confidence jawaban** (usulan, keputusan ada di pemilik):

- `confidence = verified / kalimat_klaim`
- `level`:
  - `high`: semua kalimat verified dan tidak ada yang contradicted
  - `medium`: confidence ≥ 0,7 dan tidak ada yang contradicted
  - `low`: kondisi lainnya
- Kalau `P(answerable)` dari abstain guard ada, `level` tidak boleh lebih tinggi dari yang
  diizinkan skor itu.

**Permukaan.** Field `confidence` dan `level` muncul di `POST /query`, Chat, MCP dan
`enigram query`.

## Pengukuran (gate sebelum hasil verifikasi mengubah jawaban)

1. Jalankan `enigram golden answer --system <x> --split dev` dengan verifikasi menyala (hanya
   dicatat, jawaban tidak diubah).
2. Bandingkan verdict per kalimat dengan label blind yang sudah ada dan dengan `claim_relation`
   versi eval:
   - **Presisi `contradicted`**: dari kalimat yang ditandai contradicted, berapa yang memang
     salah. Ini menentukan apakah mode strict aman.
   - **Laju `unverified` palsu**: kalimat yang benar tapi tidak lolos verifikasi, misalnya
     kalimat yang menggabungkan beberapa sumber.
   - Tambahan waktu (target sekitar 0,3–1 detik) dan token Jev.
3. Threshold 0,8 berasal dari resep eval, bukan dari jawaban live. Setel ulang dari hasil
   pengukuran ini dan tulis angkanya di `decisions.yaml`, seperti yang dilakukan untuk
   `answerable`.
4. Cek regresi: skor `dev` tidak boleh turun. Jalur tanpa Jev (`llm_judge`) harus tetap
   menghasilkan jawaban yang sama dengan sekarang.

Catatan: kalau prompt atau alur agent berubah, naikkan `AGENT_VERSION`. Kalau tidak, cache eval
akan menyembunyikan perubahannya.

## Urutan PR

| PR | Isi | Model | Bisa di-merge sendiri |
|---|---|---|---|
| V1 | Cek kutipan, hapus tag tulisan LLM, `extra.citations` | tidak | ya |
| V2+V3 | Teks sumber dan verdict per kalimat, hanya dicatat di trace | Jev | ya (tidak mengubah jawaban) |
| V4a | Badge verdict di web, `confidence`/`level` di API, Chat dan MCP | — | setelah pengukuran |
| V4b | Mode strict | — | setelah presisi `contradicted` diukur |

Dependensi: PR #64 (unified agent loop, docs dan notes sebagai tool agent) masih terbuka. V2
harus mengikuti tempat chunk dokumen berada setelah #64. Jadi V1 bisa dikerjakan sekarang, dan
V2+V3 sebaiknya dikerjakan setelah #64 di-merge.

## Keputusan untuk pemilik

1. Kalimat `contradicted` di mode normal: hanya ditandai merah (usulan), atau langsung dibuang?
2. Mode strict jadi default, atau tetap opsional per request?
3. Rumus confidence dan batas `level` seperti di atas, atau rumus lain?
4. Data yang dikirim ke TypeSafe bertambah, yaitu potongan kode dan chunk per kalimat. Apakah
   ini boleh untuk semua workspace?
5. Verifikasi berjalan di semua permukaan (web, Chat, MCP, CLI), atau di web dan Chat saja
   dulu?
