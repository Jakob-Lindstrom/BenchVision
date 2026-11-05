"""Generic plugin registry for extensible camera backends and processing methods."""

from __future__ import annotations

from collections.abc import Callable
from typing import Generic, TypeVar

T = TypeVar("T")


class PluginRegistry(Generic[T]):
    """Thread-safe registry mapping string plugin keys to classes or factories."""

    def __init__(self, category: str) -> None:
        self.category = category
        self._items: dict[str, type[T]] = {}

    def register(self, key: str) -> Callable[[type[T]], type[T]]:
        """Decorator to register a plugin class under a unique key."""

        def _decorator(cls: type[T]) -> type[T]:
            norm = key.strip().lower()
            self._items[norm] = cls
            return cls

        return _decorator

    def get(self, key: str) -> type[T]:
        """Retrieve a registered plugin class by key."""
        norm = key.strip().lower()
        if norm not in self._items:
            available = ", ".join(sorted(self._items.keys()))
            raise KeyError(
                f"Unknown {self.category} plugin '{key}'. Available: [{available}]"
            )
        return self._items[norm]

    def list_keys(self) -> list[str]:
        """Return sorted list of registered plugin keys."""
        return sorted(self._items.keys())

    def all_items(self) -> dict[str, type[T]]:
        """Return a shallow copy of all registered plugins."""
        return dict(self._items)
