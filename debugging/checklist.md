# Debugging Checklist

## Before merge or deployment

- [ ] Python syntax passes
- [ ] App imports successfully
- [ ] Required env vars are configured
- [ ] Azure secrets are present
- [ ] CI workflow YAML parses
- [ ] Docker image builds successfully
- [ ] Container App responds on the public URL
- [ ] Groq API key is valid
- [ ] Search tools and web downloads are functioning

## If the app fails in production

1. Check the Azure Container App logs.
2. Confirm environment variables are set correctly.
3. Re-run the local smoke import test.
4. Validate the Groq model quota and network access.
5. Review recent GitHub Actions logs.
6. Capture the exact error and log it in the incident template.
