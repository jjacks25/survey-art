"""Tests for costs.estimate() — the all-in per-run cost breakdown.

These pin the shape and the relationships, not the exact dollar values: the
rates are a published price list that will change, and asserting them here would
just mean editing two files every time one moves.
"""

from __future__ import annotations

from pytest import approx

from survey_art import costs

# Roughly one real Weld property: ~50 min, 86 documents, 37 MB, 1.7M input tokens.
REAL_RUN = {
    "elapsed_s": 3058.0,
    "bedrock_usd": 1.9763,
    "input_tokens": 1_717_584,
    "output_tokens": 51_736,
    "document_bytes": 37_000_000,
    "document_count": 86,
    "log_appends": 400,
    "log_bytes": 120_000,
}


def _by_key(lines: list[dict]) -> dict[str, dict]:
    return {line["key"]: line for line in lines}


class TestEstimate:
    def test_every_service_a_run_touches_gets_a_line(self):
        lines = _by_key(costs.estimate(**REAL_RUN))

        assert set(lines) == {"bedrock", "fargate", "s3", "dynamodb", "api", "cloudwatch"}

    def test_lines_are_ordered_biggest_first(self):
        amounts = [line["usd"] for line in costs.estimate(**REAL_RUN)]

        assert amounts == sorted(amounts, reverse=True)

    def test_only_bedrock_claims_to_be_measured(self):
        """Everything else is this module's arithmetic over a rate card, and the
        UI labels it as such — don't let a line quietly claim to be billed."""
        lines = costs.estimate(**REAL_RUN)

        assert [line["key"] for line in lines if line["basis"] == "measured"] == ["bedrock"]

    def test_bedrock_is_passed_through_untouched(self):
        lines = _by_key(costs.estimate(**REAL_RUN))

        assert lines["bedrock"]["usd"] == REAL_RUN["bedrock_usd"]

    def test_the_model_dominates_a_real_run(self):
        """The whole reason for itemising: if this ever stops being true, the
        run has changed shape enough that the optimisation target moved."""
        lines = _by_key(costs.estimate(**REAL_RUN))
        total = sum(line["usd"] for line in lines.values())

        assert lines["bedrock"]["usd"] / total > 0.9
        # ...and everything except compute really is rounding error.
        assert sum(lines[k]["usd"] for k in ("s3", "dynamodb", "api", "cloudwatch")) < 0.05

    def test_fargate_scales_with_runtime(self):
        slow = _by_key(costs.estimate(**{**REAL_RUN, "elapsed_s": 6116.0}))
        fast = _by_key(costs.estimate(**REAL_RUN))

        assert slow["fargate"]["usd"] == approx(fast["fargate"]["usd"] * 2, rel=1e-6)

    def test_storage_scales_with_bytes_kept(self):
        big = _by_key(costs.estimate(**{**REAL_RUN, "document_bytes": 74_000_000}))
        small = _by_key(costs.estimate(**REAL_RUN))

        assert big["s3"]["usd"] > small["s3"]["usd"]

    def test_an_empty_failed_run_still_prices_out(self):
        """A job that crashed before downloading anything still burned Fargate
        seconds, so it still gets a number rather than a division error."""
        lines = costs.estimate(
            elapsed_s=12.0,
            bedrock_usd=0.0,
            input_tokens=0,
            output_tokens=0,
            document_bytes=0,
            document_count=0,
            log_appends=0,
            log_bytes=0,
        )

        assert all(line["usd"] >= 0 for line in lines)
        assert _by_key(lines)["fargate"]["usd"] > 0
