from typing import TYPE_CHECKING, Optional

from dlt.common.configuration import configspec
from dlt.common.configuration.specs import LoadVolumeConfiguration
from dlt.common.runners.configuration import PoolRunnerConfiguration, TPoolType


@configspec(init=True)
class LoaderConfiguration(PoolRunnerConfiguration):
    workers: int = 20
    """how many parallel loads can be executed"""
    pool_type: TPoolType = "thread"  # mostly i/o (upload) so may be thread pool
    raise_on_failed_jobs: bool = False
    """when True, raises on terminally failed jobs immediately"""
    raise_on_max_retries: int = 5
    """When gt 0 will raise when job reaches raise_on_max_retries"""
    _load_storage_config: LoadVolumeConfiguration = None

    if TYPE_CHECKING:
        def __init__(
            self,
            pool_type: TPoolType = None,
            workers: int = None,
            raise_on_failed_jobs: bool = False,
            _load_storage_config: LoadVolumeConfiguration = None
        ) -> None:
            ...
