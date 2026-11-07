"""The chart workflow stays manual (WP10 phase 6): Open Waters' archive is read
only in runs the owner approves, at the date the run names, and only the
publish job holds the R2 secrets."""

from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "charts.yml"


def test_charts_run_only_by_hand_at_a_named_seamap_date():
    text = WORKFLOW.read_text()
    trigger = text.split("\non:\n", 1)[1].split("\nconcurrency:", 1)[0]
    assert "workflow_dispatch:" in trigger and "schedule" not in trigger and "push" not in trigger
    seamap = trigger.split("seamap:", 1)[1].split("protomaps:", 1)[0]
    assert "required: true" in seamap and "default" not in seamap
    assert "default: true" in trigger.split("dry_run:", 1)[1], (
        "a dispatch is a dry run unless asked"
    )


def test_only_the_publish_job_sees_the_r2_secrets():
    text = WORKFLOW.read_text()
    jobs = text.split("\njobs:\n", 1)[1]
    publish = jobs.split("\n  publish:\n", 1)[1]
    before = jobs.split("\n  publish:\n", 1)[0]
    assert "secrets." not in before
    assert "secrets.R2_SECRET_ACCESS_KEY" in publish
    assert publish.count("secrets.") == 4
