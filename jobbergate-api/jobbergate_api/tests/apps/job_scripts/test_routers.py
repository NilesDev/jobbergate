"""
Tests for the /job-scripts/ endpoint.
"""
import json
from textwrap import dedent
from unittest import mock

import asyncpg
import pytest
from botocore.exceptions import BotoCoreError
from fastapi import status
from fastapi.exceptions import HTTPException

from jobbergate_api.apps.applications.models import applications_table
from jobbergate_api.apps.job_scripts.models import job_scripts_table
from jobbergate_api.apps.job_scripts.routers import (
    build_job_script_data_as_string,
    get_s3_object_as_tarfile,
    inject_sbatch_params,
    render_template,
    s3man,
)
from jobbergate_api.apps.job_scripts.schemas import JobScriptResponse
from jobbergate_api.apps.permissions import Permissions
from jobbergate_api.storage import database


@pytest.fixture
def job_script_data_as_string():
    """
    Provide a fixture that returns an example of a default application script.
    """
    content = json.dumps(
        {
            "application.sh": dedent(
                """
                #!/bin/bash

                #SBATCH --job-name=rats
                #SBATCH --partition=debug
                #SBATCH --output=sample-%j.out


                echo $SLURM_TASKS_PER_NODE
                echo $SLURM_SUBMIT_DIR
                """
            ).strip(),
        }
    )
    return content


@pytest.fixture
def new_job_script_data_as_string():
    """
    Provide a fixture that returns an application script after the injection of the sbatch params.
    """
    content = json.dumps(
        {
            "application.sh": dedent(
                """
                #!/bin/bash

                #SBATCH --comment=some_comment
                #SBATCH --nice=-1
                #SBATCH -N 10
                #SBATCH --job-name=rats
                #SBATCH --partition=debug
                #SBATCH --output=sample-%j.out


                echo $SLURM_TASKS_PER_NODE
                echo $SLURM_SUBMIT_DIR
                """
            ).strip(),
        }
    )
    return content


@pytest.fixture
def sbatch_params():
    """
    Provide a fixture that returns string content of the argument --sbatch-params.
    """
    return ["--comment=some_comment", "--nice=-1", "-N 10"]


def test_inject_sbatch_params(job_script_data_as_string, sbatch_params, new_job_script_data_as_string):
    """
    Test the injection of sbatch params in a default application script.
    """
    injected_string = inject_sbatch_params(job_script_data_as_string, sbatch_params)
    assert injected_string == new_job_script_data_as_string


@pytest.mark.asyncio
@database.transaction(force_rollback=True)
async def test_create_job_script(
    fill_application_data,
    job_script_data,
    fill_job_script_data,
    param_dict,
    client,
    inject_security_header,
    time_frame,
    s3_object,
):
    """
    Test POST /job_scripts/ correctly creates a job_script.

    This test proves that a job_script is successfully created via a POST request to the /job-scripts/
    endpoint. We show this by asserting that the job_script is created in the database after the post
    request is made, the correct status code (201) is returned.
    """
    inserted_application_id = await database.execute(
        query=applications_table.insert(),
        values=fill_application_data(application_owner_email="owner1@org.com"),
    )

    inject_security_header("owner1@org.com", Permissions.JOB_SCRIPTS_EDIT)
    with time_frame() as window:
        with mock.patch.object(s3man, "s3_client") as s3man_client_mock:
            s3man_client_mock.get_object.return_value = s3_object
            response = await client.post(
                "/jobbergate/job-scripts/",
                json=fill_job_script_data(
                    application_id=inserted_application_id,
                    param_dict=param_dict,
                ),
            )

    assert response.status_code == status.HTTP_201_CREATED
    s3man_client_mock.get_object.assert_called_once()

    id_rows = await database.fetch_all("SELECT id FROM job_scripts")
    assert len(id_rows) == 1

    job_script = JobScriptResponse(**response.json())

    assert job_script.id == id_rows[0][0]
    assert job_script.job_script_name == job_script_data["job_script_name"]
    assert job_script.job_script_owner_email == "owner1@org.com"
    assert job_script.job_script_description is None
    assert job_script.job_script_data_as_string
    assert job_script.job_script_data_as_string != job_script_data["job_script_data_as_string"]
    assert job_script.application_id == inserted_application_id
    assert job_script.created_at in window
    assert job_script.updated_at in window


@pytest.mark.asyncio
@database.transaction(force_rollback=True)
async def test_create_job_script_bad_permission(
    fill_application_data,
    fill_job_script_data,
    param_dict,
    client,
    inject_security_header,
):
    """
    Test that it is not possible to create job_script without proper permission.

    This test proves that is not possible to create a job_script without the proper permission.
    We show this by trying to create a job_script without a permission that allow "create" then assert
    that the job_script still does not exists in the database, and the correct status code (403) is returned.
    and that the boto3 method is never called.
    """
    inserted_application_id = await database.execute(
        query=applications_table.insert(),
        values=fill_application_data(application_owner_email="owner1@org.com"),
    )

    inject_security_header("owner1@org.com", "INVALID_PERMISSION")
    response = await client.post(
        "/jobbergate/job-scripts/",
        json=fill_job_script_data(
            application_id=inserted_application_id,
            param_dict=param_dict,
        ),
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN

    count = await database.fetch_all("SELECT COUNT(*) FROM job_scripts")
    assert count[0][0] == 0


@pytest.mark.asyncio
@database.transaction(force_rollback=True)
async def test_create_job_script_without_application(
    fill_job_script_data,
    param_dict,
    client,
    inject_security_header,
):
    """
    Test that is not possible to create a job_script without an application.

    This test proves that is not possible to create a job_script without an existing application.
    We show this by trying to create a job_script without an application created before, then assert that the
    job_script still does not exists in the database and the correct status code (404) is returned.
    """
    inject_security_header("owner1@org.com", Permissions.JOB_SCRIPTS_EDIT)
    response = await client.post(
        "/jobbergate/job-scripts/",
        json=fill_job_script_data(application_id=9999, param_dict=param_dict),
    )
    assert response.status_code == status.HTTP_404_NOT_FOUND

    count = await database.fetch_all("SELECT COUNT(*) FROM job_scripts")
    assert count[0][0] == 0


@pytest.mark.asyncio
@database.transaction(force_rollback=True)
async def test_create_job_script_file_not_found(
    fill_application_data,
    fill_job_script_data,
    param_dict,
    client,
    inject_security_header,
):
    """
    Test that is not possible to create a job_script if the application is in the database but not in S3.

    This test proves that is not possible to create a job_script with an existing application in the
    database but not in S3, this covers for when for some reason the application file in S3 is deleted but it
    remains in the database. We show this by trying to create a job_script with an existing application that
    is not in S3 (raises BotoCoreError), then assert that the job_script still does not exists in the
    database, the correct status code (404) is returned and that the boto3 method was called.
    """
    inserted_application_id = await database.execute(
        query=applications_table.insert(),
        values=fill_application_data(application_owner_email="owner1@org.com"),
    )

    inject_security_header("owner1@org.com", Permissions.JOB_SCRIPTS_EDIT)
    with mock.patch.object(s3man, "s3_client") as s3man_client_mock:
        s3man_client_mock.get_object.side_effect = BotoCoreError()
        response = await client.post(
            "/jobbergate/job-scripts/",
            json=fill_job_script_data(
                application_id=inserted_application_id,
                param_dict=param_dict,
            ),
        )

    assert response.status_code == status.HTTP_404_NOT_FOUND
    s3man_client_mock.get_object.assert_called_once()

    count = await database.fetch_all("SELECT COUNT(*) FROM job_scripts")
    assert count[0][0] == 0


@pytest.mark.asyncio
@mock.patch.object(s3man, "s3_client")
@database.transaction(force_rollback=True)
async def test_get_s3_object_as_tarfile(s3man_client_mock, s3_object):
    """
    Test getting a file from S3 with get_s3_object function.
    """
    s3man_client_mock.get_object.return_value = s3_object

    s3_file = get_s3_object_as_tarfile(1)

    assert s3_file is not None
    s3man_client_mock.get_object.assert_called_once()


@mock.patch.object(s3man, "s3_client")
def test_get_s3_object_not_found(s3man_client_mock):
    """
    Test exception when file not exists in S3 for get_s3_object function.
    """
    s3man_client_mock.get_object.side_effect = BotoCoreError()

    s3_file = None
    with pytest.raises(HTTPException) as exc:
        s3_file = get_s3_object_as_tarfile(1)

    assert "Application with id=1 not found" in str(exc)

    assert s3_file is None
    s3man_client_mock.get_object.assert_called_once()


def test_render_template(param_dict_flat, template_files, job_script_data_as_string):
    """
    Test correctly rendered template for job_script template.
    """
    job_script_rendered = render_template(template_files, param_dict_flat)
    assert json.loads(job_script_rendered) == json.loads(job_script_data_as_string)


def test_build_job_script_data_as_string(s3_object_as_tar, param_dict, job_script_data_as_string):
    """
    Test build_job_script_data_as_string function correct output.
    """
    data_as_string = build_job_script_data_as_string(s3_object_as_tar, param_dict)
    assert json.loads(data_as_string) == json.loads(job_script_data_as_string)


@pytest.mark.asyncio
@database.transaction(force_rollback=True)
async def test_get_job_script_by_id(
    client,
    fill_application_data,
    job_script_data,
    fill_job_script_data,
    inject_security_header,
):
    """
    Test GET /job-scripts/<id>.

    This test proves that GET /job-scripts/<id> returns the correct job-script, owned by
    the user making the request. We show this by asserting that the job_script data
    returned in the response is equal to the job_script data that exists in the database
    for the given job_script id.
    """
    inserted_application_id = await database.execute(
        query=applications_table.insert(),
        values=fill_application_data(application_owner_email="owner1@org.com"),
    )
    inserted_job_script_id = await database.execute(
        query=job_scripts_table.insert(),
        values=fill_job_script_data(application_id=inserted_application_id),
    )

    count = await database.fetch_all("SELECT COUNT(*) FROM job_scripts")
    assert count[0][0] == 1

    inject_security_header("owner1@org.com", Permissions.JOB_SCRIPTS_VIEW)
    response = await client.get(f"/jobbergate/job-scripts/{inserted_job_script_id}")
    assert response.status_code == status.HTTP_200_OK

    data = response.json()
    assert data["id"] == inserted_job_script_id
    assert data["job_script_name"] == job_script_data["job_script_name"]
    assert data["job_script_data_as_string"] == job_script_data["job_script_data_as_string"]
    assert data["job_script_owner_email"] == "owner1@org.com"
    assert data["application_id"] == inserted_application_id


@pytest.mark.asyncio
@database.transaction(force_rollback=True)
async def test_get_job_script_by_id_invalid(client, inject_security_header):
    """
    Test the correct response code is returned when a job_script does not exist.

    This test proves that GET /job-script/<id> returns the correct response code when the
    requested job_script does not exist. We show this by asserting that the status code
    returned is what we would expect given the job_script requested doesn't exist (404).
    """
    inject_security_header("owner1@org.com", Permissions.JOB_SCRIPTS_VIEW)
    response = await client.get("/jobbergate/job-scripts/9999")
    assert response.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.asyncio
@database.transaction(force_rollback=True)
async def test_get_job_script_by_id_bad_permission(
    client,
    fill_application_data,
    fill_job_script_data,
    inject_security_header,
):
    """
    Test the correct response code is returned when the user don't have the proper permission.

    This test proves that GET /job-script/<id> returns the correct response code when the
    user don't have the proper permission. We show this by asserting that the status code
    returned is what we would expect (403).
    """
    inserted_application_id = await database.execute(
        query=applications_table.insert(),
        values=fill_application_data(application_owner_email="owner1@org.com"),
    )
    inserted_job_script_id = await database.execute(
        query=job_scripts_table.insert(),
        values=fill_job_script_data(application_id=inserted_application_id),
    )
    inject_security_header("owner1@org.com", "INVALID_PERMISSION")
    response = await client.get(f"/jobbergate/job-scripts/{inserted_job_script_id}")
    assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.asyncio
@database.transaction(force_rollback=True)
async def test_get_job_script__no_params(
    client,
    fill_application_data,
    fill_all_job_script_data,
    inject_security_header,
):
    """
    Test GET /job-scripts/ returns only job_scripts owned by the user making the request.

    This test proves that GET /job-scripts/ returns the correct job_scripts for the user making
    the request. We show this by asserting that the job_scripts returned in the response are
    only job_scripts owned by the user making the request.
    """
    inserted_application_id = await database.execute(
        query=applications_table.insert(),
        values=fill_application_data(application_owner_email="owner1@org.com"),
    )
    await database.execute_many(
        query=job_scripts_table.insert(),
        values=fill_all_job_script_data(
            dict(
                job_script_name="js1",
                job_script_owner_email="owner1@org.com",
                application_id=inserted_application_id,
            ),
            dict(
                job_script_name="js2",
                job_script_owner_email="owner999@org.com",
                application_id=inserted_application_id,
            ),
            dict(
                job_script_name="js3",
                job_script_owner_email="owner1@org.com",
                application_id=inserted_application_id,
            ),
        ),
    )

    count = await database.fetch_all("SELECT COUNT(*) FROM job_scripts")
    assert count[0][0] == 3

    inject_security_header("owner1@org.com", Permissions.JOB_SCRIPTS_VIEW)
    response = await client.get("/jobbergate/job-scripts/")
    assert response.status_code == status.HTTP_200_OK

    data = response.json()
    results = data.get("results")
    assert results
    assert [d["job_script_name"] for d in results] == ["js1", "js3"]

    pagination = data.get("pagination")
    assert pagination == dict(
        total=2,
        start=None,
        limit=None,
    )


@pytest.mark.asyncio
@database.transaction(force_rollback=True)
async def test_get_job_scripts__bad_permission(
    client,
    fill_application_data,
    fill_job_script_data,
    inject_security_header,
):
    """
    Test GET /job-scripts/ returns 403 since the user don't have the proper permission.

    This test proves that GET /job-scripts/ returns the 403 status code when the user making the request
    don't have the permission to list. We show this by asserting that the response status code is 403.
    """
    inserted_application_id = await database.execute(
        query=applications_table.insert(),
        values=fill_application_data(application_owner_email="owner1@org.com"),
    )
    await database.execute(
        query=job_scripts_table.insert(),
        values=fill_job_script_data(application_id=inserted_application_id),
    )

    count = await database.fetch_all("SELECT COUNT(*) FROM job_scripts")
    assert count[0][0] == 1

    inject_security_header("owner1@org.com", "INVALID_PERMISSION")
    response = await client.get("/jobbergate/job-scripts/")
    assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.asyncio
@database.transaction(force_rollback=True)
async def test_get_job_scripts__with_all_param(
    client,
    fill_application_data,
    fill_all_job_script_data,
    inject_security_header,
):
    """
    Test that listing job_scripts, when all=True, contains job_scripts owned by other users.

    This test proves that the user making the request can see job_scripts owned by other users.
    We show this by creating three job_scripts, one that are owned by the user making the request, and two
    owned by another user. Assert that the response to GET /job-scripts/?all=True includes all three
    job_scripts.
    """
    inserted_application_id = await database.execute(
        query=applications_table.insert(),
        values=fill_application_data(application_owner_email="owner1@org.com"),
    )
    await database.execute_many(
        query=job_scripts_table.insert(),
        values=fill_all_job_script_data(
            {
                "job_script_name": "script1",
                "job_script_owner_email": "owner1@org.com",
                "application_id": inserted_application_id,
            },
            {
                "job_script_name": "script2",
                "job_script_owner_email": "owner999@org.com",
                "application_id": inserted_application_id,
            },
            {
                "job_script_name": "script3",
                "job_script_owner_email": "owner1@org.com",
                "application_id": inserted_application_id,
            },
        ),
    )

    count = await database.fetch_all("SELECT COUNT(*) FROM job_scripts")
    assert count[0][0] == 3

    inject_security_header("owner1@org.com", Permissions.JOB_SCRIPTS_VIEW)
    response = await client.get("/jobbergate/job-scripts/?all=True")
    assert response.status_code == status.HTTP_200_OK

    data = response.json()
    results = data.get("results")
    assert results
    assert [d["job_script_name"] for d in results] == ["script1", "script2", "script3"]

    pagination = data.get("pagination")
    assert pagination == dict(
        total=3,
        start=None,
        limit=None,
    )


@pytest.mark.asyncio
@database.transaction(force_rollback=True)
async def test_get_job_scripts__with_search_param(client, inject_security_header, fill_application_data):
    """
    Test that listing job scripts, when search=<search terms>, returns matches.

    This test proves that the user making the request will be shown job scripts that match the search string.
    We show this by creating job scripts and using various search queries to match against them.

    Assert that the response to GET /job_scripts?search=<search temrms> includes correct matches.
    """
    inserted_application_id = await database.execute(
        query=applications_table.insert(),
        values=fill_application_data(application_owner_email="owner1@org.com"),
    )
    common = dict(job_script_data_as_string="whatever", application_id=inserted_application_id)
    await database.execute_many(
        query=job_scripts_table.insert(),
        values=[
            dict(
                id=1,
                job_script_name="test name one",
                job_script_owner_email="one@org.com",
                **common,
            ),
            dict(
                id=2,
                job_script_name="test name two",
                job_script_owner_email="two@org.com",
                **common,
            ),
            dict(
                id=22,
                job_script_name="test name twenty-two",
                job_script_owner_email="twenty-two@org.com",
                job_script_description="a long description of this job_script",
                **common,
            ),
        ],
    )
    count = await database.fetch_all("SELECT COUNT(*) FROM job_scripts")
    assert count[0][0] == 3

    inject_security_header("admin@org.com", Permissions.JOB_SCRIPTS_VIEW)

    response = await client.get("/jobbergate/job-scripts?all=true&search=one")
    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    results = data.get("results")
    assert [d["id"] for d in results] == [1]

    response = await client.get("/jobbergate/job-scripts?all=true&search=two")
    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    results = data.get("results")
    assert [d["id"] for d in results] == [2, 22]

    response = await client.get("/jobbergate/job-scripts?all=true&search=long")
    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    results = data.get("results")
    assert [d["id"] for d in results] == [22]

    response = await client.get("/jobbergate/job-scripts?all=true&search=name+test")
    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    results = data.get("results")
    assert [d["id"] for d in results] == [1, 2, 22]


@pytest.mark.asyncio
@database.transaction(force_rollback=True)
async def test_get_job_scripts__with_sort_params(
    client,
    fill_application_data,
    inject_security_header,
):
    """
    Test that listing job_scripts with sort params returns correctly ordered matches.

    This test proves that the user making the request will be shown job_scripts sorted in the correct order
    according to the ``sort_field`` and ``sort_ascending`` parameters.
    We show this by creating job_scripts and using various sort parameters to order them.

    Assert that the response to GET /job_scripts?sort_field=<field>&sort_ascending=<bool> includes correctly
    sorted job_script.
    """
    inserted_application_id = await database.execute(
        query=applications_table.insert(),
        values=fill_application_data(application_owner_email="admin@org.com"),
    )
    common = dict(
        job_script_owner_email="admin@org.com",
        job_script_data_as_string="whatever",
        application_id=inserted_application_id,
    )
    await database.execute_many(
        query=job_scripts_table.insert(),
        values=[
            dict(
                job_script_name="Z",
                **common,
            ),
            dict(
                job_script_name="Y",
                **common,
            ),
            dict(
                job_script_name="X",
                **common,
            ),
        ],
    )
    count = await database.fetch_all("SELECT COUNT(*) FROM job_scripts")
    assert count[0][0] == 3

    inject_security_header("admin@org.com", Permissions.JOB_SCRIPTS_VIEW)

    response = await client.get("/jobbergate/job-scripts?sort_field=id")
    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    results = data.get("results")
    assert [d["job_script_name"] for d in results] == ["Z", "Y", "X"]

    response = await client.get("/jobbergate/job-scripts?sort_field=id&sort_ascending=false")
    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    results = data.get("results")
    assert [d["job_script_name"] for d in results] == ["X", "Y", "Z"]

    response = await client.get("/jobbergate/job-scripts?sort_field=job_script_name")
    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    results = data.get("results")
    assert [d["job_script_name"] for d in results] == ["X", "Y", "Z"]

    response = await client.get("/jobbergate/job-scripts?all=true&sort_field=job_script_data_as_string")
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "Invalid sorting column requested" in response.text


@pytest.mark.asyncio
@database.transaction(force_rollback=True)
async def test_get_job_scripts__with_pagination(
    client,
    fill_application_data,
    fill_job_script_data,
    inject_security_header,
):
    """
    Test that listing job_scripts works with pagination.

    This test proves that the user making the request can see job_scripts paginated.
    We show this by creating three job_scripts and assert that the response is correctly paginated.
    """
    inserted_application_id = await database.execute(
        query=applications_table.insert(),
        values=fill_application_data(application_owner_email="owner1@org.com"),
    )
    await database.execute_many(
        query=job_scripts_table.insert(),
        values=[
            fill_job_script_data(
                job_script_name=f"script{i}",
                job_script_owner_email="owner1@org.com",
                application_id=inserted_application_id,
            )
            for i in range(1, 6)
        ],
    )

    count = await database.fetch_all("SELECT COUNT(*) FROM job_scripts")
    assert count[0][0] == 5

    inject_security_header("owner1@org.com", Permissions.JOB_SCRIPTS_VIEW)
    response = await client.get("/jobbergate/job-scripts?start=0&limit=1")
    assert response.status_code == status.HTTP_200_OK

    data = response.json()
    results = data.get("results")
    assert results
    assert [d["job_script_name"] for d in results] == ["script1"]

    pagination = data.get("pagination")
    assert pagination == dict(total=5, start=0, limit=1)

    response = await client.get("/jobbergate/job-scripts?start=1&limit=2")
    assert response.status_code == status.HTTP_200_OK

    data = response.json()
    results = data.get("results")
    assert results
    assert [d["job_script_name"] for d in results] == ["script3", "script4"]

    pagination = data.get("pagination")
    assert pagination == dict(total=5, start=1, limit=2)

    response = await client.get("/jobbergate/job-scripts?start=2&limit=2")
    assert response.status_code == status.HTTP_200_OK

    data = response.json()
    results = data.get("results")
    assert results
    assert [d["job_script_name"] for d in results] == ["script5"]

    pagination = data.get("pagination")
    assert pagination == dict(total=5, start=2, limit=2)


@pytest.mark.freeze_time
@pytest.mark.asyncio
@database.transaction(force_rollback=True)
async def test_update_job_script(
    client,
    fill_application_data,
    fill_job_script_data,
    inject_security_header,
    time_frame,
):
    """
    Test update job_script via PUT.

    This test proves that the job_script values are correctly updated following a PUT request to the
    /job-scripts/<id> endpoint. We show this by assert the response status code to 201, the response data
    corresponds to the updated data, and the data in the database is also updated.
    """
    inserted_application_id = await database.execute(
        query=applications_table.insert(),
        values=fill_application_data(application_owner_email="owner1@org.com"),
    )
    inserted_job_script_id = await database.execute(
        query=job_scripts_table.insert(),
        values=fill_job_script_data(application_id=inserted_application_id),
    )

    inject_security_header("owner1@org.com", Permissions.JOB_SCRIPTS_EDIT)
    with time_frame() as window:
        response = await client.put(
            f"/jobbergate/job-scripts/{inserted_job_script_id}",
            json={
                "job_script_name": "new name",
                "job_script_description": "new description",
                "job_script_data_as_string": "new value",
            },
        )

    assert response.status_code == status.HTTP_200_OK
    data = response.json()

    assert data["job_script_name"] == "new name"
    assert data["job_script_description"] == "new description"
    assert data["job_script_data_as_string"] == "new value"
    assert data["id"] == inserted_job_script_id

    query = job_scripts_table.select(job_scripts_table.c.id == inserted_job_script_id)
    job_script = JobScriptResponse.parse_obj(await database.fetch_one(query))

    assert job_script is not None
    assert job_script.job_script_name == "new name"
    assert job_script.job_script_description == "new description"
    assert job_script.job_script_data_as_string == "new value"
    assert job_script.updated_at in window


@pytest.mark.asyncio
@database.transaction(force_rollback=True)
async def test_update_job_script_not_found(
    client,
    inject_security_header,
):
    """
    Test that it is not possible to update a job_script not found.

    This test proves that it is not possible to update a job_script if it is not found. We show this by
    asserting that the response status code of the request is 404.
    """
    inject_security_header("owner1@org.com", Permissions.JOB_SCRIPTS_EDIT)
    response = await client.put("/jobbergate/job-scripts/123", json={"job_script_name": "new name"})

    assert response.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.asyncio
@database.transaction(force_rollback=True)
async def test_update_job_script_bad_permission(
    client,
    fill_application_data,
    fill_job_script_data,
    inject_security_header,
):
    """
    Test that it is not possible to update a job_script if the user don't have the proper permission.

    This test proves that it is not possible to update a job_script if the user don't have permission. We
    show this by asserting that the response status code of the request is 403, and that the data stored in
    the database for the job_script is not updated.
    """
    inserted_application_id = await database.execute(
        query=applications_table.insert(),
        values=fill_application_data(application_owner_email="owner1@org.com"),
    )
    await database.execute(
        query=job_scripts_table.insert(),
        values=fill_job_script_data(job_script_name="target-js", application_id=inserted_application_id),
    )

    inject_security_header("owner1@org.com", "INVALID_PERMISSION")
    response = await client.put("/jobbergate/job-scripts/1", data={"job_script_name": "new name"})

    assert response.status_code == status.HTTP_403_FORBIDDEN

    query = job_scripts_table.select(job_scripts_table.c.job_script_name == "target-js")
    job_script_row = await database.fetch_one(query)

    assert job_script_row is not None
    assert job_script_row["job_script_name"] == "target-js"


@pytest.mark.asyncio
@database.transaction(force_rollback=True)
async def test_delete_job_script(
    client,
    fill_application_data,
    fill_job_script_data,
    inject_security_header,
):
    """
    Test delete job_script via DELETE.

    This test proves that a job_script is successfully deleted via a DELETE request to the /job-scripts/<id>
    endpoint. We show this by asserting that the job_script no longer exists in the database after the
    request is made and the correct status code is returned (204).
    """
    inserted_application_id = await database.execute(
        query=applications_table.insert(),
        values=fill_application_data(),
    )
    inserted_job_script_id = await database.execute(
        query=job_scripts_table.insert(),
        values=fill_job_script_data(application_id=inserted_application_id),
    )

    count = await database.fetch_all("SELECT COUNT(*) FROM job_scripts")
    assert count[0][0] == 1

    inject_security_header("owner1@org.com", Permissions.JOB_SCRIPTS_EDIT)
    response = await client.delete(f"/jobbergate/job-scripts/{inserted_job_script_id}")

    assert response.status_code == status.HTTP_204_NO_CONTENT

    count = await database.fetch_all("SELECT COUNT(*) FROM job_scripts")
    assert count[0][0] == 0


@pytest.mark.asyncio
@database.transaction(force_rollback=True)
async def test_delete_job_script_not_found(client, inject_security_header):
    """
    Test that it is not possible to delete a job_script that is not found.

    This test proves that it is not possible to delete a job_script if it does not exists. We show this by
    assert that a 404 response status code is returned.
    """
    inject_security_header("owner1@org.com", Permissions.JOB_SCRIPTS_EDIT)
    response = await client.delete("/jobbergate/job-scripts/9999")

    assert response.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.asyncio
@database.transaction(force_rollback=True)
async def test_delete_job_script_bad_permission(
    client,
    fill_application_data,
    fill_job_script_data,
    inject_security_header,
):
    """
    Test that it is not possible to delete a job_script when the user don't have the permission.

    This test proves that it is not possible to delete a job_script if the user don't have the permission.
    We show this by assert that a 403 response status code is returned and the job_script still exists in
    the database after the request.
    """
    inserted_application_id = await database.execute(
        query=applications_table.insert(),
        values=fill_application_data(),
    )
    inserted_job_script_id = await database.execute(
        query=job_scripts_table.insert(),
        values=fill_job_script_data(application_id=inserted_application_id),
    )

    count = await database.fetch_all("SELECT COUNT(*) FROM job_scripts")
    assert count[0][0] == 1

    inject_security_header("owner1@org.com", "INVALID_PERMISSION")
    response = await client.delete(f"/jobbergate/job-scripts/{inserted_job_script_id}")

    assert response.status_code == status.HTTP_403_FORBIDDEN

    count = await database.fetch_all("SELECT COUNT(*) FROM job_scripts")
    assert count[0][0] == 1


@pytest.mark.asyncio
@database.transaction(force_rollback=True)
async def test_delete_job_script__fk_error(
    client,
    fill_application_data,
    fill_job_script_data,
    inject_security_header,
):
    """
    Test DELETE /job_script/<id> correctly returns a 409 on a foreign-key constraint error.
    """
    inserted_application_id = await database.execute(
        query=applications_table.insert(),
        values=fill_application_data(),
    )
    inserted_job_script_id = await database.execute(
        query=job_scripts_table.insert(),
        values=fill_job_script_data(application_id=inserted_application_id),
    )

    count = await database.fetch_all("SELECT COUNT(*) FROM job_scripts")
    assert count[0][0] == 1

    inject_security_header("owner1@org.com", Permissions.JOB_SCRIPTS_EDIT)
    with mock.patch(
        "jobbergate_api.storage.database.execute",
        side_effect=asyncpg.exceptions.ForeignKeyViolationError(
            f"""
            update or delete on table "job_scripts" violates foreign key constraint
            "job_submissions_job_script_id_fkey" on table "job_submissions"
            DETAIL:  Key (id)=({inserted_job_script_id}) is still referenced from table "job_submissions".
            """
        ),
    ):
        response = await client.delete(f"/jobbergate/job-scripts/{inserted_job_script_id}")
    assert response.status_code == status.HTTP_409_CONFLICT
    error_data = json.loads(response.text)["detail"]
    assert error_data["message"] == "Delete failed due to foreign-key constraint"
    assert error_data["table"] == "job_submissions"
    assert error_data["pk_id"] == f"{inserted_job_script_id}"
