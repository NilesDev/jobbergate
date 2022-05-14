import json

import httpx
import plummet
import pytest
from datetime import datetime

from jobbergate_cli.schemas import JobbergateContext, ClusterCacheData
from jobbergate_cli.exceptions import Abort
from jobbergate_cli.clusters import (
    pull_cluster_names_from_api,
    save_clusters_to_cache,
    load_clusters_from_cache,
)
from jobbergate_cli.config import settings


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


def test_save_clusters_to_cache(tmp_path, tweak_settings):
    cluster_cache_path = tmp_path / "clusters.json"
    with tweak_settings(JOBBERGATE_CLUSTER_LIST_PATH=cluster_cache_path):
        with plummet.frozen_time("2022-05-13 16:56:00"):
            save_clusters_to_cache(["cluster1", "cluster2", "cluster3"])

    cache_data = ClusterCacheData(**json.loads(cluster_cache_path.read_text()))
    assert cache_data.cluster_names == ["cluster1", "cluster2", "cluster3"]
    assert plummet.moments_match(cache_data.updated_at, "2022-05-13 16:56:00")


def test_load_clusters_from_cache__success(tmp_path, tweak_settings):
    cluster_cache_path = tmp_path / "clusters.json"
    with tweak_settings(JOBBERGATE_CLUSTER_LIST_PATH=cluster_cache_path, JOBBERGATE_CLUSTER_CACHE_LIFETIME=5):
        with plummet.frozen_time("2022-05-13 16:56:00"):
            cache_data = ClusterCacheData(
                updated_at=datetime.utcnow(),
                cluster_names=["cluster1", "cluster2", "cluster3"],
            )
            cluster_cache_path.write_text(cache_data.json())

            assert load_clusters_from_cache() == ["cluster1", "cluster2", "cluster3"]


def test_load_clusters_from_cache__returns_None_if_cache_is_expired(tmp_path, tweak_settings):
    cluster_cache_path = tmp_path / "clusters.json"
    with tweak_settings(JOBBERGATE_CLUSTER_LIST_PATH=cluster_cache_path, JOBBERGATE_CLUSTER_CACHE_LIFETIME=5):
        with plummet.frozen_time("2022-05-13 16:56:00"):
            cache_data = ClusterCacheData(
                updated_at=datetime.utcnow(),
                cluster_names=["cluster1", "cluster2", "cluster3"],
            )
            cluster_cache_path.write_text(cache_data.json())

        with plummet.frozen_time("2022-05-13 16:56:06"):
            assert load_clusters_from_cache() is None


def test_load_clusters_from_cache__returns_None_if_cache_is_invalid(tmp_path, tweak_settings):
    cluster_cache_path = tmp_path / "clusters.json"
    with tweak_settings(JOBBERGATE_CLUSTER_LIST_PATH=cluster_cache_path, JOBBERGATE_CLUSTER_CACHE_LIFETIME=5):
        cluster_cache_path.write_text("BAD DATA")
        assert load_clusters_from_cache() is None
