import asyncio
import json
import logging
import os
from typing import Annotated

import aiohttp
from ddgs import DDGS
from langchain_core.messages import AnyMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.tools import tool
from langchain_groq import ChatGroq
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import ToolNode
from langgraph.graph import StateGraph, END, add_messages
from pydantic import BaseModel, Field
from trafilatura import extract

logger = logging.getLogger(__name__)
ASSISTANT_SYSTEM_PROMPT_BASE = "You are a helpful assistant."

search_enabled = True
groq_api_key = os.getenv("GROQ_API_KEY")
if not groq_api_key:
    raise RuntimeError("GROQ_API_KEY is required. Add it to .env before starting the app.")

@tool
async def download_website_text(url: str) -> str:
    """Download the text from a website"""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                response.raise_for_status()
                downloaded = await response.text()
        result = extract(
            downloaded,
            include_formatting=True,
            include_links=True,
            output_format="json",
            with_metadata=True,
        )
        return result or "No text found on the website"
    except Exception as e:
        logger.error(f"Failed to download {url}: {str(e)}")
        return f"Error retrieving website content: {str(e)}"

@tool
async def duckduckgo_search(query: str) -> str:
    """Search the web with DuckDuckGo."""
    def search():
        with DDGS() as ddgs:
            return list(ddgs.text(query, max_results=5))

    try:
        results = await asyncio.to_thread(search)
        return json.dumps(results, ensure_ascii=False)
    except Exception as e:
        logger.exception("DuckDuckGo search failed")
        return f"DuckDuckGo search failed: {e}"

tools = [download_website_text, duckduckgo_search]

weak_model = ChatGroq(
    model=os.getenv("GROQ_MODEL", "openai/gpt-oss-20b"),
    temperature=0,
    max_tokens=1024,
    api_key=groq_api_key,
)
model = weak_model
assistant_model = weak_model

class GraphProcessingState(BaseModel):
    messages: Annotated[list[AnyMessage], add_messages] = Field(default_factory=list)
    prompt: str = Field(default_factory=str, description="The prompt to be used for the model")
    tools_enabled: dict = Field(default_factory=dict, description="The tools enabled for the assistant")
    search_enabled: bool = Field(default=True, description="Whether to enable search tools")

async def assistant_node(state: GraphProcessingState, config=None):
    assistant_tools = []
    if state.tools_enabled.get("download_website_text", True):
        assistant_tools.append(download_website_text)
    if search_enabled and state.tools_enabled.get("duckduckgo_search", True):
        assistant_tools.append(duckduckgo_search)
    assistant_model = model.bind_tools(assistant_tools)
    if state.prompt:
        final_prompt = "\n".join([state.prompt, ASSISTANT_SYSTEM_PROMPT_BASE])
    else:
        final_prompt = ASSISTANT_SYSTEM_PROMPT_BASE

    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", final_prompt),
            MessagesPlaceholder(variable_name="messages"),
        ]
    )
    chain = prompt | assistant_model
    response = await chain.ainvoke({"messages": state.messages}, config=config)

    return {
        "messages": response
    }

def assistant_cond_edge(state: GraphProcessingState):
    last_message = state.messages[-1]
    if hasattr(last_message, "tool_calls") and last_message.tool_calls:
        logger.info(f"Tool call detected: {last_message.tool_calls}")
        return "tools"
    return END

def define_workflow() -> CompiledStateGraph:
    """Defines the workflow graph"""
    # Initialize the graph
    workflow = StateGraph(GraphProcessingState)

    # Add nodes
    workflow.add_node("assistant_node", assistant_node)
    workflow.add_node("tools", ToolNode(tools))

    # Edges
    workflow.add_edge("tools", "assistant_node")

    # Conditional routing
    workflow.add_conditional_edges(
        "assistant_node",
        assistant_cond_edge,
    )
    workflow.set_entry_point("assistant_node")

    return workflow.compile()

graph = define_workflow()
