"""MCP 客户端层：让 **Agent** 用上一个外部的 MCP 服务。

⚠⚠ **这个包在 `src/bidding_qa/` 下面，不是 `src/mcp/`。** 后者会**遮蔽官方 `mcp`
SDK** —— `sys.path.insert(0, .../src)` 是本仓库 23 个脚本的既有写法（`.pth` 也把
`src/` 挂上了），而 `src/` 排在 site-packages **前面**，所以 `src/mcp/` 一旦存在，
`import mcp` 拿到的就是它，`from mcp import ClientSession` 直接 ImportError。
这条已在真机上实测过（不是推断），详见 `MCP.md` §3.3 与 CLAUDE.md。
本仓库另一个同形状的坑是 `skill_store.py` 不能叫 `skills.py`（包内有 `skills/` 目录）。

本层有三个模块，分工照 `web_search.py` 与 `tools/rag_tools.py` 那条既有惯例：

```
mcp_client.py       通用但**刻意做小**的 MCP 客户端：连接 / 握手 / 调一次工具 / 把
                    text block 交回来。不认识 Exa，也不认识 WebRow。
                    ⚠ 只管 **Exa 那个托管端点**。
web_search_exa.py   Exa 这一个能力的适配：搜索词与条数怎么给、返回的 JSON 怎么变成
                    WebRow、什么时候算「没搜到」、失败怎么冷却。
mcp_bridge.py       自建服务器的客户端半（`mcp_server.py` 是服务端半）：
                    三个工具经它走独立进程。与 `mcp_client` 刻意分开，
                    理由见它自己的 docstring 开头（两边的契约形状不同）。
```

⚠⚠ **两个客户端刻意不共用一份连接样板**（约 25 行重复）。理由不是懒，是
`mcp_client` 拒绝 `structuredContent` 与工具发现**有它自己的推理**，而那套推理对
Exa 是真话、对自建服务器是假话 —— 把它们塞进同一个模块，就得在文档里给一个模块写两种
互相矛盾的契约。判据是「这是谁的端点」而不是「代码长得像不像」。

⚠ **工具**（`BaseTool` 子类 `search_web_exa`）**不在这里**，在 `tools/rag_tools.py`
—— 与 `web_search.py` 不定义 `WebSearchTool` 是同一条分工（能力模块 vs 工具定义），
也顺手避开了 import 环：`tools/__init__.py` 会把 `rag_tools` 拉进来，若本包反过来
import `..tools.base`，从「先 import 本包」这条入口进来时 `rag_tools` 会拿到一个
**只执行了一半的模块**（抓不到里面的工具实例）。那是个**入口相关**的静默故障：
服务端起来没事、某个脚本一跑就炸。
"""

from . import mcp_bridge, mcp_client, web_search_exa

__all__ = ["mcp_bridge", "mcp_client", "web_search_exa"]
