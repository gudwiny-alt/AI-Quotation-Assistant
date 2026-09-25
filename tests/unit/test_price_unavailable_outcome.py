from dataclasses import replace
from decimal import Decimal

import pytest

from quote_app.evidence.geometry import CssRect
from quote_app.evidence.models import EvidenceState
from quote_app.evidence.semantic_state import VerifiedSemanticState
from quote_app.sites.protocol import AdapterObservation
from quote_app.tasks.models import BusinessOutcome


def test_unpublished_price_requires_status_evidence_and_no_numeric_price():
    state = VerifiedSemanticState(
        canonical_url='https://www.honor.com/cn/shop/product/100.html',
        brand='HONOR', model_name='荣耀Magic9 超能版', capacity='12GB+512GB', color='开场白',
        current_sku='not-applicable', region='not-applicable', stock_state='not-applicable',
        price=None, outcome=BusinessOutcome.PRICE_UNAVAILABLE,
        css_rectangles=(CssRect(10, 20, 100, 30, 'price_status'),),
    )
    observation = AdapterObservation(state.outcome, None, state.canonical_url, state.css_rectangles, state)
    assert observation.price is None
    assert EvidenceState.PRICE_UNAVAILABLE.value == state.outcome.value
    with pytest.raises(ValueError):
        replace(state, price=Decimal('999999'))
    with pytest.raises(ValueError):
        replace(state, css_rectangles=())
