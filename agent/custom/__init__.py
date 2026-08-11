import sys

from . import action

sys.modules.setdefault("custom", sys.modules[__name__])
sys.modules.setdefault("custom.action", action)


def register_all() -> None:
    action.register_all()


__all__ = ["register_all"]
