import abc
import contextlib
import tomlkit
from typing import Any, ClassVar, List, Sequence, Tuple, Type, TypeVar

from dlt.common.configuration.container import Container
from dlt.common.configuration.exceptions import ConfigFieldMissingException, LookupTrace
from dlt.common.configuration.providers.provider import ConfigProvider
from dlt.common.configuration.specs import BaseConfiguration, is_base_configuration_inner_hint
from dlt.common.configuration.utils import deserialize_value, log_traces, auto_cast
from dlt.common.configuration.specs.config_providers_context import ConfigProvidersContext
from dlt.common.typing import AnyType, ConfigValue, TSecretValue

DLT_SECRETS_VALUE = "secrets.value"
DLT_CONFIG_VALUE = "config.value"
TConfigAny = TypeVar("TConfigAny", bound=Any)

class _Accessor(abc.ABC):

    def __getitem__(self, field: str) -> Any:
        value, traces = self._get_value(field)
        if value is None:
            raise ConfigFieldMissingException("Any", {field: traces})
        if isinstance(value, str):
            return auto_cast(value)
        else:
            return value

    def get(self, field: str, expected_type: Type[TConfigAny] = None) -> TConfigAny:
        value: TConfigAny
        value, _ = self._get_value(field, expected_type)
        if value is None:
            return None
        # cast to required type
        if expected_type:
            return deserialize_value(field, value, expected_type)
        else:
            return value

    @property
    @abc.abstractmethod
    def config_providers(self) -> Sequence[ConfigProvider]:
        pass

    @property
    @abc.abstractmethod
    def default_type(self) -> AnyType:
        pass

    def _get_providers_from_context(self) -> Sequence[ConfigProvider]:
        return Container()[ConfigProvidersContext].providers

    def _get_value(self, field: str, type_hint: Type[Any] = None) -> Tuple[Any, List[LookupTrace]]:
        # get default hint type, in case of dlt.secrets it it TSecretValue
        type_hint = type_hint or self.default_type
        # split field into sections and a key
        sections = field.split(".")
        key = sections.pop()
        value = None
        traces: List[LookupTrace] = []
        for provider in self.config_providers:
            value, effective_field = provider.get_value(key, type_hint, None, *sections)
            trace = LookupTrace(provider.name, sections, effective_field, value)
            traces.append(trace)
            if value is not None:
                # log trace
                if is_base_configuration_inner_hint(type_hint):
                    config: BaseConfiguration = type_hint  # type: ignore
                else:
                    config = None
                log_traces(config, key, type_hint, value, None, [trace])
                break
        return value, traces


class _ConfigAccessor(_Accessor):
    """Provides direct access to configured values that are not secrets."""

    @property
    def config_providers(self) -> Sequence[ConfigProvider]:
        """Return a list of config providers, in lookup order"""
        return [p for p in self._get_providers_from_context()]

    @property
    def default_type(self) -> AnyType:
        return AnyType

    value: ClassVar[None] = ConfigValue
    "A placeholder that tells dlt to replace it with actual config value during the call to a source or resource decorated function."


class _SecretsAccessor(_Accessor):
    """Provides direct access to secrets."""

    @property
    def config_providers(self) -> Sequence[ConfigProvider]:
        """Return a list of config providers that can hold secrets, in lookup order"""
        return [p for p in self._get_providers_from_context() if p.supports_secrets]

    @property
    def default_type(self) -> AnyType:
        return TSecretValue

    value: ClassVar[None] = ConfigValue
    "A placeholder that tells dlt to replace it with actual secret during the call to a source or resource decorated function."


config = _ConfigAccessor()
"""Dictionary-like access to all config values to dlt"""

secrets = _SecretsAccessor()
"""Dictionary-like access to all secrets known known to dlt"""
