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

The CI workflow checks code quality, Python syntax, dependency installation, and smoke-import validation before merging changes. The CD workflow builds a container image and can deploy it to Azure Container Apps when the required cloud secrets are configured.

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
