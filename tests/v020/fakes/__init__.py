"""In-memory fakes for every v0.2 seam, so no task is blocked by another.

These fakes speak the *frozen* interfaces of docs/v020-contracts.md and nothing more. They are not
a simulator of the real hardware (that is tests/v020/sim_v020.py, task F): they are a seam.
"""

import os
import sys

_LIB = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "lib"))
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

from .fake_group import FakeGroup  # noqa: E402
from .fake_media import FakeMedia, FakeProxy  # noqa: E402
from .fake_receiver import FakeReceiver  # noqa: E402

__all__ = ["FakeGroup", "FakeMedia", "FakeProxy", "FakeReceiver"]
