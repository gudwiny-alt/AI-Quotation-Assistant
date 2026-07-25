from quote_app.core.normalization import normalize_brand, normalize_code


def test_code_normalization_preserves_digits_and_removes_noise() -> None:
    assert normalize_code(" 910200000041080\n") == "910200000041080"
    assert normalize_code(910200000041080) == "910200000041080"
    assert normalize_code(123.0) == "123"


def test_brand_aliases_use_approved_names() -> None:
    assert normalize_brand("荣耀") == "HONOR"
    assert normalize_brand(" vivo ") == "维沃"
    assert normalize_brand("OPPO") == "欧珀"
