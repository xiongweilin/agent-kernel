# Deployment: portable-local

Runs anywhere without Codex, Docker, or Prometheus.

```powershell
uv sync
.venv\Scripts\python.exe -m agent_kernel --state data/agent-kernel.db status
.venv\Scripts\python.exe -m agent_kernel --state data/agent-kernel.db work submit --title "Echo test" --description "hello" --kind generic-task
.venv\Scripts\python.exe -m agent_kernel --state data/agent-kernel.db work list
.venv\Scripts\python.exe -m agent_kernel plugin validate examples/echo-provider
.venv\Scripts\python.exe -m agent_kernel plugin test examples/echo-provider
```

Runtime factory:

```python
from pathlib import Path
from agent_kernel.deployment.local import create_local_runtime

runtime = create_local_runtime(Path("data/agent-kernel.db"), Path("data/artifacts"))
```

Docker (Core not dependent on Docker):

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
RUN pip install fastapi uvicorn pydantic httpx prometheus-client
COPY src ./src
CMD ["python", "-m", "agent_kernel", "--state", "/data/agent-kernel.db", "status"]
```
