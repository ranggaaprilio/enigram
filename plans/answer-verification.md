# Verifikasi jawaban per kalimat

Rencana dibuat 2026-11-09, direvisi 2026-11-18 (konteks kode diperbarui, celah desain ditutup).
V1, V2 dan V3 sudah dibangun (2026-11-18); V2+V3 mati secara default (`VERIFY_ANSWERS`). V4
belum, menunggu pengukuran.

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
4. **Kalimat pendek tidak ditandai.** Kalimat tanpa kutipan di bawah `MIN_CLAIM_WORDS` (4 kata)
   tidak diberi tag, termasuk tidak diberi `[unsupported]`. Klaim pendek yang salah
   ("Token tidak kedaluwarsa.") lolos tanpa tanda.

| Celah | Ditutup oleh |
|---|---|
| 1 | V2+V3 |
| 2 | V1 |
| 3 | V1 |
| 4 | V1 (aturan kalimat pendek) |

## Tujuan

Setiap kalimat klaim dalam jawaban dicek terhadap teks sumber yang dikutipnya. Jawaban membawa
skor confidence yang bisa dijelaskan, yaitu dengan menunjuk kalimat mana yang menurunkannya.

Halusinasi tidak bisa dijamin nol. Model penulis jawaban maupun Jev sama-sama bisa salah. Yang
dijamin rencana ini:

- Setiap kutipan menunjuk ke sesuatu yang benar-benar ada.
- Setiap kalimat punya verdict yang tercatat di trace.
- Tingkat kesalahan verifier diukur di split `dev` sebelum hasilnya dipakai untuk mengubah
  jawaban atau ditampilkan ke pengguna.

**Arti verdict.** `verified` berarti "sumber yang dikutip mengatakan hal ini". Itu bukan berarti
"hal ini benar". Untuk `[summary]`, sumbernya adalah ringkasan flow buatan LLM, dan untuk
`[human]` sumbernya note tim. Keduanya bisa salah terhadap kode. Tooltip di web harus
menyebut jenis sumbernya.

**Bukan tujuan:** meminta Jev memberi satu skor untuk seluruh jawaban. Jev adalah pengambil
keputusan yes/no dan pilihan ganda (`DecisionProvider`), bukan penilai teks bebas. Skor jawaban
dihitung dari verdict per kalimat.

## Bahan yang sudah ada

Dicek terhadap kode pada 2026-11-18 (path relatif ke `apps/api/src/enigram/`).

| Bahan | Lokasi | Dipakai untuk |
|---|---|---|
| `tag_origins`, dipanggil dari `attach_notes` | `domain/origin.py:71`, `application/annotations.py:529` | satu titik masuk untuk kedua jalur jawaban |
| `claim_relation` (`supports` / `contradicts` / `says_nothing`, threshold 0,8) | `application/decisions.yaml:4` | pola pertanyaan; instruksinya menunjuk key `context` bersama, jadi tidak bisa dipakai langsung (lihat V3) |
| `judge_claims_decision`: satu request untuk banyak klaim | `application/evaluation/judges.py:54` | contoh pemanggilan |
| `question()`, `clip()`, `STATE_MAX_CHARS` (40.000) | `application/decisions.py:50`, `:56`, `:20` | mengisi placeholder, memotong state |
| `decide()` beserta trace dan fallback | `application/decisions.py:91` | memanggil Jev, mencatat `extra.decisions` |
| Aturan "hanya Jev yang boleh mengubah jawaban" | `application/abstain.py:20` (`REPLACING_PROVIDERS`) | pola yang sama untuk verifikasi |
| `RepoStore.read_file(path, start, end)` pada sha | `application/repo_store.py:157` | teks sumber untuk kutipan kode |
| `ContextChunk(ref, text)` | `domain/answer.py:36` | teks sumber `[n]` di jalur dokumen |
| `doc_refs`, `doc_citations` | `application/code_agent.py:272-273`, diisi di `:581` | nomor `[n]` di jalur agent; hanya menyimpan sitasi, belum menyimpan teks chunk |
| Cek kutipan struktural setelah run eval (sha, file, baris) pada clone | `application/evaluation/citations.py` | pembanding V1: setelah V1, `citations_valid` jalur agent harus 100% |
| Label manusia grounded / tidak per kalimat | `application/evaluation/claims.py` | pembanding verdict di pengukuran |
| Pemecahan kalimat | `domain/claims.py`, `domain/origin.py` | unit yang diverifikasi |

Dua jalur jawaban saat ini:

- **Dokumen:** `querying.answer_question` (`application/querying.py:49`) memanggil
  `attach_notes` lalu `guard_abstain`.
- **Agent:** `code_agent.py` menjalankan `run_agent_loop` dari `application/pydantic_loop.py`
  (Pydantic AI, PR #75), lalu `loop_answer`, `attach_notes`, dan abstain guard. Dokumen dan
  notes sudah menjadi tool agent (PR #64, merged 2026-11-09).

## Desain

Alur setelah jawaban selesai ditulis, sama di kedua jalur:

```
jawaban LLM
  → V1 cek kutipan tanpa LLM (kutipan tidak valid dibuang, tag tulisan LLM dihapus)
  → tag_origins (dengan aturan kalimat pendek yang baru)
  → abstain guard (seperti sekarang)
  → V2 cari teks sumber tiap kalimat
  → V3 verdict per kalimat (satu request ke Jev)
  → V4 badge, confidence
```

### V1: cek kutipan tanpa LLM (dibangun 2026-11-18)

`check_citations` (`domain/citations.py`) membuang kutipan yang tidak valid dari teks jawaban.
Fungsi ini jalan **sebelum** sources dibuat, bukan di `attach_notes`: di jalur agent,
`loop_answer` membangun sources dari kutipan di teks, jadi kutipan palsu harus sudah hilang
sebelum itu. Yang dipotong hanya kutipannya, beserta backtick atau kurung yang hanya
membungkus kutipan itu. Blok kode dan bagian References tidak disentuh, baik oleh
`check_citations` maupun oleh `tag_origins`.

- `[n]` harus menunjuk source yang ada. Jalur agent: nomor di `doc_citations`. Jalur dokumen:
  `result.sources`. `[1, 9]` dengan 9 tidak dikenal menjadi `[1]`.
- `path:start-end@sha` dicek di jalur agent (`CodeAgentTools.citation_problem`): sha-nya awalan
  sha repo yang diindeks (`wrong sha`), file-nya ada pada sha itu (`missing file`), dan
  barisnya di dalam file (`line beyond file`). Jumlah baris diambil dengan
  `GitTools.line_count`. Kutipan hanya dibuang kalau ada bukti. Kutipan dibiarkan kalau store
  tidak ada, git gagal, file melebihi batas baca `read_file`, atau jawaban sudah mengutip lebih
  dari 30 file (`CITATION_MAX_FILES`).
- Jalur dokumen tidak punya akses repo, jadi kutipan kode inline di sana tidak dicek dan
  dicatat di `extra.citations.unchecked`. Celah 2 masih terbuka untuk jalur ini.
- `note#N` harus termasuk note yang memang diberikan ke model (`notes_seen` atau `found`).
- Tag asal yang sudah ada di posisi tag (akhir kalimat, atau awal kalimat berikutnya) dihapus
  lalu dibuat ulang oleh `tag_origins`, jadi tag selalu buatan kode. Teks seperti `[document]`
  di tengah kalimat tidak disentuh.
- Kalimat yang kehilangan semua kutipannya menjadi `[unsupported]`.
- **Aturan kalimat pendek (celah 4).** Kalimat pendek tanpa kutipan mendapat `[unsupported]`,
  kecuali berupa label: di dalam item list, tidak berakhiran tanda akhir kalimat
  ("**Ringkasan**", "Singkatnya:"), atau hanya nomor langkah ("**1." dari "**1. Guard** — …").
- Trace: `extra.citations = {"invalid": [{"citation": "...", "reason": "..."}]}`, ditambah
  `"unchecked": [...]` di jalur dokumen.
- `AGENT_VERSION` naik ke 3.6.

Tidak ada panggilan model di tahap ini. Biayanya satu `git ls-tree` dan satu `git cat-file` per
file yang dikutip.

Hasil pengecekan:

- **Efek aturan kalimat pendek.** Dihitung offline pada 202 jawaban agent tersimpan di
  `data/eval/` (23 run atas pertanyaan yang sama, jadi banyak yang mirip): 1 dari 1.248 kalimat
  bertag berubah, yaitu "**15 minutes.**" yang memang tanpa kutipan. Versi pertama aturan ini
  salah menandai nomor langkah tebal (27 kalimat); itu sebabnya ada pengecualian nomor langkah.
- **Belum diukur: percakapan lanjutan.** `doc_refs` dibuat per run agent, jadi nomor `[n]`
  tidak berlanjut antar giliran. Kalau model mengulang `[n]` dari jawaban sebelumnya (riwayat
  chat, PR #76), V1 membuangnya dan mencatatnya di `extra.citations.invalid`. Kalau angkanya
  besar di pemakaian nyata, nomor dari giliran sebelumnya harus dipetakan ulang.
- **Belum dijalankan: golden eval `dev`** dengan V1 menyala. Tag dibuang oleh normalisasi blind,
  jadi skor seharusnya tidak berubah, tapi kutipan yang dibuang mengubah teks jawaban.

### V2: teks sumber per kalimat (dibangun 2026-11-18)

Yang dibangun: `application/verification.py` (`verify_answer`), `CodeAgentTools.source_text`
dan `doc_texts` di jalur agent, `_source_text` di `querying.py`. Kalimat dan kutipannya diambil
dari jawaban yang sudah bertag (`tagged_sentences` di `domain/origin.py`). Hanya jalan kalau
`VERIFY_ANSWERS=on`; default mati karena mengirim teks sumber ke decision provider (keputusan
pemilik nomor 4 masih terbuka).

Untuk setiap kalimat yang punya kutipan valid, kumpulkan teks dari kutipannya saja, tidak dari
seluruh evidence. Kalimat dengan beberapa kutipan mendapat gabungan teks semua kutipannya.

| Kutipan | Teks |
|---|---|
| `[n]` dokumen, jalur dokumen | gabungan teks semua `ContextChunk` dengan `ref == n` |
| `[n]` dokumen, jalur agent | `doc_texts[n]`: teks chunk yang dilihat model, sudah dipotong `DOC_CHUNK_CHARS` |
| `path:a-b@sha`, jalur agent | `read_file` pada sha itu, dengan rentang dilebarkan (lihat di bawah) |
| `path:a-b@sha`, jalur dokumen | tidak ada; kalimat yang hanya punya kutipan ini mendapat verdict `unchecked` |
| `note#N` | teks note |

**Rentang kode dilebarkan.** Klaim tentang perilaku ("fungsi X memvalidasi token lalu memanggil
Y") sering butuh konteks di luar baris yang dikutip. Kalau hanya baris yang dikutip yang
dikirim, Jev akan sering menjawab `says_nothing` untuk kalimat yang benar. Jadi teks yang
dikirim adalah `a-20` sampai `b+20`, dibatasi awal dan akhir file. Angka 20 adalah titik awal
dan disetel dari pengukuran (bandingkan 0, 20 dan 50 pada laju `unverified` palsu untuk
`[code]`). Konstanta: `CODE_CONTEXT_LINES` di `code_agent.py`. Rentang yang dengan tambahan
konteks melebihi 200 baris (`READ_MAX_LINES`) dibaca tanpa konteks, dan tetap terpotong di 200
baris. File yang tidak bisa dibaca (terlalu besar, rahasia, symlink) tidak memberi teks.

Teks dipotong per kalimat (misalnya 4.000 karakter) dengan `clip()`, dan total per request
dibatasi `STATE_MAX_CHARS`. Kalau terpotong, dicatat `truncated: true`. Kalau total melebihi
batas, kalimat dibagi ke beberapa request. Tidak ada kalimat yang dilewati diam-diam.

### V3: verdict per kalimat (dibangun 2026-11-18, hanya dicatat)

- Key baru di `decisions.yaml`, `sentence_relation`, dengan instruksi
  ``"How does `{context}` relate to `{claim}`?"`` dan criteria yang sama dengan
  `claim_relation`. `claim_relation` tidak diubah karena instruksinya menunjuk key `context`
  bersama dan eval memakainya.
- Satu panggilan `decide()` per batch dengan purpose `answer_verification`; batch dijalankan
  paralel. State-nya
  `{"claims": {i: kalimat}, "contexts": {i: teks}}`, dan pertanyaan per kalimat dibuat dengan
  `question("sentence_relation", claim=f"claims.{i}", context=f"contexts.{i}")`.
- Kalimat tanpa kutipan valid tidak dikirim. Verdict-nya langsung `unsupported`. Kalimat yang
  kutipannya tidak punya teks (misalnya kutipan kode di jalur dokumen) mendapat `unchecked`.
- Verdict hanya dipakai kalau provider-nya Jev, sama seperti abstain guard. Kalau yang menjawab
  judge fallback (yang hanya memberi 0 atau 1), verdict tetap dicatat dengan
  `reason: uncalibrated provider` tapi tidak dipakai untuk badge maupun confidence.
- Jawaban yang sudah abstain (`found: false`) tidak diverifikasi.
- Kalau panggilan decision gagal, jawaban tetap dikirim, kalimat di batch itu tanpa verdict
  (`null`), dan trace mencatat `verification.error`. Jawaban seperti itu tidak disimpan di cache
  agent.
- Verdict sudah dihitung dengan tabel di V4 dan threshold 0,8, karena itulah yang diukur.
  `contradicted` belum memakai threshold: pilihan `contradicts` dengan keyakinan rendah tetap
  dihitung `contradicted`. Pengukuran presisi `contradicted` harus memisahkan menurut
  `p_supports` / keyakinan, sebelum gate strict diputuskan.
  `confidence`, `coverage` dan `level` belum dihitung: rumusnya menunggu keputusan pemilik
  nomor 3.
- Trace: `extra.verification = {"provider", "model", "reason"?, "error"?, "sentences": [{"text",
  "citations", "verdict", "relation", "p_supports", "truncated"}], "counts"}`.
- Cache agent: kunci cache mendapat akhiran saat verifikasi menyala, jadi jawaban tanpa
  verifikasi tidak dipakai ulang (dan kunci lama tetap sah).

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
`p_supports`, jenis sumber, dan potongan teks sumbernya
(`apps/web/src/components/Answer.tsx`).

Badge hanya ditampilkan untuk asal yang lolos gate pengukuran. Kalau `[document]` lolos tapi
`[code]` tidak, kalimat `[code]` tetap tanpa tanda verdict.

**Mode strict** (opsional, per request: `verify: "strict"`). Kalimat `contradicted` dibuang
dari jawaban. Kalau tidak ada satu pun kalimat `verified`, jawaban diganti dengan abstain. Versi
aslinya disimpan di trace.

**Confidence jawaban** (usulan, keputusan ada di pemilik). Dua angka, karena satu rasio
mencampur dua hal yang berbeda:

- `confidence = verified / kalimat_dicek`. Kalimat dicek adalah kalimat yang punya kutipan
  valid dan dikirim ke Jev. Ini menjawab "seberapa sering kutipannya benar".
- `coverage = kalimat_dicek / kalimat_klaim`. Kalimat klaim adalah semua kalimat bertag,
  termasuk `[unsupported]`. Ini menjawab "seberapa banyak jawaban yang punya sumber".

Versi sebelumnya memakai `verified / kalimat_klaim` dan mensyaratkan semua kalimat verified
untuk `high`. Kalimat pembuka dan penghubung tanpa kutipan dihitung `unsupported`, jadi `high`
hampir tidak pernah tercapai dan `level` tidak membawa informasi.

`level` (batas sementara, disetel dari pengukuran):

- `low`: ada kalimat `contradicted`, atau `confidence` < 0,7
- `high`: `confidence` ≥ 0,9 dan `coverage` ≥ 0,8
- `medium`: kondisi lainnya

Kalau `P(answerable)` dari abstain guard ada, `level` tidak boleh lebih tinggi dari yang
diizinkan skor itu.

**Permukaan.** Field `confidence`, `coverage` dan `level` muncul di `POST /query`, Chat, MCP
dan `enigram query`.

## Pengukuran (gate sebelum V4)

**Alat ukur (dibangun 2026-11-18).** Golden run menyimpan `verification` per jawaban, dan
`enigram eval --labels <sheet>` menulis bagian "Answer verification": sebaran verdict per asal,
dan terhadap label manusia per asal presisi `contradicted`, laju `unverified` palsu dan recall
(`application/evaluation/verification_eval.py`). Tidak memanggil model.

**Belum ada label klaim manusia.** Per 2026-11-18, ke-32 label sheet di `data/eval/` belum ada
yang diisi (0 klaim bertanda). Label blind yang ada menilai jawaban utuh, bukan kalimat. Jadi
langkah 2 di bawah butuh orang yang melabel dulu, minimal 30 kalimat per asal.

**Pengukuran pertama (2026-11-18, label draf dari model, belum diperiksa pemilik).** Split
`dev`, sistem `agent-auto` dan `agent-business`, Jev `jev-1.14.0`; 235 kalimat berlabel
(code 163, document 72, human 12).

- Kata-kata awal (tiga jawaban, threshold 0,8): `unverified` palsu code 4-7%, document
  11-14%; tapi dari 7 kalimat code yang kutipannya tidak cukup hanya 1-2 yang tertangkap.
- `sentence_relation` diberi jawaban `partly` dan threshold 0,5: `unverified` palsu code
  11-12%, document 17%; 12-13 dari 14 kalimat tak didukung tertangkap.
- `contradicted` tidak pernah muncul, jadi presisinya belum terukur. Human baru 12 kalimat.
- Threshold dipilih dari kalimat yang sama dengan yang dipakai mengukur; perlu run baru
  dan label yang diperiksa manusia sebelum gate dianggap lolos.

V2+V3 di-merge dalam mode hanya mencatat. V4 baru dikerjakan kalau gate di bawah lolos.

1. Jalankan `enigram golden answer --system <x> --split dev` dengan verifikasi menyala (hanya
   dicatat, jawaban tidak diubah).
2. Bandingkan verdict per kalimat dengan label manusia grounded / tidak
   (`application/evaluation/claims.py`). Semua angka dilaporkan **per asal** (`code`,
   `document`, `summary`, `human`), karena risikonya berbeda:
   - **Presisi `contradicted`**: dari kalimat yang ditandai contradicted, berapa yang berlabel
     tidak grounded.
   - **Laju `unverified` palsu**: dari kalimat berlabel grounded, berapa yang tidak mendapat
     `verified`.
   - **Recall**: dari kalimat berlabel tidak grounded, berapa yang tertangkap sebagai
     `contradicted` atau `unverified`.
   - Sebaran `level` di seluruh jawaban `dev`.
   - Tambahan waktu (target sekitar 0,3–1 detik) dan token Jev.
3. Label manusia menilai kalimat terhadap seluruh evidence, sedangkan verifier menilai terhadap
   sumber yang dikutip. Kalimat yang benar tapi mengutip chunk yang salah akan berlabel
   grounded dan mendapat `unverified`. Itu bukan kesalahan verifier. Kasus `unverified` palsu
   dibaca manual dan dipisahkan menjadi "verifier salah" dan "kutipan salah sasaran".
4. Jumlah kalimat berlabel per asal dicatat. Kalau sebuah asal punya kurang dari 30 kalimat
   berlabel, angkanya tidak dipakai untuk memutuskan dan label ditambah dulu.
5. Threshold 0,8 berasal dari resep eval, bukan dari jawaban live. Setel ulang dari hasil
   pengukuran ini dan tulis angkanya di `decisions.yaml`, seperti yang dilakukan untuk
   `answerable`.
6. Cek regresi: skor `dev` tidak boleh turun. Jalur tanpa Jev (`llm_judge`) harus tetap
   menghasilkan jawaban yang sama dengan sekarang.

**Gate** (usulan, angkanya keputusan pemilik):

| Untuk | Syarat |
|---|---|
| V4a badge, per asal | laju `unverified` palsu karena verifier ≤ 20% dan presisi `contradicted` ≥ 80% |
| V4a `level` | tidak ada satu level yang berisi lebih dari 80% jawaban `dev` |
| V4b mode strict | presisi `contradicted` ≥ 90% |

Kalau `[code]` gagal gate setelah rentang kode disetel, verifikasi `[code]` berhenti di trace
dan tidak ditampilkan. Kalau semua asal gagal, V4 dibatalkan dan V2+V3 dimatikan lewat config
supaya tidak ada biaya Jev tanpa hasil.

Catatan: V2 mengubah tool output yang disimpan. Naikkan `AGENT_VERSION` (`code_agent.py:67`,
sekarang `3.6` sejak V1). Kalau tidak, cache eval akan menyembunyikan perubahannya.

## Urutan PR

| PR | Isi | Model | Bisa di-merge sendiri |
|---|---|---|---|
| V1 (dibangun) | Cek kutipan, hapus tag tulisan LLM, aturan kalimat pendek, `extra.citations` | tidak | ya |
| V2+V3 (dibangun) | Teks sumber dan verdict per kalimat, hanya dicatat di trace, `VERIFY_ANSWERS` (default off) | Jev | ya (tidak mengubah jawaban) |
| V4a | Badge verdict di web, `confidence`/`coverage`/`level` di API, Chat dan MCP | — | setelah gate lolos |
| V4b | Mode strict | — | setelah gate strict lolos |

Langkah berikutnya bukan kode: jalankan pengukuran di atas dengan `VERIFY_ANSWERS=on` dan
`DECISION_PROVIDER=jev`, lalu putuskan gate. V4 baru dikerjakan setelah itu.

## Keputusan untuk pemilik

1. Kalimat `contradicted` di mode normal: hanya ditandai merah (usulan), atau langsung dibuang?
2. Mode strict jadi default, atau tetap opsional per request (usulan)?
3. Rumus `confidence`, `coverage` dan batas `level` seperti di atas, atau rumus lain?
4. Data yang dikirim ke TypeSafe bertambah, yaitu potongan kode dan chunk per kalimat. Apakah
   ini boleh untuk semua workspace?
5. Verifikasi berjalan di semua permukaan (web, Chat, MCP, CLI), atau di web dan Chat saja
   dulu?
6. ~~Aturan kalimat pendek.~~ Diputuskan 2026-11-18: kalimat pendek tanpa kutipan di dalam prosa
   menjadi `[unsupported]`. Kutipan kode inline di jalur dokumen dibiarkan dan dicatat sebagai
   `unchecked`.
7. Angka gate di bagian Pengukuran (20%, 80%, 90%, minimal 30 kalimat per asal): setuju, atau
   angka lain?
