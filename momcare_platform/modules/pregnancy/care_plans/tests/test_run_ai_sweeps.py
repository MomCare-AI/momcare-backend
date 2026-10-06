"""One scheduled command runs both AI jobs, and one failing never stops the other."""

from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

pytestmark = pytest.mark.django_db

CARE_PLAN_HANDLE = "momcare_platform.modules.pregnancy.care_plans.management.commands.sweep_care_plans.run_sweep"
SUMMARY_HANDLE = "momcare_platform.core.ai.management.commands.refresh_ai_summaries.generate_patient_summary"


def run():
    out, err = StringIO(), StringIO()
    try:
        call_command("run_ai_sweeps", stdout=out, stderr=err)
        return out.getvalue(), err.getvalue(), None
    except CommandError as exc:
        return out.getvalue(), err.getvalue(), exc


def test_both_jobs_run_in_order_and_report_separately():
    out, err, error = run()

    assert error is None
    assert out.index("== sweep_care_plans") < out.index("== refresh_ai_summaries")
    assert "No care plan needed evaluating." in out
    assert "No AI summary was due for a refresh." in out
    assert "All scheduled AI jobs finished." in out


def test_the_care_plan_sweep_actually_runs_through_the_combined_command():
    with patch(CARE_PLAN_HANDLE, return_value=(3, 1)) as sweep:
        out, _, error = run()

    assert error is None
    sweep.assert_called_once()
    assert "Started 3 week plan(s); retried 1 stuck on the fallback." in out


def test_a_failing_care_plan_sweep_does_not_stop_the_summary_refresh_but_fails_the_run():
    with patch(CARE_PLAN_HANDLE, side_effect=RuntimeError("the sweep broke")):
        out, err, error = run()

    assert "No AI summary was due for a refresh." in out  # the second job still ran
    assert "sweep_care_plans failed" in err
    assert error is not None and "sweep_care_plans" in str(error)
    assert "refresh_ai_summaries" not in str(error)


def test_a_failing_summary_refresh_does_not_stop_the_care_plan_sweep_but_fails_the_run(make_hospital, make_patient):
    from momcare_platform.core.ai.management.commands import refresh_ai_summaries  # noqa: PLC0415

    with patch.object(refresh_ai_summaries.Command, "handle", side_effect=RuntimeError("summaries broke")):
        out, err, error = run()

    assert "No care plan needed evaluating." in out  # the first job ran
    assert "refresh_ai_summaries failed" in err
    assert error is not None and "refresh_ai_summaries" in str(error)


def test_both_failing_reports_both():
    from momcare_platform.core.ai.management.commands import refresh_ai_summaries  # noqa: PLC0415

    with (
        patch(CARE_PLAN_HANDLE, side_effect=RuntimeError("a")),
        patch.object(refresh_ai_summaries.Command, "handle", side_effect=RuntimeError("b")),
    ):
        _, _, error = run()

    assert error is not None
    assert "sweep_care_plans" in str(error) and "refresh_ai_summaries" in str(error)
