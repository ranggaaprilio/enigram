---
title: Correcting the knowledge base
sidebar_label: Correcting answers
description: Fix a wrong answer by editing the entity graph, or by replacing the document it came from.
---

# Correcting the knowledge base

When an answer about the **code** is wrong, write a [team note](notes.md): it outranks the code
summaries in every answer. When an answer from the **documents** is wrong, fix the graph or the
document:

| The wrong fact is stated in | Fix |
|---|---|
| Graph entities or relations | [Edit the graph](#correcting-the-graph) |
| Only the document text | [Replace the document](#replacing-a-document) |

## Correcting the graph

1. On the **Graph** page (`/graph`), search for the wrong fact (e.g. `60%`).
2. Fix **every** entity and relation that still states it. An answer follows the edit only when
   the graph no longer contradicts itself.

| Change | API |
|---|---|
| Edit an entity's description or type | `POST /graph/entity/edit` |
| Edit a relation | `POST /graph/relation/edit` |
| Merge duplicate entities | `POST /graph/entity/merge` |
| Delete an entity and its relations | `DELETE /graph/entity/{name}` |
| Delete one relation | `DELETE /graph/relation?source=&target=` |

Each change is a job for the worker (the only process that writes the index), so it waits behind
running uploads. Its result keeps the old values for a manual undo. After each change the worker
deletes the cached answers (only `cache_type='query'` rows, never the extraction cache).

Every query tells the answer model that graph descriptions win over the original text chunks, so
corrections are not outvoted by the chunk they correct.

:::note

This rule changed answers for all questions: compare `enigram eval` runs made before it with that
in mind. A fact that only the chunk text states cannot be fixed in the graph.

:::

## Replacing a document

A fact that only the text states needs a corrected document. Uploading a changed file under the
same name is refused (409), so:

1. Delete it (Documents page, or `DELETE /documents/{book}`).
2. Wait until it disappears from the list (status `deleting` meanwhile).
3. Upload the new version.

Deletion runs in the worker. LightRAG removes the document's chunks and the entities and
relations only it produced. Entities and relations it shares with other documents are rebuilt
from those documents.

:::caution

Rebuilding shared entities **drops manual graph edits on them**.

:::

The extraction cache is kept, so uploading the same file again is quick and costs no LLM calls. A
deletion that fails or is interrupted leaves the document as `deleting`; deleting it again
resumes.
