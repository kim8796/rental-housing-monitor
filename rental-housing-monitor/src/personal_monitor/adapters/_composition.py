from __future__ import annotations

from weakref import WeakKeyDictionary


def _composition_accessors():
    registry: WeakKeyDictionary[object, tuple[object, ...]] = WeakKeyDictionary()

    def bind(owner: object, components: tuple[object, ...]) -> None:
        if owner in registry:
            raise RuntimeError("adapter composition is already bound")
        registry[owner] = components

    def acquire(
        owner: object,
        current_components: tuple[object, ...],
    ) -> tuple[object, ...] | None:
        pinned = registry.get(owner)
        if pinned is None or len(pinned) != len(current_components):
            return None
        if any(
            current is not original
            for current, original in zip(current_components, pinned, strict=True)
        ):
            return None
        return pinned

    return bind, acquire


_bind_adapter_composition, _acquire_adapter_composition = _composition_accessors()
