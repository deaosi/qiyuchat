# VOC v2 shadow engine

`src.voc_engine` reads the existing business SQLite database in `mode=ro` and
writes only `<business-stem>.voc-v2.db` beside it. Legacy VOC, rule clusters,
manually maintained knowledge and all source rows remain unchanged.

## Service contract

- `path(cfg) -> pathlib.Path`
- `check(cfg, filters)` is strictly read-only: no schema, download or inference.
- `summary(cfg, filters)` returns `total_events, topic_count, ready_knowledge,
  exception_count, answered_events, source_pairs, coverage, semantics, latest_job,
  needs_compile`. `needs_compile` is the number of visible topics with missing or
  outdated compiled results; `check` exposes it at top level for hourly routing.
- `topics(cfg, filters) -> {items,total}`. IDs are strings; counts use valid
  current source events, distinct session IDs and distinct customer IDs.
- `topic_detail(cfg, filters, topic_id) -> {topic,events,evidence,total}`.
- `event_detail(cfg, filters, event_id) -> {event}`.
- `knowledge(cfg, filters, knowledge_id='') -> {items,total}` or
  `{knowledge,evidence}`. The list only contains current, gate-passed `ready`
  versions; snapshots and prior versions remain stored.
- `exceptions(cfg, filters) -> {items,total}` includes source failures and
  knowledge gaps/conflicts. They are not made into fabricated answers.
- `search(cfg, filters, query, limit=5) -> {topics,knowledge,events}` returns
  bounded semantic results for read-only assistant tools.
- `run(cfg, filters, limit=20, max_calls=20, max_tokens=40000, shadow=True,
  progress=None) -> {job,summary,shadow}` is synchronous, one inference at a time.
  `progress(job)` observes counters; a web/subprocess wrapper owns its own durable
  job ID. Both shadow values keep the old pipeline read-only.

The engine has an internal `v2_job` ledger. A unique active-job index protects
v2 workers; dead PID owners are failed, never silently treated as completed.
Before starting it also checks live legacy VOC/knowledge owners. The caller must
coordinate the old hourly task and new jobs, since different databases cannot
provide one atomic cross-pipeline model lock.

## Grounding, identity and quality

The provider adapter supports arbitrary configured OpenAI-compatible model IDs.
No provider/model prefix controls schema or gates. `json_mode` and `thinking`
come from the saved capabilities/config; the existing safe gateway handles the
response-format fallback. The full task/schema is also included before/after the
escaped data block in the user message, for channels that discard system content.
The same instructions are used for every model. Strict JSON/fenced JSON/text
blocks are normalized and then
fully validated; missing/extra fields or unprovided candidate IDs are errors.
`Provider.extract` is the seam for future native providers.

Real LangExtract `Resolver.align` and `UnicodeTokenizer` locate each customer
quote, staff claim and scope quote; fuzzy/partial alignment is prohibited.
Transport remains the existing controlled gateway, not LangExtract's SDKs.
Claims only quote the supplied staff answer and ready knowledge additionally
requires verification against actual raw-message staff roles. Quotes include
character spans and, where available, precise source-message references.
The span coordinate system is cleaned/deidentified visible text, not raw
HTML/JSON byte offsets. Source `qa_origin` fingerprints/revisions participate in
invalidation, including changes hidden by deidentification.

Events use session + visitor + actual source message ID/local span. `qa_origin`
provides the message anchors, preserving identity through QA renumbering and
preceding-message insertion. Legacy records are explicitly marked
`legacy-reference` (or `evidence-key-reference` when the raw span cannot be
reconstructed); they do not claim cross-reordering stability. Identical text
from different true message IDs is counted separately.
Internal raw-session hashes, rather than masked display strings, drive distinct
session counts and independent-source support. The internal keys are not exposed.
Overlapping/nested intent quotes are rejected, while disjoint occurrences in
different actual messages remain separate. Empty replies are not marked verified.

Retrieval is BGE-small-zh-v1.5 on CPU, normalized vectors cached by model/text
hash in the derived database. Only top five candidates enter each model request;
the full existing topic directory is never sent. No keyword/hash-vector fallback
pretends to be semantic retrieval. Source fingerprints make repeated runs free
of repeated inference; answer edits generate new asset versions. Every read
revalidates source fingerprints so stale evidence cannot remain usable.
Failed sources rotate behind fresh sources, preventing a malformed early item
from starving the rest of an incremental backlog. Changing only a quality-gate
version recompiles cached extraction rather than paying for source inference.
Canonical question + product/version/audience is the topic identity even when a
model returns `topic_id=null`. Soft duration/condition differences cannot create
separate identities that hide policy conflicts. A near-null ambiguous duplicate
blocks automatic knowledge on both related topics pending resolution.

Ready answers are extractive staff text. Gates require resolved-answer type,
exact staff provenance, complete logical sentences (including the subject,
audience, conditions and negation), independently assigned intent context,
no missing context or explicit conflict, and consistent scope. Information
collection and contextual replies remain gaps. A complete low-risk single source
may be ready and is labeled `single_source`. Policies/promises/numeric/negative
claims require repeated exact support across independent sessions. Alternate
wording is admitted only for low-risk evidence with cosine >= 0.94; this initial
threshold is conservative and must be calibrated with labeled business samples.
Model self-confidence is never accepted as a gate. Multi-intent claim unions
cannot certify another intent's missing premise. Topic-specific sentence context
and adjacent prerequisites must remain with the same intent; ambiguous ownership
is an exception. Claims/steps are always ordered by trusted source spans, never
the model's array order. Navigation to a page named installation/configuration is
not counted as a repair when the purpose is screenshot collection; an independent
save/restart/reinstall followed by conditional collection can remain a solution.

Assets compiled by any previous quality version are immediately unavailable for
automatic use. `needs_compile` keeps hourly work active with zero pending source
pairs, and `run` recompiles those cached claims without model calls. An identical
human ignored disposition is retained across quality-only recompilation.

`max_calls` caps inference invocations (a format-negotiation HTTP retry may occur
inside the shared provider). Token admission conservatively reserves UTF-8 bytes
of the complete request plus requested output tokens before each call; reported
usage is recorded separately, including validation failures. Oversized complete
sources are exceptions rather than silently truncated data.
Three consecutive source failures stop further calls in that round (success
resets the streak); remaining sources are skipped and stay pending. Each failure
still has its own safe exception and usage accounting.

## Local model and evaluation

Install `requirements-voc.txt` (LangExtract 1.7.2, sentence-transformers 6.2.0)
into the worker environment with a CPU PyTorch
build. Explicitly cache `BAAI/bge-small-zh-v1.5` under
`<STATE_DIR>/models`, or set `VOC_EMBED_MODEL_PATH` to an already downloaded local
model directory. Runtime uses `local_files_only=True` and
`trust_remote_code=False`; a request cannot download or execute remote model
code. Missing dependencies/cache produce a clear unavailable state.

```shell
python -m src.voc_engine --check
python -m src.voc_engine --limit 5 --max-calls 5 --max-tokens 60000
python -m src.voc_eval --models MODEL_A MODEL_B
python -m src.voc_eval --models MODEL_A MODEL_B --execute --max-calls 6 --max-tokens 200000 --output evaluation.json
```

The multi-model harness defaults to plan-only. Explicit execution uses isolated
synthetic QA databases, real local embeddings/alignment and the configured safe
gateway, without changing the saved model profile or production data. Its gold
cases cover synonyms, different intents, numeric conflict and information
collection; it reports pairwise grouping precision/recall, answer-kind accuracy,
critical false-ready results, positive-ready recall, usage and unchanged-source
digests. Every positive resolved case (golden IDs 1, 2, 3) must be covered by ready
asset evidence; zero ready assets cannot pass. A report with
missing cases/budget skips is incomplete, never a passing evaluation.
With the complete repeated task and this six-case fixture, conservative
reservation is about 86,421 with no candidates, and <=184,869 at the fixture's
maximum allowed Top5 fields; 80,000 is insufficient. Use 200,000 per model for
these cases (harness default 300,000 supplies additional margin). Inference calls
remain capped separately; these are admission reserves, not actual billed usage.


## Zhihuige merchant-support domain (explicit opt-in)

Set runtime `voc_domain=zhihuige` / `VOC_DOMAIN=zhihuige`. The empty default
retains the six-case generic contract. This is project context, never a model
name rule. Extraction cache checkpoints include `voc-grounded-v3/zhg-support-v4`;
quality checkpoints additionally include `quality-v4/zhg-support-v4/software-branches-v4`.
Generic cached claims cannot be reused as domain knowledge and vice versa.

`domain` metadata distinguishes configuration guidance / functional anomaly /
unclear, reported/pending vs staff-confirmed causes, and suggested actions vs
actually verified effects. Every reference carries source, exact clean-text
span, program-derived role and message-relative `source_refs`. Merchant SKU,
product specs and quoted buyer questions are reproduction context, not buyer
policy knowledge. Same-visitor nearby context is bounded comparison material
only; its staff text cannot become a current QA's knowledge claim.

Topics expose `consultation_count` (session + visitor + topic distinct) as the
primary demand count. `event_count` / `mention_count` remain sourced fragment
counts. Context-only fragments are processed into `context_needed` exceptions,
not artificial product topics. Coverage adds `context_needed_pairs` and `domain`;
source-pair totals remain the selected date/group/staff range, whereas domain
filters apply only to processed event metadata.

Knowledge has multiple independently versioned `variant_id` branches per topic,
with `kind` (`configuration_guide`, `troubleshooting_advice`,
`verified_workaround`, `capability_fact`), `diagnosis_status`, `effect_status`, `applicability: Ref[]`
and `confirmed_cause: Ref|null`. Pending incidents do not poison established
methods. Same symptom with different confirmed causes can retain different
bounded methods. Suggested parameter strategies preserve their original values;
exclusive necessary limits under the same applicability are checked across
method-code aliases. Codes alone never establish truth.

Software knowledge compiles only complete, current staff software sentences
and their necessary premises. Independent buyer-policy/reproduction sentences
are excluded without changing source text, fingerprints or offsets. An ambiguous
mixed sentence is withheld rather than cut at a comma. Pure policy text cannot
become a software method by supplying an `actions` label. Within this proven
software subset, numeric values/negation/conditions remain verbatim; the generic
buyer-policy repetition gate is not applied to a source-bounded software step.
Verified claims still require explicit staff retest/confirmation evidence.

`defects(cfg, filters)` returns existing event citations for explicitly confirmed
defects. An unresolved defect without a usable method is not ready knowledge.
All read methods support existing source filters plus `support_intent` and
`diagnosis_status`; generic mode ignores these domain selectors.

```shell
python -m src.voc_eval --domain zhihuige --models MODEL_A MODEL_B
python -m src.voc_eval --domain zhihuige --models MODEL_A MODEL_B --execute --max-calls 18 --max-tokens 1300000 --output support-evaluation.json
```

The nine-case support harness keeps the original generic six unchanged. It
requires all nine sources processed, eight consultation fragments and one pure
context fragment; correct primary intent/diagnosis/fragment labels; same-symptom
SKU grouping; both configuration-cause branches; a cited verified workaround;
all positive evidence covered by ready assets and zero forbidden ready evidence.
It accepts documented legacy fixture-kind names through an explicit semantic
mapping, never by weakening the runtime knowledge kind enum. Admission budget
is conservative UTF-8 reservation, not billed tokens; use 1,300,000 / at most eighteen calls per
model for the full domain set. No execution occurs without `--execute`.


The merchant-support raw protocol explicitly distinguishes Ref arrays from
nullable single Refs, and includes a complete legal domain JSON example in both
system and user task copies. Raw references still accept only `source` and
`quote`; program-derived role/offsets never come from model authority. Invalid
reference errors report a static schema path and controlled JSON type only,
without echoing customer text or arbitrary keys. Domain output admission is
12,000 tokens, while the generic contract remains at 3,000. A length-terminated
response is rejected with a safe truncation reason and its usage counted.
Changing the domain schema version invalidates extraction cache; changing only
the software-branch quality version remains a free local recompile.


Domain task construction replaces the generic product/version grouping sentence
with software-capability/merchant-goal/reported-symptom identity. Business scene
content, SKU, version, diagnosis and troubleshooting stages remain cited
attributes or bounded knowledge variants, rather than artificial demand topics.
It never forces an unspecified module into an incompatible narrow candidate.
Anchored “still needs investigation” language is accepted as an unknown blocking
gap; local negated investigation and already established causes cannot supply
that support. This records a pending event, never a confirmed defect or ready
answer. Empty/length-ended model content remains a safe failure with real usage
accounted; the source stays pending for a later budgeted run. Turning thinking
off is a provider capability setting, not evidence that output cannot truncate.


Each source gets at most one automatic correction attempt after a retryable
model/structural/exact-quote error. The same original source and bounded TopK
are supplied again; feedback includes only controlled error codes/schema paths,
never the previous unchecked output or provider text. Each attempt independently
reserves budget and increments calls/usage, including failed attempts. Network
and inference hold no database connection. Nonretryable, unknown service and
post-validation/manual-conflict errors are not corrected automatically. A source
is processed once after success or failed once after both attempts; a retry that
cannot fit the remaining budget leaves the source pending/skipped with partial
status. The three-consecutive-failure breaker counts final failed sources.
The harness reports `actual_attempts`, `successful_retried_sources` and
`first_pass_complete`; a result recovered by retry is never presented as a clean
first pass. Generic six-case budgets/defaults remain unchanged. The domain
nine-source run permits at most eighteen attempts with a 1.3M conservative
reservation budget; these are ceilings rather than mandatory calls or billing.

`capability_fact` is a local compiler knowledge kind for complete, cited software
support/limitation facts. It requires no invented action or confirmed root cause,
keeps actual temporal/platform/version constraints, and excludes buyer policy,
manual-service promises and statements that only promise a later notification.


The synchronous engine and its CLI default to a 400,000 conservative token
budget, matching the web service. Explicit budgets still apply per attempt and
the 2,000,000 hard ceiling remains unchanged. This admits the complete domain
schema plus 12,000 output allowance without a zero-call default run.


V4 compilation uses a branch-local standard question from its own verified
source intents for the knowledge title. It does not copy the global demand
question, which may retain a different incident's reproduction background. Topic
identity and source records remain unchanged. Scope projection and branch keys
share current validated generic scope plus domain references. A quality-only
projection/key upgrade preserves the latest human disposition for the exact
same topic/event/source-fingerprint set, including a later explicit activation.

The local orchestration helper `work/replay-voc-evaluation.py` defaults to plan
only. After the new code stage is ready, run it with `--execute`. It reads the
old network-stage manifest and the new code-stage manifest, replays exact fixed
synthetic completion attempts through the real provider parser, LangExtract,
CPU BGE and engine, and blocks sockets/DNS in the replay process. It writes
`work/voc-domain-quality-replay.json`; original paid model attempts/usage remain
in a separately referenced/copied network report. Replay usage/network calls
are zero and clearly labeled. Divergent attempt order, unsupported HTTP
negotiation traces, modified original snapshots or incomplete scope/title audits
fail the replay instead of inventing responses or claiming another network pass.


Merchant-support gap normalization now separates incident progress from a
reusable-knowledge blocker. A legal, enum-valid annotation with exact current
request/evidence that does not prove a necessary knowledge gap is preserved as
`gap_audit` (`authority=model_annotation`, `blocking=false`), never answer facts.
Generic extraction keeps its original strict rejection. Malformed fields, missing
or foreign evidence, cross-intent references and true unanswered requests stay
strict. Independent current-staff guards withhold unknown requested dates/values
and genuine missing knowledge dependencies even if model annotations are absent
or misclassified. Complete conditional permission/configuration checks can remain
suggested advice without claiming the customer case is resolved. Information
collection, notification promises and no-method incidents still cannot become
ready knowledge. Exact role, full-sentence, condition, number and negation gates
remain mandatory. Quality `quality-v4` invalidates prior ready results for free
recompilation; it does not treat an old decision as current evidence.

Gap correction feedback uses a safe `intents[i].missing_info[j].kind` path and
`unsupported_blocking_gap` code; it never echoes provider text or unchecked
output. Domain classification uses distinct `domain_classification` feedback for
`domain.primary_intent` / `domain.fragment_role`, constrained to current Q+A.

## Conversation-supported automatic answers

The full Chinese business specification is [voc-algorithm.md](voc-algorithm.md).
`ready_knowledge` counts source-supported compilations. `knowledge_reuse.level`
drives the default answer view. `guidance` requires complete, exact human
evidence for low-risk configuration instructions or read-only diagnostics with
their applicable conditions. `verified` requires explicit customer application
and effect for the displayed procedure. `reference` remains visible as a source
record. `auto_reusable_knowledge` is the sum of `verified_effect_knowledge` and
`guidance_knowledge`.

`voc_outcome.py` reads complete, reproducible official message snapshots with
current completed sync versions, exact question/answer anchors, verified roles
and visitor identity. Certificates contain status, acceptance, effect, feedback
quotes/message references, coverage, reason codes, version and fingerprint.
No business rows are changed and no inference is used for certificate reads.

Explicit understanding is not operational success. Silence, END, truncated
windows, unknown visitors, ambiguous/shared intents, prospective or interrogative
success phrases, and abandonment never prove acceptance. Two independent weak
closures may pass the feedback certificate, with their threshold and counts retained.
Continuing questions, reopened topics or negative feedback block automatic reuse
even if another source succeeded. New substantive staff intervention prevents
assigning later success to the earlier answer; polite closing does not erase an
already observed customer acceptance.

Certificates cover every current citation in the global knowledge branch.
Date/customer filters affect visibility, not the presence of counterevidence.
Missing/changed citations withhold reuse. Feedback changes are re-evaluated at
read time without changing extraction cache identity. This conservative first
version leaves unbound language unknown; it is conversation evidence, not a
claim of universal product truth or perfect natural-language classification.

## Multi-turn VOC replay

`voc_episode_replay` samples completed official sessions by session end date and
rechecks their source snapshots. Each run groups the messages of one verified
visitor segment, masks source identifiers, display names, links and existing PII
patterns, and outputs aggregate counts. The current comparison is restricted to
the same successfully replayed source segments and their active question anchors.

```sh
python -m src.voc_episode_replay --days 7 --limit 12 --require-staff
python -m src.voc_episode_replay --days 7 --limit 12 --require-staff \
  --with-existing-topics --execute --max-calls 3
```

The first command inspects coverage without inference. `--execute` uses the
configured gateway and existing local BGE embeddings. `--reranker /local/model`
enables an optional locally cached cross-encoder for candidate pairs. The report
includes the cohort digest, completed and rejected segments, old and new topic
counts, and case-level evidence grades. Topic IDs are reproducible within a
fixed cohort. Replays run outside the dashboard worker and do not publish their
results. Case success records an observed result tied to one action in one
episode. Promotion to reusable knowledge requires separate release evaluation.

## Multi-turn topic ranking

In the `zhihuige` domain, the existing VOC worker reserves a small part of the
job budget for complete official sessions with a known visitor. It checks the
official version and message anchors before publishing a result to the derived
database. A stale, incomplete or failed episode leaves its existing topic
evidence available. The recurring job can continue processing these sessions
after the per-question queue is empty.

The topic catalog assigns durable IDs to grounded issues. It reuses an existing
topic when the evidence uniquely identifies it, compares new issues with saved
representatives, and creates a topic for a grounded new issue. Business-field
conflicts and ambiguous matches withhold a merge. Local BGE vectors take part
in matching. An already prepared local model can be selected with
`VOC_EPISODE_RERANKER_PATH` for pairwise reranking.

Topic rankings count qualified linked consultations and retain the existing
results for sessions that have yet to qualify. The workbench reports how many
consultations were linked. A linked consultation is counted on the date of
its first question. Its evidence can include later replies and follow-ups.
The knowledge compiler continues to verify its
original message evidence before making any answer available for reuse.

## Handling SOPs

`GET /api/voc2/sops` presents the handling path in the knowledge library. It
reads the persisted episode ledger, rechecks the current official snapshot and
first-question anchor, and keeps the original message and timestamp for each
step. Only a directive supported by complete staff text enters the procedure.
Conditions and outcomes remain case-specific. Failed attempts remain visible
as evidence and are excluded from copied procedures.

A procedure with a sourced retest criterion can be offered as guidance within
its known conditions. A verified effect requires an explicit customer report
that covers the actual action path and has no conflicting follow-up. Other
cases show their known steps and missing evidence. SOP reads use the existing
episode output and do not call a model or change the business source.

## Actionable issues and handling playbooks

`GET /api/voc2/issue-pilot` builds scoped product issues from the current event
and episode view. Specific configuration and reply patterns improve known
workflows, while other grounded tasks and symptoms can form their own issues.
Routing labels do not limit which issues can be represented. A result with
unknown applicability remains distinct from a fully scoped issue, and inferred
causes never stand in for source evidence. Consultation counts and identified
merchant counts are separate.

`GET /api/voc2/playbooks` distinguishes investigation from a sourced procedure.
Reusable branches require bound staff instructions, applicable conditions and a
post-action check. Compatible evidence can corroborate a branch across cases.
The requested period controls which citations are returned; conflicting cases
outside it can still withhold reuse. These read paths do not call the gateway.
