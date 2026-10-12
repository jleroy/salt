import pytest

import salt.utils.json
from salt.netapi.rest_tornado import saltnado


@pytest.fixture
def app_urls():
    return [
        (r"/jobs/(.*)", saltnado.JobsSaltAPIHandler),
        (r"/jobs", saltnado.JobsSaltAPIHandler),
    ]


@pytest.mark.slow_test
@pytest.mark.async_timeout(seconds=120)
async def test_get(http_client):
    # test with no JID
    response = await http_client.fetch("/jobs", method="GET", follow_redirects=False)
    response_obj = salt.utils.json.loads(response.body)["return"][0]
    assert response_obj
    assert isinstance(response_obj, dict)
    required_fields = {
        "Function",
        "Target",
        "Target-type",
        "User",
        "StartTime",
        "Arguments",
    }
    # Per-field subtests trigger expensive process statistics collection in CI.
    for jid, ret in response_obj.items():
        missing = required_fields.difference(ret)
        assert not missing, f"Job {jid} is missing fields: {sorted(missing)}"

    # test with a specific JID passed in
    jid = next(iter(response_obj.keys()))
    response = await http_client.fetch(
        f"/jobs/{jid}",
        method="GET",
        follow_redirects=False,
    )
    response_obj = salt.utils.json.loads(response.body)["return"][0]
    assert response_obj
    assert isinstance(response_obj, dict)

    missing = (required_fields | {"Result"}).difference(response_obj)
    assert not missing, f"Job {jid} is missing fields: {sorted(missing)}"
