from pathlib import Path
from tessella.cluster.signature import cluster, normalize
from tessella.collect.junit import READER as JUNIT

FIXTURES = Path(__file__).parent / "fixtures"


def test_normalize_removes_ids_and_numbers():
    a = normalize("expected status 200, got 500 for order 8812")
    b = normalize("expected status 200, got 500 for order 9931")
    assert a == b


def test_same_problem_different_data_is_one_cluster():
    cases = JUNIT.read(FIXTURES / "junit_sample.xml", [])
    groups = cluster(cases)
    assert len(groups) == 2  # two cancel-order failures + one login timeout
    assert sorted(len(m) for m in groups.values()) == [1, 2]
