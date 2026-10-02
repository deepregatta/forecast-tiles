"""Attempt evidence must distinguish missing upstream data from publication.

Use real cropped OM files and the normal scratch publisher; failed attempts
must preserve a good forecast and reports must never enter its byte layout.
"""

import json
from dataclasses import replace

import pytest
from test_openmeteo_pipeline import crop_product, cycle_of, install

from ingest import cli
from ingest.publish import PublishError
from ingest.regional_attempt import RegionalAttempt
from ingest.sources.base import CycleNotAvailableError
from ingest.sources.openmeteo import adapter, catalog, reader
from ingest.sources.openmeteo.reader import SourceError
from ingest.validate import ValidationReport


@pytest.fixture
def regional(tmp_path, monkeypatch):
    product = crop_product("arome-alps")
    bucket = install(monkeypatch, tmp_path, "arome-alps", product)
    out = tmp_path / "forecast"
    report = tmp_path / "attempt.json"
    args = ["weather-arome", "--cycle", "20261002T03", "--dry-run", str(out)]
    return product, bucket, out, report, args


def snapshot(out):
    return {str(p.relative_to(out)): p.read_bytes() for p in out.rglob("*") if p.is_file()}


def test_report_is_separate_and_matches_published_bytes(regional, monkeypatch):
    _, bucket, out, report, args = regional
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "private-test-secret")
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    assert cli.main(args + ["--attempt-report", str(report)]) == 0
    data = json.loads(report.read_text())
    assert data["outcome"] == "scratch_published"
    assert data["destination"] == "scratch" and data["pointer"] == "latest-regional.json"
    assert data["pointer_commit_confirmed"] is True and data["failure_category"] is None
    assert data["last_success_before"] is None
    assert data["source"]["metadata_lag_s"] == 10060
    files = data["source"]["completed_objects"]
    assert len(files) == len(bucket.downloads) == 3
    assert data["source"]["downloads_complete"] is True
    assert all(f["etag"] and f["bytes"] > 0 for f in files)
    assert data["source"]["completed_download_bytes"] == sum(f["bytes"] for f in files)
    assert data["source"]["temporary_file_peak_bytes"] == max(f["bytes"] for f in files)
    assert data["source"]["temporary_disk_free_bytes_before"] > 0
    for key in ("download_s", "decode_s", "conversion_s"):
        assert data["source"][key] >= 0
    assert all(v >= 0 for v in data["phases_s"].values())
    assert data["phases_s"]["encode"] >= 0 and data["phases_s"]["publish"] >= 0
    run = out / "forecast-runs" / data["committed_run_id"]
    manifest = json.loads((run / "manifest.json").read_text())
    tiles = list(manifest["tiles"].values())
    assert data["output"] == {
        "tiles": len(tiles),
        "gzip_bytes": sum(t["bytes"] for t in tiles),
        "largest_gzip_bytes": max(t["bytes"] for t in tiles),
        "largest_decoded_bytes": max(t["decoded_bytes"] for t in tiles),
        "largest_inflated_bytes": max(t["uncompressed_bytes"] for t in tiles),
    }
    assert data["validation"]["ok"] is True and data["validation"]["failure_count"] == 0
    assert data["github"]["github_run_id"] == "123"
    assert "private-test-secret" not in report.read_text()
    assert str(out) not in report.read_text()
    assert "attempt" not in json.dumps(manifest)
    before = snapshot(out)
    assert cli.main(args + ["--attempt-report", str(report)]) == 0
    skipped = json.loads(report.read_text())
    assert skipped["outcome"] == "already_published"
    assert skipped["pointer_commit_confirmed"] is None
    assert skipped["confirmed_current_run_id"] == data["run_id"]
    assert skipped["last_success_before"]["run_id"] == data["run_id"]
    assert skipped["output"] is None and skipped["source"] == {}
    assert len(bucket.downloads) == 3 and snapshot(out) == before
    without_report = out.parent / "without-report"
    assert cli.main([str(without_report) if arg == str(out) else arg for arg in args]) == 0
    plain_run = without_report / "forecast-runs" / data["run_id"]
    assert {str(p.relative_to(run)): p.read_bytes() for p in run.rglob("*.bin.gz")} == {
        str(p.relative_to(plain_run)): p.read_bytes() for p in plain_run.rglob("*.bin.gz")
    }
    assert (
        json.loads((plain_run / "manifest.json").read_text())["provenance"]
        == manifest["provenance"]
    )


def test_optional_completion_time_is_unknown_without_breaking_ingestion(regional, monkeypatch):
    _, bucket, _, report, args = regional
    original = bucket.fetch_meta
    monkeypatch.setattr(
        catalog, "fetch_meta", lambda *a, **kw: replace(original(*a, **kw), created_at="unknown")
    )
    assert cli.main(args + ["--attempt-report", str(report)]) == 0
    assert json.loads(report.read_text())["source"]["metadata_lag_s"] is None


def test_upstream_skip_is_not_counted_as_success_and_keeps_good_data(regional):
    _, bucket, out, report, args = regional
    assert cli.main(args) == 0
    before = snapshot(out)
    later = [a.replace("20261002T03", "20261002T09") for a in args]
    assert cli.main(later + ["--attempt-report", str(report)]) == 0
    data = json.loads(report.read_text())
    assert data["outcome"] == "source_unavailable"
    assert data["failure_category"] == "upstream_not_available" and data["exit_code"] == 0
    assert data["pointer_commit_confirmed"] is None and data["output"] is None
    assert data["last_success_before"]["run_id"] == "weather-arome-20261002T03Z"
    assert len(bucket.downloads) == 3 and snapshot(out) == before


@pytest.mark.parametrize("failure", ["decode", "validation", "source_recheck", "publish", "crash"])
def test_failures_are_recorded_without_advancing_or_erasing_pointer(regional, monkeypatch, failure):
    _, bucket, out, report, args = regional
    # Seed a prior pointer so a failed new attempt has useful last-success evidence.
    out.mkdir()
    pointer = out / "latest-regional.json"
    pointer.write_text(
        json.dumps(
            {
                "layers": {
                    "weather-arome": {
                        "run_id": "weather-arome-20261001T21Z",
                        "cycle": "2026-10-01T21:00Z",
                        "published_at": "2026-10-02T01:00:00Z",
                    }
                }
            }
        )
    )
    before = snapshot(out)

    def fail(*a, **kw):
        error = {
            "decode": SourceError,
            "source_recheck": SourceError,
            "publish": PublishError,
            "crash": RuntimeError,
        }[failure]
        raise error("private-test-secret must not appear in the report")

    if failure in ("decode", "crash"):
        # Resolver metadata reads also use decode in this fixture, so use the
        # genuine metadata captured before substituting the failing decoder.
        meta = bucket.fetch_meta(regional[0], cycle_of("arome-alps"))
        monkeypatch.setattr(catalog, "fetch_meta", lambda *a, **kw: meta)
        monkeypatch.setattr(reader, "decode", fail)
    elif failure == "validation":
        monkeypatch.setattr(
            adapter, "validate", lambda *a: ValidationReport(failures=["private-test-secret"])
        )
    elif failure == "source_recheck":
        monkeypatch.setattr(catalog, "recheck", fail)
    else:
        monkeypatch.setattr(cli, "publish_run", fail)
    if failure == "crash":
        with pytest.raises(RuntimeError):
            cli.main(args + ["--attempt-report", str(report)])
    else:
        assert cli.main(args + ["--attempt-report", str(report)]) == 1
    data = json.loads(report.read_text())
    assert (
        data["failure_category"]
        == {
            "decode": "source_check",
            "validation": "validation",
            "source_recheck": "source_identity",
            "publish": "publication",
            "crash": "build",
        }[failure]
    )
    assert data["exit_code"] == 1 and data["pointer_commit_confirmed"] is None
    assert data["last_success_before"]["run_id"] == "weather-arome-20261001T21Z"
    assert "private-test-secret" not in report.read_text()
    assert snapshot(out) == before
    if failure in ("decode", "crash"):
        assert len(data["source"]["completed_objects"]) == 1
        assert data["source"]["downloads_complete"] is False


def test_wait_timeout_reports_upstream_failure(regional, monkeypatch):
    _, _, _, report, args = regional

    def unavailable(*a, **kw):
        raise CycleNotAvailableError("private-test-secret")

    monkeypatch.setattr(cli, "_resolve", unavailable)
    ticks = iter([0, 1])
    assert (
        cli.main(
            args + ["--wait-minutes", "0.01", "--attempt-report", str(report)],
            clock=lambda: next(ticks),
            sleep=lambda _: None,
        )
        == 1
    )
    data = json.loads(report.read_text())
    assert (
        data["outcome"] == "source_wait_expired" and data["failure_category"] == "upstream_timeout"
    )
    assert data["pointer_commit_confirmed"] is None and data["output"] is None
    assert "private-test-secret" not in report.read_text()


def test_disabled_publication_records_configuration_failure_without_credentials(
    tmp_path, monkeypatch
):
    for name in ("R2_BUCKET", "R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        monkeypatch.delenv(name, raising=False)
    report = tmp_path / "attempt.json"
    assert cli.main(["weather-arome", "--attempt-report", str(report)]) == 1
    data = json.loads(report.read_text())
    assert data["outcome"] == "disabled" and data["failure_category"] == "configuration"
    assert data["source"] == {} and data["pointer_commit_confirmed"] is None


def test_report_cannot_overwrite_immutable_forecast_files(regional):
    _, _, out, _, args = regional
    with pytest.raises(SystemExit):
        cli.main(args + ["--attempt-report", str(out / "latest-regional.json")])
    assert not out.exists()
    with pytest.raises(SystemExit):
        cli.main(["weather", "--attempt-report", "/tmp/unused-report.json"])


def test_failed_report_write_does_not_mislabel_confirmed_publication(regional, monkeypatch, capsys):
    _, _, out, report, args = regional

    def fail(self):
        raise OSError("private-test-secret")

    monkeypatch.setattr(RegionalAttempt, "write", fail)
    assert cli.main(args + ["--attempt-report", str(report)]) == 0
    assert (out / "latest-regional.json").exists() and not report.exists()
    stderr = capsys.readouterr().err
    assert "attempt report unavailable (OSError)" in stderr and "private-test-secret" not in stderr


def test_parser_rejection_keeps_actual_nonzero_exit_code(regional):
    _, _, out, report, args = regional
    with pytest.raises(SystemExit) as exc:
        cli.main(args + ["--force", "--attempt-report", str(report)])
    assert exc.value.code == 2 and not out.exists()
    data = json.loads(report.read_text())
    assert data["exit_code"] == 2 and data["failure_category"] == "configuration"
    assert data["pointer_commit_confirmed"] is None
