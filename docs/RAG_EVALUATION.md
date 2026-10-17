# Document Assistant — RAG Evaluation Plan

This document defines how to measure retrieval and answer quality as chunking, embeddings, retrieval, and prompts change. It is a plan and scorecard template. The project does not currently ship an evaluation dataset or claim measured benchmark results.

## Evaluation goals

Measure four distinct outcomes:

1. **Retrieval relevance:** Did the selected passages include the evidence needed to answer the question?
2. **Answer quality:** Is the answer correct, complete enough for the request, and supported by the indexed document?
3. **Citation quality:** Do the cited source IDs resolve to the right passages and PDF pages?
4. **Performance:** How long did indexing and subsequent questions take under a documented hardware/provider setup?

Keep these separate. A fluent answer and HTTP 200 are not retrieval-quality metrics.

## Dataset design

Create a small, version-controlled evaluation set from non-sensitive PDFs that may be distributed with the project or privately stored outside Git. Include questions with known answer passages and unanswerable questions.

Suggested JSON Lines record:

~~~json
{"case_id":"sample-001","document":"evaluation/sample.pdf","query":"What was the stated target?","expected_answer_points":["The target value stated in the document"],"relevant_pages":[4],"relevant_chunk_ids":[12],"answerable":true,"notes":"Direct fact in the findings section"}
~~~

Use stable case IDs. Recompute relevant chunk IDs when chunking changes; page ranges can remain a more stable human reference. Do not commit private documents, API keys, personal data, or provider responses that contain sensitive content.

Cover at least:

- Direct fact questions, paraphrases, and multi-part questions.
- Questions whose answer appears near the beginning, middle, and end of a long PDF.
- Exact-page questions and page references near the document boundaries.
- Document-wide summary/overview questions that need broad coverage.
- Headings, lists, tables, repeated headers/footers, unusual layouts, and scanned pages where relevant.
- Unanswerable questions to measure whether the system admits missing evidence.
- Greetings/small talk to confirm that unrelated citations are not attached.

## Metrics

### Retrieval

- **Recall@k:** fraction of cases where a labelled relevant chunk appears in the top k results.
- **MRR:** average reciprocal rank of the first relevant result.
- **nDCG@k:** ranking quality when multiple passages have graded relevance.
- **Page coverage for overview:** whether selected passages represent relevant sections across the document, not only one neighborhood.
- **Exact-page accuracy:** for page-number queries, every returned chunk should belong to the requested page set.

Record the value of k and the relevant-passage annotation policy. For overview and multi-part queries, label more than one passage when needed.

### Answer and citations

Have a reviewer score each answer for:

- **Correctness:** whether each factual claim agrees with the PDF.
- **Grounding:** whether claims are supported by retrieved passages.
- **Completeness:** whether the answer covers the question's requested parts without unnecessary material.
- **Abstention:** whether unanswerable questions are handled without invented facts.
- **Citation precision:** whether every citation refers to an evidence-bearing passage.
- **Citation recall:** whether important answer claims have a citation.
- **Page correctness:** whether the cited passage opens on the expected PDF page.

Use a documented rubric (for example, 0 = fail, 1 = partial, 2 = pass) and retain a short explanation for each non-passing score. Human review remains important; automated text similarity is not a substitute for factual support checks.

### Performance

Record cold and warm timings separately:

- Upload transfer time.
- Extraction/OCR time.
- Structuring and chunking time.
- Embedding and vector-write time.
- Retrieval/query-embedding time.
- LLM response time and total ask latency.
- Cache-hit state, PDF page/chunk counts, CPU/GPU device, OS, and provider/model.

Use medians and p95 over repeated queries for comparisons. Do not compare cold model initialization with warm runs as if they were equivalent.

## Baseline procedure

1. Freeze the dataset and record its version/hash.
2. Record code revision, embedding model name/dimension, retrieval settings, chunk size/overlap, LLM provider/model, and hardware.
3. Clear or isolate only the test storage, then index the evaluation documents once.
4. Save raw retrieval results and answers with source IDs and latency metadata.
5. Score results using the same relevance labels and rubric.
6. Compare changes against the previous run, review regressions by case ID, and keep the prior baseline.

Do not delete a user's production storage to prepare an evaluation. Use a separate temporary storage root and provider key with limited permissions.

## Results template

No values are filled in until a reproducible run is completed.

| Run | Dataset version | Code revision | Embedding / LLM | Recall@5 | MRR | Answer support pass rate | Citation page pass rate | Median ask latency |
|---|---|---|---|---:|---:|---:|---:|---:|
| Not measured | — | — | — | — | — | — | — | — |

## Current implementation areas to monitor

- Retrieval combines semantic similarity and a cached BM25-style lexical score. Track exact terms and paraphrases separately.
- Page-specific questions are routed to chunks from the requested page(s); test page number parsing and out-of-range requests.
- Whole-document overview queries choose representative chunks across the page range. Measure whether the selected passages cover the actual thesis, key sections, conclusions, and requested number of points.
- The answer model can still omit or misstate retrieved evidence. Compare answer claims to source text and open citations.
- The default embedding model is English-oriented. Add multilingual cases only after documenting the selected model and OCR language support.

For the application-level quality requirements, see [PRD.md](../PRD.md). For the request and source formats, see [API.md](API.md).
