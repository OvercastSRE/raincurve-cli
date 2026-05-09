# Raincurve

**One command to run any codebase locally.**

Raincurve is a CLI that turns any project into a fully running local environment — automatically. Point it at a codebase and AI agents will analyze the stack, generate Dockerfiles, spin up databases and caches, seed realistic data, and hand you a production-grade sandbox. No manual Docker setup, no hunting for environment configs, no "works on my machine."

## Install

```bash
curl -fsSL https://raincurve.com/install.sh | sh
```

Windows:
```powershell
irm https://raincurve.com/install.ps1 | iex
```

Or with pip:
```bash
pip install raincurve
```

## Quick Start

```bash
# Configure your LLM provider (Claude or OpenAI)
raincurve init

# Spin up a sandbox from any project directory
cd your-project/
raincurve sandbox
```

That's it. Raincurve scans your code, builds containers, starts services, runs migrations, and verifies everything is healthy.

## What It Does

- **Analyzes your codebase** — detects frameworks, databases, caches, and external dependencies
- **Builds Docker containers** — generates Dockerfiles if none exist, optimized for your stack
- **Starts auxiliary services** — PostgreSQL, Redis, MongoDB, Elasticsearch, and more
- **Stubs external APIs** — Stripe, SendGrid, Twilio, AWS S3 run as local mocks so you don't need real API keys
- **Seeds realistic data** — hits your API endpoints to populate the app with fake but plausible data
- **Watches for changes** — file edits trigger automatic rebuilds
- **Chat interface** — interact with your running sandbox via shell commands or a live browser

## More Commands

```bash
raincurve doctor     # check Docker, disk, RAM, and auth readiness
raincurve chat       # interactive chat with your running sandbox
raincurve down       # snapshot and tear down
raincurve up         # restore from last snapshot
```

## Requirements

- Python 3.11+
- Docker
- An API key for Claude or OpenAI

## License

Apache-2.0
