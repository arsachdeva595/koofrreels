"""
Shared physical-realism prompt fragments for AI Reels video/image generation. Dependency-free
by design — imported by both backend/ai_runner.py and tools/ai/keyframe_generator.py, which
must not import from each other (ai_runner.py already imports KeyframeGenerator, so the
reverse would be circular).

Mitigates recurring Veo3/fal/WaveSpeed artifacts: extra limbs/hands, unnatural or ghostly
movement, and physically implausible object-to-body placement (e.g. a bag strap worn around
the neck instead of the shoulder). Prompt engineering alone can't eliminate these — the goal
is reducing how often they slip through, not eliminating them.
"""
from __future__ import annotations

# Negative prompt: flat comma-separated terms (not "no X" sentences) — matches how this
# codebase's existing negative prompts are phrased, and how these models respond best to
# unwanted-element lists. Applied unconditionally (unlike the speech-suppression terms,
# which only apply when voiceover is on) since this is a visual-quality concern.
ANATOMY_ARTIFACT_NEGATIVE = (
    "extra limbs, extra fingers, extra hands, missing fingers, fused fingers, deformed hands, "
    "malformed hands, mutated hands, distorted anatomy, warped body, morphing, warping, melting, "
    "ghosting, motion smear, unnatural jerky movement, floating limbs, disconnected limbs, "
    "duplicate body parts, asymmetrical face, uncanny motion"
)

# Positive reinforcement tag appended at every still/video prompt assembly point, independent
# of what the storyboard-writing LLM wrote — cheap defense-in-depth. Positive anchoring is the
# stronger lever against subtle biomechanical errors; negative prompts alone catch only the
# blatant ones.
POSITIVE_REALISM_TAG = (
    "anatomically correct, natural human proportions, exactly two arms and two hands with five "
    "fingers each, hands anchored to a single object or surface, one steady deliberate motion"
)
