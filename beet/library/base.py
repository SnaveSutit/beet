__all__ = [
    "Pack",
    "Namespace",
    "NamespaceFile",
    "NamespaceJsonFile",
    "FileContainer",
    "FileContainerProxy",
    "FileContainerProxyDescriptor",
]


import shutil
from contextlib import nullcontext
from dataclasses import InitVar, dataclass, field
from functools import partial
from itertools import accumulate, count
from pathlib import Path, PurePosixPath
from typing import (
    Any,
    ClassVar,
    Generic,
    Iterator,
    Mapping,
    Optional,
    Tuple,
    Type,
    TypeVar,
    Union,
    get_args,
    get_origin,
    get_type_hints,
    overload,
)
from zipfile import ZipFile

from beet.core.container import Container, ContainerProxy, MatchMixin, MergeMixin
from beet.core.file import File, FileOrigin, JsonFile, PngFile
from beet.core.utils import FileSystemPath, extra_field, unreachable

from .utils import list_files

T = TypeVar("T")
NamespaceFileType = TypeVar("NamespaceFileType", bound="NamespaceFile")


# TODO: Redo load/dump


class NamespaceFile(File[object, object]):
    scope: ClassVar[Tuple[str, ...]]
    extension: ClassVar[str]

    def merge(self: NamespaceFileType, other: NamespaceFileType) -> bool:
        """Merge the given file or return False to indicate no special handling."""
        return False

    def bind(self, pack: Any, namespace: str, path: str):
        """Handle insertion."""


class NamespaceJsonFile(JsonFile, NamespaceFile):
    extension = ".json"


@dataclass(repr=False)
class FileContainer(
    MergeMixin,
    MatchMixin,
    Container[str, NamespaceFileType],
):
    """Generic container that maps a string path to a file instance."""

    namespace: Optional["Namespace"] = extra_field(default=None)

    def process(self, key: str, value: NamespaceFileType) -> NamespaceFileType:
        if self.namespace and self.namespace.pack and self.namespace.name:
            value.bind(self.namespace.pack, self.namespace.name, key)
        return value

    def bind(self, namespace: "Namespace"):
        """Handle insertion."""
        self.namespace = namespace

        for key, value in self.items():
            self.process(key, value)


NamespaceType = TypeVar("NamespaceType", bound="Namespace")


@dataclass
class Namespace(Mapping[Type[NamespaceFile], FileContainer[NamespaceFile]]):
    """Base class for pack namespaces.

    Subclasses are expected to extend the dataclass by adding fields of
    type `FileContainer`. The Namespace then automatically provides a
    dict-like interface that maps each type of file to the corresponding
    container.
    """

    pack: Optional["Pack[Namespace]"] = extra_field(default=None)
    name: Optional[str] = extra_field(default=None)

    directory: ClassVar[str]
    scope_map: ClassVar[Mapping[Tuple[Tuple[str, ...], str], Type[NamespaceFile]]]
    container_fields: ClassVar[Mapping[Type[NamespaceFile], str]]

    def __init_subclass__(cls: Type["Namespace"]):
        cls.container_fields = {
            get_args(hint)[0]: attr
            for attr, hint in get_type_hints(cls).items()
            if get_origin(hint) is FileContainer
        }
        cls.scope_map = {
            (file_type.scope, file_type.extension): file_type
            for file_type in cls.container_fields
        }

    def __getitem__(
        self, key: Type[NamespaceFileType]
    ) -> FileContainer[NamespaceFileType]:
        return getattr(self, self.container_fields[key])

    def __setitem__(self, key: str, item: NamespaceFileType):
        container = getattr(self, self.container_fields[type(item)])
        container[key] = item

    def __iter__(self) -> Iterator[Type[NamespaceFile]]:
        return iter(self.container_fields)

    def __len__(self) -> int:
        return len(self.container_fields)

    def bind(self, pack: "Pack[Namespace]", name: str):
        """Handle insertion."""
        self.pack = pack
        self.name = name

        for container in self.values():
            container.bind(self)

    @property
    def content(self) -> Iterator[Tuple[str, NamespaceFile]]:
        """Iterator that yields all the files stored in the namespace."""
        for container in self.values():
            yield from container.items()

    @property
    def empty(self) -> bool:
        """Whether all the containers in the namespace are empty."""
        return not any(self.values())

    def merge(
        self,
        other: Mapping[Type[NamespaceFile], FileContainer[NamespaceFile]],
    ) -> bool:
        """Merge containers from the given namespace."""
        for file_type, container in self.items():
            if other_container := other.get(file_type):
                container.merge(other_container)
        return True

    @classmethod
    def scan(cls, pack: FileOrigin) -> Iterator[Tuple[str, "Namespace"]]:
        """Load namespaces by walking through a zipfile or directory."""
        if isinstance(pack, ZipFile):
            filenames = map(PurePosixPath, pack.namelist())
        else:
            filenames = list_files(pack)

        name = None
        namespace = None

        for filename in sorted(filenames):
            try:
                directory, namespace_dir, head, *rest, _ = filename.parts
            except ValueError:
                continue

            if directory != cls.directory:
                continue
            if name != namespace_dir:
                if name and namespace:
                    yield name, namespace
                name = namespace_dir
                namespace = cls()

            assert name and namespace is not None
            extension = "".join(filename.suffixes)

            for path in accumulate(rest, lambda a, b: a + (b,), initial=(head,)):
                if file_type := cls.scope_map.get((path, extension)):
                    key = "/".join(
                        filename.relative_to(Path(directory, name, *path)).parts
                    )[: -len(extension)]

                    namespace[file_type][key] = file_type.load(pack, filename)

        if name and namespace:
            yield name, namespace

    # TODO: create mapping of { parent_dir: Set[file] } then mkdir, then dump
    def dump(self, namespace: str, pack: FileOrigin):
        """Write the namespace to a zipfile or to the filesystem."""
        for content_type, container in self.items():
            if not container:
                continue

            path = (self.directory, namespace) + content_type.scope

            for name, item in container.items():
                full_path = path + (name + content_type.extension,)

                if isinstance(pack, ZipFile):
                    item.dump(pack, PurePosixPath(*full_path))
                else:
                    filename = Path(pack, *full_path).resolve()
                    filename.parent.mkdir(parents=True, exist_ok=True)
                    item.dump(pack, Path(*full_path))

    def __repr__(self) -> str:
        args = ", ".join(
            f"{self.container_fields[key]}={value}"
            for key, value in self.items()
            if value
        )
        return f"{self.__class__.__name__}({args})"


class FileContainerProxy(
    MergeMixin,
    MatchMixin,
    ContainerProxy[Type[NamespaceFileType], str, NamespaceFileType],
):
    """Aggregated view over files of a specific type from all namespaces."""

    def split_key(self, key: str) -> Tuple[str, str]:
        namespace, _, file_path = key.partition(":")
        if not file_path:
            raise KeyError(key)
        return namespace, file_path

    def join_key(self, key1: str, key2: str) -> str:
        return f"{key1}:{key2}"


@dataclass
class FileContainerProxyDescriptor(Generic[NamespaceFileType]):
    """Descriptor providing a bounded `FileContainerProxy`."""

    proxy_key: Type[NamespaceFileType]

    def __get__(
        self,
        obj: Any,
        objtype: Optional[Type[object]] = None,
    ) -> FileContainerProxy[NamespaceFileType]:
        return FileContainerProxy(obj, self.proxy_key)


@dataclass
class Pack(
    MergeMixin,
    MatchMixin,
    Container[str, NamespaceType],
):
    """Base class for resource packs and data packs.

    The class exposes a dict-like interface for manipulating namespaces.
    Subclasses defining fields of type `FileContainerProxy` can take
    advantage of the `__setitem__` shortcut overload.

    By default, the constructor will lazily load the pack if a path or
    a zipfile is provided. You can create the pack with `eager=True` to
    load everything up-front.
    """

    name: Optional[str] = None
    mcmeta: JsonFile = field(default_factory=lambda: JsonFile({}))
    image: Optional[PngFile] = None

    zipped: Optional[bool] = extra_field(default=None)
    path: Optional[FileSystemPath] = extra_field(default=None)
    zipfile: Optional[ZipFile] = extra_field(default=None)

    eager: InitVar[bool] = extra_field(default=False)

    proxy_map: Mapping[
        Type[NamespaceFile], FileContainerProxy[NamespaceFile]
    ] = extra_field(init=False)

    namespace_type: ClassVar[Type[NamespaceType]]
    default_name: ClassVar[str]
    latest_pack_format: ClassVar[int]

    def __init_subclass__(cls):
        cls.namespace_type = get_args(getattr(cls, "__orig_bases__")[0])[0]

    def __post_init__(self, eager: bool):
        self.proxy_map = {}

        for attr in dir(self):
            value = getattr(self, attr)
            if isinstance(value, FileContainerProxy):
                self.proxy_map[value.proxy_key] = value  # type: ignore

        self.load(lazy=not eager)

    @overload
    def __setitem__(self, key: str, namespace: NamespaceType):
        ...

    @overload
    def __setitem__(self, key: str, file: NamespaceFile):
        ...

    def __setitem__(self, key: str, value: Any):
        if isinstance(value, File):
            self.proxy_map[type(value)][key] = value  # type: ignore
        else:
            super().__setitem__(key, value)

    def __enter__(self: T) -> T:
        return self

    def __exit__(self, *_):
        self.dump(overwrite=True)

    def process(self, key: str, value: NamespaceType) -> NamespaceType:
        value.bind(self, key)
        return value

    def missing(self, key: str) -> NamespaceType:
        return self.namespace_type()

    @property
    def content(self) -> Iterator[Tuple[str, NamespaceFile]]:
        """Iterator that yields all the files stored in the pack."""
        for container_proxy in self.proxy_map.values():
            yield from container_proxy.items()

    @property
    def empty(self) -> bool:
        """Whether all the namespaces in the pack are empty."""
        return all(namespace.empty for namespace in self.values())

    @property
    def description(self) -> Any:
        info = self.mcmeta.data.get("pack", {})
        return info.get("description", "")

    @description.setter
    def description(self, value: Any):
        info = self.mcmeta.data.setdefault("pack", {})
        info["description"] = value

    @property
    def pack_format(self) -> int:
        info = self.mcmeta.data.get("pack", {})
        return info.get("pack_format", 0)

    @pack_format.setter
    def pack_format(self, value: int):
        info = self.mcmeta.data.setdefault("pack", {})
        info["pack_format"] = value

    def load(self, pack: Optional[FileOrigin] = None, lazy: bool = False):
        """Load pack from a zipfile or the filesystem.

        Unless `lazy=True` is specified, all the files will be loaded
        eagerly. If no arguments are provided, the method will make
        sure that all the files in the pack are loaded.
        """
        if pack is not None:
            if isinstance(pack, ZipFile):
                self.zipfile = pack
            elif not Path(pack).exists():
                raise FileNotFoundError(pack)
            else:
                self.path = pack

        path = Path(self.path).resolve() if self.path else None

        if not self.name:
            self.name = (
                self.zipfile
                and self.zipfile.filename
                and Path(self.zipfile.filename).stem
            ) or (path and path.stem)

        if self.zipped is None:
            self.zipped = bool(self.zipfile) or path and path.suffix == ".zip"

        origin: Union[Path, ZipFile, None] = self.zipfile

        if not origin and path:
            if path.is_file():
                origin = ZipFile(path)
            elif path.is_dir():
                origin = path

        if origin:
            self.mcmeta = JsonFile.load(origin, "pack.mcmeta")
            self.image = PngFile.try_load(origin, "pack.png")

            for name, namespace in self.namespace_type.scan(origin):
                self[name].merge(namespace)

        if not self.pack_format:
            self.pack_format = self.latest_pack_format
        if not self.description:
            self.description = ""

        if not lazy:
            for _, pack_file in self.content:
                pack_file.value

    def dump(
        self,
        directory: Optional[FileSystemPath] = None,
        zipped: Optional[bool] = None,
        overwrite: Optional[bool] = False,
    ) -> Path:
        """Write the pack to a zipfile or to the filesystem."""
        name = self._ensure_name()

        if not directory:
            directory = Path(self.path).parent if self.path else Path.cwd()

        path = Path(directory).resolve()

        if zipped is None:
            zipped = bool(self.zipped)

        pack_factory: Any

        if zipped:
            output_path = path / f"{name}.zip"
            pack_factory = partial(ZipFile, mode="w")
        else:
            output_path = path / name
            pack_factory = nullcontext

        if output_path.exists():
            if not overwrite:
                raise FileExistsError(f"Couldn't overwrite {str(output_path)!r}.")

            if output_path.is_dir():
                shutil.rmtree(output_path)
            else:
                Path(output_path).unlink()

        if not zipped:
            output_path.mkdir(parents=True)

        with pack_factory(output_path) as pack:
            self.mcmeta.dump(pack, "pack.mcmeta")
            if self.image:
                self.image.dump(pack, "pack.png")

            for namespace_name, namespace in self.items():
                namespace.dump(namespace_name, pack)

        return output_path

    def _ensure_name(self) -> str:
        if self.name:
            return self.name
        for i in count():
            if not Path(name := self.default_name + (str(i) if i else "")).exists():
                self.name = name
                return name
        unreachable()
