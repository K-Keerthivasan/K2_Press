# K2 Press Setup

## Windows PowerShell

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
