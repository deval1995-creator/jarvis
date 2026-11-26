"""Make the CUDA 12 / cuDNN 9 DLLs from the nvidia-* wheels findable on Windows.

faster-whisper (CTranslate2) loads cublas and cudnn by name; the pip wheels put
them in site-packages/nvidia/*/bin, which isn't on the DLL search path.
"""

import os
import sys
from pathlib import Path

_done = False


def add_dll_directories() -> list[str]:
    global _done
    if _done or sys.platform != "win32":
        return []
    _done = True
    added = []
    for root in {Path(sys.prefix), Path(sys.base_prefix)}:
        for bin_dir in (root / "Lib" / "site-packages" / "nvidia").glob("*/bin"):
            os.add_dll_directory(str(bin_dir))
            os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
            added.append(str(bin_dir))
    return added
