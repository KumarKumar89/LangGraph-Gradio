#!/usr/bin/env bash
set -e

cd '/workspaces/LangGraph-Gradio/Lang Graph Gradio'

export GROQ_API_KEY=test-key
export GROQ_MODEL=openai/gpt-oss-20b
export GRADIO_SERVER_PORT=7860

uv sync --frozen >/dev/null 2>&1 || true
uv run python -m py_compile app.py graph.py
uv run python - <<'PY'
import os
os.environ['GROQ_API_KEY'] = 'test-key'
import app
print('IMPORT_OK', hasattr(app, 'chat_fn'))
PY

echo 'Smoke checks passed.'
