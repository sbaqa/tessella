import base64
import io
import json
import zipfile

import pytest

from tessella.collect.allure import READER as ALLURE
from tessella.collect.playwright_html import READER as PLAYWRIGHT
from tessella.collect.pytest_html import READER as PYTEST_HTML
from tessella.draft.ticket import ticket
from tessella.models import Case, Category, Evidence, EvidenceKind, Origin, Verdict

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def test_allure_finds_screenshots_in_nested_steps(tmp_path):
    (tmp_path / "data" / "attachments").mkdir(parents=True)
    (tmp_path / "data" / "attachments" / "shot.png").write_bytes(PNG_1PX)
    tc = tmp_path / "data" / "test-cases"
    tc.mkdir(parents=True)
    (tc / "a.json").write_text(json.dumps({
        "fullName": "shop.Checkout.pay", "status": "failed",
        "statusDetails": {"message": "Pay button not clickable"},
        "testStage": {"steps": [{"name": "click pay", "attachments": [
            {"name": "failure", "source": "shot.png", "type": "image/png"},
            {"name": "log", "source": "log.txt", "type": "text/plain"}]}]},
    }))
    [case] = ALLURE.read(tmp_path, [])
    assert case.screenshots == [str(tmp_path / "data" / "attachments" / "shot.png")]


def test_playwright_html_report_screenshots(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "abc.png").write_bytes(PNG_1PX)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("1.json", json.dumps({"tests": [{
            "title": "can pay", "path": ["checkout.spec.ts"], "outcome": "unexpected",
            "results": [{"errors": ["Error: locator.click: Timeout 5000ms"],
                         "attachments": [{"name": "screenshot", "contentType": "image/png",
                                          "path": "data/abc.png"}]}],
        }]}))
    encoded = base64.b64encode(buf.getvalue()).decode()
    (tmp_path / "index.html").write_text(
        f'<script>window.playwrightReportBase64 = "data:application/zip;base64,{encoded}";</script>')
    assert PLAYWRIGHT.can_read(tmp_path)
    [case] = PLAYWRIGHT.read(tmp_path, [])
    assert case.screenshots == [str(tmp_path / "data" / "abc.png")]


def test_pytest_html_embedded_screenshot_is_written_out(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    blob = {"tests": {"tests/test_ui.py::test_pay": [{
        "result": "Failed", "log": "E   AssertionError: not paid",
        "extras": [{"format_type": "image", "extension": "png",
                    "content": base64.b64encode(PNG_1PX).decode()}]}]}}
    attr = json.dumps(blob).replace('"', "&quot;")
    report = tmp_path / "report.html"
    report.write_text(f'<div id="data-container" data-jsonblob="{attr}"></div>')
    [case] = PYTEST_HTML.read(report, [])
    assert len(case.screenshots) == 1
    assert (tmp_path / case.screenshots[0]).read_bytes() == PNG_1PX


def test_visual_diff_finds_the_changed_region(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    from tessella.investigate.visual import diff

    green = tmp_path / "green.png"
    red = tmp_path / "red.png"
    Image.new("RGB", (200, 100), "white").save(green)
    img = Image.new("RGB", (200, 100), "white")
    img.paste((0, 0, 0), (150, 10, 190, 40))  # a dark block, like an unexpected dialog
    img.save(red)

    same = diff(green, green, out_dir=tmp_path)
    assert "not visual" in same.summary

    ev = diff(red, green, out_dir=tmp_path)
    assert ev.kind == EvidenceKind.visual_diff
    assert "x 150-190" in ev.summary and "y 10-40" in ev.summary
    assert ev.image and (tmp_path / "red-diff.png").is_file()


def test_ticket_lists_screenshots():
    case = Case(id="c", origin=Origin.test_failure, title="t", message="boom",
                screenshots=["shots/fail.png"],
                evidence=[Evidence(kind=EvidenceKind.visual_diff, summary="3% changed",
                                   image="diffs/fail-diff.png")],
                verdict=Verdict(category=Category.product_bug, confidence=0.8,
                                root_cause="Dialog covers the Pay button", evidence_used=[0]))
    body = ticket(case).body
    assert "shots/fail.png" in body and "diffs/fail-diff.png" in body
