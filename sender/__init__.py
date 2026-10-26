"""v0.2.0 owner app ("cast-sender"): HTTP API, session state machine, quiet hours, group lock.

Every module in this package talks to the frozen seams of `docs/v020-contracts.md` only. It
opens no socket to a speaker, a group or a Chromecast by itself: the seams (``SenderLink``,
``CastGroup``, ``MediaProxy``, ``Controller``) own the I/O and are injected here, which is what
lets the whole skeleton run offline against ``tests/v020/fakes``.

Entry point: ``python -m sender.service`` (see ``sender/service.py``).
"""

from __future__ import annotations

import os
import sys

__version__ = "0.2.0"

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LIB = os.path.join(_ROOT, "lib")
if _LIB not in sys.path:
    # The frozen interfaces live in lib/v020; running `python -m sender.service` from the
    # repository root must find them without an installed package.
    sys.path.insert(0, _LIB)

ROOT = _ROOT

__all__ = ["ROOT", "__version__"]
