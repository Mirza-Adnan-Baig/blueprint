"""
title: Document Intelligence
description: Uploads a document to the local pipeline API, asks questions against it.

Paste this whole file into Open WebUI: Admin Panel -> Functions -> New Function.
Only this one file is needed there -- the FastAPI service (pipeline_api.py)
runs separately (see README, "Running the service").

Deliberately does NOT use Open WebUI's native model tool-calling. The chat
model you pick in the UI is not what answers the question -- this Pipe
intercepts the message directly and calls the pipeline API's /query
endpoint, which does its own model calls internally (see pipeline_api.py).
That sidesteps tool-call parsing entirely; there is nothing for it to get
wrong. Uses a plain sync generator with a heartbeat, not an async
generator -- Open WebUI does not reliably signal completion on an async
generator during a long-running call (open-webui#20196).
"""

import json
import threading
import time

import requests
from pydantic import BaseModel, Field


class Pipe:
    class Valves(BaseModel):
        API_BASE: str = Field(default="http://localhost:8080", description="pipeline_api.py base URL")
        HEARTBEAT_SECONDS: int = Field(default=8, description="keep-alive interval during long calls")

    def __init__(self):
        self.valves = self.Valves()
        self._doc_ids: dict[str, str] = {}  # chat_id -> most recently ingested doc_id

    def pipe(self, body: dict, __user__: dict = None, __request__=None):
        chat_id = body.get("chat_id", "default")
        messages = body.get("messages", [])
        question = messages[-1]["content"] if messages else ""
        files = body.get("files") or []

        stop_heartbeat = threading.Event()
        heartbeat = threading.Thread(target=self._heartbeat, args=(stop_heartbeat,))
        heartbeat.start()

        try:
            for new_file in files:
                doc_id = self._ingest(new_file)
                if doc_id:
                    self._doc_ids[chat_id] = doc_id

            doc_id = self._doc_ids.get(chat_id)
            if not doc_id:
                yield "Please upload a document first."
                return

            answer = self._query(doc_id, question)
            yield answer
        except requests.RequestException as e:
            yield f"Pipeline service error: {e}"
        finally:
            stop_heartbeat.set()
            heartbeat.join()

    def _heartbeat(self, stop_event: threading.Event):
        # Open WebUI's connection can appear to hang with no output during
        # Ollama prefill on a large prompt; a periodic no-op keeps it alive.
        while not stop_event.wait(self.valves.HEARTBEAT_SECONDS):
            pass

    def _ingest(self, file_info: dict) -> str | None:
        file_path = file_info.get("file", {}).get("path") or file_info.get("path")
        if not file_path:
            return None

        with open(file_path, "rb") as f:
            resp = requests.post(
                f"{self.valves.API_BASE}/ingest",
                files={"file": (file_info.get("name", "upload"), f)},
                timeout=120,
            )
        resp.raise_for_status()
        return resp.json()["doc_id"]

    def _query(self, doc_id: str, question: str) -> str:
        resp = requests.post(
            f"{self.valves.API_BASE}/query",
            params={"doc_id": doc_id, "question": question},
            timeout=180,
        )
        resp.raise_for_status()
        return resp.json()["answer"]
