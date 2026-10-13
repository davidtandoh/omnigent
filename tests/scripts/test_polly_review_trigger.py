"""Run Polly's command validation with real comment bodies in a shell."""

import ast
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.posix_only

_WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/polly-review.yml"


def test_review_identity_is_recorded_before_the_model_starts() -> None:
    steps = yaml.safe_load(_WORKFLOW.read_text())["jobs"]["review"]["steps"]
    record = next(step for step in steps if step.get("name") == "Record review target")
    context = next(step for step in steps if step.get("id") == "ctx")
    reviewer = next(step for step in steps if step.get("id") == "polly")
    publication = next(step for step in steps if step.get("id") == "publish")
    assert steps.index(context) < steps.index(record) < steps.index(reviewer)
    assert record["if"] == "steps.ctx.outcome == 'success'"
    # Runner-generated env headers survive a model timeout or summary failure.
    # Use the frozen diff's head, which can differ from the earlier duplicate check.
    assert record["env"]["PR_NUMBER"] == publication["env"]["PR_NUMBER"]
    assert record["env"]["HEAD_SHA"] == "${{ steps.ctx.outputs.head_sha }}"
    assert record["env"]["HEAD_SHA"] == publication["env"]["HEAD_SHA"]


@pytest.mark.parametrize(
    ("body", "eligible"),
    [
        pytest.param("/review", True, id="command"),
        pytest.param("  \t/review  ", True, id="leading-whitespace"),
        pytest.param("/review force", True, id="force"),
        pytest.param("Please check this.\n  /review force\nThanks!", True, id="later-line"),
        pytest.param("Please check this.\r\n/review\r\nThanks!", True, id="crlf"),
        pytest.param("/review\tforce", True, id="tab-separated"),
        pytest.param("Try /review later.", False, id="inline-mention"),
        pytest.param("Try `/review` later.", False, id="inline-code"),
        pytest.param(
            "## Polly AI Review\nUse `/review force` to review again.", False, id="bot-footer"
        ),
        pytest.param("> /review", False, id="quoted-command"),
        pytest.param("/reviewer", False, id="different-command"),
        pytest.param("/review-force", False, id="no-word-boundary"),
        pytest.param("/REVIEW", False, id="case-sensitive"),
        pytest.param("", False, id="empty"),
        pytest.param(" \n\t", False, id="whitespace"),
        pytest.param("$(touch injected) /review", False, id="shell-substitution"),
    ],
)
def test_comment_command_eligibility(tmp_path: Path, body: str, eligible: bool) -> None:
    workflow = yaml.safe_load(_WORKFLOW.read_text())
    step = next(s for s in workflow["jobs"]["trigger"]["steps"] if s.get("id") == "command")
    output = tmp_path / "github_output"
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", step["run"]],
        cwd=tmp_path,
        env={"PATH": os.defpath, "COMMENT_BODY": body, "GITHUB_OUTPUT": str(output)},
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert output.read_text() == f"eligible={str(eligible).lower()}\n"
    assert not (tmp_path / "injected").exists()


@pytest.mark.parametrize(
    ("force", "body", "skipped"),
    [("false", "", True), ("true", "", False), ("false", "/review force", False)],
    ids=["duplicate", "manual-force", "comment-force"],
)
def test_force_review_bypasses_duplicate_check(
    tmp_path: Path, force: str, body: str, skipped: bool
) -> None:
    workflow = yaml.safe_load(_WORKFLOW.read_text())
    step = next(s for s in workflow["jobs"]["review"]["steps"] if s.get("id") == "dupe")
    gh = tmp_path / "gh"
    gh.write_text(
        '#!/bin/sh\ncase "$*" in\n'
        "  *pulls*) echo test-sha ;;\n"
        '  *) echo "https://example.test/comment\t<!-- polly-skipped-sha: test-sha -->" ;;\n'
        "esac\n"
    )
    gh.chmod(0o755)
    output = tmp_path / "github_output"
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", step["run"].replace("/tmp/", f"{tmp_path}/")],
        cwd=tmp_path,
        env={
            "PATH": f"{tmp_path}{os.pathsep}{os.defpath}",
            "COMMENT_BODY": body,
            "FORCE_REVIEW": force,
            "GITHUB_OUTPUT": str(output),
            "REPO": "test/repo",
            "PR_NUMBER": "1",
        },
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert ("duplicate=true" in output.read_text()) is skipped


def _evaluate_expression(expression: str, context: dict[str, Any]) -> Any:
    expression = expression.removeprefix("${{").removesuffix("}}")
    expression = re.sub(
        r"'[^']*'|&&|\|\||!(?!=)",
        lambda match: {"&&": " and ", "||": " or ", "!": " not "}.get(match[0], match[0]),
        expression,
    )

    def evaluate(node: ast.AST) -> Any:
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            return {"true": True, "false": False, "null": None, **context}[node.id]
        if isinstance(node, ast.Attribute):
            return evaluate(node.value).get(node.attr, "")
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return not evaluate(node.operand)
        if isinstance(node, ast.BoolOp):
            value = evaluate(node.values[0])
            for operand in node.values[1:]:
                if (isinstance(node.op, ast.And) and value) or (
                    isinstance(node.op, ast.Or) and not value
                ):
                    value = evaluate(operand)
            return value
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            if isinstance(node.ops[0], ast.Eq):
                return evaluate(node.left) == evaluate(node.comparators[0])
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "cancelled"
            and not node.args
            and not node.keywords
        ):
            return context["cancelled"]
        raise AssertionError(f"Unsupported workflow expression: {ast.dump(node)}")

    return evaluate(ast.parse(expression.strip(), mode="eval").body)


def _render_expression(value: str, context: dict[str, Any]) -> str:
    return re.sub(
        r"\$\{\{(.*?)\}\}",
        lambda match: str(_evaluate_expression(match[1], context)),
        value,
    )


@pytest.mark.parametrize(
    "event,fork,draft,gate,eligible,cancelled,expected_gate,expected_review",
    [
        ("pull_request_target", False, False, "success", "", False, True, True),
        ("pull_request_target", True, False, "success", "", False, False, False),
        ("pull_request_target", False, True, "success", "", False, True, False),
        ("pull_request_target", False, False, "failure", "", False, True, False),
        ("workflow_dispatch", True, True, "skipped", "", False, False, True),
        ("issue_comment", True, False, "skipped", "true", False, False, True),
        ("issue_comment", True, False, "skipped", "false", False, False, False),
        ("pull_request", False, False, "success", "", False, False, False),
        ("workflow_dispatch", False, False, "skipped", "", True, False, False),
    ],
)
def test_automatic_review_runs_base_workflow_and_excludes_fork_prs(
    event, fork, draft, gate, eligible, cancelled, expected_gate, expected_review
):
    workflow = yaml.safe_load(_WORKFLOW.read_text())
    triggers = workflow.get("on", workflow.get(True))
    assert "pull_request" not in triggers
    assert triggers["pull_request_target"]["types"] == ["opened", "reopened", "ready_for_review"]
    context = {
        "github": {
            "event_name": event,
            "repository": "owner/repo",
            "sha": "dispatch-sha",
            "event": {
                "repository": {"default_branch": "main"},
                "pull_request": {
                    "draft": draft,
                    "head": {
                        "sha": "untrusted-head",
                        "ref": "untrusted-branch",
                        "repo": {"full_name": "fork/repo" if fork else "owner/repo"},
                    },
                },
            },
        },
        "needs": {
            "gate": {"result": gate},
            "trigger": {"result": "success", "outputs": {"eligible": eligible}},
        },
        "cancelled": cancelled,
    }
    jobs = workflow["jobs"]
    assert _evaluate_expression(jobs["gate"]["if"], context) is expected_gate
    assert _evaluate_expression(jobs["review"]["if"], context) is expected_review
    checkouts = [
        step
        for step in jobs["review"]["steps"]
        if step.get("uses", "").split("@")[0] == "actions/checkout"
    ]
    assert checkouts
    for checkout in checkouts:
        assert _render_expression(checkout["with"]["ref"], context) == (
            "dispatch-sha" if event == "workflow_dispatch" else "main"
        )


@pytest.mark.parametrize("outcome", ["success", "failure", "skipped", "cancelled"])
@pytest.mark.parametrize("pr,head", [("7", "frozen-sha"), ("9", "new-sha")])
def test_completion_receipt_identifies_the_published_revision(outcome, pr, head):
    steps = yaml.safe_load(_WORKFLOW.read_text())["jobs"]["review"]["steps"]
    receipt = next(s for s in steps if s.get("name") == "Upload Polly completion receipt")
    context = {
        "steps": {
            "publish": {"outcome": outcome},
            "pr": {"outputs": {"pr_number": pr}},
            "ctx": {"outputs": {"head_sha": head}},
        }
    }
    assert _evaluate_expression(receipt["if"], context) is (outcome == "success")
    assert _render_expression(receipt["with"]["name"], context) == f"polly-completed-{pr}-{head}"


@pytest.mark.parametrize(
    "expected_head,passes", [("old-head", False), ("new-head", True), ("", True)]
)
def test_requested_head_cannot_change_before_polly_reads_the_diff(tmp_path, expected_head, passes):
    workflow = yaml.safe_load(_WORKFLOW.read_text())
    step = next(s for s in workflow["jobs"]["review"]["steps"] if s.get("id") == "ctx")
    script = step["run"]
    marker = 'gh api "repos/${REPO}/compare/'
    assert marker in script, "diff-fetch command not found in ctx step"
    script = script[: script.index(marker)].replace("/tmp/", str(tmp_path) + "/")
    assert step["env"]["EXPECTED_HEAD"] == "${{ inputs.expected_head }}"
    jq = tmp_path / "jq"
    jq.write_text(
        '#!/bin/sh\ncase "$2" in .baseRefOid) echo base;; .headRefOid) echo new-head;; esac\n'
    )
    jq.chmod(0o755)
    gh = tmp_path / "gh"
    gh.write_text('#!/bin/sh\necho \'{"baseRefOid":"base","headRefOid":"new-head"}\'\n')
    gh.chmod(0o755)
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", script],
        env={
            **os.environ,
            "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
            "EXPECTED_HEAD": expected_head,
            "PR_NUMBER": "7",
            "REPO": "o/r",
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert (result.returncode == 0) is passes
    assert ("PR moved" in result.stdout) is not passes
