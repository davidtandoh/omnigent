"""Mobile form typography across chat, workspace, and dialogs in the built SPA."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from tests.browser_ui.files.test_file_line_navigation import BrowserSession, _mock_markdown_files
from tests.browser_ui.files.test_file_line_navigation import (
    seeded_session as seeded_session_fixture,
)

seeded_session = seeded_session_fixture

_FIELDS = (
    'input:not([type="hidden"]), textarea, select, '
    '[contenteditable]:not([contenteditable="false"])'
)


@pytest.mark.browser_context_args(is_mobile=True, has_touch=True)
@pytest.mark.parametrize(
    "viewport",
    [{"width": 390, "height": 844}, {"width": 844, "height": 390}],
    ids=["portrait", "landscape"],
)
def test_mobile_forms_avoid_focus_zoom(
    page: Page, seeded_session: BrowserSession, tmp_path: Path, viewport: dict[str, int]
) -> None:
    """Every visible form field stays above Safari's focus-zoom threshold."""
    page.set_viewport_size(viewport)
    base_url, sid = seeded_session
    seeded_session.contract.json(
        f"/v1/sessions/{sid}/resources/environments/default/search",
        {"object": "list", "data": [], "has_more": False},
    )
    metrics = {}
    for surface, path, field in (
        ("composer", f"/c/{sid}", 'textarea[placeholder="Send a message…"]'),
        ("workspace", f"/c/{sid}", 'input[aria-label="Search all files"]'),
        ("rename", f"/c/{sid}", '[data-testid="header-rename-conversation-input"]'),
    ):
        page.goto(base_url + path)
        if surface == "workspace":
            if viewport["width"] < 768:
                page.get_by_test_id("header-conversation-actions").click()
                page.get_by_role("menuitem", name="Files", exact=True).click()
            else:
                page.get_by_role("button", name="Close sidebar", exact=True).click()
                page.get_by_role("button", name="Expand right panel", exact=True).click()
                page.get_by_role("tab", name="Files", exact=True).click()
        if surface == "rename":
            page.get_by_test_id("header-conversation-actions").click()
            page.get_by_role("menuitem", name="Rename", exact=True).click()
        target = page.locator(field).filter(visible=True)
        expect(target).to_be_visible(timeout=30_000)
        target.tap(position={"x": 8, "y": 8})
        expect(target).to_be_focused()
        target.fill("Mobile typing")
        page.screenshot(path=tmp_path / f"{surface}.png")
        metrics[surface] = page.locator(_FIELDS).evaluate_all("""fields => fields
            .filter(el => el.checkVisibility())
            .map(el => ({
                field: el.getAttribute('aria-label') || el.dataset.testid || el.tagName,
                fontSize: parseFloat(getComputedStyle(el).fontSize),
                touchAction: getComputedStyle(el).touchAction,
            }))""")
    (tmp_path / "metrics.json").write_text(json.dumps(metrics, indent=2) + "\n")
    undersized = {
        surface: [field for field in fields if field["fontSize"] < 16]
        for surface, fields in metrics.items()
    }
    assert not any(undersized.values()), undersized
    assert all(
        field["touchAction"] == "manipulation" for fields in metrics.values() for field in fields
    ), metrics


@pytest.mark.parametrize(
    "touch",
    [
        pytest.param(True, marks=pytest.mark.browser_context_args(has_touch=True), id="touch"),
        pytest.param(False, marks=pytest.mark.browser_context_args(has_touch=False), id="mouse"),
    ],
)
@pytest.mark.parametrize("width", [390, 767, 768, 844])
@pytest.mark.parametrize("font_size", [11, 18])
def test_form_font_floor_cascade(
    page: Page, seeded_session: BrowserSession, width: int, font_size: int, touch: bool
) -> None:
    """The built CSS covers field kinds, portals and inline editor styles."""
    page.set_viewport_size({"width": width, "height": 844})
    page.add_init_script(f"localStorage.setItem('omnigent:ui-font-size', '{font_size}')")
    page.goto(seeded_session.contract.base_url + "/settings/appearance")
    expect(page.get_by_test_id("ui-font-family-input")).to_be_visible(timeout=30_000)
    page.evaluate("""() => {
        const form = document.createElement('div');
        form.id = 'form-font-floor';
        form.innerHTML = `
            <input type="text"><input type="search"><input type="password">
            <input type="email"><input type="url"><input type="tel">
            <input type="number"><input type="date"><input type="file">
            <textarea></textarea><select><option>Choice</option></select>
            <div contenteditable="true">Rich editor</div>
            <div contenteditable="">Editable title</div>
            <div contenteditable="plaintext-only">Plain editor</div>
            <button>Action</button><a href="#">Link</a>
            <div role="button">Custom action</div><div role="tab">Tab</div>
            <div role="menuitem">Menu item</div>
            <div contenteditable="false">Read only</div>`;
        // Editor libraries can set an inline font; portals sit outside AppShell.
        for (const el of form.children) el.style.fontSize = '12px';
        document.body.append(form);
    }""")
    fields = page.locator(f"#form-font-floor :is({_FIELDS})")
    assert fields.count() == 14
    sizes = fields.evaluate_all("els => els.map(el => parseFloat(getComputedStyle(el).fontSize))")
    assert page.evaluate("matchMedia('(hover: none) and (pointer: coarse)').matches") == touch
    expected_size = max(16, font_size) if touch else 12
    assert sizes == [expected_size] * 14
    controls = page.locator('#form-font-floor > :not([contenteditable="false"])')
    actions = controls.evaluate_all("els => els.map(el => getComputedStyle(el).touchAction)")
    assert actions == ["manipulation" if touch else "auto"] * controls.count()
    assert (
        page.locator('#form-font-floor [contenteditable="false"]').evaluate(
            "el => getComputedStyle(el).fontSize"
        )
        == "12px"
    )
    viewport = page.locator('meta[name="viewport"]').get_attribute("content") or ""
    assert "maximum-scale" not in viewport and "user-scalable=no" not in viewport


@pytest.mark.browser_context_args(is_mobile=True, has_touch=True)
@pytest.mark.parametrize("mode", ["source", "editor"])
def test_mobile_file_fields_avoid_focus_zoom(
    page: Page, seeded_session: BrowserSession, mode: str
) -> None:
    """File find and the rich text editor use the same mobile font floor."""
    page.set_viewport_size({"width": 390, "height": 844})
    path = "src/mobile.md"
    _mock_markdown_files(page, seeded_session, {path: "# Mobile file\n\nFind this paragraph."})
    preferences = json.dumps(json.dumps({"diffActive": False, "previewableViewMode": mode}))
    page.add_init_script(f"localStorage.setItem('omnigent:file-view-preferences', {preferences})")
    base_url, sid = seeded_session
    page.goto(f"{base_url}/c/{sid}?file={path}")
    viewer = page.locator('[data-testid="file-viewer"]:visible')
    expect(viewer).to_contain_text("Find this paragraph", timeout=30_000)
    if mode == "source":
        viewer.get_by_role("button", name=re.compile(r"^(More actions|View settings)$")).click()
        page.get_by_role("menuitem", name="Find in file", exact=True).click()
        field = viewer.get_by_placeholder("Find…")
        field.fill("paragraph")
    else:
        field = viewer.locator('[contenteditable="true"]')
    field.tap()
    expect(field).to_be_focused()
    assert field.evaluate("el => parseFloat(getComputedStyle(el).fontSize)") >= 16
    assert field.evaluate("el => getComputedStyle(el).touchAction") == "manipulation"


@pytest.mark.browser_context_args(is_mobile=True, has_touch=True)
@pytest.mark.parametrize("shell", ["ios", "android", None], ids=["ios", "android", "web"])
def test_native_portaled_field_preserves_typography(
    page: Page, seeded_session: BrowserSession, shell: str | None
) -> None:
    """Native dialogs keep their typography outside AppShell."""
    page.set_viewport_size({"width": 844, "height": 390})
    if shell is not None:
        page.add_init_script(
            "window.omnigentNative = {kind: " + json.dumps(shell)
            + ", setBadgeCount() {}, notify: async () => false}"
        )
    page.goto(seeded_session.contract.base_url + "/c/" + seeded_session.session_id)
    page.get_by_role("button", name="New project", exact=True).click()
    dialog = page.get_by_role("dialog", name="New project", exact=True)
    field = dialog.get_by_placeholder("Project name…")
    expect(field).to_be_visible()
    assert field.evaluate("el => el.closest('.app-shell') === null")
    expect(field).to_have_css("font-size", "13px" if shell else "16px")
    expect(field).to_have_css("touch-action", "auto" if shell else "manipulation")
    expect(dialog.get_by_role("button", name="Cancel", exact=True)).to_have_css(
        "touch-action", "auto" if shell else "manipulation"
    )
    field.fill("Native project")
    expect(field).to_have_value("Native project")
