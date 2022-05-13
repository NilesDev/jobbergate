"""
Utilities for finding out clusters that are available
"""

from typing import List, Dict, cast

from jobbergate_cli.exceptions import Abort
from jobbergate_cli.schemas import JobbergateContext
from jobbergate_cli.requests import make_request

def pull_cluster_names_from_api(ctx: JobbergateContext) -> List[str]:
    assert ctx.client is not None

    response_data = cast(
        Dict,
        make_request(
            ctx.client,
            "/cluster/graphql/query",
            "POST",
            expected_status=200,
            abort_message="There was a problem retrieving registered clusters from the Cluster API",
            abort_subject="COULD NOT RETRIEVE CLUSTERS",
            support=True,
            json=dict(
                query="query {cluster{clientId}}",
                variables=dict(),
            ),
        ),
    )

    try:
        cluster_names = [e["clientId"] for e in response_data["data"]["cluster"]]
    except Exception as err:
        raise Abort(
            "Couldn't unpack cluster names from Cluster API response",
            subject="COULD NOT RETRIEVE CLUSTERS",
            support=True,
            original_error=err,
            log_message=f"Failed to unpack data from cluster-api: {response_data}",
        )
    return cluster_names
