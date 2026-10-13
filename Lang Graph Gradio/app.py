#!/usr/bin/env python

import os
import logging
import logging.config
from collections import deque
from pathlib import Path
from typing import Any
from uuid import uuid4, UUID
import json
import time

import gradio as gr
from dotenv import load_dotenv
from langchain_core.messages import HumanMessage
from langgraph.types import RunnableConfig
from pydantic import BaseModel

load_dotenv()

# There are tools set here dependent on environment variables
from graph import graph, weak_model, search_enabled # noqa

FOLLOWUP_QUESTION_NUMBER = 3
TRIM_MESSAGE_LENGTH = 16  # Includes tool messages
USER_INPUT_MAX_LENGTH = 10000  # Characters


def _env_int(name: str, default: int) -> int:
    """Read an integer environment variable with a safe fallback."""
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    """Read a boolean environment variable ('1'/'true'/'yes'/'on')."""
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# --- Scaling knobs (tune these via env vars when serving thousands of users) ---
# Max simultaneous in-flight chat requests per replica. Additional requests are
# queued by Gradio instead of being rejected.
CHAT_CONCURRENCY_LIMIT = _env_int("CHAT_CONCURRENCY_LIMIT", 32)
# Global queue size per replica; requests beyond this get a "queue full" error.
QUEUE_MAX_SIZE = _env_int("QUEUE_MAX_SIZE", 1000)
# Enable the public /api endpoint so the bot can be embedded/automated.
API_ENABLE = _env_bool("API_ENABLE", True)
# Optional simple rate limiting for the public deployment (per client IP,
# in-memory sliding window). Set RATE_LIMIT_ENABLED=false to disable.
RATE_LIMIT_ENABLED = _env_bool("RATE_LIMIT_ENABLED", True)
RATE_LIMIT_PER_MINUTE = _env_int("RATE_LIMIT_PER_MINUTE", 150)


class SlidingWindowRateLimiter:
    """Small in-memory per-client rate limiter (sliding window)."""

    def __init__(self, max_per_minute: int):
        self.max_per_minute = max(1, max_per_minute)
        self._hits: dict[str, deque[float]] = {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        window = self._hits.setdefault(key, deque())
        while window and now - window[0] >= 60.0:
            window.popleft()
        if len(window) >= self.max_per_minute:
            return False
        window.append(now)
        # Prevent unbounded memory growth from many distinct clients.
        if len(self._hits) > 10_000:
            stale = [
                k for k, dq in self._hits.items() if not dq or now - dq[-1] >= 120.0
            ]
            for k in stale:
                self._hits.pop(k, None)
        return True


rate_limiter = SlidingWindowRateLimiter(RATE_LIMIT_PER_MINUTE)


def client_key_from_request(request) -> str:
    """Best-effort client identity: first hop of X-Forwarded-For, else socket."""
    try:
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            return xff.split(",")[0].strip()
        host = request.client.host if request.client is not None else None
        if host:
            return host
    except Exception:
        logger.exception("Could not determine client key; using 'unknown'")
    return "unknown"

# Set a stable value in .env to preserve browser state across server restarts.
BROWSER_STORAGE_SECRET = os.getenv("GRADIO_BROWSER_STORAGE_SECRET")

LOGGING_CONFIG_PATH = Path(__file__).with_name("logging-config.json")
if LOGGING_CONFIG_PATH.exists():
    with open(LOGGING_CONFIG_PATH, "r", encoding="utf-8") as fh:
        config = json.load(fh)
else:  # Fallback so the app still starts without the file (e.g. slim images).
    config = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "verbose": {
                "format": "%(asctime)s:%(name)s:%(levelname)s: %(message)s",
                "datefmt": "%Y-%m-%d %H:%M:%S",
            }
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "level": "INFO",
                "formatter": "verbose",
            }
        },
        "root": {"level": "INFO", "handlers": ["console"]},
    }
logging.config.dictConfig(config)
logger = logging.getLogger(__name__)

async def chat_fn(user_input: str, history: dict, input_graph_state: dict, uuid: UUID, prompt: str, search_enabled: bool, download_website_text_enabled: bool):
    """
    Args:
        user_input (str): The user's input message
        history (dict): The history of the conversation in gradio
        input_graph_state (dict): The current state of the graph. This includes tool call history
        uuid (UUID): The unique identifier for the current conversation. This can be used in conjunction with langgraph or for memory
        prompt (str): The system prompt
    Yields:
        str: The output message
        dict|Any: The final state of the graph
        bool|Any: Whether to trigger follow up questions

        We do not use gradio history in the graph since we want the ToolMessage in the history
        ordered properly. GraphProcessingState.messages is used as history instead
    """
    try:
        logger.info(f"Prompt: {prompt}")
        if not user_input or not user_input.strip():
            yield "Please enter a message.", gr.skip(), False
            return
        input_graph_state["tools_enabled"] = {
            "download_website_text": download_website_text_enabled,
            "duckduckgo_search": search_enabled,
        }
        if prompt:
            input_graph_state["prompt"] = prompt
        if "messages" not in input_graph_state:
            input_graph_state["messages"] = []
        input_graph_state["messages"].append(
            HumanMessage(user_input[:USER_INPUT_MAX_LENGTH])
        )
        input_graph_state["messages"] = input_graph_state["messages"][-TRIM_MESSAGE_LENGTH:]
        config = RunnableConfig(
            recursion_limit=20,
            run_name="user_chat",
            configurable={"thread_id": uuid}
        )

        output: str = ""
        final_state: dict | Any = {}
        waiting_output_seq: list[str] = []

        async for stream_mode, chunk in graph.astream(
                    input_graph_state,
                    config=config,
                    stream_mode=["values", "messages"],
                ):
            if stream_mode == "values":
                final_state = chunk
                last_message = chunk["messages"][-1]
                if hasattr(last_message, "tool_calls"):
                    for msg_tool_call in last_message.tool_calls:
                        tool_name: str = msg_tool_call['name']
                        if tool_name == "duckduckgo_search":
                            query = msg_tool_call['args']['query']
                            waiting_output_seq.append(f"Searching for '{query}'...")
                            yield "\n".join(waiting_output_seq), gr.skip(), gr.skip()
                        # download_website_text is the name of the function defined in graph.py
                        elif tool_name == "download_website_text":
                            url = msg_tool_call['args']['url']
                            waiting_output_seq.append(f"Downloading text from '{url}'...")
                            yield "\n".join(waiting_output_seq), gr.skip(), gr.skip()
                        else:
                            waiting_output_seq.append(f"Running {tool_name}...")
                            yield "\n".join(waiting_output_seq), gr.skip(), gr.skip()
            elif stream_mode == "messages":
                msg, metadata = chunk
                # print("output: ", msg, metadata)
                # assistant_node is the name we defined in the langgraph graph
                if metadata['langgraph_node'] == "assistant_node" and msg.content:
                    output += msg.content
                    yield output, gr.skip(), gr.skip()
        # Trigger for asking follow up questions
        # + store the graph state for next iteration
        # yield output, dict(final_state), gr.skip()
        yield output + " ", dict(final_state), True
    except Exception:
        logger.exception("Exception occurred")
        user_error_message = "There was an error processing your request. Please try again."
        yield user_error_message, gr.skip(), False

def clear():
    return dict(), uuid4()

class FollowupQuestions(BaseModel):
    """Model for langchain to use for structured output for followup questions"""
    questions: list[str]

async def populate_followup_questions(end_of_chat_response: bool, messages: list[dict[str, str]], uuid: UUID):
    """
    This function gets called a lot due to the asynchronous nature of streaming

    Only populate followup questions if streaming has completed and the message is coming from the assistant
    """
    if not end_of_chat_response or not messages or messages[-1]["role"] != "assistant":
        return *[gr.skip() for _ in range(FOLLOWUP_QUESTION_NUMBER)], False
    config = RunnableConfig(
        run_name="populate_followup_questions",
        configurable={"thread_id": uuid}
    )
    weak_model_with_config = weak_model.with_config(config)
    follow_up_questions = await weak_model_with_config.with_structured_output(FollowupQuestions).ainvoke([
        ("system", f"suggest {FOLLOWUP_QUESTION_NUMBER} followup questions for the user to ask the assistant. Refrain from asking personal questions."),
        *messages,
    ])
    if len(follow_up_questions.questions) != FOLLOWUP_QUESTION_NUMBER:
        raise ValueError("Invalid value of followup questions")
    buttons = []
    for i in range(FOLLOWUP_QUESTION_NUMBER):
        buttons.append(
            gr.Button(follow_up_questions.questions[i], visible=True, elem_classes="chat-tab"),
        )
    return *buttons, False

async def summarize_chat(end_of_chat_response: bool, messages: list[dict[str, str]], sidebar_summaries: dict, uuid: UUID):
    """Summarize chat for tab names"""
    # print("\n------------------------")
    # print("not end_of_chat_response", not end_of_chat_response)
    # print("not messages", not messages)
    # if messages:
    #     print("messages[-1][role] != assistant", messages[-1]["role"] != "assistant")
    # print("isinstance(sidebar_summaries, type(lambda x: x))", isinstance(sidebar_summaries, type(lambda x: x)))
    # print("uuid in sidebar_summaries", uuid in sidebar_summaries)
    should_return = (
        not end_of_chat_response or
        not messages or
        messages[-1]["role"] != "assistant" or
        # This is a bug with gradio
        isinstance(sidebar_summaries, type(lambda x: x)) or
        # Already created summary
        uuid in sidebar_summaries
    )
    if should_return:
        return gr.skip(), gr.skip()
    config = RunnableConfig(
        run_name="summarize_chat",
        configurable={"thread_id": uuid}
    )
    weak_model_with_config = weak_model.with_config(config)
    summary_response = await weak_model_with_config.ainvoke([
        ("system", "summarize this chat in 7 tokens or less. Refrain from using periods"),
        *messages,
    ])
    if uuid not in sidebar_summaries:
        sidebar_summaries[uuid] = summary_response.content
    return sidebar_summaries, False

async def new_tab(uuid, gradio_graph, messages, tabs, prompt, sidebar_summaries):
    new_uuid = uuid4()
    new_graph = {}
    if uuid not in sidebar_summaries:
        sidebar_summaries, _ = await summarize_chat(True, messages, sidebar_summaries, uuid)
    tabs[uuid] = {
        "graph": gradio_graph,
        "messages": messages,
        "prompt": prompt,
    }
    suggestion_buttons = []
    for _ in range(FOLLOWUP_QUESTION_NUMBER):
        suggestion_buttons.append(gr.Button(visible=False))
    new_messages = {}
    new_prompt = "You are a helpful assistant."
    return new_uuid, new_graph, new_messages, tabs, new_prompt, sidebar_summaries, *suggestion_buttons

def switch_tab(selected_uuid, tabs, gradio_graph, uuid, messages, prompt):
    # I don't know of another way to lookup uuid other than
    # by the button value

    # Save current state
    if messages:
        tabs[uuid] = {
            "graph": gradio_graph,
            "messages": messages,
            "prompt": prompt
        }

    if selected_uuid not in tabs:
        logger.error(f"Could not find the selected tab in offloaded_tabs_data_storage {selected_uuid}")
        return tuple(gr.skip() for _ in range(5 + FOLLOWUP_QUESTION_NUMBER))
    selected_tab_state = tabs[selected_uuid]
    selected_graph = selected_tab_state["graph"]
    selected_messages = selected_tab_state["messages"]
    selected_prompt = selected_tab_state.get("prompt", "")
    suggestion_buttons = []
    for _ in range(FOLLOWUP_QUESTION_NUMBER):
        suggestion_buttons.append(gr.Button(visible=False))
    return selected_graph, selected_uuid, selected_messages, tabs, selected_prompt, *suggestion_buttons

def delete_tab(current_chat_uuid, selected_uuid, sidebar_summaries, tabs):
    output_messages = gr.skip()
    if current_chat_uuid == selected_uuid:
        output_messages = dict()
    if selected_uuid in tabs:
        del tabs[selected_uuid]
    if selected_uuid in sidebar_summaries:
        del sidebar_summaries[selected_uuid]
    return sidebar_summaries, tabs, output_messages

def submit_edit_tab(selected_uuid, sidebar_summaries, text):
    sidebar_summaries[selected_uuid] = text
    return sidebar_summaries, ""

CSS = """
footer {visibility: hidden}
.followup-question-button {font-size: 12px }
.chat-tab {
    font-size: 12px;
    padding-inline: 0;
}
.chat-tab.active {
    background-color: #654343;
}
#new-chat-button { background-color: #0f0f11; color: white; }

.tab-button-control {
    min-width: 0;
    padding-left: 0;
    padding-right: 0;
}
"""

# We set the ChatInterface textbox id to chat-textbox for this to work
TRIGGER_CHATINTERFACE_BUTTON = """
function triggerChatButtonClick() {

  // Find the div with id "chat-textbox"
  const chatTextbox = document.getElementById("chat-textbox");

  if (!chatTextbox) {
    console.error("Error: Could not find element with id 'chat-textbox'");
    return;
  }

  // Find the button that is a descendant of the div
  const button = chatTextbox.querySelector("button");

  if (!button) {
    console.error("Error: No button found inside the chat-textbox element");
    return;
  }

  // Trigger the click event
  button.click();
}"""

if __name__ == "__main__":
    logger.info("Starting the interface")
    with gr.Blocks(
        title="Langgraph Template",
        fill_height=True,
        css=CSS,
    ) as app:
        current_prompt_state = gr.BrowserState(
            storage_key="current_prompt_state",
            secret=BROWSER_STORAGE_SECRET,
        )
        current_uuid_state = gr.BrowserState(
            uuid4,
            storage_key="current_uuid_state",
            secret=BROWSER_STORAGE_SECRET,
        )
        current_langgraph_state = gr.BrowserState(
            dict(),
            storage_key="current_langgraph_state",
            secret=BROWSER_STORAGE_SECRET,
        )
        end_of_assistant_response_state = gr.State(
            bool(),
        )
        # [uuid] -> summary of chat
        sidebar_names_state = gr.BrowserState(
            dict(),
            storage_key="sidebar_names_state",
            secret=BROWSER_STORAGE_SECRET,
        )
        # [uuid] -> {"graph": gradio_graph, "messages": messages}
        offloaded_tabs_data_storage = gr.BrowserState(
            dict(),
            storage_key="offloaded_tabs_data_storage",
            secret=BROWSER_STORAGE_SECRET,
        )

        chatbot_message_storage = gr.BrowserState(
            [],
            storage_key="chatbot_message_storage",
            secret=BROWSER_STORAGE_SECRET,
        )
        with gr.Column():
            prompt_textbox = gr.Textbox(show_label=False, interactive=True)
        with gr.Row():
            checkbox_search_enabled = gr.Checkbox(
                value=True,
                label="Enable search",
                show_label=True,
                visible=search_enabled,
                scale=1,
            )
            checkbox_download_website_text = gr.Checkbox(
                value=True,
                show_label=True,
                label="Enable downloading text from urls",
                scale=1,
            )
        chatbot = gr.Chatbot(
            type="messages",
            scale=1,
            show_copy_button=True,
            height=600,
            editable="all",
        )
        tab_edit_uuid_state = gr.State(
            str()
        )
        prompt_textbox.change(lambda prompt: prompt, inputs=[prompt_textbox], outputs=[current_prompt_state])
        with gr.Sidebar() as sidebar:
            @gr.render(inputs=[tab_edit_uuid_state, end_of_assistant_response_state, sidebar_names_state, current_uuid_state, chatbot, offloaded_tabs_data_storage])
            def render_chats(tab_uuid_edit, end_of_chat_response, sidebar_summaries, active_uuid, messages, tabs):
                current_tab_button_text = ""
                if active_uuid not in sidebar_summaries:
                    current_tab_button_text = "Current Chat"
                elif active_uuid not in tabs:
                    current_tab_button_text = sidebar_summaries[active_uuid]
                if current_tab_button_text:
                    gr.Button(current_tab_button_text, elem_classes=["chat-tab", "active"])
                for chat_uuid, tab in reversed(tabs.items()):
                    elem_classes = ["chat-tab"]
                    if chat_uuid == active_uuid:
                        elem_classes.append("active")
                    button_uuid_state = gr.State(chat_uuid)
                    with gr.Row():
                        clear_tab_button = gr.Button(
                            "🗑",
                            scale=0,
                            elem_classes=["tab-button-control"]
                        )
                        clear_tab_button.click(
                            fn=delete_tab,
                            inputs=[
                                current_uuid_state,
                                button_uuid_state,
                                sidebar_names_state,
                                offloaded_tabs_data_storage
                            ],
                            outputs=[
                                sidebar_names_state,
                                offloaded_tabs_data_storage,
                                chat_interface.chatbot_value
                            ]
                        )
                        chat_button_text = sidebar_summaries.get(chat_uuid)
                        if not chat_button_text:
                            chat_button_text = str(chat_uuid)
                        if chat_uuid != tab_uuid_edit:
                            set_edit_tab_button = gr.Button(
                                "✎",
                                scale=0,
                                elem_classes=["tab-button-control"]
                            )
                            set_edit_tab_button.click(
                                fn=lambda x: x,
                                inputs=[button_uuid_state],
                                outputs=[tab_edit_uuid_state]
                            )
                            chat_tab_button = gr.Button(
                                chat_button_text,
                                elem_id=f"chat-{chat_uuid}-button",
                                elem_classes=elem_classes,
                                scale=2
                            )
                            chat_tab_button.click(
                                fn=switch_tab,
                                inputs=[
                                    button_uuid_state,
                                    offloaded_tabs_data_storage,
                                    current_langgraph_state,
                                    current_uuid_state,
                                    chatbot,
                                    prompt_textbox
                                ],
                                outputs=[
                                    current_langgraph_state,
                                    current_uuid_state,
                                    chat_interface.chatbot_value,
                                    offloaded_tabs_data_storage,
                                    prompt_textbox,
                                    *followup_question_buttons
                                ]
                            )
                        else:
                            chat_tab_text = gr.Textbox(
                                chat_button_text,
                                scale=2,
                                interactive=True,
                                show_label=False
                            )
                            chat_tab_text.submit(
                                fn=submit_edit_tab,
                                inputs=[
                                    button_uuid_state,
                                    sidebar_names_state,
                                    chat_tab_text
                                ],
                                outputs=[
                                    sidebar_names_state,
                                    tab_edit_uuid_state
                                ]
                            )
                    # )
                # return chat_tabs, sidebar_summaries
            new_chat_button = gr.Button("New Chat", elem_id="new-chat-button")
        chatbot.clear(fn=clear, outputs=[current_langgraph_state, current_uuid_state])
        with gr.Row():
            followup_question_buttons = []
            for i in range(FOLLOWUP_QUESTION_NUMBER):
                btn = gr.Button(f"Button {i+1}", visible=False)
                followup_question_buttons.append(btn)

        multimodal = False
        textbox_component = (
            gr.MultimodalTextbox if multimodal else gr.Textbox
        )
        with gr.Column():
            textbox = textbox_component(
                show_label=False,
                label="Message",
                placeholder="Type a message...",
                scale=7,
                autofocus=True,
                submit_btn=True,
                stop_btn=True,
                elem_id="chat-textbox",
                lines=1,
            )
        chat_interface = gr.ChatInterface(
            chatbot=chatbot,
            fn=chat_fn,
            additional_inputs=[
                current_langgraph_state,
                current_uuid_state,
                prompt_textbox,
                checkbox_search_enabled,
                checkbox_download_website_text,
            ],
            additional_outputs=[
                current_langgraph_state,
                end_of_assistant_response_state
            ],
            type="messages",
            multimodal=multimodal,
            textbox=textbox,
            # Allow many simultaneous chats per replica; extra requests queue.
            concurrency_limit=CHAT_CONCURRENCY_LIMIT,
        )

        new_chat_button.click(
            new_tab,
            inputs=[
                current_uuid_state,
                current_langgraph_state,
                chatbot,
                offloaded_tabs_data_storage,
                prompt_textbox,
                sidebar_names_state,
            ],
            outputs=[
                current_uuid_state,
                current_langgraph_state,
                chat_interface.chatbot_value,
                offloaded_tabs_data_storage,
                prompt_textbox,
                sidebar_names_state,
                *followup_question_buttons,
            ]
        )

        def click_followup_button(btn):
            buttons = [gr.Button(visible=False) for _ in range(len(followup_question_buttons))]
            return btn, *buttons
        for btn in followup_question_buttons:
            btn.click(
                fn=click_followup_button,
                inputs=[btn],
                outputs=[
                    chat_interface.textbox,
                    *followup_question_buttons
                ]
            ).success(lambda: None, js=TRIGGER_CHATINTERFACE_BUTTON)

        chatbot.change(
            fn=populate_followup_questions,
            inputs=[
                end_of_assistant_response_state,
                chatbot,
                current_uuid_state
            ],
            outputs=[
                *followup_question_buttons,
                end_of_assistant_response_state
            ],
            trigger_mode="multiple"
        )
        chatbot.change(
            fn=summarize_chat,
            inputs=[
                end_of_assistant_response_state,
                chatbot,
                sidebar_names_state,
                current_uuid_state
            ],
            outputs=[
                sidebar_names_state,
                end_of_assistant_response_state
            ],
            trigger_mode="multiple"
        )
        chatbot.change(
            fn=lambda x: x,
            inputs=[chatbot],
            outputs=[chatbot_message_storage],
            trigger_mode="always_last"
        )

        @app.load(inputs=[chatbot_message_storage], outputs=[chat_interface.chatbot_value])
        def load_messages(messages):
            return messages

        @app.load(inputs=[current_prompt_state], outputs=[prompt_textbox])
        def load_prompt(current_prompt):
            return current_prompt

    # Production tuning: enable the request queue with a large capacity and
    # configure concurrency for the auxiliary LLM functions (follow-up
    # questions / summaries) so slow calls don't block other users.
    app.queue(default_concurrency_limit=CHAT_CONCURRENCY_LIMIT, max_size=QUEUE_MAX_SIZE)

    server_name = os.getenv("GRADIO_SERVER_NAME", "127.0.0.1")
    root_path = os.getenv("GRADIO_ROOT_PATH", "")

    if RATE_LIMIT_ENABLED:
        from fastapi.responses import JSONResponse
        from starlette.middleware.base import BaseHTTPMiddleware
        from gradio.routes import App

        class _RateLimitMiddleware(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                # Only guard the endpoints that hit the LLM; static assets and
                # the initial page load must stay fast for everyone.
                path = request.url.path
                guarded = (
                    request.method in {"POST", "PUT"}
                    and ("/call" in path or "/queue/" in path or "/predict" in path)
                )
                if guarded and not rate_limiter.allow(client_key_from_request(request)):
                    return JSONResponse(
                        {"error": "Rate limit exceeded. Please slow down."},
                        status_code=429,
                    )
                return await call_next(request)

        # Gradio builds its FastAPI app inside launch(); inject our middleware
        # right after the app object is created.
        original_create_app = App.create_app.__func__

        @classmethod
        def _create_app_with_rate_limiting(cls, *args, **kwargs):
            fastapi_app = original_create_app(cls, *args, **kwargs)
            try:
                fastapi_app.add_middleware(_RateLimitMiddleware)
                logger.info(
                    "Edge rate limiting active: %s requests/min per client",
                    RATE_LIMIT_PER_MINUTE,
                )
            except Exception:
                logger.exception("Failed to add rate limiting middleware")
            return fastapi_app

        App.create_app = _create_app_with_rate_limiting

    app.launch(
        server_name=server_name,
        server_port=int(os.getenv("GRADIO_SERVER_PORT", 7860)),
        root_path=root_path or None,
        max_threads=_env_int("GRADIO_MAX_THREADS", 64),
        show_api=API_ENABLE,
        quiet=True,
        # favicon_path="assets/favicon.ico"
    )
