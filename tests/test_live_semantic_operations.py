from __future__ import annotations

import pytest

from issue_graphrag.live.extraction import LLMExtractor
from issue_graphrag.live.semantic_operations import (
    BatchPolicy,
    ExtractionCache,
    ExtractionIdentity,
    QuotaExceeded,
    QuotaLedger,
    QuotaPolicy,
    SemanticBatchRunner,
)
from issue_graphrag.llm.client import CompletionMetadata, StructuredCompletion
from issue_graphrag.models import TextUnit

NOW = "2026-08-24T00:00:00Z"


class FakeStructuredClient:
    model = "google/gemini-3.1-flash-lite"

    def __init__(self):
        self.calls = []

    def complete_structured(self, prompt, **kwargs):  # noqa: ANN001
        self.calls.append((prompt, kwargs))
        sequence = len(self.calls)
        return StructuredCompletion(
            content=(
                '{"entities":[{"name":"Entity %d","type":"CONCEPT",'
                '"description":"grounded"}],"relationships":[]}' % sequence
            ),
            metadata=CompletionMetadata(
                requested_model=self.model,
                actual_model=f"{self.model}-actual",
                provider="Google",
                generation_id=f"gen-{sequence}",
                input_tokens=100,
                output_tokens=20,
                cost_usd=0.0001,
            ),
        )


def units(count: int) -> list[TextUnit]:
    return [
        TextUnit(id=f"unit-{index}", document_id="doc", text=f"text {index}", order=index)
        for index in range(count)
    ]


def runner(tmp_path, client, *, calls=12, quota=None):  # noqa: ANN001
    return SemanticBatchRunner(
        repo="owner/repo",
        extractor=LLMExtractor(client),
        cache=ExtractionCache(tmp_path / "extraction_cache.sqlite"),
        quota=quota or QuotaLedger(tmp_path / "llm_operations.sqlite"),
        batch_policy=BatchPolicy(
            max_calls=calls,
            max_input_tokens=100_000,
            max_output_tokens=100_000,
            max_output_tokens_per_call=800,
        ),
    )


def test_cache_lookup_namespace_uses_requested_identity_not_response_metadata(tmp_path):
    client = FakeStructuredClient()
    first_runner = runner(tmp_path, client)
    batch = first_runner.run_batch(
        content_signature="content-a",
        units=units(1),
        next_unit_index=0,
        bootstrap=False,
        now=NOW,
    )
    assert batch.complete
    assert len(client.calls) == 1

    # Reconstruct every runtime object: the result must survive a restart.
    second_runner = SemanticBatchRunner(
        repo="owner/repo",
        extractor=LLMExtractor(client),
        cache=ExtractionCache(tmp_path / "extraction_cache.sqlite"),
        quota=QuotaLedger(
            tmp_path / "llm_operations.sqlite",
            QuotaPolicy(daily_calls=0),
        ),
    )
    second = second_runner.run_batch(
        content_signature="content-a",
        units=units(1),
        next_unit_index=0,
        bootstrap=False,
        now=NOW,
    )
    assert second.complete and second.cache_hits == 1
    assert len(client.calls) == 1

    different_content = runner(tmp_path, client).run_batch(
        content_signature="content-b",
        units=units(1),
        next_unit_index=0,
        bootstrap=False,
        now=NOW,
    )
    assert different_content.complete
    assert len(client.calls) == 2

    identity = ExtractionIdentity(
        content_signature="content-a",
        gateway="openrouter",
        requested_model=client.model,
    )
    cached = ExtractionCache(tmp_path / "extraction_cache.sqlite").get(identity, units(1)[0])
    assert cached is not None
    assert cached.metadata.actual_model.endswith("-actual")
    assert cached.metadata.generation_id == "gen-1"


def test_every_semantic_identity_dimension_creates_a_distinct_cache_namespace():
    base = ExtractionIdentity("content", "openrouter", "requested-model")
    alternatives = [
        ExtractionIdentity("other-content", "openrouter", "requested-model"),
        ExtractionIdentity("content", "other-gateway", "requested-model"),
        ExtractionIdentity("content", "openrouter", "other-model"),
        ExtractionIdentity(
            "content", "openrouter", "requested-model", prompt_version="other-version"
        ),
        ExtractionIdentity(
            "content", "openrouter", "requested-model", prompt_sha256="other-hash"
        ),
        ExtractionIdentity(
            "content",
            "openrouter",
            "requested-model",
            extraction_schema_version="other-schema",
        ),
    ]

    assert len({base.cache_key, *(identity.cache_key for identity in alternatives)}) == 7


def test_long_document_resumes_across_batches_and_only_publishes_complete_result(tmp_path):
    client = FakeStructuredClient()
    document_units = units(5)
    cursor = 0
    outcomes = []

    for _ in range(3):
        outcome = runner(tmp_path, client, calls=2).run_batch(
            content_signature="long-content",
            units=document_units,
            next_unit_index=cursor,
            bootstrap=False,
            now=NOW,
        )
        outcomes.append(outcome)
        cursor = outcome.next_unit_index

    assert [outcome.next_unit_index for outcome in outcomes] == [2, 4, 5]
    assert [outcome.complete for outcome in outcomes] == [False, False, True]
    assert outcomes[0].result is None and outcomes[1].result is None
    assert outcomes[2].result is not None
    assert len(outcomes[2].result.entities) == 5
    assert len(client.calls) == 5


def test_crash_after_cache_write_before_cursor_update_does_not_repeat_provider_call(tmp_path):
    client = FakeStructuredClient()
    first = runner(tmp_path, client)

    def crash(_cursor):  # noqa: ANN001
        raise OSError("cursor fsync failed")

    with pytest.raises(OSError, match="cursor fsync failed"):
        first.run_batch(
            content_signature="crash-content",
            units=units(1),
            next_unit_index=0,
            bootstrap=False,
            now=NOW,
            on_advance=crash,
        )
    assert len(client.calls) == 1

    recovered = runner(tmp_path, client).run_batch(
        content_signature="crash-content",
        units=units(1),
        next_unit_index=0,
        bootstrap=False,
        now=NOW,
    )
    assert recovered.complete and recovered.cache_hits == 1
    assert len(client.calls) == 1


def test_global_daily_call_cap_is_atomic_across_repositories(tmp_path):
    path = tmp_path / "llm_operations.sqlite"
    policy = QuotaPolicy(
            daily_calls=1,
            daily_input_tokens=1_000_000,
            daily_output_tokens=1_000_000,
            monthly_cost_usd=100,
            bootstrap_calls=100,
            bootstrap_input_tokens=1_000_000,
            bootstrap_output_tokens=1_000_000,
    )
    ledger = QuotaLedger(path, policy)
    identity = ExtractionIdentity("sig", "openrouter", "model")
    unit = units(1)[0]
    reservation = ledger.reserve(
        repo="owner/one",
        identity=identity,
        unit=unit,
        estimated_input_tokens=10,
        max_output_tokens=10,
        bootstrap=False,
        now=NOW,
    )
    ledger.settle(
        reservation,
        CompletionMetadata("model", "actual", "provider", "gen", 5, 5, 0.001),
        NOW,
    )
    summary = QuotaLedger(path, policy).usage_summary(NOW)
    assert summary.daily_calls == 1
    assert summary.daily_input_tokens == 5
    assert summary.daily_output_tokens == 5
    assert summary.monthly_cost_usd == 0.001
    assert summary.request_states == {"completed": 1}

    with pytest.raises(QuotaExceeded, match="daily calls"):
        QuotaLedger(path, policy).reserve(
            repo="owner/two",
            identity=identity,
            unit=unit,
            estimated_input_tokens=10,
            max_output_tokens=10,
            bootstrap=False,
            now=NOW,
        )


@pytest.mark.parametrize(
    ("policy", "bootstrap", "message"),
    [
        (QuotaPolicy(daily_input_tokens=9), False, "daily input tokens"),
        (QuotaPolicy(daily_output_tokens=9), False, "daily output tokens"),
        (QuotaPolicy(monthly_cost_usd=0), False, "monthly cost"),
        (QuotaPolicy(bootstrap_calls=0), True, "bootstrap calls"),
    ],
)
def test_each_quota_dimension_rejects_before_reservation(
    tmp_path,
    policy,
    bootstrap,
    message,
):
    ledger = QuotaLedger(tmp_path / f"{message.replace(' ', '-')}.sqlite", policy)
    with pytest.raises(QuotaExceeded, match=message):
        ledger.reserve(
            repo="owner/repo",
            identity=ExtractionIdentity("sig", "openrouter", "model"),
            unit=units(1)[0],
            estimated_input_tokens=10,
            max_output_tokens=10,
            bootstrap=bootstrap,
            now=NOW,
        )
    assert ledger.counts() == {}


def test_quota_exhaustion_stops_before_provider_dispatch(tmp_path):
    client = FakeStructuredClient()
    ledger = QuotaLedger(
        tmp_path / "llm_operations.sqlite",
        QuotaPolicy(daily_calls=0),
    )

    with pytest.raises(QuotaExceeded, match="daily calls"):
        runner(tmp_path, client, quota=ledger).run_batch(
            content_signature="content",
            units=units(1),
            next_unit_index=0,
            bootstrap=False,
            now=NOW,
        )

    assert client.calls == []
    assert ExtractionCache(tmp_path / "extraction_cache.sqlite").count() == 0


def test_schema_invalid_response_is_billed_as_actual_usage_but_never_cached(tmp_path):
    class InvalidClient(FakeStructuredClient):
        def complete_structured(self, prompt, **kwargs):  # noqa: ANN001
            self.calls.append((prompt, kwargs))
            return StructuredCompletion(
                content="not json",
                metadata=CompletionMetadata(
                    self.model,
                    self.model,
                    "Google",
                    "gen-invalid",
                    12,
                    3,
                    0.00001,
                ),
            )

    client = InvalidClient()
    with pytest.raises(ValueError, match="not valid JSON"):
        runner(tmp_path, client).run_batch(
            content_signature="invalid",
            units=units(1),
            next_unit_index=0,
            bootstrap=False,
            now=NOW,
        )

    assert ExtractionCache(tmp_path / "extraction_cache.sqlite").count() == 0
    assert QuotaLedger(tmp_path / "llm_operations.sqlite").counts() == {"completed": 1}
