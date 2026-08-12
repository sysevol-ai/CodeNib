import json

from scripts.guardian.deepswe.harness.delivery import (
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
