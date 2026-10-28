"""Contract for recording guidance in the final emitted reproduction-agent prompt."""

from pathlib import Path

import pytest

from omnigent.runtime.prompt import build_instructions
from omnigent.spec import load

_REPRO_AGENT = Path(__file__).resolve().parents[2] / "dev" / "repro-agent"


@pytest.mark.parametrize(
    "guidance",
    [
        "record the real command and its output, even if only an error message changes",
        "For internal/API-only results with no visible user interaction, written evidence is enough",
        "Text-only CLI output is not a reason to skip recording",
        "name the specific blocker in `recording_unavailable_reason`",
        "Do not block the verdict because footage is missing or rejected",
        "a **`reproduced`** or **`likely_repro`** facet → **before-fix footage**",
        "When the preflight recorder check passed, point the recorder at the facet's lane",
        "quote only visibly rendered text",
        "hidden accessibility labels and backend logs are not screen evidence",
    ],
)
def test_recording_guidance_reaches_the_final_prompt(guidance: str) -> None:
    spec = load(_REPRO_AGENT, expand_env=False)
    prompt = build_instructions(spec, None, [])
    assert guidance in " ".join(prompt.split())
