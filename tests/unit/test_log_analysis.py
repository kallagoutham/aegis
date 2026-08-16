"""Tests for log parsing, template clustering, and anomaly detection."""

from __future__ import annotations

from datetime import (
    UTC,
    datetime,
    timedelta,
)

from aegis.analysis import analyse_logs
from aegis.analysis.clustering import (
    analyse_entries,
    bucket_entries,
    cluster_entries,
    detect_anomalies,
    extract_template,
)
from aegis.analysis.parser import (
    LogLevel,
    normalise_level,
    parse_line,
    parse_logs,
    parse_timestamp,
)


class TestLevelNormalisation:
    """Severity vocabulary collapsing."""

    def test_common_aliases_map_to_canonical_levels(self):
        assert normalise_level("WARN") is LogLevel.WARNING
        assert normalise_level("warning") is LogLevel.WARNING
        assert normalise_level("ERR") is LogLevel.ERROR
        assert normalise_level("FATAL") is LogLevel.CRITICAL
        assert normalise_level("panic") is LogLevel.CRITICAL

    def test_unknown_token_is_unknown(self):
        assert normalise_level("banana") is LogLevel.UNKNOWN
        assert normalise_level(None) is LogLevel.UNKNOWN

    def test_problem_levels(self):
        assert LogLevel.ERROR.is_problem
        assert LogLevel.WARNING.is_problem
        assert LogLevel.CRITICAL.is_problem
        assert not LogLevel.INFO.is_problem
        assert not LogLevel.DEBUG.is_problem


class TestTimestampParsing:
    """Timestamp recovery across formats."""

    def test_iso_with_zulu(self):
        parsed = parse_timestamp("2026-08-15T10:23:04Z")
        assert parsed is not None and parsed.tzinfo is not None
        assert parsed.hour == 10

    def test_space_separated(self):
        parsed = parse_timestamp("2026-08-15 10:23:04")
        assert parsed is not None and parsed.year == 2026

    def test_epoch_seconds(self):
        parsed = parse_timestamp(1755253384)
        assert parsed is not None and parsed.tzinfo is UTC

    def test_epoch_milliseconds(self):
        seconds = parse_timestamp(1755253384)
        millis = parse_timestamp(1755253384000)
        assert seconds is not None and millis is not None
        # Both must resolve to the same instant, or timelines drift by 1000x.
        assert abs((seconds - millis).total_seconds()) < 1

    def test_naive_timestamps_are_assumed_utc(self):
        parsed = parse_timestamp("2026-08-15 10:23:04")
        assert parsed is not None and parsed.tzinfo is not None

    def test_garbage_returns_none(self):
        assert parse_timestamp("not a timestamp") is None
        assert parse_timestamp("") is None
        assert parse_timestamp(None) is None


class TestParsing:
    """Format detection and field extraction."""

    def test_json_lines(self, json_logs):
        entries = parse_logs(json_logs)
        assert len(entries) == 6
        assert all(entry.format == "json" for entry in entries)
        assert entries[1].level is LogLevel.ERROR
        assert entries[1].service == "payments"
        assert "upstream connect timeout" in entries[1].message

    def test_logfmt(self, logfmt_logs):
        entries = parse_logs(logfmt_logs)
        assert len(entries) == 3
        assert all(entry.format == "logfmt" for entry in entries)
        assert entries[0].level is LogLevel.ERROR
        assert entries[0].service == "payments"
        assert entries[0].message == "connection pool exhausted"

    def test_plain_text(self, text_logs):
        entries = parse_logs(text_logs)
        assert entries
        assert entries[0].level is LogLevel.ERROR
        assert entries[0].service == "payments"
        assert entries[0].timestamp is not None

    def test_stack_trace_folds_into_preceding_entry(self, text_logs):
        entries = parse_logs(text_logs)
        # 4 real entries; the 'at ...' and 'Caused by:' lines belong to entry 3.
        assert len(entries) == 4
        trace_entry = entries[2]
        assert "at com.example.payments.Gateway.authorise" in trace_entry.message
        assert "Caused by" in trace_entry.message

    def test_bracketed_level_is_not_mistaken_for_a_service(self):
        entry = parse_line("2026-08-15 10:23:04 [ERROR] something failed")
        assert entry.level is LogLevel.ERROR
        assert entry.service != "ERROR"

    def test_blank_lines_are_skipped(self):
        assert parse_logs("\n\n   \n\n") == []

    def test_unparseable_line_is_still_retained(self):
        entries = parse_logs("!!! total gibberish ###")
        assert len(entries) == 1
        assert entries[0].raw == "!!! total gibberish ###"

    def test_max_lines_truncates(self):
        logs = "\n".join(f"line {index}" for index in range(100))
        assert len(parse_logs(logs, max_lines=10)) <= 10


class TestTemplateExtraction:
    """Variable masking."""

    def test_masks_numbers(self):
        assert extract_template("timed out after 30000") == "timed out after <NUM>"

    def test_masks_uuids(self):
        template = extract_template("request 550e8400-e29b-41d4-a716-446655440000 failed")
        assert "<UUID>" in template

    def test_masks_ip_addresses(self):
        assert "<IP>" in extract_template("connection refused from 10.0.1.42")

    def test_masks_identifier_tokens(self):
        assert "<ID>" in extract_template("pod payments-7f3a crashed")

    def test_similar_lines_collapse_to_one_template(self):
        a = extract_template("Connection to db-primary-7f3a timed out after 30000ms")
        b = extract_template("Connection to db-primary-9c21 timed out after 5000ms")
        assert a == b, f"{a!r} != {b!r}"

    def test_genuinely_different_lines_stay_distinct(self):
        a = extract_template("Connection timed out")
        b = extract_template("Permission denied")
        assert a != b

    def test_only_first_line_of_a_trace_is_templated(self):
        template = extract_template("Error occurred\n    at Foo.bar(Foo.java:1)")
        assert "at Foo.bar" not in template


class TestClustering:
    """Grouping and counting."""

    def test_identical_templates_group_together(self, log_entry_factory):
        entries = [
            log_entry_factory(message="Connection to db-1 timed out after 100ms", level=LogLevel.ERROR),
            log_entry_factory(message="Connection to db-2 timed out after 200ms", level=LogLevel.ERROR),
            log_entry_factory(message="Connection to db-3 timed out after 300ms", level=LogLevel.ERROR),
        ]
        clusters = cluster_entries(entries)
        assert len(clusters) == 1
        assert clusters[0].count == 3

    def test_clusters_sorted_by_descending_count(self, log_entry_factory):
        entries = [log_entry_factory(message="frequent event") for _ in range(5)]
        entries.append(log_entry_factory(message="rare event"))
        clusters = cluster_entries(entries)
        assert clusters[0].count == 5
        assert clusters[-1].count == 1

    def test_cluster_takes_highest_severity_seen(self, log_entry_factory):
        entries = [
            log_entry_factory(message="pool saturated", level=LogLevel.INFO),
            log_entry_factory(message="pool saturated", level=LogLevel.ERROR),
        ]
        clusters = cluster_entries(entries)
        assert clusters[0].level is LogLevel.ERROR

    def test_cluster_records_all_services(self, log_entry_factory):
        entries = [
            log_entry_factory(message="timeout", service="payments"),
            log_entry_factory(message="timeout", service="checkout"),
        ]
        clusters = cluster_entries(entries)
        assert clusters[0].services == {"payments", "checkout"}


class TestBucketing:
    """Time bucketing."""

    def test_buckets_span_the_time_range(self, log_entry_factory):
        base = datetime(2026, 8, 15, 10, 0, 0, tzinfo=UTC)
        entries = [
            log_entry_factory(message=f"event {i}", timestamp=base + timedelta(seconds=i * 10)) for i in range(20)
        ]
        buckets = bucket_entries(entries, bucket_count=10)
        assert len(buckets) == 10
        assert sum(bucket.total for bucket in buckets) == 20

    def test_no_timestamps_produces_no_buckets(self, log_entry_factory):
        entries = [log_entry_factory(message="x", timestamp=None) for _ in range(5)]
        assert bucket_entries(entries) == []

    def test_single_entry_produces_no_buckets(self, log_entry_factory):
        assert bucket_entries([log_entry_factory()]) == []


class TestAnomalyDetection:
    """Heuristic anomaly notes."""

    def test_detects_error_burst(self, log_entry_factory):
        base = datetime(2026, 8, 15, 10, 0, 0, tzinfo=UTC)
        entries = [
            log_entry_factory(message=f"ok {i}", level=LogLevel.INFO, timestamp=base + timedelta(seconds=i))
            for i in range(60)
        ]
        # A concentrated burst late in the window.
        entries += [
            log_entry_factory(
                message=f"boom {i}", level=LogLevel.ERROR, timestamp=base + timedelta(seconds=55, milliseconds=i)
            )
            for i in range(40)
        ]
        analysis = analyse_entries(entries)
        assert analysis.anomalies

    def test_detects_dominant_error_template(self, log_entry_factory):
        entries = [log_entry_factory(message=f"pool exhausted on node-{i}", level=LogLevel.ERROR) for i in range(30)]
        entries += [log_entry_factory(message="unrelated failure", level=LogLevel.ERROR)]
        clusters = cluster_entries(entries)
        anomalies = detect_anomalies(clusters, [])
        assert any("accounts for" in note for note in anomalies)

    def test_detects_multi_service_correlation(self, log_entry_factory):
        entries = [
            log_entry_factory(message="timeout contacting upstream", level=LogLevel.ERROR, service="payments")
            for _ in range(5)
        ]
        entries += [
            log_entry_factory(message="timeout contacting upstream", level=LogLevel.ERROR, service="checkout")
            for _ in range(5)
        ]
        clusters = cluster_entries(entries)
        anomalies = detect_anomalies(clusters, [])
        assert any("shared dependency" in note for note in anomalies)

    def test_clean_logs_produce_no_anomalies(self, log_entry_factory):
        base = datetime(2026, 8, 15, 10, 0, 0, tzinfo=UTC)
        entries = [
            log_entry_factory(
                message=f"request handled in {i}ms", level=LogLevel.INFO, timestamp=base + timedelta(seconds=i)
            )
            for i in range(50)
        ]
        analysis = analyse_entries(entries)
        assert analysis.anomalies == []


class TestAnalyseLogs:
    """The public entry point."""

    def test_end_to_end_on_json_logs(self, json_logs):
        analysis = analyse_logs(json_logs)
        assert analysis.total_entries == 6
        assert analysis.error_count == 4  # 3 errors + 1 warning
        assert "payments" in analysis.services
        assert analysis.time_range[0] is not None

    def test_error_rate(self, json_logs):
        analysis = analyse_logs(json_logs)
        assert 0 < analysis.error_rate < 1

    def test_prompt_summary_is_non_empty_text(self, json_logs):
        summary = analyse_logs(json_logs).to_prompt_summary()
        assert "Log analysis" in summary
        assert "entries" in summary

    def test_prompt_summary_is_far_smaller_than_raw_input(self):
        # The point of clustering: 2000 near-identical lines must compress.
        logs = "\n".join(
            f"2026-08-15 10:23:{i % 60:02d} ERROR [payments] Connection to db-{i} timed out after {i}ms"
            for i in range(2000)
        )
        analysis = analyse_logs(logs)
        summary = analysis.to_prompt_summary()
        assert len(summary) < len(logs) / 10

    def test_serialises_to_dict(self, json_logs):
        payload = analyse_logs(json_logs).to_dict()
        assert payload["total_entries"] == 6
        assert "top_error_templates" in payload
        assert "time_range" in payload

    def test_truncation_is_flagged(self):
        logs = "\n".join(f"line {i}" for i in range(500))
        analysis = analyse_logs(logs, max_lines=50)
        assert analysis.truncated
        assert "truncated" in analysis.to_prompt_summary().lower()

    def test_empty_input(self):
        analysis = analyse_logs("")
        assert analysis.total_entries == 0
        assert analysis.error_count == 0
