import json

from scripts.guardian.deepswe.harness.delivery import (
    completed_report,
    completed_responses,
    review_request_commits,
)


def test_published_request_is_not_complete_until_response_finishes(tmp_path):
    exchange = tmp_path / "exchange"
    requests = exchange / "requests"
    response = exchange / "responses" / "abc1234"
    requests.mkdir(parents=True)
    response.mkdir(parents=True)
    (requests / "abc1234.json").write_text("{}")

    commits = review_request_commits(exchange)
    assert commits == {"abc1234"}
    assert completed_responses(exchange, commits) == set()

    (response / "status.json").write_text(json.dumps({"running": True}))
    assert completed_responses(exchange, commits) == set()

    (response / "status.json").write_text(json.dumps({"running": False}))
    assert completed_responses(exchange, commits) == {"abc1234"}


def test_completed_report_does_not_depend_on_latest_publication(tmp_path):
    exchange = tmp_path / "exchange"
    response = exchange / "responses" / "abc1234"
    response.mkdir(parents=True)
    (response / "findings.md").write_text("# Commit report\n")
    (response / "findings.json").write_text(
        json.dumps(
            {
                "findings": [{"specification_id": "LS-1"}],
                "uncertain_specifications": [{"specification_id": "LS-2"}],
            }
        )
    )
    (response / "status.json").write_text(
        json.dumps(
            {
                "commit": "abc1234",
                "running": False,
                "review_performed": True,
            }
        )
    )

    report = completed_report(exchange, {"abc1234"})

    assert report is not None
    assert report.commit == "abc1234"
    assert report.report == "# Commit report\n"
    assert report.identifiers == ("LS-1", "LS-2")
    assert not (exchange / "latest").exists()


def test_completed_report_ignores_terminal_non_review_response(tmp_path):
    exchange = tmp_path / "exchange"
    response = exchange / "responses" / "abc1234"
    response.mkdir(parents=True)
    (response / "findings.md").write_text("# Stale report\n")
    (response / "status.json").write_text(
        json.dumps(
            {
                "commit": "abc1234",
                "running": False,
                "review_performed": False,
            }
        )
    )

    assert completed_report(exchange, {"abc1234"}) is None
