# ServerPulse — Setup Guide

## Correct Project Structure

server-monitor/
├── app.py              ← Flask backend (main app)
├── requirements.txt    ← Python dependencies
├── templates/
│   └── index.html      ← Dashboard HTML (must be here, not root)
├── data/
│   └── servers.json    ← Auto-created when you add first server
└── .env                ← Your API key (create this manually)
