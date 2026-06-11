# Reviewer — Meta Skill

## When to Use
After every pipeline stage. You are the quality gate. Your job is to evaluate the artifact produced by that stage against the pipeline manifest's `review_focus` and `success_criteria`.

## Review Protocol

### Step 1: Load Context
- Read the stage's `review_focus` items from the pipeline manifest
- Read the stage's `success_criteria` from the manifest
- Load the artifact that was just produced

### Step 2: Schema Validation
- If a JSON schema exists for this artifact (in `schemas/artifacts/<name>.schema.json`), validate the artifact against it
- If schema validation fails → CRITICAL finding, block immediately

### Step 3: Review Against Focus Items
For each `review_focus` item, evaluate the artifact and assign a severity:
- **critical** — Must fix before proceeding. Include a proposed fix.
- **suggestion** — Should fix. Include proposed change.
- **nitpick** — Could fix. Minor.

### Step 4: Evaluate Success Criteria
For each `success_criteria` item, determine: met / not met / partial.
- Not met → CRITICAL finding.

### Step 5: Make a Decision

| Scenario | Action |
|----------|--------|
| 0 critical findings | **PASS** — proceed to next stage |
| 1+ critical findings | **REVISE** — fix all critical, re-review (max 2 rounds) |
| After 2 rounds, still critical | **PASS WITH WARNINGS** — note issues, proceed |

### Step 6: Record Review

Write your review inline using this format:

```
## Review: [stage_name] — Round [N]

**Decision:** PASS / REVISE / PASS_WITH_WARNINGS

### Findings

1. [CRITICAL] <title>
   - Description: what's wrong
   - Action: what to fix
   - Status: pending / fixed / accepted

2. [SUGGESTION] <title>
   - Description: ...
   - Proposed change: ...

### Summary
- Critical: N (N fixed)
- Suggestions: N
- Success criteria met: N/M
```

## Special Review: Final Review Stage
For the `final_review` stage, you must:
1. Call `video_prober` — verify codec h264, resolution 1080x1920, fps 30, duration within ±5% of target
2. Call `frame_sampler` — extract 4 frames at 10%, 35%, 65%, 90% of duration; assess each for blur-fill quality (no black bars)
3. Call `audio_prober` — verify music present, original audio ducked, no clipping
4. Produce `final_review` artifact with all check results
5. Set `final_review.status` = pass / revise / fail

## Key Rules
- Never skip critical findings without logging a reason in the decision_log
- A PASS means the artifact is good enough to proceed — not perfect
- Maximum 2 revision rounds per stage; after that, pass with warnings
