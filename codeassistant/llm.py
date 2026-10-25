"""LLM 抽象层。

三种后端可互换（都实现 ``langchain_core`` 的 ``BaseChatModel``）：

* ``openai``    —— OpenAI 兼容接口，通过 ``base_url`` 适配 DeepSeek / Kimi / Qwen / OpenAI
* ``anthropic`` —— Anthropic Claude
* ``mock``      —— 离线确定性模型，无 Key 也能跑通全链路与自动化测试
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk
from langchain_core.outputs import ChatResult as ChatResultType
from langchain_core.tools import BaseTool
from pydantic import ConfigDict, Field

from .settings import Settings


class MockChat(BaseChatModel):
    """离线 Mock 模型。

    两种工作方式：

    * **脚本模式** —— ``script`` 显式给出每一轮的返回内容，可精确驱动
      ReAct 循环（先要求调工具、再给结论），自动化测试用它替代真实 LLM。
    * **启发模式** —— ``script`` 为空时，按调用轮次返回确定性的计划/结论文本。
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    model: str = "mock-code-assistant"
    script: list[dict[str, Any]] = Field(default_factory=list)
    cursor: int = 0
    tag: str = ""

    # -- BaseChatModel ----------------------------------------------------

    @property
    def _llm_type(self) -> str:
        return "codeassistant-mock"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResultType:
        reply = self._next_reply(messages)
        return ChatResultType(generations=[ChatGeneration(message=reply)])

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> Any:
        reply = self._next_reply(messages)
        chunk = ChatGenerationChunk(message=reply)
        yield chunk

    def bind_tools(
        self, tools: Sequence[BaseTool | dict[str, Any]], **kwargs: Any
    ) -> Any:
        names = [getattr(t, "name", None) or t.get("name") for t in tools]
        return self.bind(tools=list(names))

    # -- 内部 -------------------------------------------------------------

    def _next_reply(self, messages: list[BaseMessage]) -> AIMessage:
        if self.script:
            index = min(self.cursor, len(self.script) - 1)
            self.cursor += 1
            turn = self.script[index]
            tool_calls = [
                {
                    "name": call["name"],
                    "args": call.get("args", {}),
                    "id": call.get("id", f"call_{index}_{position}"),
                    "type": "tool_call",
                }
                for position, call in enumerate(turn.get("tool_calls", []))
            ]
            return AIMessage(
                content=turn.get("content", ""),
                tool_calls=tool_calls,
            )

        task = self._latest_user_text(messages)
        if self.cursor == 0:
            self.cursor += 1
            return AIMessage(
                content=(
                    f"（mock）已接收任务：{task}\n"
                    "计划：1) 定位相关代码 2) 分析实现方案 3) 输出可执行结论。"
                )
            )
        self.cursor += 1
        return AIMessage(
            content=(
                f"（mock）针对「{task}」的处理建议：\n"
                "1. 先用 list_files / search_code 摸清现状；\n"
                "2. 再用 read_file 精读关键文件；\n"
                "3. 最后给出结论与后续动作。"
            )
        )

    @staticmethod
    def _latest_user_text(messages: list[BaseMessage]) -> str:
        for message in reversed(messages):
            if message.type == "human":
                return str(message.content)
        return ""


def build_chat_model(
    settings: Settings,
    model: str | None = None,
    factory: Callable[[Settings, str | None], BaseChatModel] | None = None,
) -> BaseChatModel:
    """按配置构造聊天模型。"""

    if factory is not None:
        return factory(settings, model)

    provider = settings.provider
    resolved_model = model or settings.model

    if provider == "mock":
        return MockChat(model=resolved_model or "mock-code-assistant")

    if provider == "openai":
        try:
            from langchain_openai import ChatOpenAI
        except ImportError as exc:  # pragma: no cover - 依赖缺失时的提示
            raise RuntimeError(
                "缺少 langchain-openai，请执行: pip install 'codeassistant[openai]'"
            ) from exc
        if not settings.api_key:
            raise RuntimeError(
                "provider=openai 需要设置 CODEASSISTANT_API_KEY 环境变量。"
            )
        return ChatOpenAI(
            model=resolved_model or "gpt-4o-mini",
            api_key=settings.api_key,
            base_url=settings.base_url or None,
            temperature=settings.temperature,
        )

    if provider == "anthropic":
        try:
            from langchain_anthropic import ChatAnthropic
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "缺少 langchain-anthropic，请执行: pip install 'codeassistant[anthropic]'"
            ) from exc
        if not settings.api_key:
            raise RuntimeError(
                "provider=anthropic 需要设置 CODEASSISTANT_API_KEY 环境变量。"
            )
        return ChatAnthropic(
            model=resolved_model or "claude-sonnet-4-5",
            api_key=settings.api_key,
            temperature=settings.temperature,
        )

    raise RuntimeError(f"未知的 LLM provider: {provider!r}")
