import importlib.util
import json
import re
from pathlib import Path

from omnigent.runtime.prompt import build_instructions
from omnigent.spec import load
from omnigent.tools.builtins.load_skill import LoadSkillTool

_AGENT = Path(__file__).resolve().parents[2] / "dev" / "feature-agent"
_SHARED = _AGENT.parent / "agent-skills"
_SHARED_SKILLS = (
    "otto-pr-review-loop",
    "otto-review-remediation",
    "otto-impact-assessment",
    "otto-drive-pr",
)


def _skills() -> dict[str, str]:
    return {skill.name: skill.content for skill in load(_AGENT).skills}


def _handoff_example() -> dict:
    handoff = _skills()["feature-handoff"]
    blocks = re.findall(r"```json\n(\{.*?\})\n```", handoff, re.DOTALL)
    assert blocks, "feature-handoff has no fenced JSON example"
    return json.loads(blocks[-1])


def test_feature_agent_loads_its_bundled_skills() -> None:
    spec = load(_AGENT)
    assert spec.name == "feature_agent"
    assert spec.spawn is False
    assert {
        "feature-inputs",
        "feature-requirements",
        "feature-design",
        "feature-implement",
        "feature-demo",
        "feature-publish",
        "feature-handoff",
        *_SHARED_SKILLS,
    } == {skill.name for skill in spec.skills}


def test_feature_startup_prompt_routes_to_procedures_without_inlining_them() -> None:
    spec = load(_AGENT)
    tool = LoadSkillTool(spec.skills, skills_filter="none")
    prompt = build_instructions(spec, None, [tool.get_schema()])

    assert len(prompt.split()) < 1500
    for skill in spec.skills:
        assert skill.name in prompt
        assert skill.content not in prompt


def test_handoff_example_has_every_field_and_exact_literals() -> None:
    example = _handoff_example()
    assert {"pushed_branch", "maintainer_review", "last_pushed_sha"} <= example.keys()
    assert example["mode"] in {"authored_feature", "review_remediation"}
    for cuj in example["cujs"]:
        assert set(cuj) >= {"id", "covered", "tests", "recordings", "not_covered_reason"}
        if cuj["covered"]:
            assert cuj["tests"] and cuj["recordings"]
        else:
            assert cuj["not_covered_reason"]


def test_feature_handoff_passes_the_shared_review_gate() -> None:
    script = _SHARED / "otto-pr-review-loop" / "review_cycle.py"
    module_spec = importlib.util.spec_from_file_location("feature_review_cycle", script)
    assert module_spec and module_spec.loader
    cycle = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(cycle)

    example = _handoff_example()
    dispositions = example["review_cycle"]["dispositions"]
    assert dispositions
    state = {
        "head_sha": example["review_cycle"]["head_sha"],
        "fingerprint": example["review_cycle"]["fingerprint"],
        "completed": {"polly": True, "ocr": True},
        "feedback": [{"key": item["key"]} for item in dispositions],
    }
    cycle.validate(state, example)


def test_feature_agent_bundle_ships_the_shared_skills(tmp_path: Path) -> None:
    from omnigent.cli import _bundle
    from omnigent.spec.tar_utils import extract_safe

    extracted = tmp_path / "bundle"
    extract_safe(_bundle(_AGENT), extracted)
    bundled_skills = {skill.name for skill in load(extracted).skills}

    for name in _SHARED_SKILLS:
        assert (_AGENT / "skills" / name).resolve() == (_SHARED / name).resolve()
        assert name in bundled_skills
        for source in (_SHARED / name).rglob("*"):
            if source.is_file() and "__pycache__" not in source.parts:
                bundled = extracted / "skills" / name / source.relative_to(_SHARED / name)
                assert not bundled.is_symlink()
                assert bundled.read_bytes() == source.read_bytes()
