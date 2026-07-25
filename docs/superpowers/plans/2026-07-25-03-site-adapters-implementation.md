# Seven-Brand Site Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement and verify JD, Tmall, and official-site querying for Xiaomi, HONOR, Huawei, Vivo, OPPO, Apple, and ZTE, including exact model/variant matching, approved price policies, valid “无” evidence, URLs, caching, and quotation-cell integration.

**Architecture:** Define one stable adapter protocol, reusable semantic matching and price extraction, channel-level adapters for JD/Tmall, and data-driven official-site specifications with brand-specific overrides. Test every business state against saved local HTML fixtures; use live sites only for explicit smoke/acceptance runs because selectors and risk controls are externally changeable.

**Tech Stack:** Python 3.12, Playwright synchronous API, Pillow, pytest, openpyxl, JSON site catalog.

## Global Constraints

- Supported brands are exactly 小米, HONOR, 华为, 维沃, 欧珀, 苹果, and ZTE中兴.
- Unsupported brands retain their quotation rows, skip AI:AN, and appear in the execution report.
- Query inputs are brand, market-common model name, RAM, storage, and color.
- Exact model matching is required; related or similar products must not be accepted.
- Target capacity and color must be explicitly selected; another variant price cannot substitute.
- JD and Tmall use the highest valid matching price.
- Xiaomi official uses the specified red selling price.
- HONOR, Huawei, Vivo, OPPO, and ZTE official use the lowest valid matching price.
- Apple official uses the highest valid matching price.
- Coupons, trade-in subsidies, installment amounts, and non-target variants are excluded.
- Business “无” requires the correct no-model, capacity-unavailable, or color-unavailable full-screen evidence.
- Technical failure leaves the quotation screenshot cell blank and records diagnostics.
- AI/AL are JD, AJ/AM are Tmall, and AK/AN are the official site.
- Minimum-price ties use JD, then Tmall, then official.
- Site changes must fail explicitly; never silently guess a price or element.
- Use TDD and commit after each task.

---

## Planned File Structure

```text
resources/sites/
  catalog.json
src/quote_app/sites/
  protocol.py
  catalog.py
  matching.py
  prices.py
  locators.py
  jd.py
  tmall.py
  official.py
  official_overrides/
    xiaomi.py
    apple.py
  registry.py
src/quote_app/services/
  web_pipeline.py
  web_to_excel.py
tests/
  conftest.py
  factories/
    web_run_factory.py
  fixtures/sites/
    jd/
    tmall/
    official/
  unit/
    test_site_catalog.py
    test_matching.py
    test_prices.py
    test_site_registry.py
  contract/
    test_jd_adapter.py
    test_tmall_adapter.py
    test_official_adapters.py
  integration/
    test_web_to_excel.py
docs/testing/
  live-site-matrix.md
```

## Task 1: Stable Site Adapter Protocol and Product Matching

**Files:**
- Modify: `pyproject.toml`
- Create: `src/quote_app/sites/protocol.py`
- Create: `src/quote_app/sites/matching.py`
- Create: `tests/unit/test_matching.py`

**Interfaces:**
- Consumes: `WebsiteTask`, `WebsiteResult`, `PlatformEvidenceCapture`.
- Produces: `SiteAdapter.execute(task, page, capture) -> WebsiteResult`, `normalize_product_text`, `model_matches`, `capacity_matches`, and `color_matches`.

- [ ] **Step 1: Write failing exact-matching tests**

```python
from quote_app.sites.matching import capacity_matches, color_matches, model_matches


def test_exact_model_allows_spacing_and_case_but_not_related_model() -> None:
    assert model_matches("HONOR 500", "honor500 12GB+256GB")
    assert not model_matches("HONOR 500", "HONOR 500 Pro 12GB+256GB")


def test_capacity_requires_ram_and_storage_when_both_are_supplied() -> None:
    assert capacity_matches("12GB+256GB", ram="12GB", storage="256GB")
    assert not capacity_matches("16GB+256GB", ram="12GB", storage="256GB")


def test_color_uses_normalized_exact_text() -> None:
    assert color_matches("雪山粉", " 雪山粉 ")
    assert not color_matches("雪山粉", "粉色")
```

- [ ] **Step 2: Run matching tests and verify module import failure**

Run: `python -m pytest tests/unit/test_matching.py -v`

Expected: FAIL during import.

- [ ] **Step 3: Implement normalized token matching and the adapter protocol**

```python
import re
import unicodedata


def normalize_product_text(value: str) -> str:
    text = unicodedata.normalize("NFKC", value).upper()
    return re.sub(r"[\s\u00a0]+", "", text)


def model_matches(target: str, candidate: str) -> bool:
    wanted = normalize_product_text(target)
    actual = normalize_product_text(candidate)
    if wanted not in actual:
        return False
    suffix = actual.split(wanted, 1)[1]
    return not suffix.startswith(("PRO", "PLUS", "ULTRA", "MAX", "SE"))


def capacity_matches(label: str, ram: str, storage: str) -> bool:
    normalized = normalize_product_text(label)
    return normalize_product_text(ram) in normalized and normalize_product_text(storage) in normalized


def color_matches(target: str, candidate: str) -> bool:
    return normalize_product_text(target) == normalize_product_text(candidate)
```

```python
from typing import Protocol
from playwright.sync_api import Page


class SiteAdapter(Protocol):
    channel: WebsiteChannel

    def execute(
        self,
        task: WebsiteTask,
        page: Page,
        capture: PlatformEvidenceCapture,
    ) -> WebsiteResult:
        raise NotImplementedError
```

- [ ] **Step 4: Run matching tests with Pro/Plus/Ultra and Chinese punctuation cases**

Run: `python -m pytest tests/unit/test_matching.py -v`

Expected: all tests PASS.

- [ ] **Step 5: Commit protocol and exact matching**

```bash
git add pyproject.toml src/quote_app/sites/protocol.py src/quote_app/sites/matching.py tests/unit/test_matching.py
git commit -m "feat: define exact product matching contract"
```

## Task 2: Price Parsing and Approved Selection Policies

**Files:**
- Create: `src/quote_app/sites/prices.py`
- Create: `tests/unit/test_prices.py`

**Interfaces:**
- Consumes: visible price candidates with text, surrounding text, visibility, and computed color.
- Produces: `parse_price`, `filter_valid_prices`, and `choose_price(candidates, policy)`.

- [ ] **Step 1: Write failing price-policy tests**

```python
from quote_app.sites.prices import PriceCandidate, PricePolicy, choose_price


def test_marketplace_uses_highest_non_installment_price() -> None:
    candidates = [
        PriceCandidate("¥4299", "销售价", True, "rgb(0, 0, 0)"),
        PriceCandidate("¥4499", "到手价", True, "rgb(0, 0, 0)"),
        PriceCandidate("¥187.46", "24期 每月", True, "rgb(0, 0, 0)"),
    ]
    assert choose_price(candidates, PricePolicy.HIGHEST) == 4499


def test_xiaomi_uses_visible_red_selling_price() -> None:
    candidates = [
        PriceCandidate("¥4299", "划线价", True, "rgb(128, 128, 128)"),
        PriceCandidate("¥4499", "销售价", True, "rgb(255, 0, 0)"),
    ]
    assert choose_price(candidates, PricePolicy.RED_SELLING) == 4499
```

- [ ] **Step 2: Run tests and verify price module import failure**

Run: `python -m pytest tests/unit/test_prices.py -v`

Expected: FAIL during import.

- [ ] **Step 3: Implement price parsing, exclusions, and policies**

```python
import re
from dataclasses import dataclass
from enum import StrEnum


class PricePolicy(StrEnum):
    LOWEST = "lowest"
    HIGHEST = "highest"
    RED_SELLING = "red_selling"


@dataclass(frozen=True, slots=True)
class PriceCandidate:
    text: str
    context: str
    visible: bool
    computed_color: str


EXCLUDED_CONTEXT = ("分期", "每月", "以旧换新", "补贴", "优惠券", "定金")


def parse_price(text: str) -> int | None:
    match = re.search(r"[¥￥]?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)", text)
    if match is None:
        return None
    return round(float(match.group(1).replace(",", "")))


def choose_price(candidates: list[PriceCandidate], policy: PricePolicy) -> int | None:
    valid = [
        (parse_price(candidate.text), candidate)
        for candidate in candidates
        if candidate.visible and not any(word in candidate.context for word in EXCLUDED_CONTEXT)
    ]
    parsed = [(price, candidate) for price, candidate in valid if price is not None]
    if policy is PricePolicy.RED_SELLING:
        parsed = [
            (price, candidate)
            for price, candidate in parsed
            if "255, 0, 0" in candidate.computed_color or "255, 51, 0" in candidate.computed_color
        ]
    if not parsed:
        return None
    values = [price for price, _ in parsed]
    return max(values) if policy in (PricePolicy.HIGHEST, PricePolicy.RED_SELLING) else min(values)
```

- [ ] **Step 4: Run tests for commas, decimals, hidden values, coupons, trade-in, and installments**

Run: `python -m pytest tests/unit/test_prices.py -v`

Expected: all tests PASS.

- [ ] **Step 5: Commit price selection policies**

```bash
git add src/quote_app/sites/prices.py tests/unit/test_prices.py
git commit -m "feat: implement approved channel price policies"
```

## Task 3: Site Catalog and Adapter Registry

**Files:**
- Create: `resources/sites/catalog.json`
- Create: `src/quote_app/sites/catalog.py`
- Create: `src/quote_app/sites/registry.py`
- Create: `tests/unit/test_site_catalog.py`
- Create: `tests/unit/test_site_registry.py`

**Interfaces:**
- Consumes: approved brand names and `WebsiteChannel`.
- Produces: `SiteSpec`, `load_site_catalog`, `adapter_for(brand, channel)`, and explicit `UnsupportedBrand`.

- [ ] **Step 1: Write failing completeness and unsupported-brand tests**

```python
from quote_app.sites.catalog import load_site_catalog
from quote_app.sites.registry import UnsupportedBrand, adapter_for
from quote_app.tasks.models import WebsiteChannel


def test_catalog_contains_all_21_brand_channel_pairs() -> None:
    catalog = load_site_catalog("resources/sites/catalog.json")
    pairs = {(item.brand, item.channel) for item in catalog}
    assert len(pairs) == 21


def test_unknown_brand_is_explicitly_unsupported() -> None:
    try:
        adapter_for("其他品牌", WebsiteChannel.JD)
    except UnsupportedBrand as exc:
        assert exc.brand == "其他品牌"
    else:
        raise AssertionError("unknown brand was accepted")
```

- [ ] **Step 2: Run tests and verify catalog/registry imports fail**

Run: `python -m pytest tests/unit/test_site_catalog.py tests/unit/test_site_registry.py -v`

Expected: FAIL during import.

- [ ] **Step 3: Add a complete catalog with channel entry points and policies**

`resources/sites/catalog.json` must contain one record per brand/channel using the exact approved direct-entry URLs from the quotation instructions:

```json
[
  {"brand":"小米","channel":"jd","entry_url":"https://mall.jd.com/index-1000004123.html?from=pc","store_name":"小米京东自营旗舰店","price_policy":"highest"},
  {"brand":"HONOR","channel":"jd","entry_url":"https://mall.jd.com/index-1000000904.html","store_name":"荣耀京东自营旗舰店","price_policy":"highest"},
  {"brand":"华为","channel":"jd","entry_url":"https://mall.jd.com/index-1000004259.html?from=pc","store_name":"华为京东自营官方旗舰店","price_policy":"highest"},
  {"brand":"维沃","channel":"jd","entry_url":"https://mall.jd.com/index-1000085868.html?from=pc","store_name":"vivo京东自营官方旗舰店","price_policy":"highest"},
  {"brand":"欧珀","channel":"jd","entry_url":"https://mall.jd.com/index-1000004065.html?from=pc","store_name":"OPPO京东自营官方旗舰店","price_policy":"highest"},
  {"brand":"苹果","channel":"jd","entry_url":"https://mall.jd.com/index-1000000127.html?from=pc","store_name":"Apple产品京东自营旗舰店","price_policy":"highest"},
  {"brand":"ZTE中兴","channel":"jd","entry_url":"https://mall.jd.com/index-1000001971.html?from=pc","store_name":"中兴京东自营官方旗舰店","price_policy":"highest"},
  {"brand":"小米","channel":"tmall","entry_url":"https://xiaomi.tmall.com/","store_name":"小米官方旗舰店","price_policy":"highest"},
  {"brand":"HONOR","channel":"tmall","entry_url":"https://hihonor.tmall.com/","store_name":"荣耀官方旗舰店","price_policy":"highest"},
  {"brand":"华为","channel":"tmall","entry_url":"https://huaweistore.tmall.com/","store_name":"华为官方旗舰店","price_policy":"highest"},
  {"brand":"维沃","channel":"tmall","entry_url":"https://vivo.tmall.com/","store_name":"vivo官方旗舰店","price_policy":"highest"},
  {"brand":"欧珀","channel":"tmall","entry_url":"https://oppo.tmall.com/","store_name":"OPPO官方旗舰店","price_policy":"highest"},
  {"brand":"苹果","channel":"tmall","entry_url":"https://apple.tmall.com/","store_name":"Apple Store官方旗舰店","price_policy":"highest"},
  {"brand":"ZTE中兴","channel":"tmall","entry_url":"https://zte.tmall.com/","store_name":"ZTE中兴官方旗舰店","price_policy":"highest"},
  {"brand":"小米","channel":"official","entry_url":"https://www.mi.com/shop","store_name":"小米商城","price_policy":"red_selling"},
  {"brand":"HONOR","channel":"official","entry_url":"https://www.honor.com/cn/shop/?cid=132355","store_name":"荣耀商城","price_policy":"lowest"},
  {"brand":"华为","channel":"official","entry_url":"https://www.vmall.com/","store_name":"华为商城","price_policy":"lowest"},
  {"brand":"维沃","channel":"official","entry_url":"https://shop.vivo.com.cn/","store_name":"vivo官方商城","price_policy":"lowest"},
  {"brand":"欧珀","channel":"official","entry_url":"https://www.opposhop.cn/cn/web/","store_name":"OPPO商城","price_policy":"lowest"},
  {"brand":"苹果","channel":"official","entry_url":"https://www.apple.com.cn/iphone/","store_name":"Apple iPhone","price_policy":"highest"},
  {"brand":"ZTE中兴","channel":"official","entry_url":"https://www.ztemall.com/","store_name":"中兴商城","price_policy":"lowest"}
]
```

The loader validates HTTPS, supported brands, unique pairs, all 21 pairs, and approved price policies before returning records.

- [ ] **Step 4: Run completeness, URL, duplicate, and policy tests**

Run: `python -m pytest tests/unit/test_site_catalog.py tests/unit/test_site_registry.py -v`

Expected: all tests PASS.

- [ ] **Step 5: Commit the site catalog and registry**

```bash
git add resources/sites src/quote_app/sites/catalog.py src/quote_app/sites/registry.py tests/unit/test_site_catalog.py tests/unit/test_site_registry.py
git commit -m "feat: register seven-brand website catalog"
```

## Task 4: JD Adapter Contract

**Files:**
- Create: `src/quote_app/sites/locators.py`
- Create: `src/quote_app/sites/jd.py`
- Modify: `tests/conftest.py`
- Create: `tests/fixtures/sites/jd/normal.html`
- Create: `tests/fixtures/sites/jd/no_model.html`
- Create: `tests/fixtures/sites/jd/capacity_disabled.html`
- Create: `tests/fixtures/sites/jd/color_disabled.html`
- Create: `tests/contract/test_jd_adapter.py`

**Interfaces:**
- Consumes: one JD `SiteSpec`, `WebsiteTask`, Playwright `Page`, evidence capture.
- Produces: `JDAdapter.execute` with `PRICE_FOUND`, `NO_MODEL`, `CAPACITY_UNAVAILABLE`, or `COLOR_UNAVAILABLE`.

- [ ] **Step 1: Save representative JD fixture pages and write four failing contract tests**

Each fixture must retain the semantic text and hierarchy needed for the adapter while removing scripts, analytics identifiers, personal data, and unrelated product blocks. `tests/conftest.py` provides `jd_adapter`, `jd_normal_page`, `xiaomi_task`, and `fake_capture` by opening the saved fixture through the shared local HTTP server. Tests assert:

```python
def test_jd_normal_uses_highest_valid_price(jd_adapter, jd_normal_page, xiaomi_task, fake_capture) -> None:
    result = jd_adapter.execute(xiaomi_task, jd_normal_page, fake_capture)
    assert result.outcome is BusinessOutcome.PRICE_FOUND
    assert result.price == 4499
    assert result.url is not None
    assert fake_capture.state is EvidenceState.NORMAL
```

Add equivalent assertions for no model, disabled capacity, and disabled color, including the expected red-frame target roles.

- [ ] **Step 2: Run JD contracts and verify adapter import failure**

Run: `python -m pytest tests/contract/test_jd_adapter.py -v`

Expected: FAIL during import.

- [ ] **Step 3: Implement layered semantic locators and the JD flow**

```python
SEARCH_INPUTS = (
    'input[placeholder*="搜索"]',
    'input[aria-label*="搜索"]',
    'input[type="search"]',
)

MODEL_NODES = (
    '[data-sku-name]',
    '.sku-name',
    '.p-name',
    'h1',
)

PRICE_NODES = (
    '[data-price]',
    '.p-price',
    '.summary-price',
    '[class*="price"]',
)
```

The flow must:

1. open the configured direct JD store URL;
2. verify the visible store name contains the catalog value;
3. search within that store for the target model;
4. collect visible exact-model candidates and reject Pro/Plus/Ultra mismatches;
5. open the exact product;
6. locate and select the exact RAM+storage option;
7. locate and select the exact color;
8. wait for price stability;
9. collect valid visible prices and choose the highest;
10. capture normal evidence or the correct red-framed valid-“无” evidence.

If none of the layered locators resolves to a unique business element, return technical failure `JD_LAYOUT_UNRECOGNIZED`.

- [ ] **Step 4: Run JD fixture contracts and one logged-out live smoke**

Run: `python -m pytest tests/contract/test_jd_adapter.py -v`

Expected: all fixture contracts PASS.

For the live smoke, use one supplied Xiaomi configuration, record the model, selected capacity/color, price, URL, screenshot path, browser-login state, and run time in `docs/testing/live-site-matrix.md`. Do not assert a fixed price because live prices change.

- [ ] **Step 5: Commit the JD adapter**

```bash
git add src/quote_app/sites/locators.py src/quote_app/sites/jd.py tests/fixtures/sites/jd tests/contract/test_jd_adapter.py docs/testing/live-site-matrix.md
git commit -m "feat: query JD official stores"
```

## Task 5: Tmall Adapter and Login Detection

**Files:**
- Create: `src/quote_app/sites/tmall.py`
- Modify: `tests/conftest.py`
- Create: `tests/fixtures/sites/tmall/normal.html`
- Create: `tests/fixtures/sites/tmall/login_required.html`
- Create: `tests/fixtures/sites/tmall/no_model.html`
- Create: `tests/fixtures/sites/tmall/capacity_disabled.html`
- Create: `tests/fixtures/sites/tmall/color_disabled.html`
- Create: `tests/contract/test_tmall_adapter.py`

**Interfaces:**
- Consumes: Tmall `SiteSpec`, `LoginGate`, `WebsiteTask`, page, evidence capture.
- Produces: `TmallAdapter.execute` or a `WAITING_FOR_LOGIN` event without consuming retry budget.

- [ ] **Step 1: Save Tmall fixtures and write failing login/business-state contracts**

```python
def test_tmall_login_page_requests_manual_login(tmall_adapter, login_page, xiaomi_task) -> None:
    result = tmall_adapter.execute(xiaomi_task, login_page, fake_capture)
    assert result.state is TaskState.WAITING_FOR_LOGIN
    assert result.error_code == "TMALL_LOGIN_REQUIRED"


def test_tmall_normal_uses_highest_valid_price(tmall_adapter, normal_page, xiaomi_task) -> None:
    result = tmall_adapter.execute(xiaomi_task, normal_page, fake_capture)
    assert result.outcome is BusinessOutcome.PRICE_FOUND
    assert result.price == 4499
```

- [ ] **Step 2: Run Tmall contracts and verify adapter import failure**

Run: `python -m pytest tests/contract/test_tmall_adapter.py -v`

Expected: FAIL during import.

- [ ] **Step 3: Implement login predicates and Tmall official-store flow**

Use these login predicates together:

```python
LOGIN_URL_PARTS = ("login.tmall.com", "login.taobao.com")
LOGIN_TEXT = ("密码登录", "扫码登录", "请登录")


def login_required(page) -> bool:
    return any(part in page.url for part in LOGIN_URL_PARTS) or any(
        page.get_by_text(text, exact=False).count() > 0 for text in LOGIN_TEXT
    )
```

After the login predicate passes, the Tmall flow must:

1. open the configured direct flagship-store URL;
2. verify the visible store name contains the catalog value;
3. search within the store for the target common model name;
4. retain only exact model matches and reject Pro/Plus/Ultra mismatches;
5. open the exact product page;
6. locate and select exact RAM+storage;
7. locate and select exact body color;
8. wait for stable visible price candidates;
9. remove installment, coupon, subsidy, trade-in, and non-target prices and choose the highest remaining value;
10. capture normal evidence or the correct red-framed no-model/capacity/color evidence.

A login page returns `WAITING_FOR_LOGIN`; a system-error page returns `TMALL_PAGE_ERROR`; neither is classified as business “无”. Failure to resolve a unique business element returns `TMALL_LAYOUT_UNRECOGNIZED`.

- [ ] **Step 4: Run fixture contracts and a persistent-profile login smoke**

Run: `python -m pytest tests/contract/test_tmall_adapter.py -v`

Expected: all fixture contracts PASS.

On the Mac test machine, manually log in once in the managed browser, close the application, reopen it, and confirm the login page is not shown for the same profile. Record the result in `docs/testing/live-site-matrix.md`.

- [ ] **Step 5: Commit the Tmall adapter**

```bash
git add src/quote_app/sites/tmall.py tests/fixtures/sites/tmall tests/contract/test_tmall_adapter.py docs/testing/live-site-matrix.md
git commit -m "feat: query Tmall flagship stores"
```

## Task 6: Seven Official-Site Adapters

**Files:**
- Create: `src/quote_app/sites/official.py`
- Create: `src/quote_app/sites/official_overrides/xiaomi.py`
- Create: `src/quote_app/sites/official_overrides/apple.py`
- Create: `tests/fixtures/sites/official/<brand>/normal.html` for all seven brands
- Create: `tests/fixtures/sites/official/<brand>/no_model.html` for all seven brands
- Create: `tests/fixtures/sites/official/<brand>/capacity_disabled.html` for all seven brands
- Create: `tests/fixtures/sites/official/<brand>/color_disabled.html` for all seven brands
- Modify: `tests/conftest.py`
- Create: `tests/contract/test_official_adapters.py`

**Interfaces:**
- Consumes: official `SiteSpec`, product task, page, evidence capture.
- Produces: one registered official adapter per supported brand.

- [ ] **Step 1: Write a parameterized 28-case contract test**

```python
import pytest


@pytest.mark.parametrize(
    ("brand", "fixture_state", "expected_outcome"),
    [
        (brand, state, outcome)
        for brand in ("小米", "HONOR", "华为", "维沃", "欧珀", "苹果", "ZTE中兴")
        for state, outcome in (
            ("normal", BusinessOutcome.PRICE_FOUND),
            ("no_model", BusinessOutcome.NO_MODEL),
            ("capacity_disabled", BusinessOutcome.CAPACITY_UNAVAILABLE),
            ("color_disabled", BusinessOutcome.COLOR_UNAVAILABLE),
        )
    ],
)
def test_official_brand_contract(brand, fixture_state, expected_outcome, official_case) -> None:
    adapter, task, page, capture = official_case(brand, fixture_state)
    result = adapter.execute(task, page, capture)
    assert result.outcome is expected_outcome
```

`tests/conftest.py` implements `official_case(brand, fixture_state)` by loading the matching saved fixture, constructing a `WebsiteTask` with the fixture's exact model/RAM/storage/color, resolving the registered official adapter, and returning a recording capture double.

- [ ] **Step 2: Run the contract and verify official adapters are missing**

Run: `python -m pytest tests/contract/test_official_adapters.py -v`

Expected: FAIL during import.

- [ ] **Step 3: Implement data-driven official flow and two explicit overrides**

`OfficialSiteAdapter` performs search, exact model, capacity, color, stable price, URL, and evidence using the site catalog and layered locators.

Brand policies:

```python
OFFICIAL_POLICIES = {
    "小米": PricePolicy.RED_SELLING,
    "HONOR": PricePolicy.LOWEST,
    "华为": PricePolicy.LOWEST,
    "维沃": PricePolicy.LOWEST,
    "欧珀": PricePolicy.LOWEST,
    "苹果": PricePolicy.HIGHEST,
    "ZTE中兴": PricePolicy.LOWEST,
}
```

Xiaomi override must filter price candidates by visible red computed color and selling-price context. Apple override must select the exact storage and color in Apple’s configuration sequence before collecting the highest full-device price. All other brands use the shared lowest-price flow with brand-specific locator lists stored in code.

Official-site capacity matching strategies are explicit:

```python
OFFICIAL_CAPACITY_STRATEGIES = {
    "小米": "ram_plus_storage",
    "HONOR": "ram_plus_storage",
    "华为": "ram_plus_storage_or_storage",
    "维沃": "ram_plus_storage",
    "欧珀": "ram_plus_storage",
    "苹果": "storage_only",
    "ZTE中兴": "ram_plus_storage",
}
```

Apple starts from the iPhone family page, finds the exact model, clicks the purchase action, then selects color and storage. Huawei first attempts the combined RAM+storage label and then permits an exact storage-only label as specified in the quotation instructions.

- [ ] **Step 4: Run 28 fixture cases and seven live normal-sale smokes**

Run: `python -m pytest tests/contract/test_official_adapters.py -v`

Expected: all 28 cases PASS.

Run one normal-sale live smoke for each official brand site. Record selected model, capacity, color, applied policy, price, URL, screenshot status, and any login/risk-control observation in `docs/testing/live-site-matrix.md`.

- [ ] **Step 5: Commit all official adapters**

```bash
git add src/quote_app/sites/official.py src/quote_app/sites/official_overrides tests/fixtures/sites/official tests/contract/test_official_adapters.py docs/testing/live-site-matrix.md
git commit -m "feat: query seven official brand stores"
```

## Task 7: Web Pipeline, Cache, and Excel Integration

**Files:**
- Create: `src/quote_app/services/web_pipeline.py`
- Create: `src/quote_app/services/web_to_excel.py`
- Create: `tests/factories/web_run_factory.py`
- Modify: `src/quote_app/excel/quote_writer.py`
- Modify: `src/quote_app/excel/report_writer.py`
- Create: `tests/integration/test_web_to_excel.py`

**Interfaces:**
- Consumes: associated `QuoteRow` records, adapter registry, task runner, `WebsiteResult` records.
- Produces: AI:AN values/images, AH/R/S results, per-channel report status, and same-run exact-query cache.

- [ ] **Step 1: Write a failing three-channel integration test**

```python
from openpyxl import load_workbook


from tests.factories.web_run_factory import run_three_channel_fixture


def test_three_channel_results_populate_price_url_minimum_and_images(tmp_path) -> None:
    web_run_result = run_three_channel_fixture(tmp_path)
    workbook = load_workbook(web_run_result.quote_path)
    sheet = workbook["5G手机"]
    assert sheet["AI2"].value == 4499
    assert sheet["AJ2"].value == 4399
    assert sheet["AK2"].value == 4399
    assert sheet["AH2"].value == 4399
    assert sheet["R2"].value == 4399
    assert sheet["S2"].value == "https://tmall.example/product"
    assert len(sheet._images) == 3
```

`run_three_channel_fixture` creates one associated Xiaomi row, three successful `WebsiteResult` records with prices 4499/4399/4399 and distinct evidence images, invokes `web_to_excel`, and returns its output paths.

- [ ] **Step 2: Run integration test and verify web pipeline is missing**

Run: `python -m pytest tests/integration/test_web_to_excel.py -v`

Expected: FAIL during import.

- [ ] **Step 3: Implement exact-query caching and standard Excel image insertion**

```python
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class QueryKey:
    brand: str
    channel: WebsiteChannel
    model_name: str
    ram: str
    storage: str
    color: str
```

Cache only within the current run and only for an identical `QueryKey`. Populate:

- AI/AJ/AK with numeric price or `无`;
- AL/AM/AN with standard openpyxl `Image` objects anchored to their cells;
- AH and R with the minimum numeric price;
- S with the corresponding URL using JD, Tmall, official tie order;
- AH/R/S with `无` when all three business results are `无`;
- blank price/screenshot for technical failure while preserving other successful channels;
- execution report statuses for success, legal “无”, waiting, failure, and unsupported.

Resize evidence without distortion to the approved row/cell box and set `image.anchor` to the correct AL/AM/AN cell.

- [ ] **Step 4: Run integration and complete regression suites**

Run: `python -m pytest tests/integration/test_web_to_excel.py tests/unit/test_quote_writer.py tests/unit/test_report_writer.py -v`

Expected: all tests PASS.

- [ ] **Step 5: Commit web-to-Excel integration**

```bash
git add src/quote_app/services/web_pipeline.py src/quote_app/services/web_to_excel.py src/quote_app/excel/quote_writer.py src/quote_app/excel/report_writer.py tests/factories/web_run_factory.py tests/integration/test_web_to_excel.py
git commit -m "feat: integrate site results into quotation output"
```

## Plan 3 Completion Gate

Run:

```bash
python -m pytest tests/unit tests/contract tests/integration/test_web_to_excel.py -v
python -m ruff check src tests
python -m mypy src/quote_app
```

Complete the live matrix for 21 brand/channel pairs:

- correct official store/site;
- exact model identification;
- target capacity and color;
- approved price policy;
- resulting URL;
- normal or legal-“无” screenshot;
- login/risk-control outcome;
- technical failure reason when applicable.

Then process the provided sample as a Mac pilot and manually compare AI:AN against the approved Word examples. Record every mismatch before proceeding to the desktop integration plan.

Commit:

```bash
git add docs/testing/live-site-matrix.md docs/testing/mac-site-pilot.md
git commit -m "test: record seven-brand site acceptance"
```
