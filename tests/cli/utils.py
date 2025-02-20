import os
import pytest
import shutil

from dlt.common import git
from dlt.common.pipeline import get_dlt_repos_dir
from dlt.common.storages.file_storage import FileStorage

from dlt.common.utils import set_working_dir, uniq_id

from dlt.cli import echo
from dlt.cli.init_command import DEFAULT_PIPELINES_REPO
from dlt.extract.decorators import _SOURCES

from tests.utils import TEST_STORAGE_ROOT


INIT_REPO_LOCATION = DEFAULT_PIPELINES_REPO
INIT_REPO_BRANCH = "master"
PROJECT_DIR = os.path.join(TEST_STORAGE_ROOT, "project")


@pytest.fixture(autouse=True)
def echo_default_choice() -> None:
    """Always answer default in CLI interactions"""
    echo.ALWAYS_CHOOSE_DEFAULT = True
    yield
    echo.ALWAYS_CHOOSE_DEFAULT = False


@pytest.fixture(scope="module")
def cloned_pipeline() -> FileStorage:
    return git.get_fresh_repo_files(INIT_REPO_LOCATION, get_dlt_repos_dir(), branch=INIT_REPO_BRANCH)


@pytest.fixture
def repo_dir(cloned_pipeline: FileStorage) -> str:
    return get_repo_dir(cloned_pipeline)


@pytest.fixture
def project_files() -> FileStorage:
    project_files = get_project_files()
    with set_working_dir(project_files.storage_path):
        yield project_files


def get_repo_dir(cloned_pipeline: FileStorage) -> str:
    repo_dir = os.path.abspath(os.path.join(TEST_STORAGE_ROOT, f"pipelines_repo_{uniq_id()}"))
    # copy the whole repo into TEST_STORAGE_ROOT
    shutil.copytree(cloned_pipeline.storage_path, repo_dir)
    return repo_dir


def get_project_files() -> FileStorage:
    _SOURCES.clear()
    # project dir
    return FileStorage(PROJECT_DIR, makedirs=True)
