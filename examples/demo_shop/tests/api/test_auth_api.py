"""Sign-in and tokens over the API."""

from __future__ import annotations

import pytest

from tests.framework.api.clients import OrdersApi
from tests.framework.data import PASSWORDS


def test_sign_in_returns_a_token(auth_api):
    res = auth_api.login("anna", PASSWORDS["anna"])
    assert res.status == 200
    body = res.json()
    assert body["token"].startswith("tok-")
    assert body["username"] == "anna"
    assert body["persona"] is None


@pytest.mark.parametrize("user,password", [("anna", "not-her-password"), ("nobody", "x"), ("", "")])
def test_sign_in_rejects_bad_credentials(auth_api, user, password):
    res = auth_api.login(user, password)
    assert res.status == 401
    assert "do not match" in res.json()["error"]


def test_locked_persona_is_refused(auth_api):
    res = auth_api.login("locked", PASSWORDS["locked"])
    assert res.status == 403
    assert res.json()["error"] == "Sorry, this user has been locked out"


@pytest.mark.parametrize("persona", ["standard", "problem", "error", "visual"])
def test_personas_are_named_in_the_sign_in_reply(auth_api, persona):
    res = auth_api.login(persona, PASSWORDS[persona])
    assert res.status == 200
    assert res.json()["persona"] == persona


def test_two_sign_ins_get_different_tokens(auth_api):
    first = auth_api.login("anna", PASSWORDS["anna"]).json()["token"]
    second = auth_api.login("anna", PASSWORDS["anna"]).json()["token"]
    assert first != second


def test_orders_need_a_token(session):
    res = OrdersApi(session).list()
    assert res.status == 401
    assert res.json()["error"] == "log in first"


def test_a_made_up_token_is_refused(session):
    res = OrdersApi(session.as_user("tok-made-up")).list()
    assert res.status == 401
