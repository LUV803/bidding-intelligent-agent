"""兼容转发 → `bidding_qa.clients.vision_client`（2026-09-20 迁移）。

⚠ **新代码请直接 `from .clients import vision_client`。**

⚠⚠ **这里刻意不导出 `_client`。** 转发模块与真模块是**两个不同的模块对象**——
往这个模块上写 `vision._client = ...` 只会写进转发模块自己的命名空间，而
`_get_client()` 读的是真模块的全局，两者互不相干。表现是**静默的**：赋值看似成功，
实际什么都没做，单例照旧复用。

所以 `scripts/probe_vision.py` 里那两处「把单例置空以验未配置分支」的代码，
必须拿到**真模块**：

    from bidding_qa.clients import vision_client as vision   # ← 探针用的是这句
    vision._client = None                                    # 这才写到了真全局上

其余名字都是函数/常量，转发是安全的。
"""

from __future__ import annotations

from .clients.vision_client import (
    VISION_FIELDS,
    VisionNotConfiguredError,
    _extract_json,
    _to_fields,
    analyze,
    is_configured,
    vision_status,
)

__all__ = [
    "VISION_FIELDS",
    "VisionNotConfiguredError",
    "_extract_json",
    "_to_fields",
    "analyze",
    "is_configured",
    "vision_status",
]
