# Copyright 2023 BentoML Team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations
import collections
import functools
import hashlib
import inspect
import logging
import os
import re
import sys
import types
import typing as t
from abc import ABC
from abc import abstractmethod
from pathlib import Path

import attr
import orjson
from huggingface_hub import hf_hub_download

import bentoml
import openllm
from bentoml._internal.models import ModelStore
from bentoml._internal.models.model import ModelSignature

from ._configuration import AdapterType
from ._configuration import FineTuneConfig
from ._quantisation import infer_quantisation_config
from .exceptions import ForbiddenAttributeError
from .exceptions import GpuNotAvailableError
from .utils import DEBUG
from .utils import ENV_VARS_TRUE_VALUES
from .utils import EnvVarMixin
from .utils import LazyLoader
from .utils import ReprMixin
from .utils import bentoml_cattr
from .utils import codegen
from .utils import first_not_none
from .utils import get_debug_mode
from .utils import in_docker
from .utils import is_peft_available
from .utils import is_torch_available
from .utils import non_intrusive_setattr
from .utils import normalize_attrs_to_model_tokenizer_pair
from .utils import pkg
from .utils import requires_dependencies
from .utils import resolve_filepath
from .utils import validate_is_path


# NOTE: We need to do this so that overload can register
# correct overloads to typing registry
if sys.version_info[:2] >= (3, 11):
    from typing import NotRequired
    from typing import overload
else:
    from typing_extensions import NotRequired
    from typing_extensions import overload

if t.TYPE_CHECKING:
    import peft
    import torch

    import transformers
    from bentoml._internal.runner.strategy import Strategy

    from ._types import AdaptersMapping
    from ._types import AdaptersTuple
    from ._types import DictStrAny
    from ._types import LiteralRuntime
    from ._types import LLMInitAttrs
    from ._types import LLMRunnable
    from ._types import LLMRunner
    from ._types import PeftAdapterOutput
    from ._types import TupleAny
    from .utils.representation import ReprArgs

    UserDictAny = collections.UserDict[str, t.Any]
    ResolvedAdaptersMapping = dict[AdapterType, dict[str | t.Literal["default"], tuple[peft.PeftConfig, str]]]
else:
    DictStrAny = dict
    TupleAny = tuple
    UserDictAny = collections.UserDict
    LLMRunnable = bentoml.Runnable
    LLMRunner = bentoml.Runner
    transformers = LazyLoader("transformers", globals(), "transformers")
    torch = LazyLoader("torch", globals(), "torch")
    peft = LazyLoader("peft", globals(), "peft")

logger = logging.getLogger(__name__)


class ModelSignatureDict(t.TypedDict, total=False):
    batchable: bool
    batch_dim: t.Union[t.Tuple[int, int], int]
    input_spec: NotRequired[t.Union[t.Any, t.Tuple[t.Any]]]
    output_spec: NotRequired[t.Any]


def normalise_model_name(name: str) -> str:
    return os.path.basename(resolve_filepath(name)) if validate_is_path(name) else re.sub("[^a-zA-Z0-9]+", "-", name)


def make_tag(
    model_id: str,
    model_version: str | None = None,
    trust_remote_code: bool = False,
    implementation: LiteralRuntime = "pt",
    quiet: bool = False,
) -> bentoml.Tag:
    """Generate a ``bentoml.Tag`` from a given transformers model name.

    Note that this depends on your model to have a config class available.

    Args:
        model_id: The transformers model name or path to load the model from.
        model_version: Optional model version to be saved with this tag. Default to None.
                       If model_id is a custom path, then the version would be the hash of the last modified
                       time from given ``model_id``.
        trust_remote_code: Whether to trust the remote code. Defaults to False.
        model_version: Optional model version to be saved with this tag.
        implementation: Given implementation for said LLM. One of t.Literal['pt', 'tf', 'flax']
        quiet: Whether to show warning logs. Default to 'False'

    Returns:
        A tuple of ``bentoml.Tag`` and a dict of unused kwargs.
    """
    model_name = normalise_model_name(model_id)

    if validate_is_path(model_id):
        model_id = resolve_filepath(model_id)
        # special cases, if it is the model store, then we return the tags
        # this will happens within the container, where we use the relative path
        if in_docker() and os.getenv("BENTO_PATH") is not None:
            _store = ModelStore(Path(model_id).parent.parent)
            tag = _store.list()[0].tag
            model_version = tag.version
            model_name = tag.name
        else:
            if model_version is None:  # noqa: PLR5501
                if not quiet:
                    logger.warning(
                        "Given 'model_id=%s' is a path, and 'model_version' is not passed. OpenLLM will generate the version based on the last modified time of this given directory.",
                        model_id,
                    )
                model_version = generate_hash_from_file(model_id)
    else:
        config = t.cast(
            "transformers.PretrainedConfig",
            transformers.AutoConfig.from_pretrained(
                model_id,
                trust_remote_code=trust_remote_code,
                revision=first_not_none(model_version, default="main"),
            ),
        )

        model_version = getattr(config, "_commit_hash", None)
        if model_version is None:
            raise ValueError(
                f"Internal errors when parsing config for pretrained {model_id} ('commit_hash' not found)"
            )

    if in_docker() and os.getenv("BENTO_PATH") is not None:
        logger.debug("The model will be loaded as relative path within BentoContainer.")
    else:
        logger.debug(
            "'model_id=%s' will use 'model_version=%s'. The full tag to be saved under model store: '%s-%s:%s'",
            model_id,
            model_version,
            implementation,
            model_name,
            model_version,
        )

    return bentoml.Tag.from_taglike(
        f"{model_name if in_docker() and os.getenv('BENTO_PATH') is not None else implementation + '-' + model_name}:{model_version}".strip()
    )


@functools.lru_cache(maxsize=128)
def generate_hash_from_file(f: str, algorithm: t.Literal["md5", "sha1"] = "sha1") -> str:
    """Generate a hash from given file's modification time.

    Args:
        f: The file to generate the hash from.
        algorithm: The hashing algorithm to use. Defaults to 'sha1' (similar to how Git generate its commit hash.)

    Returns:
        The generated hash.
    """
    modification_time = str(os.path.getmtime(resolve_filepath(f)))
    hashed = getattr(hashlib, algorithm)(modification_time.encode())
    return hashed.hexdigest()


# the below is similar to peft.utils.other.CONFIG_NAME
PEFT_CONFIG_NAME = "adapter_config.json"


def resolve_peft_config_type(adapter_map: dict[str, str | None] | None):
    """Resolve the type of the PeftConfig given the adapter_map.

    This is similar to how PeftConfig resolve its config type.

    Args:
        adapter_map: The given mapping from either SDK or CLI. See CLI docs for more information.
    """
    if adapter_map is None:
        return

    resolved: AdaptersMapping = {}
    _has_set_default = False
    for path_or_adapter_id, name in adapter_map.items():
        if _has_set_default:
            raise ValueError("Only one adapter can be set as default.")
        resolve_name = name
        if resolve_name is None:
            resolve_name = "default"
            _has_set_default = True
        if os.path.isfile(os.path.join(path_or_adapter_id, PEFT_CONFIG_NAME)):
            config_file = os.path.join(path_or_adapter_id, PEFT_CONFIG_NAME)
        else:
            try:
                config_file = hf_hub_download(path_or_adapter_id, PEFT_CONFIG_NAME)
            except Exception as err:
                raise ValueError(f"Can't find '{PEFT_CONFIG_NAME}' at '{path_or_adapter_id}'") from err
        with open(config_file, "r") as file:
            resolved_config = orjson.loads(file.read())
        # all peft_type should be available in PEFT_CONFIG_NAME
        _peft_type: AdapterType = resolved_config["peft_type"].lower()
        if _peft_type not in resolved:
            resolved[_peft_type] = ()
        resolved[_peft_type] += (_AdaptersTuple((path_or_adapter_id, resolve_name, resolved_config)),)
    return resolved


_reserved_namespace = {"config_class", "model", "tokenizer", "import_kwargs"}

M = t.TypeVar("M", bound="transformers.PreTrainedModel")
T = t.TypeVar("T", bound="t.Union[transformers.PreTrainedTokenizerFast, transformers.PreTrainedTokenizer]")


def _default_post_init(self: LLM[t.Any, t.Any]):
    self.device = None

    if self.__llm_implementation__ == "pt" and is_torch_available():
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")


class LLMInterface(ABC, t.Generic[M, T]):
    """This defines the loose contract for all openllm.LLM implementations."""

    @property
    def import_kwargs(self) -> tuple[DictStrAny, DictStrAny] | None:
        """The default import kwargs to used when importing the model.

        This will be passed into 'openllm.LLM.import_model'.
        It returns two dictionaries: one for model kwargs and one for tokenizer kwargs.

        Returns:
            Optional tuple of model kwargs and tokenizer kwargs
        """
        return

    def embeddings(self, prompt: str) -> torch.Tensor:
        """The implementation for generating text embeddings from given prompt.

        It takes the prompt and output the embeddings for this given LLM.

        Returns:
            The embeddings for the given prompt.
        """
        raise NotImplementedError

    @abstractmethod
    def generate(self, prompt: str, **preprocess_generate_kwds: t.Any) -> t.Any:
        """The implementation for text generation from given prompt.

        It takes the prompt and 'generation_kwargs' from 'self.sanitize_parameters' and then
        pass it to 'self.model.generate'.
        """
        raise NotImplementedError

    def generate_one(
        self,
        prompt: str,
        stop: list[str],
        **preprocess_generate_kwds: t.Any,
    ) -> list[dict[t.Literal["generated_text"], str]]:
        """The entrypoint for generating one prompt.

        This provides additional stop tokens for generating per token level.
        This is useful when running with agents, or initial streaming support.
        """
        raise NotImplementedError

    def generate_iterator(self, prompt: str, **attrs: t.Any) -> t.Iterator[t.Any]:
        """T iterator version of `generate` function."""
        raise NotImplementedError(
            "Currently generate_iterator requires SSE (Server-side events) support, which is not yet implemented."
        )

    def sanitize_parameters(self, prompt: str, **attrs: t.Any) -> tuple[str, DictStrAny, DictStrAny]:
        """This handler will sanitize all attrs and setup prompt text.

        It takes a prompt that is given by the user, attrs that can be parsed with the prompt.

        Returns a tuple of three items:
        - The attributes dictionary that can be passed into LLMConfig to generate a GenerationConfig
        - The attributes dictionary that will be passed into `self.postprocess_generate`.
        """
        return prompt, attrs, attrs

    def postprocess_generate(self, prompt: str, generation_result: t.Any, **attrs: t.Any) -> t.Any:
        """This handler will postprocess generation results from LLM.generate and then output nicely formatted results (if the LLM decide to do so.).

        You can customize how the output of the LLM looks with this hook. By default, it is a simple echo.

        NOTE: this will be used from the client side.
        """
        return generation_result

    def llm_post_init(self):
        """This function can be implemented if you need to initialized any additional variables that doesn't concern OpenLLM internals."""
        pass

    def import_model(self, *args: t.Any, trust_remote_code: bool, **attrs: t.Any) -> bentoml.Model:
        """This function can be implemented if default import_model doesn't satisfy your needs.

        Note that tokenizer attrs can be accessed via ``llm.llm_parameters``.

        ```python
        _, tokenizer_attrs = llm.llm_parameters
        ```

        By default, `model_decls` and `model_attrs` is already sanitised and concatenated into `args` and `attrs`
        """
        raise NotImplementedError

    def load_model(self, tag: bentoml.Tag, *args: t.Any, **attrs: t.Any) -> M:
        """This function can be implemented to override the default load_model behaviour.

        See falcon for example implementation.
        """
        raise NotImplementedError

    def load_tokenizer(self, tag: bentoml.Tag, **attrs: t.Any) -> T:
        """This function can be implemented to override how to load the tokenizer.

        See falcon for example implementation.
        """
        raise NotImplementedError

    # NOTE: All fields below are attributes that can be accessed by users.
    config_class: type[openllm.LLMConfig]
    """The config class to use for this LLM. If you are creating a custom LLM, you must specify this class."""

    bettertransformer: bool
    """Whether to load this LLM with FasterTransformer enabled. The order of loading is:

    - If pass within `for_model`, `from_pretrained` or `__init__`.
    - If `self.bettertransformer` is set within `llm_post_init`.
    - Finally, if none of the above, default to self.config['bettertransformer']

    > **Note** that if LoRA is enabled, bettertransformer will be disabled.
    """

    device: torch.device | str | int | None
    """The device to be used for this LLM. If the implementation is 'pt', then it will be torch.device, else string."""

    # NOTE: The following will be populated by __init_subclass__, note that these should be immutable.
    __llm_trust_remote_code__: bool
    """This is used to determine during 'import_model' whether to trust remote code or not.

    This works synonymous with `trust_remote_code` kwarg in transformers Auto classes. If not passed,
    then by default fallback to config_class['trust_remote_code']
    """
    __llm_implementation__: LiteralRuntime
    """This is used to determine which implementation that this LLM has.

    Usually, this will inferred from class name, that follows the HuggingFace's naming convention:

    - `OPTForConditionalGeneration` -> `pt`
    - `TFOPTForConditionalGeneration` -> `tf`
    - `FlaxOPTForConditionalGeneration` -> `flax`
    """
    __llm_model__: M | peft.PeftModel | None
    """A reference to the actual model. Instead of access this directly, you should use `model` property instead."""
    __llm_tokenizer__: T | None
    """A reference to the actual tokenizer. Instead of access this directly, you should use `tokenizer` property instead."""
    __llm_bentomodel__: bentoml.Model | None
    """A reference to the bentomodel used for this LLM. Instead of access this directly, you should use `_bentomodel` property instead."""
    __llm_adapter_map__: dict[AdapterType, dict[str | t.Literal["default"], tuple[peft.PeftConfig, str]]] | None
    """A reference to the the cached LoRA adapter mapping."""

    __llm_custom_import__: bool
    """Whether this LLM has a custom import_model"""
    __llm_custom_load__: bool
    """A boolean to determine whether a custom 'load_model' is implemented"""
    __llm_custom_tokenizer__: bool
    """A boolean to determine whether a custom 'load_tokenizer' is implemented"""
    __llm_init_kwargs__: property | None
    """A check if 'import_kwargs' is implemented in subclass."""

    if t.TYPE_CHECKING:

        def __attrs_init__(
            self,
            config: openllm.LLMConfig,
            quantization_config: transformers.BitsAndBytesConfig | None,
            model_id: str,
            runtime: t.Literal["ggml", "transformers"],
            model_decls: TupleAny,
            model_attrs: DictStrAny,
            tokenizer_attrs: DictStrAny,
            tag: bentoml.Tag,
            adapters_mapping: AdaptersMapping,
            model_version: str | None,
            quantize_method: t.Literal["int8", "int4", "gptq"] | None,
            /,
            **attrs: t.Unpack[LLMInitAttrs],
        ) -> None:
            """Generated __attrs_init__ for openllm.LLM."""


_AdaptersTuple: type[AdaptersTuple] = codegen.make_attr_tuple_class("AdaptersTuple", ["adapter_id", "name", "config"])


@attr.define(slots=True, repr=False)
class LLM(LLMInterface[M, T], ReprMixin):
    config: openllm.LLMConfig
    """The config instance to use for this LLM. This will be created based on config_class and available
    when initialising the LLM."""

    quantization_config: transformers.BitsAndBytesConfig | None
    """Quantisation config for quantised model on the fly."""

    _model_id: str
    _runtime: t.Literal["ggml", "transformers"]
    _model_decls: TupleAny
    _model_attrs: DictStrAny
    _tokenizer_attrs: DictStrAny
    _tag: bentoml.Tag
    _adapters_mapping: AdaptersMapping
    _model_version: str
    _quantize_method: t.Literal["int8", "int4", "gptq"] | None

    @staticmethod
    def _infer_implementation_from_name(name: str) -> tuple[LiteralRuntime, str]:
        if name.startswith("Flax"):
            return "flax", name[4:]
        elif name.startswith("TF"):
            return "tf", name[2:]
        else:
            return "pt", name

    def __init_subclass__(cls):
        cd = cls.__dict__
        implementation, config_class_name = cls._infer_implementation_from_name(cls.__name__)
        cls.__llm_implementation__ = implementation
        config_class = openllm.AutoConfig.infer_class_from_name(config_class_name)

        if "__openllm_internal__" in cd:
            if "config_class" not in cd:
                cls.config_class = config_class
            logger.debug("Using config class %s for %s.", cls.config_class, cls.__name__)
        elif "config_class" not in cd:
            raise RuntimeError("Missing required key 'config_class'. Make sure to define it within the LLM subclass.")

        _custom_import = True
        if cls.import_model is LLMInterface[M, T].import_model:
            # using the default import model if no custom import is set
            _custom_import = False
            setattr(cls, "import_model", openllm.serialisation.import_model)
        else:
            import_func = getattr(cls, "import_model")

            def _wrapped_import_model(self: LLM[M, T], *decls: t.Any, trust_remote_code: bool, **attrs: t.Any):
                # wrapped around custom init to provide some meta compression
                # for all decls and attrs
                (model_decls, model_attrs), _ = self.llm_parameters

                decls = (*model_decls, *decls)
                attrs = {**model_attrs, **attrs}

                return import_func(self, *decls, trust_remote_code=trust_remote_code, **attrs)

            setattr(cls, "import_model", functools.update_wrapper(_wrapped_import_model, cls.import_model))

        if cls.llm_post_init is LLMInterface[M, T].llm_post_init:
            # using the default post init if no custom post init is set
            wrapped_post_init = _default_post_init
        else:
            original_post_init = getattr(cls, "llm_post_init")

            def wrapped_post_init(self: LLM[M, T]):
                _default_post_init(self)
                original_post_init(self)

        setattr(cls, "llm_post_init", wrapped_post_init)

        cls.__llm_custom_import__ = _custom_import
        cls.__llm_custom_load__ = False if cls.load_model is LLMInterface[M, T].load_model else True
        cls.__llm_custom_tokenizer__ = False if cls.load_tokenizer is LLMInterface[M, T].load_tokenizer else True
        cls.__llm_init_kwargs__ = None if cls.import_kwargs is LLMInterface[M, T].import_kwargs else cls.import_kwargs

        for at in {"bentomodel", "model", "tokenizer", "adapter_map"}:
            setattr(cls, f"__llm_{at}__", None)

        # update docstring for given entrypoint
        for fn in {"generate", "generate_one", "generate_iterator"}:
            original_fn = getattr(cls, fn, getattr(LLMInterface, fn))
            original_fn.__doc__ = (
                original_fn.__doc__
                or f"""\
            '{fn}' implementation {cls.__name__}.

            Note that if LoRA is enabled (via either SDK or CLI), `self.model` will become a `peft.PeftModel`
            The original can then be accessed with 'self.model.get_base_model()'.
            """
            )
            setattr(cls, fn, original_fn)

    # The following is the similar interface to HuggingFace pretrained protocol.
    def save_pretrained(self, save_directory: str | Path, **attrs: t.Any) -> None:
        if isinstance(save_directory, Path):
            save_directory = str(save_directory)
        if self.__llm_model__ is not None and self.bettertransformer:
            from optimum.bettertransformer import BetterTransformer

            self.__llm_model__ = t.cast(
                M,
                BetterTransformer.reverse(t.cast("transformers.PreTrainedModel", self.__llm_model__)),  # type: ignore
            )

        openllm.serialisation.save_pretrained(self, save_directory, **attrs)

    @classmethod
    def from_pretrained(
        cls,
        model_id: str | None = None,
        model_version: str | None = None,
        llm_config: openllm.LLMConfig | None = None,
        *args: t.Any,
        runtime: t.Literal["ggml", "transformers"] | None = None,
        quantize: t.Literal["int8", "int4", "gptq"] | None = None,
        bettertransformer: str | bool | None = None,
        adapter_id: str | None = None,
        adapter_name: str | None = None,
        adapter_map: dict[str, str | None] | None = None,
        quantization_config: transformers.BitsAndBytesConfig | None = None,
        **attrs: t.Any,
    ) -> LLM[M, T]:
        """Instantiate a pretrained LLM.

        ``LLM.from_pretrained`` follows the same design principle as HuggingFace's `from_pretrained` method, plus the following:

        ### Optimization options:

        > This is most notable during serving time.

        - quantize: quantize the model with the given quantization method. Currently supported int8, int4 quantization
        - bettertransformer: Apply FasterTransformer to given pretrained weight

        > Currently, the above two options are mutually exclusive.

        ### Adapter options:

        > This is used in conjunction with the fine-tuning features

        - adapter_id: Optional [LoRA](https://arxiv.org/pdf/2106.09685.pdf) pretrained id or local path to apply to said model.
        - adapter_name: Optional name of the adapter to apply to said model. If not provided, it will be handled internally by OpenLLM.
        - adapter_map: optional dictionary of adapter_id to adapter_name. Note that this is mutually exclusive with adapter_id/adapter_name arguments.

        Args:
            model_id: The pretrained model to use. Defaults to None. If None, 'self.default_id' will be used.
                      > **Warning**: If custom path is passed, make sure it contains all available file to construct
                      > ``transformers.PretrainedConfig``, ``transformers.PreTrainedModel``, and ``transformers.PreTrainedTokenizer``.
            model_name: Optional model name to be saved with this LLM. Default to None. It will be inferred automatically from model_id.
                        If model_id is a custom path, it will be the basename of the given path.
            model_version: Optional version for this given model id. Default to None. This is useful for saving from custom path.
                           If set to None, the version will either be the git hash from given pretrained model, or the hash inferred
                           from last modified time of the given directory.
            llm_config: The config to use for this LLM. Defaults to None. If not passed, OpenLLM
                        will use `config_class` to construct default configuration.
            quantize: The quantization to use for this LLM. Defaults to None. Possible values
                      include int8, int4 and gptq.
            runtime: Optional runtime to run this LLM. Default to 'transformers'. 'ggml' supports is working in progress.
            quantization_config: The quantization config (`transformers.BitsAndBytesConfig`) to use. Note that this is mutually exclusive with `quantize`
            bettertransformer: Whether to use BetterTransformer with this model. Defaults to False.
            adapter_id: The [LoRA](https://arxiv.org/pdf/2106.09685.pdf) pretrained id or local path to use for this LLM. Defaults to None.
            adapter_name: The adapter name to use for this LLM. Defaults to None.
            adapter_map: The adapter map to use for this LLM. Defaults to None. Note that this is mutually exclusive with adapter_id/adapter_name arguments.
            *args: The args to be passed to the model.
            **attrs: The kwargs to be passed to the model.
        """
        cfg_cls = cls.config_class
        if model_id is None:
            model_id = first_not_none(
                cfg_cls.__openllm_env__["model_id_value"], default=cfg_cls.__openllm_default_id__
            )
        if runtime is None:
            runtime = cfg_cls.__openllm_runtime__

        # quantization setup
        if quantization_config and quantize:
            raise ValueError(
                """'quantization_config' and 'quantize' are mutually exclusive. Either customise
            your quantization_config or use the 'quantize' argument."""
            )
        if quantization_config is None and quantize is not None:
            logger.debug(
                "'quantize' is not None. %s will use a default 'quantization_config' for %s. "
                "If you want to customise the quantization config, make sure to pass your "
                "own 'quantization_config'",
                cls.__name__,
                quantize,
            )
            quantization_config, attrs = infer_quantisation_config(cls, quantize, **attrs)

        # NOTE: Fine-tuning setup
        if adapter_map and adapter_id:
            raise ValueError(
                """'adapter_map' and 'adapter_id' are mutually exclusive. Either provide a
                'adapter_map' ({adapter_id: adapter_name | None, ...}) or use
                the combination of adapter_id/adapter_name arguments.
                """
            )
        if adapter_map is None and adapter_id is not None:
            adapter_map = {adapter_id: adapter_name}

        if adapter_map is not None and not is_peft_available():
            raise RuntimeError(
                "LoRA adapter requires 'peft' to be installed. Make sure to install OpenLLM with 'pip install \"openllm[fine-tune]\"'"
            )

        if llm_config is None:
            llm_config = cls.config_class.model_construct_env(**attrs)
            # The rests of the kwargs that is not used by the config class should be stored into __openllm_extras__.
            attrs = llm_config["extras"]

        _tag = cls._infer_tag_from_model_id(model_id, model_version)
        if _tag.version is None:
            raise RuntimeError("Failed to resolve model version.")

        return cls(
            *args,
            model_id=model_id,
            llm_config=llm_config,
            bettertransformer=str(bettertransformer).upper() in ENV_VARS_TRUE_VALUES,
            quantization_config=quantization_config,
            _quantize_method=quantize,
            _adapters_mapping=resolve_peft_config_type(adapter_map),
            _runtime=runtime,
            _model_version=_tag.version,
            _tag=_tag,
            **attrs,
        )

    @classmethod
    def _infer_tag_from_model_id(cls, model_id: str, model_version: str | None) -> bentoml.Tag:
        try:
            return bentoml.Tag.from_taglike(model_id)
        except ValueError:
            return make_tag(
                model_id,
                model_version=model_version,
                trust_remote_code=cls.config_class.__openllm_trust_remote_code__,
                implementation=cls.__llm_implementation__,
                quiet=True,
            )

    def __init__(
        self,
        *args: t.Any,
        model_id: str,
        llm_config: openllm.LLMConfig,
        bettertransformer: bool | None,
        quantization_config: transformers.BitsAndBytesConfig | None,
        _adapters_mapping: AdaptersMapping | None,
        _tag: bentoml.Tag,
        _quantize_method: t.Literal["int8", "int4", "gptq"] | None,
        _runtime: t.Literal["ggml", "transformers"],
        _model_version: str,
        **attrs: t.Any,
    ):
        """Initialize the LLM with given pretrained model.

        > **Warning**
        > To initializing any LLM, you should use `openllm.AutoLLM` or `openllm.LLM.from_pretrained` instead.
        > `__init__` initialization is only for internal use.

        Note:
        - *args to be passed to the model.
        - **attrs will first be parsed to the AutoConfig, then the rest will be parsed to the import_model
        - for tokenizer kwargs, it should be prefixed with _tokenizer_*

        For custom pretrained path, it is recommended to pass in 'model_version' alongside with the path
        to ensure that it won't be loaded multiple times.
        Internally, if a pretrained is given as a HuggingFace repository path , OpenLLM will usethe commit_hash
        to generate the model version.

        For better consistency, we recommend users to also push the fine-tuned model to HuggingFace repository.

        If you need to overwrite the default ``import_model``, implement the following in your subclass:

        ```python
        def import_model(
            self,
            *args: t.Any,
            trust_remote_code: bool,
            **attrs: t.Any,
        ):
            _, tokenizer_attrs = self.llm_parameters

            return bentoml.transformers.save_model(
                tag,
                transformers.AutoModelForCausalLM.from_pretrained(
                    self.model_id, device_map="auto", torch_dtype=torch.bfloat16, **attrs
                ),
                custom_objects={
                    "tokenizer": transformers.AutoTokenizer.from_pretrained(
                        self.model_id, padding_size="left", **tokenizer_attrs
                    )
                },
            )
        ```

        If your import model doesn't require customization, you can simply pass in `import_kwargs`
        at class level that will be then passed into The default `import_model` implementation.
        See ``openllm.DollyV2`` for example.

        ```python
        dolly_v2_runner = openllm.Runner(
            "dolly-v2", _tokenizer_padding_size="left", torch_dtype=torch.bfloat16, device_map="cuda"
        )
        ```

        Note: If you implement your own `import_model`, then `import_kwargs` will be the
        base kwargs. You can still override those via ``openllm.Runner``.

        Note that this tag will be generated based on `self.default_id`.
        passed from the __init__ constructor.

        ``llm_post_init`` can also be implemented if you need to do any additional
        initialization after everything is setup.

        Note: If you need to implement a custom `load_model`, the following is an example from Falcon implementation:

        ```python
        def load_model(self, tag: bentoml.Tag, *args: t.Any, **attrs: t.Any) -> t.Any:
            torch_dtype = attrs.pop("torch_dtype", torch.bfloat16)
            device_map = attrs.pop("device_map", "auto")

            _ref = bentoml.transformers.get(tag)

            model = bentoml.transformers.load_model(_ref, device_map=device_map, torch_dtype=torch_dtype, **attrs)
            return transformers.pipeline("text-generation", model=model, tokenizer=_ref.custom_objects["tokenizer"])
        ```

        Args:
            model_id: The pretrained model to use. Defaults to None. If None, 'self.default_id' will be used.
            llm_config: The config to use for this LLM. Defaults to None. If not passed, OpenLLM
                        will use `config_class` to construct default configuration.
            bettertransformer: Whether to use BetterTransformer with this model. Defaults to False.
            quantization_config: ``transformers.BitsAndBytesConfig`` configuration, or 'gptq' denoting this model to be loaded with GPTQ.
            *args: The args to be passed to the model.
            **attrs: The kwargs to be passed to the model.
        """
        # low_cpu_mem_usage is only available for model
        # this is helpful on system with low memory to avoid OOM
        low_cpu_mem_usage = attrs.pop("low_cpu_mem_usage", True)

        if self.__llm_implementation__ == "pt":
            attrs.update({"low_cpu_mem_usage": low_cpu_mem_usage, "quantization_config": quantization_config})

        model_kwds, tokenizer_kwds = {}, {}
        if self.__llm_init_kwargs__:
            # NOTE: recast here for type safety
            model_kwds, tokenizer_kwds = t.cast("tuple[DictStrAny, DictStrAny]", self.__llm_init_kwargs__)
            logger.debug(
                '\'%s\' default kwargs for model: "%s", tokenizer: "%s"',
                self.__class__.__name__,
                model_kwds,
                tokenizer_kwds,
            )

        # parsing tokenizer and model kwargs, as the hierachy is param pass > default
        normalized_model_kwds, normalized_tokenizer_kwds = normalize_attrs_to_model_tokenizer_pair(**attrs)
        # NOTE: Save the args and kwargs for latter load

        self.__attrs_init__(
            llm_config,
            quantization_config,
            model_id,
            _runtime,
            args,
            {**model_kwds, **normalized_model_kwds},
            {**tokenizer_kwds, **normalized_tokenizer_kwds},
            _tag,
            _adapters_mapping,
            _model_version,
            _quantize_method,
        )
        # handle trust_remote_code
        self.__llm_trust_remote_code__ = self._model_attrs.pop("trust_remote_code", self.config["trust_remote_code"])

        self.llm_post_init()

        # we set it here so that we allow subclass to overwrite bettertransformer in llm_post_init
        if bettertransformer is True:
            logger.debug("Using %r with BetterTransformer", self)
            self.bettertransformer = bettertransformer
        else:
            non_intrusive_setattr(self, "bettertransformer", self.config["bettertransformer"])
        # If lora is passed, the disable bettertransformer
        if _adapters_mapping and self.bettertransformer is True:
            logger.debug("LoRA is visible for %s, disabling BetterTransformer", self)
            self.bettertransformer = False

    def __setattr__(self, attr: str, value: t.Any):
        if attr in _reserved_namespace:
            raise ForbiddenAttributeError(
                f"{attr} should not be set during runtime "
                f"as these value will be reflected during runtime. "
                f"Instead, you can create a custom LLM subclass {self.__class__.__name__}."
            )

        super().__setattr__(attr, value)

    @property
    def adapters_mapping(self) -> AdaptersMapping:
        return self._adapters_mapping

    @adapters_mapping.setter
    def adapters_mapping(self, value: AdaptersMapping):
        self._adapters_mapping = value

    @property
    def __repr_keys__(self) -> set[str]:
        return {"model_id", "runner_name", "config", "adapters_mapping", "runtime", "tag"}

    def __repr_args__(self) -> ReprArgs:
        for k in self.__repr_keys__:
            if k == "config":
                yield k, self.config.model_dump(flatten=True)
            else:
                yield k, getattr(self, k)

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def runtime(self) -> t.Literal["ggml", "transformers"]:
        return self._runtime

    @property
    def runner_name(self) -> str:
        return f"llm-{self.config['start_name']}-runner"

    # NOTE: The section below defines a loose contract with langchain's LLM interface.
    @property
    def llm_type(self) -> str:
        return normalise_model_name(self._model_id)

    @property
    def identifying_params(self) -> DictStrAny:
        return {
            "configuration": self.config.model_dump_json().decode(),
            "model_ids": orjson.dumps(self.config["model_ids"]).decode(),
        }

    @property
    def llm_parameters(self) -> tuple[tuple[tuple[t.Any, ...], DictStrAny], DictStrAny]:
        """Returning the processed model and tokenizer parameters.

        These can then be used with 'import_model' or any other place that requires loading model and tokenizer.

        See 'openllm.cli.download_models' for example usage.

        Returns:
            ``tuple``: It returns a tuple of (model_args, model_kwargs) & tokenizer_kwargs
        """
        return (self._model_decls, self._model_attrs), self._tokenizer_attrs

    @property
    def tag(self) -> bentoml.Tag:
        return self._tag

    def ensure_model_id_exists(self) -> bentoml.Model:
        """This utility function will download the model if it doesn't exist yet.

        Make sure to call this function if 'ensure_available' is not set during
        Auto LLM initialisation.

        The equivalent for ``openllm.Runner`` is ``openllm.Runner.download_model``.
        """
        additional_args = ["--machine"]
        if not get_debug_mode():
            additional_args.append("--quiet")
        return openllm.import_model(
            self.config["start_name"],
            model_id=self.model_id,
            model_version=self._model_version,
            runtime=self.runtime,
            implementation=self.__llm_implementation__,
            quantize=self._quantize_method,
            additional_args=additional_args,
        )

    @property
    def _bentomodel(self) -> bentoml.Model:
        if self.__llm_bentomodel__ is None:
            self.__llm_bentomodel__ = openllm.serialisation.get(self)
        return self.__llm_bentomodel__

    @property
    def model(self) -> M:
        """The model to use for this LLM. This shouldn't be set at runtime, rather let OpenLLM handle it."""
        # Run check for GPU
        if self.config["requires_gpu"] and len(openllm.utils.gpu_count()) < 1:
            raise GpuNotAvailableError(f"{self} only supports running with GPU (None available).") from None

        if self.__llm_model__ is None:
            self.__llm_model__ = t.cast(
                M, openllm.serialisation.load_model(self, *self._model_decls, **self._model_attrs)
            )
        return t.cast(M, self.__llm_model__)

    @property
    def tokenizer(self) -> T:
        """The tokenizer to use for this LLM. This shouldn't be set at runtime, rather let OpenLLM handle it."""
        if self.__llm_tokenizer__ is None:
            self.__llm_tokenizer__ = t.cast(T, openllm.serialisation.load_tokenizer(self))
        return self.__llm_tokenizer__

    def _default_ft_config(self, _adapter_type: AdapterType, inference_mode: bool) -> FineTuneConfig:
        strategy = first_not_none(
            self.config["fine_tune_strategies"].get(_adapter_type),
            default=FineTuneConfig(adapter_type=_adapter_type, llm_config_class=self.config_class),
        )
        return strategy.eval() if inference_mode else strategy.train()

    def _transpose_adapter_mapping(
        self,
        inference_mode: bool = True,
        use_cache: bool = True,
    ) -> ResolvedAdaptersMapping:
        if self._adapters_mapping is None:
            raise ValueError("LoRA mapping is not set up correctly.")

        if use_cache and self.__llm_adapter_map__ is not None:
            # early out if we already serialized everything.
            return self.__llm_adapter_map__

        if not use_cache:
            logger.debug("Adapter mapping resolution will not be cached. This should only be used during training.")

        adapter_map: ResolvedAdaptersMapping = {k: {} for k in self._adapters_mapping}
        # this is a temporary check to accept the first option name as 'default'
        # then we will raise Error when the optional_name is set to None in next iteration.
        _converted_first_none = False
        for _adapter_type, _adapters_tuples in self._adapters_mapping.items():
            default_config = self._default_ft_config(_adapter_type, inference_mode)
            for adapter in _adapters_tuples:
                if not adapter.name and _converted_first_none:
                    raise ValueError(
                        f"{self.__class__.__name__} doesn't know how to resolve adapter_name None mapping: {adapter.adapter_id, adapter.config}"
                    )
                name = adapter.name
                if name is None:
                    _converted_first_none = True
                    name = "default"
                peft_config = (
                    default_config.with_config(**adapter.config).to_peft_config()
                    if name == "default"
                    else FineTuneConfig(
                        adapter_type=_adapter_type,
                        adapter_config=adapter.config,
                        inference_mode=inference_mode,
                        llm_config_class=self.config_class,
                    ).to_peft_config()
                )
                adapter_map[_adapter_type][name] = (peft_config, adapter.adapter_id)

        if self.__llm_adapter_map__ is None and use_cache:
            self.__llm_adapter_map__ = adapter_map
        return adapter_map

    @requires_dependencies("peft", extra="fine-tune")
    def prepare_for_training(self, adapter_type: AdapterType = "lora", **attrs: t.Any) -> tuple[peft.PeftModel, T]:
        if pkg.pkg_version_info("peft")[:2] >= (0, 4):
            from peft import prepare_model_for_kbit_training
        else:
            from peft import prepare_model_for_int8_training as prepare_model_for_kbit_training

        peft_config = (
            self.config["fine_tune_strategies"]
            .get(adapter_type, FineTuneConfig(adapter_type=adapter_type, llm_config_class=self.config_class))
            .train()
            .with_config(**attrs)
            .to_peft_config()
        )
        wrapped_peft = peft.get_peft_model(prepare_model_for_kbit_training(self.model), peft_config)
        if DEBUG:
            wrapped_peft.print_trainable_parameters()
        return wrapped_peft, self.tokenizer

    @requires_dependencies("peft", extra="fine-tune")
    def apply_adapter(
        self,
        inference_mode: bool = True,
        adapter_type: AdapterType = "lora",
        load_adapters: t.Literal["all"] | list[str] | None = None,
        use_cache: bool = True,
    ) -> peft.PeftModel | M:
        """Apply given LoRA mapping to the model.

        Note that the base model can still be accessed via self.model.get_base_model().
        """
        assert self.__llm_model__ is not None  # noqa: S101

        # early out if _adapters_mapping is empty or it is already wrapped with peft.
        if not self._adapters_mapping:
            logger.debug("No adapter mapping is found. Skip applying adapter.")
            return self.__llm_model__
        if isinstance(self.__llm_model__, peft.PeftModel):
            logger.debug("Model is already wrapped with peft. Skip applying adapter.")
            return self.__llm_model__

        _mapping = self._transpose_adapter_mapping(inference_mode=inference_mode, use_cache=use_cache)
        if adapter_type not in _mapping:
            raise ValueError(
                f"Given adapter type {adapter_type} is not supported. Please choose from {list(_mapping.keys())}"
            )
        adapter_mapping = _mapping[adapter_type]

        self.__llm_model__ = self._wrap_default_peft_model(adapter_mapping, inference_mode=inference_mode)

        if not isinstance(self.__llm_model__, peft.PeftModel):
            # We hit this branch during inference
            # TODO: load multiple adapters
            return self.__llm_model__

        # now we loop through the rest with add_adapter
        if len(adapter_mapping) > 0:
            for adapter_name, (_peft_config, _) in adapter_mapping.items():
                self.__llm_model__.add_adapter(adapter_name, _peft_config)

            # optionally load adapters. In case of multiple adapters, or on Runner,
            # we will need to set load_adapters='all'
            if load_adapters is not None:
                adapters_to_load = adapter_mapping.keys() if load_adapters == "all" else load_adapters
                for adapter_name in adapters_to_load:
                    _peft_config, _peft_model_id = adapter_mapping[adapter_name]
                    self.__llm_model__.load_adapter(
                        _peft_model_id,
                        adapter_name=adapter_name,
                        is_trainable=not inference_mode,
                        **dict(_peft_config.to_dict()),
                    )

        return self.__llm_model__

    def _wrap_default_peft_model(self, adapter_mapping: dict[str, tuple[peft.PeftConfig, str]], inference_mode: bool):
        assert self.__llm_model__ is not None, "Error: Model is not loaded correctly"  # noqa: S101
        if isinstance(self.__llm_model__, peft.PeftModel):
            logger.warning("Model is already wrapped with peft. Skip wrapping with default peft model.")
            return self.__llm_model__

        if "default" not in adapter_mapping:
            raise ValueError(
                "There is no 'default' mapping. Please check the adapter mapping and report this bug to the OpenLLM team."
            )

        default_config, peft_model_id = adapter_mapping.pop("default")

        # the below shared similar logics with `get_peft_model`
        # TODO: Support PromptLearningConfig
        if default_config.task_type not in peft.MODEL_TYPE_TO_PEFT_MODEL_MAPPING.keys() and not isinstance(
            default_config, peft.PromptLearningConfig
        ):
            logger.debug(
                "Given task type '%s' is not supported by peft. Make sure the adapter is loaded manually before running inference.",
                default_config.task_type,
            )
            model = peft.PeftModel(self.__llm_model__, default_config)
        else:
            # XXX: this is not ideal to serialize like this, maybe for fine-tune we will only support 0.4.0
            # onwards. For now, keep this logic here.
            peft_class = peft.MODEL_TYPE_TO_PEFT_MODEL_MAPPING[default_config.task_type]
            if default_config.base_model_name_or_path:
                kwargs: DictStrAny = {"is_trainable": not inference_mode}
                if "config" in inspect.signature(peft_class.from_pretrained).parameters:
                    kwargs["config"] = default_config
                else:
                    kwargs.update(dict(default_config.to_dict().items()))
                # BUG: This hits during inference, need fixing
                model = peft_class.from_pretrained(self.__llm_model__, peft_model_id, **kwargs)
            else:
                # in this case, the given base_model_name_or_path is None. This will be hit during training
                model = peft_class(self.__llm_model__, default_config)
        return model

    # order of these fields matter here, make sure to sync it with
    # openllm.models.auto.factory.BaseAutoLLMClass.for_model
    def to_runner(
        self,
        models: list[bentoml.Model] | None = None,
        max_batch_size: int | None = None,
        max_latency_ms: int | None = None,
        scheduling_strategy: type[Strategy] | None = None,
    ) -> LLMRunner:
        """Convert this LLM into a Runner.

        Args:
            models: Any additional ``bentoml.Model`` to be included in this given models.
                    By default, this will be determined from the model_name.
            max_batch_size: The maximum batch size for the runner.
            max_latency_ms: The maximum latency for the runner.
            strategy: The strategy to use for this runner.
            embedded: Whether to run this runner in embedded mode.
            scheduling_strategy: Whether to create a custom scheduling strategy for this Runner.

        Returns:
            A generated LLMRunner for this LLM.

        NOTE: There are some difference between bentoml.models.get().to_runner() and LLM.to_runner(): 'name'.
        - 'name': will be generated by OpenLLM, hence users don't shouldn't worry about this.
            The generated name will be 'llm-<model-start-name>-runner' (ex: llm-dolly-v2-runner, llm-chatglm-runner)
        - 'embedded': Will be disabled by default. There is no reason to run LLM in embedded mode.
        - 'method_configs': The method configs for the runner will be managed internally by OpenLLM.
        """
        models = models if models is not None else []

        try:
            models.append(self._bentomodel)
        except bentoml.exceptions.NotFound:
            models.append(openllm.serialisation.get(self, auto_import=True))

        if scheduling_strategy is None:
            from ._strategies import CascadingResourceStrategy

            scheduling_strategy = CascadingResourceStrategy

        generate_sig = ModelSignature.from_dict(ModelSignatureDict(batchable=False))
        generate_iterator_sig = ModelSignature.from_dict(ModelSignatureDict(batchable=True))

        # NOTE: returning the two langchain API's to the runner
        return llm_runner_class(self)(
            llm_runnable_class(self, generate_sig, generate_iterator_sig),
            name=self.runner_name,
            embedded=False,
            models=models,
            max_batch_size=max_batch_size,
            max_latency_ms=max_latency_ms,
            method_configs=bentoml_cattr.unstructure(
                {
                    "generate": generate_sig,
                    "generate_one": generate_sig,
                    "generate_iterator": generate_iterator_sig,
                }
            ),
            scheduling_strategy=scheduling_strategy,
        )

    def predict(self, prompt: str, **attrs: t.Any) -> t.Any:
        """The scikit-compatible API for self(...)."""
        return self.__call__(prompt, **attrs)

    def __call__(self, prompt: str, **attrs: t.Any) -> t.Any:
        """Returns the generation result and format the result.

        First, it runs `self.sanitize_parameters` to sanitize the parameters.
        The the sanitized prompt and kwargs will be pass into self.generate.
        Finally, run self.postprocess_generate to postprocess the generated result.

        This allows users to do the following:

        ```python
        llm = openllm.AutoLLM.for_model("dolly-v2")
        llm("What is the meaning of life?")
        ```
        """
        prompt, generate_kwargs, postprocess_kwargs = self.sanitize_parameters(prompt, **attrs)
        generated_result = self.generate(prompt, **generate_kwargs)
        return self.postprocess_generate(prompt, generated_result, **postprocess_kwargs)


@overload
def Runner(
    model_name: str,
    *,
    model_id: str | None = None,
    init_local: t.Literal[False, True] = ...,
    **attrs: t.Any,
) -> LLMRunner:
    ...


@overload
def Runner(
    model_name: str,
    *,
    model_id: str = ...,
    models: list[bentoml.Model] | None = ...,
    max_batch_size: int | None = ...,
    max_latency_ms: int | None = ...,
    method_configs: dict[str, ModelSignatureDict | ModelSignature] | None = ...,
    embedded: t.Literal[True, False] = ...,
    scheduling_strategy: type[Strategy] | None = ...,
    **attrs: t.Any,
) -> LLMRunner:
    ...


def Runner(
    model_name: str,
    ensure_available: bool | None = None,
    init_local: bool = False,
    implementation: LiteralRuntime | None = None,
    llm_config: openllm.LLMConfig | None = None,
    **attrs: t.Any,
) -> LLMRunner:
    """Create a Runner for given LLM. For a list of currently supported LLM, check out 'openllm models'.

    The behaviour of ensure_available that is synonymous to `AutoLLM.for_model` depends on `init_local`.
    By default, `ensure_available` is synonymous to `init_local`, meaning on the service when creating
    runner, it won't download the model. So before running your BentoML Service, you should create a `on_startup`
    hook to check download if you don't want to do it manually:

    ```python

    runner = openllm.Runner("dolly-v2")

    @svc.on_startup
    def download():
        runner.llm.ensure_model_id_exists()
    ...
    ```

    if `init_local=True` (For development workflow), it will also enable `ensure_available`.
    Default value of `ensure_available` is None. If set then use that given value, otherwise fallback to the aforementioned behaviour.

    Args:
        model_name: Supported model name from 'openllm models'
        ensure_available: If True, it will download the model if it is not available. If False, it will skip downloading the model.
                          If False, make sure the model is available locally.
        implementation: The given Runner implementation one choose for this Runner. By default, it is retrieved from the enviroment variable
                        of the respected model_name. For example: 'flan-t5' -> "OPENLLM_FLAN_T5_FRAMEWORK"
        llm_config: Optional ``openllm.LLMConfig`` to initialise this ``openllm.LLMRunner``.
        init_local: If True, it will initialize the model locally. This is useful if you want to
                    run the model locally. (Symmetrical to bentoml.Runner.init_local())
        **attrs: The rest of kwargs will then be passed to the LLM. Refer to the LLM documentation for the kwargs
                behaviour
    """
    if llm_config is not None:
        attrs.update(
            {
                "bettertransformer": llm_config["env"]["bettertransformer_value"],
                "quantize": llm_config["env"]["quantize_value"],
                "runtime": llm_config["env"]["runtime_value"],
            }
        )

    if implementation is None:
        implementation = EnvVarMixin(model_name)["framework_value"]

    runner = openllm.infer_auto_class(implementation).create_runner(
        model_name,
        llm_config=llm_config,
        ensure_available=ensure_available if ensure_available is not None else init_local,
        **attrs,
    )

    if init_local:
        runner.init_local(quiet=True)

    return runner


def method_signature(sig: ModelSignature) -> ModelSignatureDict:
    return bentoml_cattr.unstructure(sig)


class SetAdapterOutput(t.TypedDict):
    success: bool
    message: str


def llm_runnable_class(
    self: openllm.LLM[M, T],
    generate_sig: ModelSignature,
    generate_iterator_sig: ModelSignature,
) -> type[LLMRunnable]:
    class _Runnable(bentoml.Runnable):
        SUPPORTED_RESOURCES = ("nvidia.com/gpu", "amd.com/gpu", "cpu")
        SUPPORTS_CPU_MULTI_THREADING = True

        def __init__(__self: _Runnable):
            # NOTE: The side effect of this line
            # is that it will load the imported model during
            # runner startup. So don't remove it!!
            __self.model = self.model  # keep a loaded reference
            if self.adapters_mapping is not None:
                logger.info("Applying LoRA to %s...", self.runner_name)
                self.apply_adapter(inference_mode=True, load_adapters="all")

        @bentoml.Runnable.method(batchable=False)
        def set_adapter(__self, adapter_name: str) -> SetAdapterOutput:
            success = False
            message = None
            if not is_peft_available():
                message = "peft is not available. Make sure to install: 'pip install \"openllm[fine-tune]\"'"
            elif self.__llm_adapter_map__ is None:
                message = "No adapters available for current running server."
            elif not isinstance(__self.model, peft.PeftModel):
                message = "Model is not a PeftModel"
            if message is not None:
                return SetAdapterOutput(success=success, message=message)

            try:
                t.cast("peft.PeftModel", __self.model).set_adapter(adapter_name)
                return SetAdapterOutput(success=True, message=f"Successfully set current adapter to {adapter_name}")
            except ValueError:
                logger.info("Adapter %s not found", adapter_name)
                return SetAdapterOutput(
                    success=success,
                    message=f"Adapter {adapter_name} not found. Available adapters: {list(t.cast('peft.PeftModel', __self.model).peft_config)}",
                )

        @bentoml.Runnable.method(**method_signature(generate_sig))
        def __call__(__self, prompt: str, **attrs: t.Any) -> list[t.Any]:
            return self.generate(prompt, **attrs)

        @bentoml.Runnable.method(**method_signature(generate_sig))
        def generate(__self, prompt: str, **attrs: t.Any) -> list[t.Any]:
            return self.generate(prompt, **attrs)

        @bentoml.Runnable.method(**method_signature(generate_sig))
        def generate_one(
            __self, prompt: str, stop: list[str], **attrs: t.Any
        ) -> list[dict[t.Literal["generated_text"], str]]:
            return self.generate_one(prompt, stop, **attrs)

        @bentoml.Runnable.method(**method_signature(generate_iterator_sig))
        def generate_iterator(__self, prompt: str, **attrs: t.Any) -> t.Generator[t.Any, None, None]:
            yield self.generate_iterator(prompt, **attrs)

    return types.new_class(
        self.__class__.__name__ + "Runnable",
        (_Runnable,),
        {},
        lambda ns: ns.update(
            {
                "SUPPORTED_RESOURCES": ("nvidia.com/gpu", "amd.com/gpu")
                if self.config["requires_gpu"]
                else ("nvidia.com/gpu", "amd.com/gpu", "cpu"),
                "__module__": self.__module__,
                "__doc__": self.config["env"].start_docstring,
            }
        ),
    )


def llm_runner_class(self: openllm.LLM[M, T]) -> type[LLMRunner]:
    def available_adapters(__self: LLMRunner) -> PeftAdapterOutput:
        if not is_peft_available():
            return {
                "success": False,
                "result": {},
                "error_msg": "peft is not available. Make sure to install: 'pip install \"openllm[fine-tune]\"'",
            }
        if self.__llm_adapter_map__ is None:
            return {
                "success": False,
                "result": {},
                "error_msg": "No adapters available for current running server.",
            }
        if not isinstance(__self.model, peft.PeftModel):
            return {"success": False, "result": {}, "error_msg": "Model is not a PeftModel"}
        return {"success": True, "result": __self.model.peft_config, "error_msg": ""}

    def _wrapped_generate_run(__self: LLMRunner, prompt: str, **kwargs: t.Any) -> t.Any:
        """Wrapper for runner.generate.run() to handle the prompt and postprocessing.

        This will be used for LangChain API.

        Usage:
        ```python
        runner = openllm.Runner("dolly-v2", init_local=True)
        runner("What is the meaning of life?")
        ```
        """
        prompt, generate_kwargs, postprocess_kwargs = self.sanitize_parameters(prompt, **kwargs)
        generated_result = __self.generate.run(prompt, **generate_kwargs)
        return self.postprocess_generate(prompt, generated_result, **postprocess_kwargs)

    def _wrapped_repr_keys(_: LLMRunner) -> set[str]:
        return {"config", "llm_type", "runner_methods", "runtime", "llm_tag"}

    def _wrapped_repr_args(__self: LLMRunner) -> ReprArgs:
        yield "runner_methods", {
            method.name: {
                "batchable": method.config.batchable,
                "batch_dim": method.config.batch_dim if method.config.batchable else None,
            }
            for method in __self.runner_methods
        }
        yield "config", self.config.model_dump(flatten=True)
        yield "llm_type", __self.llm_type
        yield "runtime", self.runtime
        yield "llm_tag", self.tag

    return types.new_class(
        self.__class__.__name__ + "Runner",
        (bentoml.Runner,),
        exec_body=lambda ns: ns.update(
            {
                "llm_type": self.llm_type,
                "identifying_params": self.identifying_params,
                "llm_tag": self.tag,
                "llm": self,  # NOTE: self reference to LLM
                "config": self.config,
                "peft_adapters": property(fget=available_adapters),
                "download_model": self.ensure_model_id_exists,
                "__call__": _wrapped_generate_run,
                "__module__": self.__module__,
                "__doc__": self.config["env"].start_docstring,
                "__repr__": ReprMixin.__repr__,
                "__repr_keys__": property(_wrapped_repr_keys),
                "__repr_args__": _wrapped_repr_args,
            }
        ),
    )
