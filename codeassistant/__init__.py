"""CodeAssistant —— 基于 LangGraph 的多专家代码助手。

三层架构：

* **专家（Expert）** —— 独立编译的 LangGraph 子图，拥有独立 State Schema /
  Checkpointer / 中断逻辑，见 :mod:`codeassistant.graphs.expert`
* **专家团（Team）** —— 专家的编排层，支持「主理人动态分派」与「并行汇」两种
  协作模式，见 :mod:`codeassistant.graphs.supervisor_team` /
  :mod:`codeassistant.graphs.parallel_team`
* **主编排器（Orchestrator）** —— 意图路由与结果汇总，见
  :mod:`codeassistant.graphs.orchestrator`

典型用法::

    from codeassistant import CodeAssistant, Settings

    app = CodeAssistant(Settings.load(workspace="./my-project"))
    result = await app.run("帮我看看这个项目的结构")
    print(result.summary)
"""

from .app import CodeAssistant, RunResult
from .settings import Settings

__version__ = "0.1.0"

__all__ = ["CodeAssistant", "RunResult", "Settings", "__version__"]
