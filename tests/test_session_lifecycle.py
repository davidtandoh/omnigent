"""Unit tests for shared session lifecycle display helpers."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

import pytest

from omnigent.session_lifecycle import (
    CLOSED_LABEL_KEY,
    CLOSED_LABEL_VALUE,
    CLOSED_TITLE_INFIX,
    has_closed_title_marker,
    is_session_closed,
    labels_with_closed_status,
    title_without_closed_marker,
)


def test_closed_session_constants() -> None:
    assert CLOSED_LABEL_KEY == "omnigent.closed"
    assert CLOSED_LABEL_VALUE == "true"
    assert CLOSED_TITLE_INFIX == ":closed:"


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        (None, None),
        ("", ""),
        ("researcher:auth", "researcher:auth"),
        ("researcher:auth:closed:conv_abc123", "researcher:auth"),
        (":closed:conv_abc123", ""),
        (
            "researcher:auth:closed:conv_abc123:closed:conv_def456",
            "researcher:auth",
        ),
    ],
)
def test_title_without_closed_marker(title: str | None, expected: str | None) -> None:
    assert title_without_closed_marker(title) == expected


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        (None, False),
        ("", False),
        ("researcher:auth", False),
        ("researcher:auth:closed:conv_abc123", True),
        (":closed:conv_abc123", True),
        (
            "researcher:auth:closed:conv_abc123:closed:conv_def456",
            True,
        ),
    ],
)
def test_has_closed_title_marker(title: str | None, expected: bool) -> None:
    assert has_closed_title_marker(title) is expected


@pytest.mark.parametrize(
    ("labels", "title", "expected"),
    [
        (None, None, {}),
        ({}, "", {}),
        ({"omnigent.wrapper": "codex"}, "researcher:auth", {"omnigent.wrapper": "codex"}),
        (
            {"omnigent.wrapper": "codex"},
            "researcher:auth:closed:conv_abc123",
            {"omnigent.wrapper": "codex", CLOSED_LABEL_KEY: CLOSED_LABEL_VALUE},
        ),
        (
            {CLOSED_LABEL_KEY: "false"},
            ":closed:conv_abc123",
            {CLOSED_LABEL_KEY: CLOSED_LABEL_VALUE},
        ),
    ],
)
def test_labels_with_closed_status(
    labels: Mapping[str, str] | None,
    title: str | None,
    expected: dict[str, str],
) -> None:
    result = labels_with_closed_status(labels, title)

    assert result == expected
    assert isinstance(result, dict)


def test_labels_with_closed_status_does_not_mutate_input_mapping() -> None:
    labels = {"omnigent.wrapper": "codex"}

    result = labels_with_closed_status(labels, "researcher:auth:closed:conv_abc123")

    assert result == {"omnigent.wrapper": "codex", CLOSED_LABEL_KEY: CLOSED_LABEL_VALUE}
    assert labels == {"omnigent.wrapper": "codex"}


def test_labels_with_closed_status_accepts_mapping_instances() -> None:
    labels = MappingProxyType({"omnigent.wrapper": "codex"})

    assert labels_with_closed_status(labels, "researcher:auth") == {
        "omnigent.wrapper": "codex",
    }


@pytest.mark.parametrize(
    ("labels", "title", "expected"),
    [
        (None, None, False),
        ({}, "", False),
        ({CLOSED_LABEL_KEY: CLOSED_LABEL_VALUE}, None, True),
        ({CLOSED_LABEL_KEY: "false"}, None, False),
        ({"omnigent.wrapper": "codex"}, "researcher:auth", False),
        ({"omnigent.wrapper": "codex"}, "researcher:auth:closed:conv_abc123", True),
        ({CLOSED_LABEL_KEY: "false"}, ":closed:conv_abc123", True),
    ],
)
def test_is_session_closed(
    labels: Mapping[str, str] | None,
    title: str | None,
    expected: bool,
) -> None:
    assert is_session_closed(labels, title) is expected
