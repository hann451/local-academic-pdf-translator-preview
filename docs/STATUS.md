# Development status

## Formula protection and continuation context

The branch `work/formula-protection-continuation`, at saved verification base
`6a55d6e31637f59f7b74b26d2d00efbc38977151`, adds conservative
column/page-boundary candidate detection. High-confidence candidates provide
bounded, masked neighboring text to the PFML3 prompt as context only; blocks
remain independent translation, validation, cache, edit, and layout units.
Ambiguous candidates are exposed as block metadata but not used in prompts.
The documentation edits made for this handoff are local working-tree changes
and are not part of that saved commit.

Inline mathematical expressions continue to use validated placeholders.
Equation references such as `Eq. (2)` are now protected as equation-number
placeholders. This pattern affects the reference label only; it does not fix or
preserve the equation body.

A separate synthetic regression now covers English-labeled stacked fractions.
The parser groups and protects the text only when it detects a horizontal
vector bar, nearby English text above and below, a left-side expression ending
in `=`, and a nearby equation number. The resulting block bounds include the
fraction bar. Its synthetic integration test confirms zero differing pixels in
the formula region while adjacent ordinary prose translates with the mock
adapter.

Read-only inspection of the provided dual PDF located the equation on physical
PDF page 4 of 5, in the original panel's left column; a printed paper page
number was not detectable. Before spatial grouping, parsing produced two
pending paragraphs: the first inline-masked only `Ratio = Detected`, while the
denominator and `(7)` had no protected items. The updated parser groups the
label, numerator, denominator, equation number, and fraction bar as one
protected block. Its synthetic visual regression reports zero differing
pixels in the formula region and verifies adjacent prose translation with a
mock. This diagnoses and covers the source-side classification gap; it does
not reproduce the historical model output or rendering. No real-model
retranslation of the provided document or PDF editing was performed.

Resume accounting now derives processed-block progress from block states,
skips already-fallback blocks, and deduplicates identical validation issues.
Existing JSON block payloads without continuation metadata load with an empty
candidate list; no database migration is required for this field.

## Synthetic real-model trial — 2026-11-08

A separate two-page synthetic PDF trial used the saved commit in the same-PC
isolated clone and the existing PLaMo 3 Translate 2B Q4_K_M model. The model
server ran at `127.0.0.1:18082` with `-ngl 0 -t 4 -tb 4`; the isolated API used
`127.0.0.1:18081`. No user PDF, existing database/job, or external translation
service was used. The existing server at `127.0.0.1:8000` was left running and
unchanged.

The inference process was active for 207.6 seconds in total (including output
inspection and shutdown); the 9-block job itself completed in 8.13 seconds:
7 blocks were inferred by the real
model (5 body blocks and 2 headings), one spatially detected equation (7) was
protected, and one running header was protected. There were no
`fallback_original` blocks, job warnings, validation issues, or retries.
Progress finished at 9/9. The 2-page output opened and rendered without
obvious overlap; the protected fraction region, including its bar and equation
number, had a 0.0 differing-pixel ratio. The input SHA-256 remained unchanged.
High-confidence cross-column continuation candidates were recorded and the
physical blocks were translated separately; this does not demonstrate improved
quality or joined-sentence translation.

The server log records four CPU threads and seven inference tasks on the
dedicated 18082 process. However, at commit `6a55d6e31637f59f7b74b26d2d00efbc38977151`,
the generated validation report incorrectly labeled the compute backend as
Vulkan GPU and PID 38816, which was the separate, unchanged 8000 server.
Treat those historical provenance fields as inaccurate for that trial; the
dedicated command, port owner, model endpoint, and inference log establish
that the trial used the CPU-only server. Subsequent follow-up edits on
2026-11-08 update `backend/diagnostics.py` and `backend/jobs/manager.py` so
local process probing matches the configured loopback endpoint/port, rejects
ambiguous multi-PID listeners and remote endpoints, keeps installed PC GPU
metadata separate from inference backend reporting, and distinguishes
`unverified_from_api` compute status from verified startup flags (such as
`n_gpu_layers=0`) and loaded DLL state (`ggml-vulkan.dll`). This follow-up
was verified deterministically with mocks (ordinary Python suite: **73 passed,
7 skipped**); the real-model trial itself was not rerun. This result is limited
to this synthetic input and PC and does not establish minimum hardware
requirements or general translation quality.

## Verification scope

- Updated on 2026-11-08 after the endpoint-specific provenance fix: the
  ordinary offline Python suite passed **73 tests** (up from 69 on 2026-11-07),
  with **7 opt-in real-LLM/GPU tests skipped** and one Starlette/httpx
  deprecation warning. The passing tests use mocks and synthetic fixtures, not
  a real translation model.
- Frontend at that verification point: **15 tests passed across 3 files**;
  TypeScript/Vite production build passed. These are existing recorded results,
  not tests rerun for this handoff.
- Regression cases cover unpunctuated separate paragraphs/list markers,
  small-font body-misclassified footnotes, indented starts, CSI/RSSI/variable
  starts, and full-width figures/equations. Integration checks cover interrupted
  resume, one-block retranslation accounting, and retries ending in original
  fallback without reinference.
- For the 2026-11-07 branch work, no real model or user runtime was used. The
  provided dual PDF was only inspected and parsed read-only; it was not used as
  a translation test or modified. The fraction regression from that work uses
  a generated synthetic PDF and mock translator. The later synthetic
  real-model trial is recorded separately above and in `VERIFICATION.md`; it
  does not establish the historical output for the provided PDF.
- No commit or push was created.

The already-running local API process was started before these source edits.
No automatic reload option or watcher was detected, so the running service
cannot be treated as evidence that it loaded this branch's changes. It was not
restarted for this verification.

These are local results for the recorded branch state, not a clean-machine,
other-GPU, or translation-quality certification. The same-PC isolated
model-free check and the separate synthetic real-model trial from the saved
commit are recorded in `VERIFICATION.md`; neither certifies clean Windows,
other hardware, minimum requirements, or general translation quality.
