# K2 Press Setup

## Windows PowerShell

For the normal first-time setup, run:

```bat
setup.bat
```

It creates `.venv`, installs Python dependencies, installs Playwright Chromium, creates a
placeholder `.env` if needed, and can build the Docker image if Docker Compose is available.

Create the virtual environment:

```powershell
python -m venv .venv
```

If PowerShell blocks activation with `running scripts is disabled on this system`, use the venv Python directly:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe feeds.py
```

Or allow activation for the current terminal session only:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python feeds.py
```

## Later Phase Dependencies

Install Chromium for Playwright rendering:

```powershell
playwright install chromium
```

Pull the local Ollama model:

```powershell
ollama pull qwen3:8b
```

Create `.env` before using Pexels image fetching:

```env
PEXELS_API_KEY=your_pexels_key_here
```

## Docker

Build and run the FastAPI app with:

```powershell
docker compose up --build
```

Then open:

```text
http://localhost:8000
```

The container uses `host.docker.internal:11434` for Ollama by default, so keep Ollama running
on Windows if you use local LLM planning:

```powershell
ollama serve
ollama pull qwen3:8b
```

Generated output, image cache, and saved library data are bind-mounted to `outputs/`,
`image_cache/`, and `library/`.

## Tailscale access

Create a Tailscale pre-auth key in the Tailscale admin console, then add it to
`.env.tailscale`:

```env
TS_AUTHKEY=<paste-your-tailscale-pre-auth-key-here>
```

Start the app and its Tailscale sidecar:

```powershell
docker compose --profile tailscale up -d --build
```

The sidecar keeps its node identity in `tailscale/state/` and publishes the FastAPI app
with Tailscale Serve over HTTPS. Check the private tailnet URL with:

```powershell
docker exec k2-posttool-tailscale tailscale serve status
```
