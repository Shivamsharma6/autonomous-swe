# Autonomous Software Engineering Platform (AutoSWE)

An autonomous multi-agent software development lifecycle (SDLC) platform powered by AI agents, sandbox isolation, risk policies, and LangSmith tracing.

## Features

- **Multi-Agent SDLC Swarm**: 7 specialized agents (`Architect`, `Researcher`, `Coder`, `Tester`, `Reviewer`, `Debugger`, `Final Reviewer`) executing tasks autonomously.
- **Model Agnostic & Local LLM Support**: Connects to Unsloth Studio (`http://localhost:8888/v1`), Ollama (`http://localhost:11434/v1`), OpenAI, Anthropic, or Gemini.
- **LangSmith Tracing Stack**: Deep observability tracking prompts, completions, tokens, latency, and USD cost per step.
- **Single-Page Glassmorphism Control Panel**: Live WebSocket task streaming, task DAG graph viewer, live code diffs, and execution metrics.
- **Dockerized One-Click Deployment**: Containerized with Docker Compose for instant execution.

## Quickstart with Docker

```bash
# Clone the repository
git clone https://github.com/shivamsharma/autonomous-swe.git
cd autonomous-swe

# Start all platform services with Docker Compose
docker compose up -d --build
```

### Accessing Services

- 🖥️ **Web Dashboard Control Panel**: [http://localhost:3000](http://localhost:3000)
- ⚙️ **FastAPI Control Plane API**: [http://localhost:8000](http://localhost:8000)
- 🎮 **Video Game Database Demo App**: [http://localhost:5001](http://localhost:5001)

## Running Without Docker

```bash
# 1. Install dependencies
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

# 2. Start Control Plane Backend
uvicorn autoswe.control_plane:app --host 127.0.0.1 --port 8000

# 3. Start Frontend UI
python3 -m http.server 3000 --directory frontend/
```
