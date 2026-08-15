import json

from scripts.guardian.deepswe.harness.delivery import (
    completed_report,
    completed_responses,
    review_request_commits,
)


def test_published_request_is_not_complete_until_response_finishes(tmp_path):
    commit = "a" * 40
    exchange = tmp_path / "exchange"
    requests = exchange / "requests"
    response = exchange / "responses" / commit
    requests.mkdir(parents=True)
    response.mkdir(parents=True)
    (requests / f"{commit}.json").write_text("{}")

    commits = review_request_commits(exchange)
    assert commits == {commit}
    assert completed_responses(exchange, commits) == set()

    (response / "status.json").write_text(json.dumps({"running": True}))
    assert completed_responses(exchange, commits) == set()

    (response / "status.json").write_text(json.dumps({"running": False}))
    assert completed_responses(exchange, commits) == {commit}


def test_completed_report_does_not_depend_on_latest_publication(tmp_path):
    commit = "b" * 40
    exchange = tmp_path / "exchange"
    response = exchange / "responses" / commit
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
                "commit": commit,
                "running": False,
                "review_performed": True,
            }
        )
    )

    report = completed_report(exchange, {commit})

    assert report is not None
    assert report.commit == commit
    assert report.report == "# Commit report\n"
    assert report.identifiers == ("LS-1", "LS-2")
    assert not (exchange / "latest").exists()


def test_completed_report_ignores_terminal_non_review_response(tmp_path):
    commit = "c" * 40
    exchange = tmp_path / "exchange"
    response = exchange / "responses" / commit
    response.mkdir(parents=True)
    (response / "findings.md").write_text("# Stale report\n")
    (response / "status.json").write_text(
        json.dumps(
            {
                "commit": commit,
                "running": False,
                "review_performed": False,
            }
        )
    )

    assert completed_report(exchange, {commit}) is None


def test_delivery_ignores_noncanonical_request_names(tmp_path):
    requests = tmp_path / "exchange" / "requests"
    requests.mkdir(parents=True)
    (requests / "...json").write_text("{}")
    (requests / "abc1234.json").write_text("{}")

    assert review_request_commits(tmp_path / "exchange") == set()
    assert completed_responses(tmp_path / "exchange", {"..", "abc1234"}) == set()
