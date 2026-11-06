from tessella.models import Flow, Step
from tessella.probe.linker import link
from tessella.probe.mutators import all_mutations
from tessella.probe.shrink import ddmin


def _flow():
    return Flow(name="checkout", steps=[
        Step(index=0, method="POST", url="https://shop.local/api/orders", body={"qty": 1},
             status=201, response={"data": {"id": "ord_8812abcd"}}),
        Step(index=1, method="POST", url="https://shop.local/api/orders/ord_8812abcd/pay",
             body={"orderId": "ord_8812abcd"}, status=200, response={"ok": True}),
    ])


def test_linker_turns_ids_into_variables():
    f = link(_flow())
    assert f.variables == {"id": "step0:$.data.id"}
    assert "{{id}}" in f.steps[1].url
    assert f.steps[1].body == {"orderId": "{{id}}"}


def test_mutations_cover_identity_order_and_concurrency():
    kinds = {m.mutator.split(".")[0] for m in all_mutations(
        _flow(), ["boundaries", "types", "identity", "order", "concurrency"], ["user_b"])}
    assert {"boundaries", "types", "identity", "order", "concurrency"} <= kinds


def test_ddmin_finds_minimal_failing_steps():
    steps = list(range(40))
    # the bug needs steps 3, 17 and 31 together
    minimal = ddmin(steps, lambda s: {3, 17, 31} <= set(s))
    assert sorted(minimal) == [3, 17, 31]
