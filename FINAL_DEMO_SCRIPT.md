# SikaRescue: final demo script

Six clips, recorded separately and joined in the edit. Human narration only: no AI voice.
Spoken narration totals about **1:53** (283 words at a calm 150 words per minute). Model and
Modal waits are cut in the edit, so the finished video runs close to 2:00 including pauses.

Browser: Chrome, window 1440 px wide, zoom 100%, bookmarks bar hidden, one tab on
`http://127.0.0.1:8000/`. Everything shown is synthetic.

---

## Before recording (run in this order, within about 2 minutes of clip 3)

Git Bash, from the repo root.

```bash
# 1. Build the frontend (only if frontend/ changed since the last build)
npm --prefix frontend run build

# 2. Gateway preflight + Modal warm-up. Must print READY TO RECORD. Fails cleanly otherwise.
#    Checks: agent_mode=pydantic, Gateway route=sr, model=models/gemini-3.8-flash, Modal.
#    Proves the live definitive path (Pydantic AI via sr + deterministic verifier) and warms
#    both Modal functions (they scale down after about 2 idle minutes).
env -u GOOGLE_API_KEY -u GEMINI_API_KEY uv run python scripts/recording_preflight.py

# 3. Start the recording server. --record refuses to start with any other configuration.
#    The longer Modal timeout only avoids a visible local fallback if a container went cold.
env -u GOOGLE_API_KEY -u GEMINI_API_KEY SIKARESCUE_MODAL_TIMEOUT_SECONDS=45 \
  uv run python scripts/serve_demo.py --compute modal --agent pydantic --record

# 4. In a second terminal: pre-run the outage analysis once (clip 6 then re-runs warm),
#    and reset to the starting state for clip 1.
curl -s -X POST http://127.0.0.1:8000/api/outage/run > /dev/null
curl -s -X POST http://127.0.0.1:8000/api/demo/reset \
  -H "Content-Type: application/json" -d '{"scenario":"definitive"}' > /dev/null
```

PowerShell equivalents for step 4:

```powershell
curl.exe -s -X POST http://127.0.0.1:8000/api/outage/run | Out-Null
curl.exe -s -X POST http://127.0.0.1:8000/api/demo/reset -H "Content-Type: application/json" -d '{\"scenario\":\"definitive\"}' | Out-Null
```

Then reload the browser tab (Ctrl+R) and scroll to the top.

**To retake any clip**, reset to the definitive starting state with the step 4 reset
command (or click **Definitive failure**, then **Reset demo**) and reload the tab.

---

## Clip 1: the broken payment

- **File:** `clip1_broken_payment.mp4`
- **Target:** 15 s
- **Starting state:** fresh definitive instance, top of page. Badge says **FAILED**; stepper is on
  **Classify**; left column heading reads **Last confirmed money position**.
- **Interaction:** none. Move the pointer slowly down the left column, then rest it on the funds
  position panel.

| Narration (say exactly) | Visual cue |
|---|---|
| "This is SK-10421: a hundred and twenty pounds, UK to Ghana mobile money." | Pointer on **£120.00** and **United Kingdom → Ghana** |
| "Debit, FX and settlement all succeeded." | Pointer down the three green **Succeeded** legs |
| "But MoMo A's payout answer isn't understood yet, so SikaRescue shows only where the money was last confirmed." | Pointer on **Payout via MoMo A: Response not classified yet**, then the **Last confirmed here** flag |

- **Pause:** 1 s of silence after the last sentence, pointer still on the flag.
- **Transition:** cut on the pause; clip 2 starts on the same page state.

---

## Clip 2: provider evidence, Pydantic AI, deterministic verifier

- **File:** `clip2_provider_evidence.mp4`
- **Target:** 30 s (the extraction wait of about 3 to 8 s is cut)
- **Starting state:** same page, scrolled so **Classify the provider's response** and the
  **Raw provider response** box fill the right column.
- **Interaction:** point at the payload, click **Classify provider response**, wait for the
  verdict, then scroll slowly through stage 2 and stage 3.

| Narration (say exactly) | Visual cue |
|---|---|
| "MoMo A answered HTTP 200, completed: a status-code check would call that success." | Pointer on `HTTP/1.1 200 OK`, then `"status": "COMPLETED"` |
| *(click **Classify provider response**; cut the wait)* | Spinner "Extracting and verifying" |
| "Pydantic AI, via the Gateway on Gemini, proposes evidence, quoting the body word for word." | Stage 2 **FailureEvidence (AI proposal)**: pointer on **MA-4017** and the quoted citations |
| "But the proposal decides nothing. Deterministic code parses the body: not accepted, code MA-4017, no transfer." | Stage 3 trusted-facts checklist: pointer down **Body shows an explicit rejection**, **Body code is a documented pre-acceptance rejection**, **Body shows no transfer was created** |
| "Only then is it a definitive failure: no value moved." | Pointer on the green **DEFINITIVE_FAILED** bar |

- **Pause:** 1 s on the green verdict bar. Also take a 2 s silent shot of the left column,
  where the funds position now reads **Funds are here** (**Available: proven in place**).
  The edit can use it as B-roll.
- **Transition:** cut; clip 3 starts at **Why not just retry?**.

---

## Clip 3: recovery candidate funnel and proposed ledger state

- **File:** `clip3_candidate_funnel_ledger.mp4`
- **Target:** 30 s (the analysis wait of about 20 to 40 s is cut)
- **Starting state:** scrolled to **Why not just retry?**, with **Find a safe recovery route**
  and its **Analyse recovery** button visible below.
- **Interaction:** point at the red column, click **Analyse recovery**, cut the wait. The page
  jumps down to **Candidate routes**. Scroll UP past **Why not just retry?** to the
  **Recovery candidate funnel** (now 4 / 2 / 2 / 2 / 1), then scroll down past
  **Stress simulation** (**Ran on Modal**) to **Recommended recovery plan** and
  **Ledger now vs after execution**.

| Narration (say exactly) | Visual cue |
|---|---|
| "Restarting would charge the sender a second hundred and twenty pounds." | **Restart from origin** column: pointer on **charges the sender again** |
| *(click **Analyse recovery**; cut the wait)* | Spinner "Analysing recovery" |
| "Instead, four candidate routes meet hard constraints: MoMo A is down, the token bridge is outside policy." | Funnel **4 / 2 / 2 / 2 / 1**, then the two **Rejected before simulation** rows |
| "Only the eligible two are stress-tested on Modal, six hundred thousand simulated outcomes; MoMo B is selected." | **Stress simulation: Ran on Modal**, **600,000**, then the green **MoMo B, Selected** row |
| "The ledger preview shows exactly what changes: one recipient credit, no second debit." | **Ledger now vs after execution**: pointer on **Recipient credits 0 → 1**, then **Sender debits 1 → 1** |

- **Pause:** 1 s on the ledger table.
- **Transition:** cut; clip 4 starts with the **Approve recovery plan** button in view.

---

## Clip 4: approve, execute, internal reconciliation

- **File:** `clip4_approve_execute_reconcile.mp4`
- **Target:** 20 s
- **Starting state:** **Approve recovery plan** button visible under the ledger preview.
- **Interaction:** click **Approve recovery plan** (the page scrolls to **Execute the
  outstanding payout**), click **Execute recovery** (about 1 s; the page scrolls to the green
  **Recovered: internal reconciliation checks passed** panel), then rest.

| Narration (say exactly) | Visual cue |
|---|---|
| "A human approves this exact plan, bound to its hash." | Click **Approve recovery plan**; the page scrolls to **Execute the outstanding payout** and the badge reads **APPROVED** |
| "Execution sends only the outstanding payout." | Click **Execute recovery**; states tick through to **Reconciled** |
| "Internal reconciliation checks pass: one sender debit, one recipient credit, zero duplicates." | Proof table: pointer down **Sender debit count 1**, **Recipient credit count 1**, **Duplicate sender debits 0** |
| "The AI read the evidence and explained the plan. It never moved money." | Hold on the proof panel; badge **RECONCILED** |

- **Pause:** 1.5 s on the proof panel.
- **Transition:** cut; before clip 5, scroll to the top of the page.

---

## Clip 5: UNKNOWN goes to manual review

- **File:** `clip5_unknown_manual_review.mp4`
- **Target:** 22 s (the extraction wait is cut)
- **Starting state:** top of page after clip 4 (badge **RECONCILED**).
- **Interaction:** click **Unknown outcome** in the **MOMO_A incident** switch (a new payment
  instance appears), click **Classify provider response**, cut the wait, then scroll slowly
  to the red **UNKNOWN** verdict and on to the **Recovery candidate funnel**.

| Narration (say exactly) | Visual cue |
|---|---|
| "Now the dangerous case: the request went out, and no answer came back." | Click **Unknown outcome**; pointer on `no response bytes received before read timeout` |
| *(click **Classify provider response**; cut the wait)* | Spinner |
| "The verifier can't prove it wasn't accepted, so the recipient may already have been credited." | Red **UNKNOWN** bar, then the red ✗ trusted facts |
| "So there's no payout candidate at all: manual review, never an automatic retry." | **Recovery candidate funnel: Manual review required. There is no payout candidate and no executable payout.**; badge **MANUAL REVIEW** |

- **Pause:** 1 s on the red funnel banner.
- **Transition:** cut; clip 6 starts on the **Rail outage** tab.

---

## Clip 6: systemic outage on Modal

- **File:** `clip6_systemic_outage.mp4`
- **Target:** 22 s
- **Starting state:** click the **Rail outage** tab in the header (the result pre-run during
  setup is already on screen).
- **Interaction:** click **Run the analysis again** (about 1 to 2 s when warm), then move the
  pointer down the scenario table and onto the **MoMo B** liquidity bar.

| Narration (say exactly) | Visual cue |
|---|---|
| "The same control plane handles a whole rail failing: forty thousand synthetic stranded payouts, five scenarios." | Heading **Systemic outage: MOMO_A fails for the whole corridor**; **Stranded payouts 40,000** |
| "Policy is decided locally; Modal runs a deterministic greedy allocation per scenario, and local code recomputes every figure." | Click **Run the analysis again**; **Ran on Modal: 5 parallel jobs** |
| "With MoMo A down, ninety-nine point eight percent are recoverable. If MoMo B fails too, only thirty percent." | Pointer on **39,935 (99.8%)**, then **Correlated MoMo outage 12,000 (30.0%)**, then the MoMo B liquidity bar |

- **Pause:** 1.5 s on the table: end of video.
- **Transition:** fade out.

---

## Claims to keep (and avoid) while narrating

Say: "internal reconciliation checks", "deterministic verifier", "last confirmed",
"deterministic greedy allocation", "simulated", "synthetic".

Never say: "cryptographically signed", "exactly once", "externally reconciled",
"multi-worker safe", "optimal allocation", "Modal proves", "the AI decided".
