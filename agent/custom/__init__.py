import sys

from . import action, recognition

sys.modules.setdefault("custom", sys.modules[__name__])
sys.modules.setdefault("custom.action", action)
sys.modules.setdefault("custom.recognition", recognition)


def register_all() -> None:
    action.register_all()
    recognition.register_all()


__all__ = ["register_all"]
