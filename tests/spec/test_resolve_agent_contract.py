import importlib.util
import json
from pathlib import Path

from omnigent.spec import load

_RESOLVE_AGENT = Path(__file__).resolve().parents[2] / "dev" / "resolve-agent"


def _handoff_example() -> dict:
    skills = {skill.name: skill.content for skill in load(_RESOLVE_AGENT).skills}
    output = skills["resolve-handoff"].split("## Output —", 1)[1]
    return json.loads(output.split("```json\n", 1)[1].split("```", 1)[0])


def test_handoff_skill_includes_impact_assessment_and_remaining_work() -> None:
    handoff = _handoff_example()
    assessment = handoff["impact_assessment"]

    assert handoff["test_audit"]
    assert handoff["remaining_work"] == []
    assert assessment["base_sha"]
    assert assessment["head_sha"]
    assert assessment["worktree_state"]
    assert assessment["uncovered_boundaries"] == []
    assert assessment["not_applicable_reason"] == ""
    assert assessment["risks"]
    for risk in assessment["risks"]:
        for key in ("files", "behavior", "consumers", "invariant", "check", "evidence"):
            assert risk[key]
        assert risk["result"] == "passed"


def test_resolve_handoff_passes_the_shared_review_gate() -> None:
    script = _RESOLVE_AGENT.parent / "agent-skills" / "otto-pr-review-loop" / "review_cycle.py"
    module_spec = importlib.util.spec_from_file_location("resolve_review_cycle", script)
    assert module_spec and module_spec.loader
    cycle = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(cycle)
    handoff = _handoff_example()
    review = handoff["review_cycle"]
    assert handoff["ocr_review"]
    assert review["dispositions"]
    state = {
        "head_sha": review["head_sha"],
        "fingerprint": review["fingerprint"],
        "completed": {"polly": True, "ocr": True},
        "feedback": [{"key": item["key"]} for item in review["dispositions"]],
    }
    cycle.validate(state, handoff)


def test_resolve_agent_bundle_ships_the_shared_skills(tmp_path: Path) -> None:
    """Linked shared skills reach the server as files, with one copy of the review helper."""
    from omnigent.cli import _bundle
    from omnigent.spec.tar_utils import extract_safe

    shared = _RESOLVE_AGENT.parent / "agent-skills"
    extracted = tmp_path / "bundle"
    extract_safe(_bundle(_RESOLVE_AGENT), extracted)
    bundled_skills = {skill.name for skill in load(extracted).skills}

    for name, path in (
        ("otto-pr-review-loop", "SKILL.md"),
        ("otto-pr-review-loop", "review_cycle.py"),
        ("otto-review-remediation", "SKILL.md"),
        ("otto-impact-assessment", "SKILL.md"),
        ("otto-drive-pr", "SKILL.md"),
        ("otto-drive-pr", "ci.md"),
        ("otto-drive-pr", "preview.md"),
    ):
        assert (_RESOLVE_AGENT / "skills" / name).resolve() == (shared / name).resolve()
        assert name in bundled_skills
        bundled = extracted / "skills" / name / path
        assert not bundled.is_symlink()
        assert bundled.read_text(encoding="utf-8") == (shared / name / path).read_text(
            encoding="utf-8"
        )
    assert not (extracted / "skills" / "resolve-drive-pr" / "review_cycle.py").exists()
