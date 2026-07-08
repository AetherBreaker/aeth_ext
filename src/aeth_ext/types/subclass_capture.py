# Standard library imports
from functools import wraps
from typing import TYPE_CHECKING, Any, ClassVar, Self

# Third party imports
from pydantic._internal._model_construction import ModelMetaclass

# First party imports
from aeth_ext.static_eval import find_subclasses, get_cls_scan_root, get_entrypoint_root

if TYPE_CHECKING:
  # Third party imports
  from pydantic import BaseModel


def _pydantic_post_init_bridge(self: CapturesSubclasses, context: Any, /) -> None:
  """Adapt pydantic's ``model_post_init`` hook to ``CapturesSubclasses.__post_init__``."""
  self.__post_init__()


class CapturesSubclasses:
  """
  Mixin that registers every instance of its subclasses and exposes a
  ``__post_init__`` hook that runs after initialisation.

  The hook is wired up automatically for each subclass:

  * Plain classes have their ``__init__`` wrapped so ``__post_init__`` runs
    exactly once, after the most-derived ``__init__`` completes.
  * :class:`pydantic.BaseModel` subclasses route pydantic's ``model_post_init``
    machinery to ``__post_init__`` (so the hook also fires for
    ``model_validate``), regardless of MRO ordering relative to ``BaseModel``.

  This class is only meant to be inherited from, never instantiated directly.
  Override ``__post_init__`` in a subclass to add behaviour; call
  ``super().__post_init__()`` to keep instance registration.
  """

  __slots__ = ()

  __instances__: ClassVar[list[Self]] = []

  def __post_init__(self) -> None:
    """
    Default post-initialisation hook.

    Registers the freshly created instance in the shared ``__instances__``
    registry.
    """
    self.__instances__.append(self)

  def __init_subclass__(cls, **kwargs: Any) -> None:
    super().__init_subclass__(**kwargs)

    # Give each hierarchy its own registry. A direct subclass of
    # ``CapturesSubclasses`` is a "root": it starts a fresh ``__instances__`` so
    # unrelated hierarchies never share instances. Deeper subclasses
    # deliberately do *not* reset it, so they register into their root's
    # registry and remain visible to ``root.get_final_cls()``.
    if CapturesSubclasses in cls.__bases__:
      cls.__instances__ = []

    if isinstance(cls, ModelMetaclass):
      # Pydantic model: bridge ``model_post_init`` -> ``__post_init__``.
      # Assigning the bridge onto the subclass guarantees pydantic enables the
      # hook irrespective of where ``BaseModel`` sits in the MRO, and ensures it
      # fires for ``model_validate`` as well as ``__init__``.
      cls.model_post_init = _pydantic_post_init_bridge  # pyright: ignore[reportAttributeAccessIssue]
      return

    # Plain class: wrap ``__init__`` so ``__post_init__`` runs afterwards.
    wrapped_init = cls.__init__

    @wraps(wrapped_init)
    def post_init_wrapper(self: Any, *args: Any, **kwargs: Any) -> None:
      wrapped_init(self, *args, **kwargs)
      # Fire only for the most-derived ``__init__`` so the hook runs exactly
      # once per instance, even through ``super().__init__()`` chains.
      if type(self).__init__ is post_init_wrapper:
        self.__post_init__()

    # Assign via ``setattr`` so the type checker keeps the original ``__init__``
    # signature: a direct ``cls.__init__ = post_init_wrapper`` makes Pyright treat
    # ``__init__`` as a bare function attribute, which then makes ``cls()`` appear
    # to require an explicit ``self`` argument.
    setattr(cls, "__init__", post_init_wrapper)  # noqa: B010

  @classmethod
  def get_final_cls(cls: type[Self]) -> Self:
    # Search in reverse so the most recently created compatible instance is returned.
    for instance in reversed(cls.__instances__):
      if isinstance(instance, cls):
        return instance
    deepest_subclass = cls.get_deepest_subclass()
    return deepest_subclass()  # Create a new instance of the deepest subclass

  @classmethod
  def get_final_model(cls: type[Self]) -> BaseModel:
    # Search in reverse so the most recently created compatible instance is returned.
    for instance in reversed(cls.__instances__):
      if isinstance(instance, cls):
        return instance  # pyright: ignore[reportReturnType]
    deepest_subclass = cls.get_deepest_subclass()
    return deepest_subclass.model_validate({})  # pyright: ignore[reportAttributeAccessIssue]

  @classmethod
  def get_deepest_subclass(cls: type[Self]) -> type[Self]:

    root = get_cls_scan_root(cls)
    entrypoint_root = get_entrypoint_root()

    # When root is scoped to the base class's own installed package (site-packages
    # mode), also scan the entrypoint package so that subclasses defined in a
    # separate installed package (e.g. the application that depends on this library)
    # are discovered.  iter_python_files deduplicates paths, so passing the same
    # root twice is harmless.
    roots = [root, entrypoint_root] if root != entrypoint_root else root

    subclasses = find_subclasses(cls, roots)

    if not subclasses:
      return cls  # No subclasses, return the class itself

    # Find the subclass with the maximum depth
    deepest_subclass = max(subclasses, key=lambda sub: sub.depth)
    return deepest_subclass.load()

  @classmethod
  def get_all_subclasses(cls: type[Self]) -> list[type[Self]]:
    root = get_cls_scan_root(cls)
    subclasses = find_subclasses(cls, root)
    return [sub.load() for sub in subclasses]
