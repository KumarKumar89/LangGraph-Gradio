# Debugging Workspace

This folder is a lightweight debugging and operational runbook for the LangGraph + Gradio chatbot.

## Purpose

Use this workspace to:

- document known issues and fixes
- record Azure deployment problems
- track model or API failures
- run smoke checks before production releases

## Common checks

1. Verify the Python app imports successfully.
2. Validate GitHub Actions YAML syntax.
3. Ensure required environment variables are set.
4. Confirm the Azure Container App is reachable.
5. Check whether Groq or external web search endpoints are healthy.

## Useful commands

```bash
cd '/workspaces/LangGraph-Gradio/Lang Graph Gradio'
GROQ_API_KEY=test-key uv run python -m py_compile app.py graph.py
GROQ_API_KEY=test-key uv run python - <<'PY'
import os
os.environ['GROQ_API_KEY'] = 'test-key'
import app
print('IMPORT_OK', hasattr(app, 'chat_fn'))
PY
```

## Issue log

Keep a short note in [incident-template.md](incident-template.md) for every future issue.
