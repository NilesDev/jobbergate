"""
Router for the JobScript resource.
"""
import json
import tarfile
import tempfile
from io import BytesIO, StringIO
from typing import List, Optional

from armasec import TokenPayload
from botocore.exceptions import BotoCoreError
from fastapi import APIRouter, Depends, HTTPException, Query, status
from jinja2 import Template
from loguru import logger
from yaml import safe_load

from jobbergate_api.apps.applications.models import applications_table
from jobbergate_api.apps.applications.schemas import ApplicationResponse
from jobbergate_api.apps.job_scripts.models import job_scripts_table, searchable_fields, sortable_fields
from jobbergate_api.apps.job_scripts.schemas import (
    JobScriptCreateRequest,
    JobScriptResponse,
    JobScriptUpdateRequest,
)
from jobbergate_api.apps.permissions import Permissions
from jobbergate_api.pagination import Pagination, ok_response, package_response
from jobbergate_api.s3_manager import S3Manager
from jobbergate_api.security import IdentityClaims, guard
from jobbergate_api.storage import INTEGRITY_CHECK_EXCEPTIONS, database, search_clause, sort_clause

router = APIRouter()
s3man = S3Manager()


def inject_sbatch_params(job_script_data_as_string: str, sbatch_params: List[str]) -> str:
    """
    Inject sbatch params into job script.

    Given the job script as job_script_data_as_string, inject the sbatch params in the correct location.
    """
    if sbatch_params == []:
        return job_script_data_as_string

    first_sbatch_index = job_script_data_as_string.find("#SBATCH")
    string_slice = job_script_data_as_string[first_sbatch_index:]
    line_end = string_slice.find("\n") + first_sbatch_index + 1

    inner_string = ""
    for parameter in sbatch_params:
        inner_string += "#SBATCH " + parameter + "\\n"

    new_job_script_data_as_string = (
        job_script_data_as_string[:line_end] + inner_string + job_script_data_as_string[line_end:]
    )
    return new_job_script_data_as_string


def get_s3_object_as_tarfile(application_id):
    """
    Return the tarfile of a S3 object.
    """
    try:
        s3_application_obj = s3man.get(app_id=application_id)
    except BotoCoreError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Application with id={application_id} not found in S3",
        )
    s3_application_tar = tarfile.open(fileobj=BytesIO(s3_application_obj["Body"].read()))
    return s3_application_tar


def render_template(template_files, param_dict_flat):
    """
    Render the templates as strings using jinja2.
    """
    for key, value in template_files.items():
        template = Template(value)
        rendered_js = template.render(data=param_dict_flat)
        template_files[key] = rendered_js
    job_script_data_as_string = json.dumps(template_files)
    return job_script_data_as_string


def build_job_script_data_as_string(s3_application_tar, param_dict):
    """
    Return the job_script_data_as string from the S3 application and the templates.
    """
    support_files_output = param_dict["jobbergate_config"].get("supporting_files_output_name")
    if support_files_output is None:
        support_files_output = dict()

    supporting_files = param_dict["jobbergate_config"].get("supporting_files")
    if supporting_files is None:
        supporting_files = list()

    default_template = [
        default_template := param_dict["jobbergate_config"].get("default_template"),
        "templates/" + default_template,
    ]

    template_files = {}
    for member in s3_application_tar.getmembers():
        if member.name in default_template:
            contentfobj = s3_application_tar.extractfile(member)
            template_files["application.sh"] = contentfobj.read().decode("utf-8")
        if member.name in supporting_files:
            match = [x for x in support_files_output if member.name in x]
            contentfobj = s3_application_tar.extractfile(member)
            filename = support_files_output[match[0]][0]
            template_files[filename] = contentfobj.read().decode("utf-8")

    # Use tempfile to generate .tar in memory - NOT write to disk
    param_dict_flat = {}
    for (key, value) in param_dict.items():
        if isinstance(value, dict):
            for nest_key, nest_value in value.items():
                param_dict_flat[nest_key] = nest_value
        else:
            param_dict_flat[key] = value
    with tempfile.NamedTemporaryFile("wb", suffix=".tar.gz", delete=False) as f:
        with tarfile.open(fileobj=f, mode="w:gz") as rendered_tar:
            for member in s3_application_tar.getmembers():
                if member.name in supporting_files:
                    contentfobj = s3_application_tar.extractfile(member)
                    supporting_file = contentfobj.read().decode("utf-8")
                    template = Template(supporting_file)
                    rendered_str = template.render(data=param_dict_flat)
                    tarinfo = tarfile.TarInfo(member.name)
                    rendered_tar.addfile(tarinfo, StringIO(rendered_str))
        f.flush()
        f.seek(0)

    job_script_data_as_string = render_template(template_files, param_dict_flat)
    return job_script_data_as_string


@router.post(
    "/job-scripts",
    status_code=status.HTTP_201_CREATED,
    response_model=JobScriptResponse,
    description="Endpoint for job_script creation",
)
async def job_script_create(
    job_script: JobScriptCreateRequest,
    token_payload: TokenPayload = Depends(guard.lockdown(Permissions.JOB_SCRIPTS_EDIT)),
):
    """
    Create a new job script.

    Make a post request to this endpoint with the required values to create a new job script.
    """
    logger.debug(f"Creating job_script with: {job_script}")
    select_query = applications_table.select().where(applications_table.c.id == job_script.application_id)
    raw_application = await database.fetch_one(select_query)

    if not raw_application:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Application with id={job_script.application_id} not found.",
        )
    application = ApplicationResponse.parse_obj(raw_application)
    logger.debug("Fetching application tarfile")
    s3_application_tar = get_s3_object_as_tarfile(application.id)

    identity_claims = IdentityClaims.from_token_payload(token_payload)

    create_dict = dict(
        **{k: v for (k, v) in job_script.dict(exclude_unset=True).items() if k != "param_dict"},
        job_script_owner_email=identity_claims.user_email,
    )

    # Use application_config from the application as a baseline of defaults
    print("APP CONFIG: ", application.application_config)
    param_dict = safe_load(application.application_config)

    # User supplied param dict is optional and may override defaults
    param_dict.update(**job_script.param_dict)

    logger.debug("Rendering job_script data as string")
    job_script_data_as_string = build_job_script_data_as_string(s3_application_tar, param_dict)

    sbatch_params = create_dict.pop("sbatch_params", [])
    create_dict["job_script_data_as_string"] = inject_sbatch_params(job_script_data_as_string, sbatch_params)

    logger.debug("Inserting job_script")
    try:
        insert_query = job_scripts_table.insert().returning(job_scripts_table)
        job_script_data = await database.fetch_one(query=insert_query, values=create_dict)

    except INTEGRITY_CHECK_EXCEPTIONS as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))

    logger.debug(f"Created job_script={job_script_data}")
    return job_script_data


@router.get(
    "/job-scripts/{job_script_id}",
    description="Endpoint to get a job_script",
    response_model=JobScriptResponse,
    dependencies=[Depends(guard.lockdown(Permissions.JOB_SCRIPTS_VIEW))],
)
async def job_script_get(job_script_id: int = Query(...)):
    """
    Return the job_script given its id.
    """
    query = job_scripts_table.select().where(job_scripts_table.c.id == job_script_id)
    job_script = await database.fetch_one(query)

    if not job_script:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"JobScript with id={job_script_id} not found.",
        )
    return job_script


@router.get(
    "/job-scripts",
    description="Endpoint to list job_scripts",
    responses=ok_response(JobScriptResponse),
)
async def job_script_list(
    pagination: Pagination = Depends(),
    all: Optional[bool] = Query(False),
    search: Optional[str] = Query(None),
    sort_field: Optional[str] = Query(None),
    sort_ascending: bool = Query(True),
    token_payload: TokenPayload = Depends(guard.lockdown(Permissions.JOB_SCRIPTS_VIEW)),
):
    """
    List job_scripts for the authenticated user.

    Note::

       Use responses instead of response_model to skip a second round of validation and serialization. This
       is already happening in the ``package_response`` method. So, we uses ``responses`` so that FastAPI
       can generate the correct OpenAPI spec but not post-process the response.
    """
    query = job_scripts_table.select()
    identity_claims = IdentityClaims.from_token_payload(token_payload)
    if not all:
        query = query.where(job_scripts_table.c.job_script_owner_email == identity_claims.user_email)
    if search is not None:
        query = query.where(search_clause(search, searchable_fields))
    if sort_field is not None:
        query = query.order_by(sort_clause(sort_field, sortable_fields, sort_ascending))
    return await package_response(JobScriptResponse, query, pagination)


@router.delete(
    "/job-scripts/{job_script_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    description="Endpoint to delete job script",
    dependencies=[Depends(guard.lockdown(Permissions.JOB_SCRIPTS_EDIT))],
)
async def job_script_delete(job_script_id: int = Query(..., description="id of the job script to delete")):
    """
    Delete job_script given its id.
    """
    where_stmt = job_scripts_table.c.id == job_script_id

    get_query = job_scripts_table.select().where(where_stmt)
    raw_job_script = await database.fetch_one(get_query)
    if not raw_job_script:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"JobScript with id={job_script_id} not found.",
        )

    delete_query = job_scripts_table.delete().where(where_stmt)
    await database.execute(delete_query)


@router.put(
    "/job-scripts/{job_script_id}",
    status_code=status.HTTP_200_OK,
    description="Endpoint to update a job_script given the id",
    response_model=JobScriptResponse,
    dependencies=[Depends(guard.lockdown(Permissions.JOB_SCRIPTS_EDIT))],
)
async def job_script_update(job_script_id: int, job_script: JobScriptUpdateRequest):
    """
    Update a job_script given its id.
    """
    update_query = (
        job_scripts_table.update()
        .where(job_scripts_table.c.id == job_script_id)
        .values(job_script.dict(exclude_unset=True))
        .returning(job_scripts_table)
    )
    try:
        result = await database.fetch_one(update_query)
    except INTEGRITY_CHECK_EXCEPTIONS as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))

    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"JobScript with id={job_script_id} not found.",
        )

    return result


def include_router(app):
    """
    Include the router for this module in the app.
    """
    app.include_router(router)
