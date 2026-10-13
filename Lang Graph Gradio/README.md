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

# CI/CD and deployment

This project includes a lightweight GitHub Actions pipeline for continuous integration and continuous delivery.

## Why CI/CD matters for this chatbot

A chatbot built with LangGraph and Gradio is a real product feature, not just a notebook. In a real-world scenario, such as a customer support copilot or internal knowledge assistant, the app must be:

- validated before every code change
- deployed consistently across environments
- protected from secret leakage
- easy to roll back if a model or UI change breaks the user flow

The CI workflow checks code quality, Python syntax, dependency installation, and smoke-import validation before merging changes. It also **builds the production Docker image and runs it end-to-end**, verifying `/healthz`, the UI (`/`), and `/config` respond correctly — so nothing broken ever reaches the cloud.

The CD workflow builds a container image, pushes it to GitHub Container Registry (free for public repos), and deploys it to Azure Container Apps when the required cloud secrets are configured. Deployments are serialized (concurrency group), auto-scaled (**2–10 replicas**, 1 CPU / 2 GiB each → capacity for thousands of concurrent users), validated with liveness/readiness probes on `/healthz`, smoke-tested against the live public URL after rollout, and **automatically rolled back** to the last healthy revision if any check fails.

## Scaling for thousands of users

The deployed app is tuned through environment variables (set automatically by the CD workflow):

| Variable | Default | Purpose |
|---|---|---|
| `CHAT_CONCURRENCY_LIMIT` | 32 | Simultaneous in-flight chat requests per replica |
| `QUEUE_MAX_SIZE` | 1000 | Pending requests queued per replica before rejecting |
| `GRADIO_MAX_THREADS` | 64 | ASGI worker threads per replica |
| `RATE_LIMIT_ENABLED` / `RATE_LIMIT_PER_MINUTE` | true / 150 | Per-client sliding-window rate limiting (HTTP 429) |
| `GRADIO_SERVER_NAME` | 0.0.0.0 (container) | Correct binding inside cloud containers |

With 10 replicas × 32 concurrent chats, the service handles ~320 simultaneous conversations plus queueing bursts, while rate limiting protects the Groq API quota from abuse.

## Real-world problem scenario

Consider a support team that needs a chatbot to:

- answer product questions using company docs
- perform web research for emerging issues
- summarize previous conversations for agents
- route follow-up questions without losing context

This kind of assistant can fail in production if model version changes, environment variables are missing, or the app cannot start after a deployment. CI/CD prevents those issues by automating validation and secure deployment.

## Benefits of this pipeline

- faster release cycles with automated validation
- safer deployments through container packaging
- easy rollback and consistent artifact tracking with image tags
- better collaboration across engineering and operations teams
- repeatable production readiness checks for LangGraph and Gradio apps

## Example deployment applications

- customer support assistant for SaaS products
- internal knowledge bot for employees
- web research assistant for analysts and operations teams
- onboarding helper connected to documentation and FAQs

## Required deployment secrets

Set the following GitHub repository secrets before enabling automated Azure deployment:

- `AZURE_CREDENTIALS`
- `AZURE_RESOURCE_GROUP`
- `AZURE_CONTAINER_APP_NAME`
- `AZURE_CONTAINERAPP_ENVIRONMENT`
- `GROQ_API_KEY`
- `GROQ_MODEL` (optional)
- `GRADIO_BROWSER_STORAGE_SECRET`

A sample environment file is provided in `.env.example`.

## Azure Container Apps quick start

If the Azure Container App environment does not yet exist, create it with the Azure CLI first:

    az login
    az group create --name rg-langgraph-gradio --location eastus
    az extension add --name containerapp --upgrade
    az provider register --namespace Microsoft.App
    az provider register --namespace Microsoft.OperationalInsights
    az containerapp env create \
      --name caenv-langgraph-gradio \
      --resource-group rg-langgraph-gradio \
      --location eastus

Then add the required GitHub secrets and trigger the `CD` workflow in GitHub Actions. The workflow will create or update the Azure Container App automatically and print the public URL.
