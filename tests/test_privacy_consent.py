import hashlib
import json

import pytest

import config
from control_db import (
    AskCoachConsent,
    canonicalize_categories,
    is_consent_valid,
    utcnow,
)


def _consent(categories=None):
    categories = categories or config.CURRENT_ASK_COACH_DATA_CATEGORIES
    canonical, category_hash = canonicalize_categories(categories)
    return AskCoachConsent(
        user_id="00000000-0000-0000-0000-000000000001",
        consent_version=config.ASK_COACH_CONSENT_VERSION,
        provider=config.ASK_COACH_PROVIDER,
        data_categories_version=config.ASK_COACH_DATA_CATEGORIES_VERSION,
        data_categories_json=canonical,
        category_hash=category_hash,
        consented_at=utcnow(),
    )


def test_category_canonicalization_reorders_deduplicates_and_strips():
    first = canonicalize_categories([" Recovery.Metrics ", "program.active"])
    second = canonicalize_categories(
        ["program.active", "recovery.metrics", "recovery.metrics"]
    )
    assert first == second
    assert first[0] == '["program.active","recovery.metrics"]'
    assert first[1] == hashlib.sha256(first[0].encode()).hexdigest()


def test_empty_or_non_string_category_is_rejected():
    with pytest.raises(ValueError):
        canonicalize_categories([" "])
    with pytest.raises(TypeError):
        canonicalize_categories(["valid", 3])


def test_consent_integrity_and_current_configuration_are_required(monkeypatch):
    consent = _consent()
    assert is_consent_valid(consent)

    consent.provider = "Another provider"
    assert not is_consent_valid(consent)
    consent.provider = config.ASK_COACH_PROVIDER

    consent.data_categories_version = "old"
    assert not is_consent_valid(consent)
    consent.data_categories_version = config.ASK_COACH_DATA_CATEGORIES_VERSION

    consent.category_hash = "0" * 64
    assert not is_consent_valid(consent)


@pytest.mark.parametrize(
    "stored",
    [
        "{bad json",
        json.dumps({"not": "a list"}),
        json.dumps(["valid", 4]),
        json.dumps([]),
    ],
)
def test_malformed_or_mismatched_stored_categories_are_rejected(stored):
    consent = _consent()
    consent.data_categories_json = stored
    assert not is_consent_valid(consent)


def test_all_hash_comparisons_use_compare_digest(monkeypatch):
    consent = _consent()
    calls = []
    real = __import__("hmac").compare_digest
    monkeypatch.setattr(
        "control_db.hmac.compare_digest",
        lambda left, right: calls.append((left, right)) or real(left, right),
    )
    assert is_consent_valid(consent)
    assert len(calls) == 2
