"""Tests for the structured report schema and its consistency validators.

These validators exist because models reliably produce structurally valid but
internally inconsistent reports: citing evidence that was never defined,
numbering steps out of order, claiming zero confidence alongside a 0.9
hypothesis. Each test below pins one of those repairs.
"""

from __future__ import annotations

from pydantic import ValidationError as PydanticValidationError
import pytest

from aegis.models.incident import Severity
from aegis.schemas.chat import (
    ChatRequest,
    Message,
)
from aegis.schemas.incident import (
    Evidence,
    EvidenceType,
    Hypothesis,
    InvestigateRequest,
    InvestigationReportSchema,
    RemediationStep,
    RiskLevel,
)


def _report(**overrides) -> InvestigationReportSchema:
    """Build a minimal valid report with optional overrides."""
    payload = {"summary": "Something broke.", **overrides}
    return InvestigationReportSchema.model_validate(payload)


class TestRiskLevel:
    """Risk semantics."""

    def test_medium_and_high_require_approval(self):
        assert RiskLevel.MEDIUM.requires_approval
        assert RiskLevel.HIGH.requires_approval

    def test_safe_and_low_do_not(self):
        assert not RiskLevel.SAFE.requires_approval
        assert not RiskLevel.LOW.requires_approval


class TestConfidenceDerivation:
    """Top-level confidence vs per-hypothesis confidence."""

    def test_derived_from_best_hypothesis_when_unset(self):
        report = _report(
            hypotheses=[
                {"id": "H1", "statement": "cause a", "confidence": 0.7},
                {"id": "H2", "statement": "cause b", "confidence": 0.3},
            ]
        )
        assert report.confidence == 0.7

    def test_explicit_confidence_is_preserved(self):
        report = _report(
            confidence=0.42,
            hypotheses=[{"id": "H1", "statement": "cause", "confidence": 0.9}],
        )
        assert report.confidence == 0.42

    def test_no_hypotheses_leaves_confidence_at_zero(self):
        assert _report().confidence == 0.0


class TestEvidenceReferenceIntegrity:
    """Hallucinated evidence references must not survive."""

    def test_dangling_reference_is_dropped(self):
        report = _report(
            evidence=[{"id": "E1", "type": "log_pattern", "description": "observed"}],
            hypotheses=[{"id": "H1", "statement": "cause", "confidence": 0.5, "evidence_ids": ["E1", "E7"]}],
        )
        assert report.hypotheses[0].evidence_ids == ["E1"]

    def test_valid_references_are_kept(self):
        report = _report(
            evidence=[
                {"id": "E1", "type": "log_pattern", "description": "a"},
                {"id": "E2", "type": "metric", "description": "b"},
            ],
            hypotheses=[{"id": "H1", "statement": "cause", "confidence": 0.5, "evidence_ids": ["E1", "E2"]}],
        )
        assert set(report.hypotheses[0].evidence_ids) == {"E1", "E2"}

    def test_duplicate_references_are_deduplicated(self):
        hypothesis = Hypothesis(id="H1", statement="s", confidence=0.5, evidence_ids=["E1", "E1", "E2"])
        assert hypothesis.evidence_ids == ["E1", "E2"]


class TestHypothesisRanking:
    """Hypotheses are ordered by confidence."""

    def test_sorted_descending(self):
        report = _report(
            hypotheses=[
                {"id": "H1", "statement": "least likely", "confidence": 0.1},
                {"id": "H2", "statement": "most likely", "confidence": 0.9},
                {"id": "H3", "statement": "middle", "confidence": 0.5},
            ]
        )
        assert [h.id for h in report.hypotheses] == ["H2", "H3", "H1"]

    def test_leading_hypothesis_is_the_first(self):
        report = _report(
            hypotheses=[
                {"id": "H1", "statement": "a", "confidence": 0.2},
                {"id": "H2", "statement": "b", "confidence": 0.8},
            ]
        )
        assert report.leading_hypothesis is not None
        assert report.leading_hypothesis.id == "H2"

    def test_leading_hypothesis_is_none_when_empty(self):
        assert _report().leading_hypothesis is None


class TestRemediationOrdering:
    """Steps are sorted and renumbered contiguously."""

    def test_out_of_order_steps_are_sorted(self):
        report = _report(
            remediation_steps=[
                {"order": 3, "action": "third"},
                {"order": 1, "action": "first"},
                {"order": 2, "action": "second"},
            ]
        )
        assert [s.action for s in report.remediation_steps] == ["first", "second", "third"]

    def test_duplicate_ordinals_are_renumbered(self):
        report = _report(
            remediation_steps=[
                {"order": 1, "action": "a"},
                {"order": 1, "action": "b"},
                {"order": 1, "action": "c"},
            ]
        )
        assert [s.order for s in report.remediation_steps] == [1, 2, 3]

    def test_gapped_ordinals_become_contiguous(self):
        report = _report(
            remediation_steps=[
                {"order": 1, "action": "a"},
                {"order": 50, "action": "b"},
            ]
        )
        assert [s.order for s in report.remediation_steps] == [1, 2]


class TestHumanReviewTrigger:
    """When a report must be escalated rather than acted on."""

    def test_no_hypotheses_triggers_review(self):
        assert _report().needs_human_review

    def test_low_confidence_triggers_review(self):
        report = _report(hypotheses=[{"id": "H1", "statement": "guess", "confidence": 0.3}])
        assert report.needs_human_review

    def test_high_risk_step_triggers_review(self):
        report = _report(
            hypotheses=[{"id": "H1", "statement": "confident", "confidence": 0.95}],
            remediation_steps=[{"order": 1, "action": "failover the primary", "risk": "high"}],
        )
        assert report.needs_human_review

    def test_confident_low_risk_report_does_not(self):
        report = _report(
            hypotheses=[{"id": "H1", "statement": "confident", "confidence": 0.9}],
            remediation_steps=[{"order": 1, "action": "read the pool metric", "risk": "safe"}],
        )
        assert not report.needs_human_review


class TestMarkdownRendering:
    """Markdown output."""

    def test_includes_all_populated_sections(self):
        report = _report(
            summary="Payments is failing.",
            hypotheses=[{"id": "H1", "statement": "pool exhausted", "confidence": 0.8}],
            evidence=[{"id": "E1", "type": "log_pattern", "description": "timeouts"}],
            remediation_steps=[{"order": 1, "action": "scale replicas", "risk": "low"}],
            immediate_actions=["shed load"],
            open_questions=["no metrics available"],
        )
        markdown = report.to_markdown()
        for expected in (
            "Payments is failing.",
            "pool exhausted",
            "timeouts",
            "scale replicas",
            "shed load",
            "no metrics available",
        ):
            assert expected in markdown

    def test_flags_steps_needing_approval(self):
        report = _report(remediation_steps=[{"order": 1, "action": "drop the table", "risk": "high"}])
        assert "needs approval" in report.to_markdown()

    def test_contradicting_evidence_is_labelled(self):
        report = _report(evidence=[{"id": "E1", "type": "metric", "description": "pool is idle", "supports": False}])
        assert "contradicts" in report.to_markdown()

    def test_minimal_report_still_renders(self):
        assert "Something broke." in _report().to_markdown()


class TestFieldValidation:
    """Bounds and required fields."""

    def test_confidence_must_be_within_unit_interval(self):
        with pytest.raises(PydanticValidationError):
            Hypothesis(id="H1", statement="s", confidence=1.5)
        with pytest.raises(PydanticValidationError):
            Hypothesis(id="H1", statement="s", confidence=-0.1)

    def test_summary_is_required(self):
        with pytest.raises(PydanticValidationError):
            InvestigationReportSchema.model_validate({})

    def test_step_order_must_be_positive(self):
        with pytest.raises(PydanticValidationError):
            RemediationStep(order=0, action="x")

    def test_unknown_fields_are_ignored_not_rejected(self):
        # Models add stray keys; dropping them beats failing the whole report.
        report = InvestigationReportSchema.model_validate({"summary": "s", "some_invented_field": "value"})
        assert report.summary == "s"

    def test_evidence_defaults_to_supporting(self):
        assert Evidence(id="E1", type=EvidenceType.METRIC, description="d").supports is True

    def test_severity_assessment_accepts_enum_value(self):
        assert _report(severity_assessment="sev1").severity_assessment is Severity.SEV1


class TestInvestigateRequest:
    """Investigation request validation."""

    def test_blank_query_is_rejected(self):
        with pytest.raises(PydanticValidationError):
            InvestigateRequest(query="   ")

    def test_query_is_trimmed(self):
        assert InvestigateRequest(query="  why is it broken  ").query == "why is it broken"


class TestChatSchemas:
    """Chat request validation."""

    def test_requires_trailing_user_message(self):
        with pytest.raises(PydanticValidationError, match="final message"):
            ChatRequest(messages=[Message(role="assistant", content="hello")])

    def test_accepts_conversation_ending_in_user_turn(self):
        request = ChatRequest(
            messages=[
                Message(role="user", content="hi"),
                Message(role="assistant", content="hello"),
                Message(role="user", content="why is checkout down"),
            ]
        )
        assert len(request.messages) == 3

    def test_rejects_script_tags(self):
        with pytest.raises(PydanticValidationError, match="script"):
            Message(role="user", content="<script>alert(1)</script>")

    def test_rejects_null_bytes(self):
        with pytest.raises(PydanticValidationError, match="null bytes"):
            Message(role="user", content="bad\x00content")

    def test_allows_angle_brackets_in_log_text(self):
        # Escaping this would corrupt the log the user pasted.
        message = Message(role="user", content="error in List<String> at Foo.java:42 && retry")
        assert "List<String>" in message.content

    def test_allows_long_pasted_stack_traces(self):
        trace = "Exception\n" + "\n".join(f"    at com.example.Class{i}.method(F.java:{i})" for i in range(500))
        assert Message(role="user", content=trace).content.startswith("Exception")
