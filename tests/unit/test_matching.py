from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from quote_app.sites.matching import (
    capacity_matches,
    color_matches,
    model_matches,
    normalize_product_text,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ＨＯＮＯＲ　５００＋", "HONOR 500+"),
        ("honor\u00a0500+", "HONOR 500+"),
        ("  HONOR   500+  ", "HONOR 500+"),
    ],
)
def test_product_normalization_is_nfkc_case_insensitive_and_idempotent(
    raw: str,
    expected: str,
) -> None:
    normalized = normalize_product_text(raw)

    assert normalized == expected
    assert normalize_product_text(normalized) == expected


@pytest.mark.parametrize("invalid", [None, 12, "", " \u00a0 "])
def test_matching_rejects_non_text_and_blank_inputs_stably(invalid: object) -> None:
    assert not model_matches(invalid, "HONOR 500")  # type: ignore[arg-type]
    assert not model_matches("HONOR 500", invalid)  # type: ignore[arg-type]
    assert not capacity_matches(
        invalid, ram="12GB", storage="256GB"  # type: ignore[arg-type]
    )
    assert not color_matches(invalid, "雪山粉")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "candidate",
    [
        "HONOR500 12GB+256GB",
        "ＨＯＮＯＲ　５００（１２ＧＢ＋２５６ＧＢ）",
        "新品 HONOR 500 12GB+256GB",
    ],
)
def test_model_matches_spacing_case_nfkc_and_surrounding_product_details(
    candidate: str,
) -> None:
    assert model_matches("HONOR 500", candidate)


@pytest.mark.parametrize(
    "candidate",
    [
        "HONOR 500 Pro",
        "HONOR500PRO+",
        "HONOR500Pro＋",
        "HONOR 500 Plus",
        "HONOR 500 Ultra",
        "HONOR 500 Max",
        "HONOR 500 SE",
        "HONOR 500 (Pro)",
        "HONOR 500（Pro）",
        "HONOR 500-Pro",
        "HONOR 500—Pro",
        "HONOR 500, Pro",
        "HONOR 500，Pro",
    ],
)
def test_base_model_rejects_every_related_variant_even_after_punctuation(
    candidate: str,
) -> None:
    assert not model_matches("HONOR 500", candidate)


@pytest.mark.parametrize(
    ("target", "candidate"),
    [
        ("小米15", "小米150"),
        ("小米15", "小米151"),
        ("小米15", "小米15S"),
        ("HONOR500", "HONOR5000"),
        ("HONOR500", "XHONOR500"),
        ("HONOR500", "HONOR500X"),
    ],
)
def test_model_rejects_numeric_and_ascii_letter_boundary_collisions(
    target: str,
    candidate: str,
) -> None:
    assert not model_matches(target, candidate)


@pytest.mark.parametrize(
    "candidate",
    [
        "HONOR 500",
        "HONOR 500 Pro+",
        "HONOR 500 Plus",
        "HONOR 500 Ultra",
        "HONOR 500 Max",
        "HONOR 500 SE",
    ],
)
def test_target_pro_model_rejects_base_and_other_variants(candidate: str) -> None:
    assert not model_matches("HONOR 500 Pro", candidate)


def test_target_pro_plus_matches_only_pro_plus() -> None:
    assert model_matches("HONOR 500 Pro+", "HONOR500PRO＋ 12GB+256GB")
    assert not model_matches("HONOR 500 Pro+", "HONOR 500 Pro")
    assert not model_matches("HONOR 500 Pro+", "HONOR 500 Plus")


@pytest.mark.parametrize(
    ("target", "candidate"),
    [
        ("HONOR 500 Pro", "HONOR 500 Pro+ 12GB+256GB"),
        ("HONOR 500 Pro", "HONOR 500 Pro + 12GB+256GB"),
        ("HONOR 500 Pro", "HONOR 500 Pro   + 12GB+256GB"),
        ("HONOR 500 Pro", "HONOR 500 Pro　＋ 12GB+256GB"),
        ("HONOR 500", "HONOR 500+ 12GB+256GB"),
        ("HONOR 500", "HONOR 500 + 12GB+256GB"),
        ("HONOR 500", "HONOR 500   ＋ 12GB+256GB"),
    ],
)
def test_target_without_plus_rejects_plus_after_any_whitespace(
    target: str,
    candidate: str,
) -> None:
    assert not model_matches(target, candidate)


@pytest.mark.parametrize(
    "candidate",
    [
        "HONOR 500 Pro+ 12GB+256GB",
        "HONOR 500 Pro + 12GB+256GB",
        "HONOR 500 Pro   + 12GB+256GB",
        "HONOR 500 Pro　＋ 12GB+256GB",
    ],
)
def test_complete_pro_plus_target_allows_spacing_before_plus(candidate: str) -> None:
    assert model_matches("HONOR 500 Pro+", candidate)


@pytest.mark.parametrize(
    "variant",
    [
        "S",
        "E",
        "Lite",
        "mini",
        "GT",
        "Neo",
        "Air",
        "Edge",
        "FE",
        "青春版",
        "活力版",
        "竞速版",
        "至尊版",
    ],
)
def test_base_model_rejects_additional_english_and_chinese_variants(
    variant: str,
) -> None:
    assert not model_matches("HONOR 500", f"HONOR 500 {variant} 12GB+256GB")
    assert not model_matches("HONOR 500", f"HONOR 500（{variant}）")


@pytest.mark.parametrize(
    "target",
    [
        "HONOR 500 S",
        "HONOR 500 E",
        "HONOR 500 Lite",
        "HONOR 500 mini",
        "HONOR 500 GT",
        "HONOR 500 Neo",
        "HONOR 500 Air",
        "HONOR 500 Edge",
        "HONOR 500 FE",
        "HONOR 500 青春版",
        "HONOR 500 活力版",
        "HONOR 500 竞速版",
        "HONOR 500 至尊版",
    ],
)
def test_explicit_additional_variant_target_matches_itself(target: str) -> None:
    assert model_matches(target, f"新品 {target} 12GB+256GB")


def test_nested_variant_requires_the_complete_target_model() -> None:
    candidate = "vivo X200 Pro mini 12GB+256GB"

    assert not model_matches("vivo X200 Pro", candidate)
    assert model_matches("vivo X200 Pro mini", candidate)


@pytest.mark.parametrize(
    ("base", "complete"),
    [
        ("Magic7", "Magic7 RSR保时捷设计"),
        ("Mate70", "Mate70 RS非凡大师"),
        ("HONOR400", "HONOR400 Smart 5G"),
        ("小米15", "小米15定制版"),
    ],
)
def test_base_model_rejects_unlisted_commercial_suffix_but_complete_target_matches(
    base: str,
    complete: str,
) -> None:
    candidate = f"{complete} 12GB+256GB"

    assert not model_matches(base, candidate)
    assert model_matches(complete, candidate)


@pytest.mark.parametrize(
    "unknown_suffix",
    [
        "TURBO",
        "FUTURE",
        "X",
        "商务版",
        "电竞版",
        "影像版",
        "卫星通信版",
    ],
)
def test_unknown_ascii_or_chinese_model_suffix_fails_closed(
    unknown_suffix: str,
) -> None:
    assert not model_matches(
        "HONOR 500",
        f"HONOR 500 {unknown_suffix} 12GB+256GB",
    )


@pytest.mark.parametrize(
    "candidate",
    [
        "HONOR 500 12GB+256GB",
        "HONOR 500 5G 12GB+256GB",
        "HONOR 500 5G手机 12GB+256GB",
        "新品 HONOR 500 官方正品 12GB+256GB",
    ],
)
def test_exact_model_allows_only_capacity_network_and_generic_descriptions(
    candidate: str,
) -> None:
    assert model_matches("HONOR 500", candidate)


@pytest.mark.parametrize(
    "candidate",
    [
        "HONOR 500 RAM:12GB/ROM:256GB",
        "HONOR 500 RAM：12GB，STORAGE：256GB",
        "HONOR 500 12GB RAM + 256GB ROM",
        "HONOR 500 运行内存：12GB；存储：256GB",
    ],
)
def test_model_remainder_allows_capacity_role_metadata(candidate: str) -> None:
    assert model_matches("HONOR 500", candidate)


def test_model_remainder_rejects_unknown_word_after_role_annotated_capacity() -> None:
    assert not model_matches(
        "HONOR 500",
        "HONOR 500 RAM:12GB/ROM:256GB TURBO",
    )


@pytest.mark.parametrize("prefix", ["XIAOMI", "REDMI", "红米"])
def test_xiaomi_family_brand_prefix_can_precede_complete_target(prefix: str) -> None:
    assert model_matches("小米15", f"{prefix} 小米15 12GB+256GB")


def test_zte_corporate_brand_prefix_can_precede_complete_target() -> None:
    assert model_matches("ZTE A31", "中兴通讯 ZTE A31 8GB+128GB")


def test_model_matching_text_must_not_mix_in_color_options() -> None:
    assert not model_matches("HONOR 500", "HONOR 500 12GB+256GB 雪山粉")


@pytest.mark.parametrize(
    "candidate",
    [
        "HONOR500/HONOR500Pro",
        "HONOR500，HONOR500",
        "HONOR500 Pro手机壳",
        "适用 HONOR500 保护壳",
        "HONOR500 钢化膜",
    ],
)
def test_model_fails_closed_for_multiple_models_repetition_and_accessories(
    candidate: str,
) -> None:
    assert not model_matches("HONOR 500", candidate)


@pytest.mark.parametrize(
    "accessory",
    [
        "CASE",
        "COVER",
        "PROTECTOR",
        "FILM",
        "CHARGER",
        "CABLE",
        "HEADPHONES",
        "EARBUDS",
        "ACCESSORY",
        "支架",
    ],
)
def test_model_rejects_english_and_chinese_accessories(accessory: str) -> None:
    assert not model_matches("HONOR 500", f"HONOR 500 {accessory}")


@pytest.mark.parametrize(
    "label",
    [
        "12GB+256GB",
        "１２ＧＢ＋２５６ＧＢ",
        "12GB\u00a0/\u00a0256GB",
        "12GB，256GB",
        "12GB、256GB",
        "RAM 12GB / ROM 256GB",
        "运行内存12GB，存储256GB",
        "RAM:12GB / ROM:256GB",
        "RAM：12GB，ROM：256GB",
        "运行内存：12GB；存储：256GB",
        "12GB RAM + 256GB ROM",
        "RAM:12GB / STORAGE:256GB",
        "12GB RAM + 256GB STORAGE",
    ],
)
def test_capacity_accepts_one_explicit_ram_then_storage_pair(label: str) -> None:
    assert capacity_matches(label, ram="12GB", storage="256GB")


@pytest.mark.parametrize(
    "label",
    [
        "256GB+12GB",
        "ROM 12GB / RAM 256GB",
        "存储12GB，运行内存256GB",
        "ROM:12GB / RAM:256GB",
        "12GB ROM + 256GB RAM",
        "RAM:12GB / RAM:256GB",
        "ROM:12GB / ROM:256GB",
        "STORAGE:12GB / RAM:256GB",
        "12GB STORAGE + 256GB RAM",
        "112GB+256GB",
        "12.5GB+256GB",
        "12GB+1256GB",
        "12GB+2560GB",
        "16GB+256GB",
        "12GB+512GB",
        "12GB+256GB/512GB",
        "12GB+256GB（另有16GB）",
        "8GB/12GB+256GB",
        "12G+256GB",
        "12GB+256G",
    ],
)
def test_capacity_rejects_wrong_roles_boundaries_multiple_values_and_units(
    label: str,
) -> None:
    assert not capacity_matches(label, ram="12GB", storage="256GB")


@pytest.mark.parametrize(
    ("ram", "storage"),
    [
        ("", "256GB"),
        ("12GB", ""),
        ("12G", "256GB"),
        ("12GB", "256G"),
        (None, "256GB"),
        ("12GB", None),
        (12, "256GB"),
        ("12GB", 256),
    ],
)
def test_capacity_rejects_invalid_requested_values(
    ram: object,
    storage: object,
) -> None:
    assert not capacity_matches(
        "12GB+256GB",
        ram=ram,  # type: ignore[arg-type]
        storage=storage,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize(
    ("target", "candidate"),
    [
        ("雪山粉", " 雪 山 粉 "),
        ("Titanium Black", "titanium\u00a0black"),
        ("Ｔｉｔａｎｉｕｍ　Ｂｌａｃｋ", "titanium black"),
    ],
)
def test_color_matches_only_after_nfkc_case_and_whitespace_normalization(
    target: str,
    candidate: str,
) -> None:
    assert color_matches(target, candidate)


@pytest.mark.parametrize(
    ("target", "candidate"),
    [
        ("雪山粉", "粉色"),
        ("雪山粉", "粉"),
        ("雪山粉", "雪山粉色"),
        ("雪山粉", "雪山粉（限定版）"),
        ("雪山粉", "雪山粉/白色"),
        ("黑色", "曜石黑"),
        ("雪山粉", "雪山-粉"),
    ],
)
def test_color_rejects_substrings_qualifiers_lists_and_similar_names(
    target: str,
    candidate: str,
) -> None:
    assert not color_matches(target, candidate)


def test_site_adapter_protocol_has_static_positive_and_negative_examples(
    tmp_path: Path,
) -> None:
    project_src = Path(__file__).parents[2] / "src"
    source = tmp_path / "protocol_contract.py"
    source.write_text(
        """
from quote_app.evidence.platform import PlatformEvidenceCapture
from quote_app.sites.protocol import BrowserPage, SiteAdapter
from quote_app.tasks.models import WebsiteChannel, WebsiteResult, WebsiteTask

class GoodAdapter:
    channel: WebsiteChannel = WebsiteChannel.JD
    def execute(
        self,
        task: WebsiteTask,
        page: BrowserPage,
        capture: PlatformEvidenceCapture,
    ) -> WebsiteResult:
        raise NotImplementedError

class MissingExecute:
    channel: WebsiteChannel = WebsiteChannel.JD

class WrongReturn:
    channel: WebsiteChannel = WebsiteChannel.JD
    def execute(
        self,
        task: WebsiteTask,
        page: BrowserPage,
        capture: PlatformEvidenceCapture,
    ) -> str:
        return "wrong"

good: SiteAdapter = GoodAdapter()
missing: SiteAdapter = MissingExecute()
wrong: SiteAdapter = WrongReturn()
""",
        encoding="utf-8",
    )

    environment = os.environ.copy()
    environment["MYPYPATH"] = str(project_src)
    completed = subprocess.run(
        [sys.executable, "-m", "mypy", "--no-error-summary", str(source)],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert completed.returncode == 1
    assert 'Incompatible types in assignment (expression has type "MissingExecute"' in (
        completed.stdout
    )
    assert 'Incompatible types in assignment (expression has type "WrongReturn"' in (
        completed.stdout
    )
    assert '"GoodAdapter"' not in completed.stdout


def test_importing_protocol_does_not_import_playwright_or_start_adapters() -> None:
    project_src = Path(__file__).parents[2] / "src"
    script = """
import importlib.abc
import sys

class BlockPlaywright(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname == "playwright" or fullname.startswith("playwright."):
            raise RuntimeError("playwright import is forbidden")
        return None

sys.meta_path.insert(0, BlockPlaywright())
import quote_app.sites.protocol
assert not any(name.startswith("playwright") for name in sys.modules)
assert "quote_app.sites.jd" not in sys.modules
assert "quote_app.sites.tmall" not in sys.modules
assert "quote_app.sites.official" not in sys.modules
"""
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(project_src)

    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert completed.returncode == 0, completed.stderr
