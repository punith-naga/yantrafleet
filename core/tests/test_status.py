from yantracore import CANONICAL, NOT_OPERATING, normalize


def test_canonical_passthrough():
    for s in CANONICAL:
        assert normalize(s) == s


def test_legacy_spellings():
    assert normalize("working") == "active"
    assert normalize("moving") == "active"
    assert normalize("to_charger") == "active"
    assert normalize("safety_stop") == "estop"
    assert normalize("estopped") == "estop"


def test_unknown_and_empty_are_idle():
    assert normalize(None) == "idle"
    assert normalize("") == "idle"
    assert normalize("summoning-demons") == "idle"


def test_not_operating_subset():
    assert NOT_OPERATING <= CANONICAL
