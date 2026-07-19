# Standard library imports
from types import FunctionType
from typing import Any, ClassVar, NoReturn, Self, override

# First party imports
from aeth_ext.static_eval import SubclassInfo, find_subclasses_local, get_caller_file, get_package_root

# Methods on ``MonkeyPatcher`` itself that drive the machinery and must keep their
# ``cls`` binding; every *other* method defined on a subclass is forced static.
_RESERVED_METHODS = frozenset({"apply_monkey_patches", "get_all_subclasses"})


class MonkeyPatcherMeta(type):
  """
  Metaclass that records, on every class it creates, the complete set of
  attribute names available on that class (including dunders and inherited
  names) at the moment the class is defined, and forces every plainly-defined
  method (other than the reserved machinery methods) to be a ``staticmethod``.
  """

  __all_attr_names__: frozenset[str]  # pyright: ignore[reportUninitializedInstanceVariable]

  def __new__(mcs, name: str, bases: tuple[type, ...], namespace: dict[str, Any]):
    # Inject ``__slots__ = ()`` for any subclass that does not declare its own.
    # ``__slots__`` only suppresses ``__dict__``/``__weakref__`` for the class
    # that defines it, so a slotted base does not stop an unslotted subclass from
    # growing both back. Defaulting it here guarantees every class in the
    # hierarchy stays slotted unless it opts out by declaring ``__slots__``.
    namespace.setdefault("__slots__", ())

    # Force every plain function defined in the body to be static, so patch
    # methods are written without a ``self``/``cls`` parameter and can be called
    # bare. ``classmethod``/``staticmethod`` objects are not ``FunctionType`` and
    # are therefore left untouched, as are dunders and the reserved methods.
    for attr_name, value in list(namespace.items()):
      if (
        isinstance(value, FunctionType)
        and attr_name not in _RESERVED_METHODS
        and not (attr_name.startswith("__") and attr_name.endswith("__"))
      ):
        namespace[attr_name] = staticmethod(value)

    cls = super().__new__(mcs, name, bases, namespace)
    cls.__all_attr_names__ = frozenset(dir(cls))
    return cls

  @override
  def __call__(cls, *args: object, **kwargs: object) -> NoReturn:
    raise TypeError(f"{cls.__name__} is not instantiable; use its classmethods directly.")


class MonkeyPatcher(metaclass=MonkeyPatcherMeta):
  """
  A class that captures all of its subclasses and provides a method to apply monkey patches.
  """

  __slots__ = ()

  __all_attr_names__: ClassVar[frozenset[str]] = frozenset()
  """Tuple of every attribute name on this class, captured at class-creation time."""

  @classmethod
  def get_all_subclasses(cls: type[Self], caller_file: str | None = None) -> tuple[SubclassInfo, ...]:
    """
    :param caller_file:
        The file to search from. Defaults to the direct caller of this method;
        pass this explicitly when called from within another wrapper (e.g.
        ``aeth_ext.initialize()``) so the search starts from the real caller
        rather than that wrapper's own location.
    """
    if caller_file is None:
      caller_file = get_caller_file(1)
      if caller_file is None:
        raise RuntimeError(
          "get_all_subclasses: could not automatically determine the calling file; pass caller_file explicitly."
        )
    # Enable the bare-name fallback: when this module is the entrypoint, the live
    # base class's ``__module__`` is ``"__main__"`` while the static scan keys the
    # same file by its import path (e.g. ``aeth_ext.monkey_patcher``). Those
    # qualified names never match, so without the fallback no subclasses are found.
    subclasses = find_subclasses_local(
      cls, caller_file, get_package_root(caller_file), include_name_fallback=__name__ == "__main__"
    )
    return subclasses

  @classmethod
  def apply_monkey_patches(cls, caller_file: str | None = None) -> None:
    """
    Apply monkey patches for all subclasses of this class.

    :param caller_file:
        The file to search from when locating subclasses. Defaults to the
        direct caller of this method; pass this explicitly when called from
        within another wrapper (e.g. ``aeth_ext.initialize()``) so the search
        starts from the real entrypoint rather than that wrapper's own location.
    """
    if caller_file is None:
      caller_file = get_caller_file(1)
      if caller_file is None:
        raise RuntimeError(
          "apply_monkey_patches: could not automatically determine the calling file; pass caller_file explicitly."
        )
    subclasses = cls.get_all_subclasses(caller_file=caller_file)
    for subclass in subclasses:
      inited_subclass = subclass.load()

      # get the NOR of cls and subclass __all_attr_names__
      patch_names = inited_subclass.__all_attr_names__ - cls.__all_attr_names__

      # iterate through all methods that are defined on the subclass but not on THIS base class
      # And call each method found matching this criteria
      for attr_name in patch_names:
        attr = getattr(inited_subclass, attr_name)
        if callable(attr):
          attr()  # call the method to apply the monkey patch
