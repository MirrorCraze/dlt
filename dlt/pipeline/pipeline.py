import contextlib
import os
import datetime  # noqa: 251
from contextlib import contextmanager
from functools import wraps
from collections.abc import Sequence as C_Sequence
from typing import Any, Callable, ClassVar, List, Iterator, Optional, Sequence, Tuple, cast, get_type_hints, ContextManager

from dlt import version
from dlt.common import json, logger, pendulum
from dlt.common.configuration import inject_section, known_sections
from dlt.common.configuration.specs import RunConfiguration, NormalizeVolumeConfiguration, SchemaVolumeConfiguration, LoadVolumeConfiguration, CredentialsConfiguration
from dlt.common.configuration.container import Container
from dlt.common.configuration.exceptions import ConfigFieldMissingException, ContextDefaultCannotBeCreated
from dlt.common.configuration.specs.config_section_context import ConfigSectionContext
from dlt.common.exceptions import MissingDependencyException
from dlt.common.normalizers import default_normalizers, import_normalizers
from dlt.common.runtime import signals, initialize_runtime
from dlt.common.schema.exceptions import InvalidDatasetName
from dlt.common.schema.typing import TColumnSchema, TSchemaTables, TWriteDisposition
from dlt.common.storages.load_storage import LoadJobInfo, LoadPackageInfo, LoadStorage
from dlt.common.typing import TFun, TSecretValue
from dlt.common.runners import pool_runner as runner
from dlt.common.storages import LiveSchemaStorage, NormalizeStorage, SchemaStorage
from dlt.common.destination import DestinationCapabilitiesContext
from dlt.common.destination.reference import DestinationReference, JobClientBase, DestinationClientConfiguration, DestinationClientDwhConfiguration, TDestinationReferenceArg
from dlt.common.pipeline import ExtractInfo, LoadInfo, NormalizeInfo, SupportsPipeline, TPipelineLocalState, TPipelineState, StateInjectableContext
from dlt.common.schema import Schema
from dlt.common.storages.file_storage import FileStorage
from dlt.common.utils import is_interactive

from dlt.destinations.exceptions import DatabaseUndefinedRelation

from dlt.extract.exceptions import DataItemRequiredForDynamicTableHints, SourceExhausted
from dlt.extract.extract import ExtractorStorage, extract_with_schema
from dlt.extract.source import DltResource, DltSource
from dlt.extract.typing import TColumnKey
from dlt.normalize import Normalize
from dlt.normalize.configuration import NormalizeConfiguration
from dlt.destinations.sql_client import SqlClientBase
from dlt.destinations.job_client_impl import SqlJobClientBase
from dlt.load.configuration import LoaderConfiguration
from dlt.load import Load

from dlt.pipeline.configuration import PipelineConfiguration
from dlt.pipeline.progress import _Collector, _NULL_COLLECTOR
from dlt.pipeline.exceptions import CannotRestorePipelineException, InvalidPipelineName, PipelineConfigMissing, PipelineStepFailed, SqlClientNotAvailable
from dlt.pipeline.trace import PipelineTrace, PipelineStepTrace, load_trace, merge_traces, start_trace, start_trace_step, end_trace_step, end_trace
from dlt.pipeline.typing import TPipelineStep
from dlt.pipeline.state_sync import STATE_ENGINE_VERSION, load_state_from_destination, merge_state_if_changed, migrate_state, state_resource, json_encode_state, json_decode_state


def with_state_sync(may_extract_state: bool = False) -> Callable[[TFun], TFun]:

    def decorator(f: TFun) -> TFun:
        @wraps(f)
        def _wrap(self: "Pipeline", *args: Any, **kwargs: Any) -> Any:
            # backup and restore state
            should_extract_state = may_extract_state and self.config.restore_from_destination
            with self.managed_state(extract_state=should_extract_state) as state:
                # add the state to container as a context
                with self._container.injectable_context(StateInjectableContext(state=state)):
                    return f(self, *args, **kwargs)

        return _wrap  # type: ignore

    return decorator


def with_schemas_sync(f: TFun) -> TFun:

    @wraps(f)
    def _wrap(self: "Pipeline", *args: Any, **kwargs: Any) -> Any:
        for name in self._schema_storage.live_schemas:
            # refresh live schemas in storage or import schema path
            self._schema_storage.commit_live_schema(name)
        rv = f(self, *args, **kwargs)
        # refresh list of schemas if any new schemas are added
        self.schema_names = self._schema_storage.list_schemas()
        return rv

    return _wrap  # type: ignore


def with_runtime_trace(f: TFun) -> TFun:

    @wraps(f)
    def _wrap(self: "Pipeline", *args: Any, **kwargs: Any) -> Any:
        trace: PipelineTrace = self._trace
        trace_step: PipelineStepTrace = None
        step_info: Any = None
        is_new_trace = self._trace is None and self.config.enable_runtime_trace

        # create a new trace if we enter a traced function and there's no current trace
        if is_new_trace:
            self._trace = trace = start_trace(cast(TPipelineStep, f.__name__), self)

        try:
            # start a trace step for wrapped function
            if trace:
                trace_step = start_trace_step(trace, cast(TPipelineStep, f.__name__), self)

            step_info = f(self, *args, **kwargs)
            return step_info
        except Exception as ex:
            step_info = ex  # step info is an exception
            raise
        finally:
            try:
                if trace_step:
                    # if there was a step, finish it
                    end_trace_step(self._trace, trace_step, self, step_info)
                if is_new_trace:
                    assert trace is self._trace, f"Messed up trace reference {id(self._trace)} vs {id(trace)}"
                    end_trace(trace, self, self._pipeline_storage.storage_path)
            finally:
                # always end trace
                if is_new_trace:
                    assert self._trace == trace, f"Messed up trace reference {id(self._trace)} vs {id(trace)}"
                    # if we end new trace that had only 1 step, add it to previous trace
                    # this way we combine several separate calls to extract, normalize, load as single trace
                    # the trace of "run" has many steps and will not be merged
                    self._last_trace = merge_traces(self._last_trace, trace)
                    self._trace = None

    return _wrap  # type: ignore


def with_config_section(sections: Tuple[str, ...]) -> Callable[[TFun], TFun]:

    def decorator(f: TFun) -> TFun:

        @wraps(f)
        def _wrap(self: "Pipeline", *args: Any, **kwargs: Any) -> Any:
            # add section context to the container to be used by all configuration without explicit sections resolution
            with inject_section(ConfigSectionContext(pipeline_name=self.pipeline_name, sections=sections)):
                return f(self, *args, **kwargs)

        return _wrap  # type: ignore

    return decorator


class Pipeline(SupportsPipeline):

    STATE_FILE: ClassVar[str] = "state.json"
    STATE_PROPS: ClassVar[List[str]] = list(get_type_hints(TPipelineState).keys())
    LOCAL_STATE_PROPS: ClassVar[List[str]] = list(get_type_hints(TPipelineLocalState).keys())
    DEFAULT_DATASET_SUFFIX: ClassVar[str] = "_dataset"

    pipeline_name: str
    """Name of the pipeline"""
    default_schema_name: str = None
    schema_names: List[str] = []
    first_run: bool = False
    """Indicates a first run of the pipeline, where run ends with successful loading of the data"""
    full_refresh: bool
    must_attach_to_local_pipeline: bool
    pipelines_dir: str
    """A directory where the pipelines' working directories are created"""
    working_dir: str
    """A working directory of the pipeline"""
    destination: DestinationReference = None
    """The destination reference which is ModuleType. `destination.__name__` returns the name string"""
    dataset_name: str = None
    """Name of the dataset to which pipeline will be loaded to"""
    credentials: Any = None
    is_active: bool = False
    """Tells if instance is currently active and available via dlt.pipeline()"""
    collector: _Collector
    config: PipelineConfiguration
    runtime_config: RunConfiguration

    def __init__(
            self,
            pipeline_name: str,
            pipelines_dir: str,
            pipeline_salt: TSecretValue,
            destination: DestinationReference,
            dataset_name: str,
            credentials: Any,
            import_schema_path: str,
            export_schema_path: str,
            full_refresh: bool,
            progress: _Collector,
            must_attach_to_local_pipeline: bool,
            config: PipelineConfiguration,
            runtime: RunConfiguration
        ) -> None:
        """Initializes the Pipeline class which implements `dlt` pipeline. Please use `pipeline` function in `dlt` module to create a new Pipeline instance."""

        self.pipeline_salt = pipeline_salt
        self.config = config
        self.runtime_config = runtime
        self.full_refresh = full_refresh
        self.collector = progress or _NULL_COLLECTOR

        self._container = Container()
        self._pipeline_instance_id = self._create_pipeline_instance_id()
        self._pipeline_storage: FileStorage = None
        self._schema_storage: LiveSchemaStorage = None
        self._schema_storage_config: SchemaVolumeConfiguration = None
        self._normalize_storage_config: NormalizeVolumeConfiguration = None
        self._load_storage_config: LoadVolumeConfiguration = None
        self._trace: PipelineTrace = None
        self._last_trace: PipelineTrace = None
        self._state_restored: bool = False

        initialize_runtime(self.runtime_config)
        # initialize pipeline working dir
        self._init_working_dir(pipeline_name, pipelines_dir)

        with self.managed_state() as state:
            # set the pipeline properties from state
            self._state_to_props(state)
            # we overwrite the state with the values from init
            self._set_destination(destination)  # changing the destination could be dangerous if pipeline has not loaded items
            self._set_dataset_name(dataset_name)
            self.credentials = credentials
            self._configure(import_schema_path, export_schema_path, must_attach_to_local_pipeline)

    def drop(self) -> "Pipeline":
        """Deletes existing pipeline state, schemas and drops datasets at the destination if present"""
        # reset the pipeline working dir
        self._create_pipeline()
        # clone the pipeline
        return Pipeline(
            self.pipeline_name,
            self.pipelines_dir,
            self.pipeline_salt,
            self.destination,
            self.dataset_name,
            self.credentials,
            self._schema_storage.config.import_schema_path,
            self._schema_storage.config.export_schema_path,
            self.full_refresh,
            self.collector,
            False,
            self.config,
            self.runtime_config
        )

    @with_runtime_trace
    @with_schemas_sync  # this must precede with_state_sync
    @with_state_sync(may_extract_state=True)
    @with_config_section((known_sections.EXTRACT,))
    def extract(
        self,
        data: Any,
        *,
        table_name: str = None,
        parent_table_name: str = None,
        write_disposition: TWriteDisposition = None,
        columns: Sequence[TColumnSchema] = None,
        primary_key: TColumnKey = None,
        schema: Schema = None,
        max_parallel_items: int = None,
        workers: int = None
    ) -> ExtractInfo:
        """Extracts the `data` and prepare it for the normalization. Does not require destination or credentials to be configured. See `run` method for the arguments' description."""
        # create extract storage to which all the sources will be extracted
        storage = ExtractorStorage(self._normalize_storage_config)
        extract_ids: List[str] = []
        try:
            with self._maybe_destination_capabilities():
                # extract all sources
                for source in self._data_to_sources(data, schema, table_name, parent_table_name, write_disposition, columns, primary_key):
                    if source.exhausted:
                        raise SourceExhausted(source.name)
                    # TODO: merge infos for all the sources
                    extract_ids.append(
                        self._extract_source(storage, source, max_parallel_items, workers)
                    )
                # commit extract ids
                # TODO: if we fail here we should probably wipe out the whole extract folder
                for extract_id in extract_ids:
                    storage.commit_extract_files(extract_id)
                return ExtractInfo()
        except Exception as exc:
            # TODO: provide metrics from extractor
            raise PipelineStepFailed(self, "extract", exc, ExtractInfo()) from exc

    @with_runtime_trace
    @with_schemas_sync
    @with_config_section((known_sections.NORMALIZE,))
    def normalize(self, workers: int = 1) -> NormalizeInfo:
        """Normalizes the data prepared with `extract` method, infers the schema and creates load packages for the `load` method. Requires `destination` to be known."""
        if is_interactive() and workers > 1:
            raise NotImplementedError("Do not use normalize workers in interactive mode ie. in notebook")
        # check if any schema is present, if not then no data was extracted
        if not self.default_schema_name:
            return None

        # make sure destination capabilities are available
        self._get_destination_capabilities()
        # create default normalize config
        normalize_config = NormalizeConfiguration(
            workers=workers,
            pool_type="none" if workers == 1 else "process",
            _schema_storage_config=self._schema_storage_config,
            _normalize_storage_config=self._normalize_storage_config,
            _load_storage_config=self._load_storage_config
        )
        # run with destination context
        with self._maybe_destination_capabilities():
            # shares schema storage with the pipeline so we do not need to install
            normalize = Normalize(collector=self.collector, config=normalize_config, schema_storage=self._schema_storage)
            try:
                with signals.delayed_signals():
                    runner.run_pool(normalize.config, normalize)
                return NormalizeInfo()
            except Exception as n_ex:
                raise PipelineStepFailed(self, "normalize", n_ex, Normalize()) from n_ex

    @with_runtime_trace
    @with_schemas_sync
    @with_state_sync()
    @with_config_section((known_sections.LOAD,))
    def load(
        self,
        destination: TDestinationReferenceArg = None,
        dataset_name: str = None,
        credentials: Any = None,
        *,
        workers: int = 20,
        raise_on_failed_jobs: bool = False
    ) -> LoadInfo:
        """Loads the packages prepared by `normalize` method into the `dataset_name` at `destination`, using provided `credentials`"""
        # set destination and default dataset if provided
        self._set_destination(destination)
        self._set_dataset_name(dataset_name)
        self.credentials = credentials or self.credentials


        # check if any schema is present, if not then no data was extracted
        if not self.default_schema_name:
            return None

        # make sure that destination is set and client is importable and can be instantiated
        client = self._get_destination_client(self.default_schema)

        # create default loader config and the loader
        load_config = LoaderConfiguration(
            workers=workers,
            raise_on_failed_jobs=raise_on_failed_jobs,
            _load_storage_config=self._load_storage_config
        )
        load = Load(self.destination, collector=self.collector, is_storage_owner=False, config=load_config, initial_client_config=client.config)
        try:
            with signals.delayed_signals():
                runner.run_pool(load.config, load)
            info = self._get_load_info(load)
            self.first_run = False
            return info
        except Exception as l_ex:
            raise PipelineStepFailed(self, "load", l_ex, self._get_load_info(load)) from l_ex

    @with_runtime_trace
    @with_config_section(("run",))
    def run(
        self,
        data: Any = None,
        *,
        destination: TDestinationReferenceArg = None,
        dataset_name: str = None,
        credentials: Any = None,
        table_name: str = None,
        write_disposition: TWriteDisposition = None,
        columns: Sequence[TColumnSchema] = None,
        primary_key: TColumnKey = None,
        schema: Schema = None
    ) -> LoadInfo:
        """Loads the data from `data` argument into the destination specified in `destination` and dataset specified in `dataset_name`.

        ### Summary
        This method will `extract` the data from the `data` argument, infer the schema, `normalize` the data into a load package (ie. jsonl or PARQUET files representing tables) and then `load` such packages into the `destination`.

        The data may be supplied in several forms:
        * a `list` or `Iterable` of any JSON-serializable objects ie. `dlt.run([1, 2, 3], table_name="numbers")`
        * any `Iterator` or a function that yield (`Generator`) ie. `dlt.run(range(1, 10), table_name="range")`
        * a function or a list of functions decorated with @dlt.resource ie. `dlt.run([chess_players(title="GM"), chess_games()])`
        * a function or a list of functions decorated with @dlt.source.

        Please note that `dlt` deals with `bytes`, `datetime`, `decimal` and `uuid` objects so you are free to load documents containing ie. binary data or dates.

        ### Execution
        The `run` method will first use `sync_destination` method to synchronize pipeline state and schemas with the destination. You can disable this behavior with `restore_from_destination` configuration option.
        Next it will make sure that data from the previous is fully processed. If not, `run` method normalizes, loads pending data items and **exits**
        If there was no pending data, new data from `data` argument is extracted, normalized and loaded.

        ### Args:
            data (Any): Data to be loaded to destination

            destination (str | DestinationReference, optional): A name of the destination to which dlt will load the data, or a destination module imported from `dlt.destination`.
            If not provided, the value passed to `dlt.pipeline` will be used.

            dataset_name (str, optional):A name of the dataset to which the data will be loaded. A dataset is a logical group of tables ie. `schema` in relational databases or folder grouping many files.
            If not provided, the value passed to `dlt.pipeline` will be used. If not provided at all then defaults to the `pipeline_name`


            credentials (Any, optional): Credentials for the `destination` ie. database connection string or a dictionary with google cloud credentials.
            In most cases should be set to None, which lets `dlt` to use `secrets.toml` or environment variables to infer right credentials values.

            table_name (str, optional): The name of the table to which the data should be loaded within the `dataset`. This argument is required for a `data` that is a list/Iterable or Iterator without `__name__` attribute.
            The behavior of this argument depends on the type of the `data`:
            * generator functions: the function name is used as table name, `table_name` overrides this default
            * `@dlt.resource`: resource contains the full table schema and that includes the table name. `table_name` will override this property. Use with care!
            * `@dlt.source`: source contains several resources each with a table schema. `table_name` will override all table names within the source and load the data into single table.

            write_disposition (Literal["skip", "append", "replace", "merge"], optional): Controls how to write data to a table. `append` will always add new data at the end of the table. `replace` will replace existing data with new data. `skip` will prevent data from loading. "merge" will deduplicate and merge data based on "primary_key" and "merge_key" hints. Defaults to "append".
            Please note that in case of `dlt.resource` the table schema value will be overwritten and in case of `dlt.source`, the values in all resources will be overwritten.

            columns (Sequence[TColumnSchema], optional): A list of column schemas. Typed dictionary describing column names, data types, write disposition and performance hints that gives you full control over the created table schema.

            primary_key (str | Sequence[str]): A column name or a list of column names that comprise a private key. Typically used with "merge" write disposition to deduplicate loaded data.

            schema (Schema, optional): An explicit `Schema` object in which all table schemas will be grouped. By default `dlt` takes the schema from the source (if passed in `data` argument) or creates a default one itself.

        ### Raises:
            PipelineStepFailed when a problem happened during `extract`, `normalize` or `load` steps.
        ### Returns:
            LoadInfo: Information on loaded data including the list of package ids and failed job statuses. Please not that `dlt` will not raise if a single job terminally fails. Such information is provided via LoadInfo.
        """
        signals.raise_if_signalled()
        self._set_destination(destination)
        self._set_dataset_name(dataset_name)

        # sync state with destination
        if self.config.restore_from_destination and not self.full_refresh and not self._state_restored and (self.destination or destination):
            self.sync_destination(destination, dataset_name)
            # sync only once
            self._state_restored = True

        # normalize and load pending data
        if self.list_extracted_resources():
            self.normalize()
        if self.list_normalized_load_packages():
            # if there were any pending loads, load them and **exit**
            if data is not None:
                logger.warn("The pipeline `run` method will now load the pending load packages. The data you passed to the run function will not be loaded. In order to do that you must run the pipeline again")
            return self.load(destination, dataset_name, credentials=credentials)

        # extract from the source
        if data is not None:
            self.extract(data, table_name=table_name, write_disposition=write_disposition, columns=columns, primary_key=primary_key, schema=schema)
            self.normalize()
            return self.load(destination, dataset_name, credentials=credentials)
        else:
            return None

    @with_schemas_sync
    def sync_destination(self, destination: TDestinationReferenceArg = None, dataset_name: str = None) -> None:
        """Synchronizes pipeline state with the `destination`'s state kept in `dataset_name`

        ### Summary
        Attempts to restore pipeline state and schemas from the destination. Requires the state that is present at the destination to have a higher version number that state kept locally in working directory.
        In such a situation the local state, schemas and intermediate files with the data will be deleted and replaced with the state and schema present in the destination.

        A special case where the pipeline state exists locally but the dataset does not exist at the destination will wipe out the local state.

        Note: this method is executed by the `run` method before any operation on data. Use `restore_from_destination` configuration option to disable that behavior.

        """
        self._set_destination(destination)
        self._set_dataset_name(dataset_name)

        state = self._get_state()
        local_state = state.pop("_local")
        merged_state: TPipelineState = None
        try:
            try:
                restored_schemas: Sequence[Schema] = None
                remote_state = self._restore_state_from_destination()

                # if remote state is newer or same
                # print(f'REMOTE STATE: {(remote_state or {}).get("_state_version")} >= {state["_state_version"]}')
                if remote_state and remote_state["_state_version"] >= state["_state_version"]:
                    # compare changes and updates local state
                    merged_state = merge_state_if_changed(state, remote_state, increase_version=False)
                    # print(f"MERGED STATE: {bool(merged_state)}")
                    if merged_state:
                        # see if state didn't change the pipeline name
                        if state["pipeline_name"] != remote_state["pipeline_name"]:
                            raise CannotRestorePipelineException(
                                state["pipeline_name"],
                                self.pipelines_dir,
                                f"destination state contains state for pipeline with name {remote_state['pipeline_name']}"
                            )
                        # if state was modified force get all schemas
                        restored_schemas = self._get_schemas_from_destination(merged_state["schema_names"], always_download=True)
                        # TODO: we should probably wipe out pipeline here

                # if we didn't full refresh schemas, get only missing schemas
                if restored_schemas is None:
                    restored_schemas = self._get_schemas_from_destination(state["schema_names"], always_download=False)
                # commit all the changes locally
                if merged_state:
                    # set the pipeline props from merged state
                    state["_local"] = local_state
                    # add that the state is already extracted
                    state["_local"]["_last_extracted_at"] = pendulum.now()
                    self._state_to_props(merged_state)
                    # on merge schemas are replaced so we delete all old versions
                    self._schema_storage.clear_storage()
                for schema in restored_schemas:
                    self._schema_storage.save_schema(schema)
                # if the remote state is present then unset first run
                if remote_state is not None:
                    self.first_run = False
            except DatabaseUndefinedRelation:
                # storage not present. wipe the pipeline if pipeline not new
                # do it only if pipeline has any data
                if self.has_data:
                    should_wipe = False
                    if self.default_schema_name is None:
                        should_wipe = True
                    else:
                        with self._sql_job_client(self.default_schema) as job_client:
                            # and storage is not initialized
                            should_wipe = not job_client.is_storage_initialized()
                    if should_wipe:
                        # reset pipeline
                        self._wipe_working_folder()
                        state = self._get_state()
                        self._configure(self._schema_storage_config.export_schema_path, self._schema_storage_config.import_schema_path, False)


            # write the state back
            state = merged_state or state
            if "_local" not in state:
                state["_local"] = local_state
            self._props_to_state(state)
            self._save_state(state)
        except Exception as ex:
            raise PipelineStepFailed(self, "run", ex, None) from ex


    @property
    def has_data(self) -> bool:
        """Tells if the pipeline contains any data: schemas, extracted files, load packages or loaded packages in the destination"""
        return not self.first_run or bool(self.schema_names) or len(self.list_extracted_resources()) > 0 or len(self.list_normalized_load_packages()) > 0

    @property
    def has_pending_data(self) -> bool:
        """Tells if the pipeline contains any extracted files or pending load packages"""
        return bool(self.list_normalized_load_packages() or self.list_extracted_resources())

    @property
    def schemas(self) -> SchemaStorage:
        return self._schema_storage

    @property
    def default_schema(self) -> Schema:
        return self.schemas[self.default_schema_name]

    @property
    def state(self) -> TPipelineState:
        """Returns a dictionary with the pipeline state"""
        return self._get_state()

    @property
    def last_trace(self) -> PipelineTrace:
        """Returns or loads last trace generated by pipeline. The trace is loaded from standard location."""
        if self._last_trace:
            return self._last_trace
        return load_trace(self.working_dir)

    def list_extracted_resources(self) -> Sequence[str]:
        """Returns a list of all the files with extracted resources that will be normalized."""
        return self._get_normalize_storage().list_files_to_normalize_sorted()

    def list_normalized_load_packages(self) -> Sequence[str]:
        """Returns a list of all load packages ids that are or will be loaded."""
        return self._get_load_storage().list_packages()

    def list_completed_load_packages(self) -> Sequence[str]:
        """Returns a list of all load package ids that are completely loaded"""
        return self._get_load_storage().list_completed_packages()

    def get_load_package_info(self, load_id: str) -> LoadPackageInfo:
        """Returns information on normalized/completed package with given load_id, all jobs and their statuses."""
        return self._get_load_storage().get_load_package_info(load_id)

    def list_failed_jobs_in_package(self, load_id: str) -> Sequence[LoadJobInfo]:
        """List all failed jobs and associated error messages for a specified `load_id`"""
        return self._get_load_storage().get_load_package_info(load_id).jobs.get("failed_jobs", [])

    def sync_schema(self, schema_name: str = None, credentials: Any = None) -> TSchemaTables:
        """Synchronizes the schema `schema_name` with the destination. If no name is provided, the default schema will be synchronized."""
        if not schema_name and not self.default_schema_name:
            raise PipelineConfigMissing(self.pipeline_name, "default_schema_name", "load", "Pipeline contains no schemas. Please extract any data with `extract` or `run` methods.")

        schema = self.schemas[schema_name] if schema_name else self.default_schema
        client_config = self._get_destination_client_initial_config(credentials)
        with self._get_destination_client(schema, client_config) as client:
            client.initialize_storage()
            return client.update_storage_schema()

    def set_local_state_val(self, key: str, value: Any) -> None:
        """Sets value in local state. Local state is not synchronized with destination."""
        try:
            # get managed state that is read/write
            state = self._container[StateInjectableContext].state
            state["_local"][key] = value  # type: ignore
        except ContextDefaultCannotBeCreated:
            state = self._get_state()
            state["_local"][key] = value  # type: ignore
            self._save_state(state)

    def get_local_state_val(self, key: str) -> Any:
        """Gets value from local state. Local state is not synchronized with destination."""
        try:
            # get managed state that is read/write
            state = self._container[StateInjectableContext].state
        except ContextDefaultCannotBeCreated:
            state = self._get_state()
        return state["_local"][key]   # type: ignore

    def sql_client(self, schema_name: str = None, credentials: Any = None) -> SqlClientBase[Any]:
        """Returns a sql connection configured to query/change the destination and dataset that were used to load the data."""
        # if not self.default_schema_name:
        #     raise PipelineConfigMissing(
        #         self.pipeline_name,
        #         "default_schema_name",
        #         "load",
        #         "Sql Client is not available in a pipeline without a default schema. Extract some data first or restore the pipeline from the destination using 'restore_from_destination' flag. There's also `_inject_schema` method for advanced users."
        #     )
        if schema_name:
            schema = self.schemas[schema_name]
        else:
            schema = self.default_schema if self.default_schema_name else Schema(self.dataset_name)
        return self._sql_job_client(schema, credentials).sql_client

    def _sql_job_client(self, schema: Schema, credentials: Any = None) -> SqlJobClientBase:
        client_config = self._get_destination_client_initial_config(credentials)
        client = self._get_destination_client(schema , client_config)
        if isinstance(client, SqlJobClientBase):
            return client
        else:
            raise SqlClientNotAvailable(self.pipeline_name, self.destination.__name__)

    def _get_normalize_storage(self) -> NormalizeStorage:
        return NormalizeStorage(True, self._normalize_storage_config)

    def _get_load_storage(self) -> LoadStorage:
        caps = self._get_destination_capabilities()
        return LoadStorage(True, caps.preferred_loader_file_format, caps.supported_loader_file_formats, self._load_storage_config)

    def _init_working_dir(self, pipeline_name: str, pipelines_dir: str) -> None:
        self.pipeline_name = pipeline_name
        self.pipelines_dir = pipelines_dir
        self._validate_pipeline_name()
        # compute the folder that keeps all of the pipeline state
        self.working_dir = os.path.join(pipelines_dir, pipeline_name)
        # create pipeline storage, do not create working dir yet
        self._pipeline_storage = FileStorage(self.working_dir, makedirs=False)
        # if full refresh was requested, wipe out all data from working folder, if exists
        if self.full_refresh:
            self._wipe_working_folder()

    def _configure(self, import_schema_path: str, export_schema_path: str, must_attach_to_local_pipeline: bool) -> None:
        # create schema storage and folders
        self._schema_storage_config = SchemaVolumeConfiguration(
            schema_volume_path=os.path.join(self.working_dir, "schemas"),
            import_schema_path=import_schema_path,
            export_schema_path=export_schema_path
        )
        # create default configs
        self._normalize_storage_config = NormalizeVolumeConfiguration(normalize_volume_path=os.path.join(self.working_dir, "normalize"))
        self._load_storage_config = LoadVolumeConfiguration(load_volume_path=os.path.join(self.working_dir, "load"),)

        # are we running again?
        has_state = self._pipeline_storage.has_file(Pipeline.STATE_FILE)
        if must_attach_to_local_pipeline and not has_state:
            raise CannotRestorePipelineException(self.pipeline_name, self.pipelines_dir, f"the pipeline was not found in {self.working_dir}.")

        self.must_attach_to_local_pipeline = must_attach_to_local_pipeline
        # attach to pipeline if folder exists and contains state
        if has_state:
            self._attach_pipeline()
        else:
            # this will erase the existing working folder
            self._create_pipeline()

        # create schema storage
        self._schema_storage = LiveSchemaStorage(self._schema_storage_config, makedirs=True)

    def _create_pipeline(self) -> None:
        self._wipe_working_folder()
        self._pipeline_storage.create_folder("", exists_ok=False)
        self.default_schema_name = None
        self.schema_names = []
        self.first_run = True

    def _wipe_working_folder(self) -> None:
        # kill everything inside the working folder
        if self._pipeline_storage.has_folder(""):
            self._pipeline_storage.delete_folder("", recursively=True, delete_ro=True)

    def _attach_pipeline(self) -> None:
        pass

    def _data_to_sources(self,
        data: Any,
        schema: Schema,
        table_name: str = None,
        parent_table_name: str = None,
        write_disposition: TWriteDisposition = None,
        columns: Sequence[TColumnSchema] = None,
        primary_key: TColumnKey = None
    ) -> List[DltSource]:

        def apply_hint_args(resource: DltResource) -> None:
            columns_dict = None
            if columns:
                columns_dict = {c["name"]:c for c in columns}
            # apply hints only if any of the hints is present, table_name must be always present
            if table_name or parent_table_name or write_disposition or columns or primary_key:
                resource_table_name: str = None
                with contextlib.suppress(DataItemRequiredForDynamicTableHints):
                    resource_table_name = resource.table_name
                resource.apply_hints(table_name or resource_table_name or resource.name, parent_table_name, write_disposition, columns_dict, primary_key)

        def choose_schema() -> Schema:
            """Except of explicitly passed schema, use a clone that will get discarded if extraction fails"""
            if schema:
                return schema
            if self.default_schema_name:
                return self.default_schema.clone()
            return self._make_schema_with_default_name()

        effective_schema = choose_schema()

        # a list of sources or a list of resources may be passed as data
        sources: List[DltSource] = []
        resources: List[DltResource] = []

        def append_data(data_item: Any) -> None:
            if isinstance(data_item, DltSource):
                # if schema is explicit then override source schema
                if schema:
                    data_item.schema = schema
                # try to apply hints to resources
                _resources = data_item.resources.values()
                for r in _resources:
                    apply_hint_args(r)
                sources.append(data_item)
            elif isinstance(data_item, DltResource):
                # apply hints
                apply_hint_args(data_item)
                sources.append(DltSource(data_item.name, data_item.section or self.pipeline_name, effective_schema, [data_item]))
            else:
                # iterator/iterable/generator
                # create resource first without table template
                resource = DltResource.from_data(data_item, name=table_name, section=self.pipeline_name)
                # apply hints
                apply_hint_args(resource)
                resources.append(resource)

        if isinstance(data, C_Sequence) and len(data) > 0:
            # if first element is source or resource
            if isinstance(data[0], (DltResource, DltSource)):
                for item in data:
                    append_data(item)
            else:
                append_data(data)
        else:
            append_data(data)

        if resources:
            # add all the appended resources in one source
            sources.append(DltSource(effective_schema.name, self.pipeline_name, effective_schema, resources))

        return sources

    def _extract_source(self, storage: ExtractorStorage, source: DltSource, max_parallel_items: int, workers: int) -> str:
        # discover the schema from source
        source_schema = source.schema

        extract_id = extract_with_schema(storage, source, source_schema, self.collector, max_parallel_items, workers)

        # if source schema does not exist in the pipeline
        if source_schema.name not in self._schema_storage:
            # save schema into the pipeline
            self._schema_storage.save_schema(source_schema)

        # and set as default if this is first schema in pipeline
        if not self.default_schema_name:
            # this performs additional validations as schema contains the naming module
            self._set_default_schema_name(source_schema)

        pipeline_schema = self._schema_storage[source_schema.name]

        # initialize import with fully discovered schema
        self._schema_storage.save_import_schema_if_not_exists(source_schema)

        # get the current schema and merge tables from source_schema
        # note we are not merging props like max nesting or column propagation
        for table in source_schema.data_tables():
            pipeline_schema.update_schema(pipeline_schema.normalize_table_identifiers(table))

        return extract_id

    def _get_destination_client_initial_config(self, credentials: Any = None) -> DestinationClientConfiguration:
        if not self.destination:
            raise PipelineConfigMissing(
                self.pipeline_name,
                "destination",
                "load",
                "Please provide `destination` argument to `pipeline`, `run` or `load` method directly or via .dlt config.toml file or environment variable."
            )
        # create initial destination client config
        client_spec = self.destination.spec()
        # initialize explicit credentials
        credentials = credentials or self.credentials
        if credentials is not None and not isinstance(credentials, CredentialsConfiguration):
            # use passed credentials as initial value. initial value may resolve credentials
            credentials = client_spec.get_resolvable_fields()["credentials"](credentials)
        # this client support schemas and datasets
        if issubclass(client_spec, DestinationClientDwhConfiguration):
            # set default schema name to load all incoming data to a single dataset, no matter what is the current schema name
            default_schema_name = None if self.config.use_single_dataset else self.default_schema_name
            return client_spec(dataset_name=self.dataset_name, default_schema_name=default_schema_name, credentials=credentials)
        else:
            return client_spec(credentials=credentials)

    def _get_destination_client(self, schema: Schema, initial_config: DestinationClientConfiguration = None) -> JobClientBase:
        try:
            # config is not provided then get it with injected credentials
            if not initial_config:
                initial_config = self._get_destination_client_initial_config()
            return self.destination.client(schema, initial_config)
        except ImportError:
            client_spec = self.destination.spec()
            raise MissingDependencyException(
                f"{client_spec.destination_name} destination",
                [f"{version.DLT_PKG_NAME}[{client_spec.destination_name}]"],
                "Dependencies for specific destinations are available as extras of dlt"
            )

    def _get_destination_capabilities(self) -> DestinationCapabilitiesContext:
        if not self.destination:
                raise PipelineConfigMissing(
                    self.pipeline_name,
                    "destination",
                    "normalize",
                    "Please provide `destination` argument to `pipeline`, `run` or `load` method directly or via .dlt config.toml file or environment variable."
                )
        return self.destination.capabilities()

    def _validate_pipeline_name(self) -> None:
        try:
            FileStorage.validate_file_name_component(self.pipeline_name)
        except ValueError as ve_ex:
            raise InvalidPipelineName(self.pipeline_name, str(ve_ex))

    def _make_schema_with_default_name(self) -> Schema:
        # make a schema from the pipeline name using the name normalizer
        return Schema(self.pipeline_name, normalize_name=True)

    def _validate_dataset_name(self, dataset_name: str) -> None:
        normalized_name = self._default_naming.normalize_identifier(dataset_name)
        if normalized_name != dataset_name:
            raise InvalidDatasetName(dataset_name, normalized_name)

    def _set_context(self, is_active: bool) -> None:
        self.is_active = is_active
        if is_active:
            # set destination context on activation
            if self.destination:
                # inject capabilities context
                self._container[DestinationCapabilitiesContext] = self._get_destination_capabilities()
        else:
            # remove destination context on deactivation
            if DestinationCapabilitiesContext in self._container:
                del self._container[DestinationCapabilitiesContext]

    def _set_destination(self, destination: TDestinationReferenceArg) -> None:
        destination_mod = DestinationReference.from_name(destination)
        self.destination = destination_mod or self.destination
        with self._maybe_destination_capabilities():
            # default normalizers must match the destination
            self._set_default_normalizers()

    @contextmanager
    def _maybe_destination_capabilities(self) -> Iterator[DestinationCapabilitiesContext]:
        try:
            caps: DestinationCapabilitiesContext = None
            injected_caps: ContextManager[DestinationCapabilitiesContext] = None
            if self.destination:
                injected_caps = self._container.injectable_context(self._get_destination_capabilities())
                caps = injected_caps.__enter__()
            yield caps
        finally:
            if injected_caps:
                injected_caps.__exit__(None, None, None)

    def _set_default_normalizers(self) -> None:
        self._default_naming, _ = import_normalizers(default_normalizers())

    def _set_dataset_name(self, dataset_name: str) -> None:
        if not dataset_name:
            if not self.dataset_name:
                # set default dataset name from pipeline name
                dataset_name = self._default_naming.normalize_identifier(self.pipeline_name)
                dataset_name += self.DEFAULT_DATASET_SUFFIX
            else:
                return

        # in case of full refresh add unique suffix
        if self.full_refresh:
            # double _ is not allowed
            if dataset_name.endswith("_"):
                dataset_name += self._pipeline_instance_id[1:]
            else:
                dataset_name += self._pipeline_instance_id
        self._validate_dataset_name(dataset_name)
        self.dataset_name = dataset_name

    def _set_default_schema_name(self, schema: Schema) -> None:
        assert self.default_schema_name is None
        self.default_schema_name = schema.name

    def _create_pipeline_instance_id(self) -> str:
        return pendulum.now().format("_YYYYMMDDhhmmss")  # type: ignore

    @with_schemas_sync
    @with_state_sync()
    def _inject_schema(self, schema: Schema) -> None:
        """Injects a schema into the pipeline. Existing schema will be overwritten"""
        self._schema_storage.save_schema(schema)
        if not self.default_schema_name:
            self._set_default_schema_name(schema)

    def _get_load_info(self, load: Load) -> LoadInfo:
        started_at: datetime.datetime = None
        if self._trace:
            started_at = self._trace.started_at
        return load.get_load_info(self, started_at)

    def _get_state(self) -> TPipelineState:
        try:
            state = json_decode_state(self._pipeline_storage.load(Pipeline.STATE_FILE))
            return migrate_state(self.pipeline_name, state, state["_state_engine_version"], STATE_ENGINE_VERSION)
        except FileNotFoundError:
            return {
                "_state_version": 0,
                "_state_engine_version": STATE_ENGINE_VERSION,
                "_local": {
                    "first_run": True
                }
            }

    def _optional_sql_job_client(self, schema_name: str) -> Optional[SqlJobClientBase]:
        try:
            return self._sql_job_client(Schema(schema_name))
        except PipelineConfigMissing as pip_ex:
            # fallback to regular init if destination not configured
            logger.info(f"Sql Client not available: {pip_ex}")
        except SqlClientNotAvailable:
            # fallback is sql client not available for destination
            logger.info("Client not available because destination does not support sql client")
        except ConfigFieldMissingException:
            # probably credentials are missing
            logger.info("Client not available due to missing credentials")
        return None

    # def _save_runtime_trace(self, step: TPipelineStep, started_at: datetime.datetime, step_info: Any) -> None:
    #     self._trace = add_trace_step(self._trace, step, started_at, step_info)
    #     save_trace(self._pipeline_storage.storage_path, self._trace)

    def _restore_state_from_destination(self, raise_on_connection_error: bool = True) -> Optional[TPipelineState]:
        # if state is not present locally, take the state from the destination
        dataset_name = self.dataset_name
        use_single_dataset = self.config.use_single_dataset
        try:
            # force the main dataset to be used
            self.config.use_single_dataset = True
            job_client = self._optional_sql_job_client(dataset_name)
            if job_client:
                # handle open connection exception silently
                state = None
                try:
                    job_client.sql_client.open_connection()
                except Exception:
                    # pass on connection errors
                    if raise_on_connection_error:
                        raise
                    pass
                else:
                    # try too get state from destination
                    state = load_state_from_destination(self.pipeline_name, job_client.sql_client)
                finally:
                    job_client.sql_client.close_connection()

                if state is None:
                    logger.info(f"The state was not found in the destination {self.destination.__name__}:{dataset_name}")
                else:
                    logger.info(f"The state was restored from the destination {self.destination.__name__}:{dataset_name}")
                return state

            else:
                return None
        finally:
            # restore the use_single_dataset option
            self.config.use_single_dataset = use_single_dataset

    def _get_schemas_from_destination(self, schema_names: Sequence[str], always_download: bool = False) -> Sequence[Schema]:
        # check which schemas are present in the pipeline and restore missing schemas
        restored_schemas: List[Schema] = []
        for schema_name in schema_names:
            if not self._schema_storage.has_schema(schema_name) or always_download:
                # assume client always exist
                with self._optional_sql_job_client(schema_name) as job_client:
                    schema_info = job_client.get_newest_schema_from_storage()
                    if schema_info is None:
                        logger.info(f"The schema {schema_name} was not found in the destination {self.destination.__name__}:{job_client.sql_client.dataset_name}.")
                        # try to import schema
                        with contextlib.suppress(FileNotFoundError):
                            self._schema_storage.load_schema(schema_name)
                    else:
                        logger.info(f"The schema {schema_name} was restored from the destination {self.destination.__name__}:{job_client.sql_client.dataset_name}")
                        restored_schemas.append(Schema.from_dict(json.loads(schema_info.schema)))
        return restored_schemas

    @contextmanager
    def managed_state(self, *, extract_state: bool = False) -> Iterator[TPipelineState]:
        # load or restore state
        state = self._get_state()
        # TODO: we should backup schemas here
        try:
            yield state
        except Exception:
            backup_state = self._get_state()
            # restore original pipeline props
            self._state_to_props(backup_state)
            # synchronize schema storage with initial list of schemas, note that we'll not be able to synchronize the schema content
            if self._schema_storage:
                # TODO: we should restore schemas backup here
                for existing_schema_name in self._schema_storage.list_schemas():
                    if existing_schema_name not in self.schema_names:
                        self._schema_storage.remove_schema(existing_schema_name)
            # raise original exception
            raise
        else:
            self._props_to_state(state)

            backup_state = self._get_state()
            # do not compare local states
            local_state = state.pop("_local")
            backup_state.pop("_local")

            # check if any state element was changed
            merged_state = merge_state_if_changed(backup_state, state)

            # extract state only when there's change in the state or state was not yet extracted AND we actually want to do it
            if (merged_state or "_last_extracted_at" not in local_state) and extract_state:
                # print(f'EXTRACT STATE merged: {bool(merged_state)} extracted timestamp in {"_last_extracted_at" not in local_state}')
                merged_state = self._extract_state(merged_state or state)
                local_state["_last_extracted_at"] = pendulum.now()

            # if state is modified and is not being extracted, mark it to be extracted next time
            if not extract_state and merged_state:
                local_state.pop("_last_extracted_at", None)

            # always save state locally as local_state is not compared
            merged_state = merged_state or state
            merged_state["_local"] = local_state
            self._save_state(merged_state)

    def _state_to_props(self, state: TPipelineState) -> None:
        """Write `state` to pipeline props."""
        for prop in Pipeline.STATE_PROPS:
            if prop in state and not prop.startswith("_"):
                setattr(self, prop, state[prop])  # type: ignore
        for prop in Pipeline.LOCAL_STATE_PROPS:
            if prop in state["_local"] and not prop.startswith("_"):
                setattr(self, prop, state["_local"][prop])  # type: ignore
        if "destination" in state:
            self._set_destination(DestinationReference.from_name(self.destination))

    def _props_to_state(self, state: TPipelineState) -> None:
        """Write pipeline props to `state`"""
        for prop in Pipeline.STATE_PROPS:
            if not prop.startswith("_"):
                state[prop] = getattr(self, prop)  # type: ignore
        for prop in Pipeline.LOCAL_STATE_PROPS:
            if not prop.startswith("_"):
                state["_local"][prop] = getattr(self, prop)  # type: ignore
        if self.destination:
            state["destination"] = self.destination.__name__
        state["schema_names"] = self._schema_storage.list_schemas()

    def _save_state(self, state: TPipelineState) -> None:
        self._pipeline_storage.save(Pipeline.STATE_FILE, json_encode_state(state))

    def _extract_state(self, state: TPipelineState) -> TPipelineState:
        # this will extract the state into current load package and update the schema with the _dlt_pipeline_state table
        # note: the schema will be persisted because the schema saving decorator is over the state manager decorator for extract
        state_source = DltSource("pipeline_state", self.pipeline_name, self.default_schema, [state_resource(state)])
        storage = ExtractorStorage(self._normalize_storage_config)
        extract_id = extract_with_schema(storage, state_source, self.default_schema, _NULL_COLLECTOR, 1, 1)
        storage.commit_extract_files(extract_id)
        return state

    def __getstate__(self) -> Any:
        # pickle only the SupportsPipeline protocol fields
        return {"pipeline_name": self.pipeline_name}
