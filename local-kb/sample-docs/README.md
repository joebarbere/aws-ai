# Sample documents

A small fictional corpus (the Tidewater Telescope Co-op) shared by Phase 0, which syncs it into a
real Bedrock knowledge base, and the local implementation, which syncs the same files. Using one
corpus for both is what makes the two runs comparable.

What each file exercises:

| File | Exercises |
|---|---|
| `kb/tidewater-handbook.pdf` | Three pages, one topic per page, so `x-amz-bedrock-kb-document-page-number` has values to report. Its sidecar uses the **simplified** format (plain strings). |
| `kb/dew-heater-guide.md` | A **typed** sidecar with every value type (`STRING`, `NUMBER`, `BOOLEAN`, `STRING_LIST`) and one attribute with `includeForEmbedding: true`. It also contains the exact codes `ZX-4471` and `DH-2` for comparing hybrid with semantic search. |
| `kb/night-log-template.txt` | Plain text with no sidecar. Phase 0's second sync **edits** it. |
| `kb/winter-closure-2025.txt` | Phase 0's second sync **deletes** it. |
| `kb/meteor-night.html` | HTML parsing. |
| `kb/equipment-inventory.txt` | About 2,000 words of numbered, distinct lines (`INV-001` to `INV-080`). It is long enough that default chunking must split it, so chunk boundaries and overlap can be compared line by line. |

`src/make_pdf.py` regenerates the PDF (it needs only `ps2pdf`).
