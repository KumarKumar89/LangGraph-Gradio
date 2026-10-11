A template for chatbot streaming from langgraph with gradio

The template features history for the LLM, a couple tool calls, streaming output, tabs, persistence, and follow-up question buttons.

![image](img1.png)

# Install

    pip install uv

Add your Groq API key to `.env` as `GROQ_API_KEY`.
The default model is `openai/gpt-oss-20b`; optionally set `GROQ_MODEL` to another model enabled for your Groq account.
DuckDuckGo web search is available without an API key.
Set `GRADIO_BROWSER_STORAGE_SECRET` in `.env` to a stable random value to preserve browser state across server restarts.

# Run

    uv run app.py
