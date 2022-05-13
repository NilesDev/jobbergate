import httpx
import pytest

from jobbergate_cli.schemas import JobbergateContext
from jobbergate_cli.exceptions import Abort
from jobbergate_cli.clusters import pull_cluster_names_from_api


@pytest.fixture
def dummy_domain():
    return "https://dummy.com"


@pytest.fixture
def dummy_context(dummy_domain):
    return JobbergateContext(
        persona=None,
        client=httpx.Client(base_url=dummy_domain, headers={"Authorization": "Bearer XXXXXXXX"}),
    )


def test_pull_cluster_names_from_api__success(respx_mock, dummy_domain, dummy_context):
    clusters_route = respx_mock.post(f"{dummy_domain}/cluster/graphql/query")
    clusters_route.mock(
        return_value=httpx.Response(
            httpx.codes.OK,
            json=dict(
                data=dict(
                    cluster=[
                        dict(clientId="cluster1"),
                        dict(clientId="cluster2"),
                        dict(clientId="cluster3"),
                    ],
                ),
            ),
        ),
    )

    assert pull_cluster_names_from_api(dummy_context) == ["cluster1", "cluster2", "cluster3"]
