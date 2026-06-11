# Checkpoint Protocol — Meta Skill

## When to Use
After every stage where `checkpoint_required: true` in the pipeline manifest.

## Protocol

### Step 1: Check Manifest Policy
Read the stage config for `checkpoint_required` and `human_approval_default`:

| checkpoint_required | human_approval_default | Action |
|---|---|---|
| true | true | Checkpoint + present to user for approval |
| true | false | Checkpoint + proceed automatically |
| false | * | Skip checkpoint |

### Step 2: Prepare Checkpoint Data
Collect:
- Stage name
- Status: "completed" (auto-proceed) or "awaiting_human" (needs approval)
- All artifacts produced by this stage
- Review findings summary (critical, suggestions, nitpicks)
- Cost spent in this stage (if tools reported cost_usd)

### Step 3: Write Checkpoint
Use the `lib/checkpoint.py` utilities:
```python
write_checkpoint(projects_root, project_id, stage_name, status, artifacts, metadata)
```

### Step 4: Human Approval (If Required)
Present this summary to the user:

```
## Stage Complete: [stage_name]

### What Was Produced
[Key details from artifact — what the agent decided and why]

### Review Findings
Critical: N | Suggestions: N | Passed: Y/N

### Decision Log Entry
[What major choices were made this stage]

### Action Required
Reply with:
- "approved" — proceed to next stage
- "revise: [feedback]" — return to this stage with your changes
- "abort" — stop the pipeline
```

Wait for the user's response before writing the next stage.

### Step 5: On Approval
- Update checkpoint status from "awaiting_human" to "approved"
- Log the approval in decision_log
- Proceed to next stage

### Step 6: On Revision Request
- Return to the stage director skill for this stage
- Apply user feedback
- Re-run the stage
- Re-review
- Checkpoint again (increment round number)
- Max 3 send-backs; after that, ask user to abort or override

### Step 7: Resume Detection
At the START of any pipeline run, check for existing checkpoints:
```python
next_stage = get_next_stage(projects_root, project_id, all_stages)
```

If next_stage != first stage:
- Inform user: "Found existing progress. Resuming from: [stage]"
- Load prior artifacts from checkpoints using `load_artifact()`
- Continue from that stage
