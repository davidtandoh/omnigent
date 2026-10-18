"""Browser-only mobile web composer input contracts."""

import pytest
from playwright.sync_api import Page, expect

from tests.browser_ui.chat._geometry_helpers import open_landing


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args):
    return {
        **browser_context_args,
        "viewport": {"width": 390, "height": 844},
        "has_touch": True,
        "is_mobile": True,
    }


def test_mobile_enter_inserts_newline_and_send_submits_once(
    page: Page, chat_session_contract
) -> None:
    page.goto(chat_session_contract.url)
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible()
    assert page.evaluate("matchMedia('(pointer: coarse)').matches")

    composer.fill("first line")
    composer.press("Enter")
    composer.type("second line")

    prompt = "first line\nsecond line"
    expect(composer).to_have_value(prompt)
    assert chat_session_contract.event_posts == []

    page.get_by_role("button", name="Send", exact=True).click()
    expect(composer).to_have_value("")
    expect(
        page.locator('[data-testid="message-bubble"][data-role="user"]', has_text="first line")
    ).to_have_count(1)
    assert len(chat_session_contract.event_posts) == 1
    event = chat_session_contract.event_posts[0]["body"]
    assert event["type"] == "message"
    assert event["data"]["content"] == [{"type": "input_text", "text": prompt}]


@pytest.mark.parametrize("font_size", [None, 11, 18], ids=["default", "small", "large"])
@pytest.mark.parametrize("surface", ["session", "landing"])
def test_mobile_web_composer_font_floor_preserves_desktop_and_zoom(
    page: Page, chat_session_contract, font_size: int | None, surface: str
) -> None:
    """Computed styles cover the iOS zoom trigger; WebKit does not emulate that zoom."""
    if font_size is not None:
        page.add_init_script(f"localStorage.setItem('omnigent:ui-font-size', '{font_size}')")
    if surface == "landing":
        open_landing(page, chat_session_contract, {"width": 390, "height": 844})
        composer = page.get_by_test_id("new-chat-landing-input")
    else:
        page.goto(chat_session_contract.url)
        composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible()
    expect(composer).to_have_css("font-size", f"{max(16, font_size or 14)}px")
    composer.tap(position={"x": 8, "y": 8})
    expect(composer).to_be_focused()
    page.keyboard.insert_text("Mobile web draft")
    page.set_viewport_size({"width": 390, "height": 500})
    expect(composer).to_be_focused()
    expect(composer).to_have_value("Mobile web draft")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")

    viewport = page.locator('meta[name="viewport"]').get_attribute("content") or ""
    assert "user-scalable=no" not in viewport.replace(" ", "").lower()
    assert "maximum-scale" not in viewport.lower()

    page.set_viewport_size({"width": 1440, "height": 900})
    expect(composer).to_have_css("font-size", f"{font_size or 13}px")
