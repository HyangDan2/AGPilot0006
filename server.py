#!/usr/bin/env python3
import uvicorn
from app.config import settings

if __name__ == "__main__":
    print(f"Starting {settings.PROJECT_NAME} on http://127.0.0.1:8006 ...")
    uvicorn.run("app.main:app", host="127.0.0.1", port=8006, reload=True)
