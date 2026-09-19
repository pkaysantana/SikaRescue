# SikaRescue: demo clip checklist

Tick each line while recording. Narration and cues are in `FINAL_DEMO_SCRIPT.md`.

## Go / no-go (before clip 1)

- [ ] `scripts/recording_preflight.py` printed **READY TO RECORD** within the last 2 minutes
- [ ] Server started with `--compute modal --agent pydantic --record` and printed
      `recording check: agent=pydantic, Gateway route=sr, Gemini 3.8 Flash, Modal`
- [ ] Outage analysis pre-run once (`POST /api/outage/run`)
- [ ] Reset to the definitive scenario, then the tab was reloaded
- [ ] Top of page shows: badge **FAILED**, stepper on **Classify**, **Definitive failure**
      selected, left heading **Last confirmed money position**
- [ ] Chrome at 1440 px wide, zoom 100%, notifications off, no other tabs
- [ ] Microphone levels checked; human voice only

If anything shows **ran locally instead**, **Deterministic fallback** or **Envelope-only**
during a take: stop, run the preflight again, reset, and retake. Don't narrate around it.

## Clips

| # | File | Target | Starting screen | Interaction | Pause / transition |
|---|---|---|---|---|---|
| 1 | `clip1_broken_payment.mp4` | 15 s | Top of page, **FAILED**, **Last confirmed money position** | Pointer only: £120.00 → three **Succeeded** legs → **Response not classified yet** → **Last confirmed here** | 1 s on the flag; cut |
| 2 | `clip2_provider_evidence.mp4` | 30 s (wait cut) | **Classify the provider's response** with the raw payload in view | Point at `HTTP/1.1 200 OK` and `"status": "COMPLETED"` → click **Classify provider response** → scroll through **FailureEvidence (AI proposal)** and the trusted-facts checklist | 1 s on green **DEFINITIVE_FAILED**; take 2 s B-roll of **Funds are here**; cut |
| 3 | `clip3_candidate_funnel_ledger.mp4` | 30 s (wait cut) | **Why not just retry?** in view, **Analyse recovery** below | Point at **charges the sender again** → click **Analyse recovery** → scroll up to funnel **4 / 2 / 2 / 2 / 1** → down to **Ran on Modal** / **600,000** → **Ledger now vs after execution** | 1 s on the ledger table; cut |
| 4 | `clip4_approve_execute_reconcile.mp4` | 20 s | **Approve recovery plan** in view | Click **Approve recovery plan** → click **Execute recovery** → rest on the proof panel | 1.5 s on the proof panel; scroll to top; cut |
| 5 | `clip5_unknown_manual_review.mp4` | 22 s (wait cut) | Top of page, badge **RECONCILED** | Click **Unknown outcome** → point at the read-timeout line → click **Classify provider response** → red **UNKNOWN** → **Recovery candidate funnel** banner | 1 s on the red banner; cut |
| 6 | `clip6_systemic_outage.mp4` | 22 s | Click the **Rail outage** tab | Click **Run the analysis again** → **39,935 (99.8%)** → **Correlated MoMo outage 12,000 (30.0%)** → MoMo B liquidity bar | 1.5 s on the table; fade out |

## Per-clip checks (what must be visible on screen)

1. [ ] **Payout via MoMo A: Response not classified yet**; **Last confirmed here** with
       "The recipient may already have been credited."
2. [ ] Stage 2 names **Pydantic AI extraction using models/gemini-3.8-flash via Gateway route sr**
       (not a fallback). Stage 3 shows every trusted fact ✓ and **DEFINITIVE_FAILED**.
3. [ ] Funnel **4 / 2 / 2 / 2 / 1**; **Ran on Modal** (not "ran locally instead"); **MoMo B**
       selected; ledger rows **Recipient credits 0 → 1** and **Sender debits 1 → 1**.
4. [ ] Badge **RECONCILED**; proof shows **1 / 1 / 0** and **£0.18, absorbed by the operator**.
5. [ ] New payment instance id; **UNKNOWN**; badge **MANUAL REVIEW**; funnel banner says there is
       no payout candidate and no executable payout.
6. [ ] **Ran on Modal: 5 parallel jobs**; the table shows all five scenarios.

## Edit

- Cut the model and Modal waits (clip 2 about 3 to 8 s, clip 3 about 20 to 40 s, clip 5 about
  3 to 8 s). Keep the spinner visible for about half a second before each cut.
- Target spoken total: about 1:53. Target video total: about 2:00.
- No AI voice, no music over narration, no zooms that crop the verdict or proof numbers.

## Reset commands

```bash
# Definitive starting state (clip 1, or any retake of clips 1 to 4)
curl -s -X POST http://127.0.0.1:8000/api/demo/reset \
  -H "Content-Type: application/json" -d '{"scenario":"definitive"}' > /dev/null
# Re-warm Modal + re-check the Gateway if more than about 2 minutes passed
env -u GOOGLE_API_KEY -u GEMINI_API_KEY uv run python scripts/recording_preflight.py
```

Reload the browser tab after every reset.
