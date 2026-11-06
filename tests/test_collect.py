import json
from pathlib import Path

from tessella.collect import collect_cases, collect_flows
from tessella.collect.allure import READER as ALLURE
from tessella.collect.junit import READER as JUNIT

FIXTURES = Path(__file__).parent / "fixtures"


def test_junit_reads_only_failures():
    cases = JUNIT.read(FIXTURES / "junit_sample.xml", [])
    assert len(cases) == 3
    assert cases[0].title == "tests.test_orders::test_cancel_order_1"
    assert "500" in cases[0].message


def test_dispatch_picks_junit_reader():
    cases = collect_cases([str(FIXTURES / "junit_sample.xml")], [])
    assert len(cases) == 3


def test_allure_generated_report(tmp_path):
    tc = tmp_path / "data" / "test-cases"
    tc.mkdir(parents=True)
    (tc / "a.json").write_text(json.dumps({
        "name": "checkout", "fullName": "shop.CheckoutTest.checkout", "status": "failed",
        "statusDetails": {"message": "Expected total 10.00 but was 9.99", "trace": "at Checkout.java:31"},
    }))
    (tc / "b.json").write_text(json.dumps({"name": "ok", "status": "passed"}))
    assert ALLURE.can_read(tmp_path)
    cases = ALLURE.read(tmp_path, [])
    assert [c.title for c in cases] == ["shop.CheckoutTest.checkout"]


def test_har_becomes_flow_and_skips_assets(tmp_path):
    har = {"log": {"entries": [
        {"request": {"method": "POST", "url": "https://shop.local/api/orders", "headers": [
            {"name": "Authorization", "value": "Bearer abc"}], "postData": {"text": "{\"qty\": 1}"}},
         "response": {"status": 201, "content": {"mimeType": "application/json",
                                                 "text": "{\"id\": \"ord_8812abcd\"}"}}},
        {"request": {"method": "GET", "url": "https://shop.local/logo.png", "headers": []},
         "response": {"status": 200, "content": {"mimeType": "image/png"}}},
    ]}}
    p = tmp_path / "checkout.har"
    p.write_text(json.dumps(har))
    flows = collect_flows([str(p)], ["Authorization"])
    assert len(flows) == 1 and len(flows[0].steps) == 1
    assert flows[0].steps[0].headers["Authorization"] == "[REDACTED]"
