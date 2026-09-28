"""
title: Document Intelligence
description: Answers questions about an attached document with exact calculations, using the local pipeline service.

Paste this whole file into Open WebUI: Admin Panel -> Functions -> New
Function. Only this one file is needed there. The pipeline service
(pipeline_api.py) runs separately, see SETUP.md.

How it works, and why it is written this way:

- Open WebUI removes `files` and `chat_id` from `body` before a Pipe runs
  and passes them separately, as `__files__` and `__chat_id__`. That is
  what this Pipe reads.
- It is an `async def` that returns the finished answer as one string. A
  sync generator would be iterated directly on Open WebUI's event loop,
  so its blocking network calls would freeze Open WebUI for every user
  while one document is being read. Here, the blocking calls run in a
  worker thread (asyncio.to_thread). Returning a plain string also avoids
  the async-generator completion problem (open-webui#20196).
- While it waits, it shows a status line above the answer that updates
  every few seconds, so a long scan never looks frozen.
- Open WebUI's own background jobs (chat title, tags, search queries)
  also arrive here; they get an instant empty reply instead of a
  calculation.
- It never uses Open WebUI's or Ollama's native tool-calling. The model
  picked in the chat is not what answers; the pipeline service calls its
  own code model directly.
- A file already read is never read again: the service remembers each
  Open WebUI file id, and remembers which document each chat is about,
  so follow-up questions without a new attachment still work, even after
  a restart.
"""

import asyncio
import glob
import os
import re
import time

import requests
from pydantic import BaseModel, Field

_USER_QUERY = re.compile(r"<user_query>\s*(.*?)\s*</user_query>", re.DOTALL)
_ATTACHED_FILES = re.compile(r"<attached_files>.*?</attached_files>\s*", re.DOTALL)

TEXT = {
    "de": {
        "no_doc": "Bitte laden Sie zuerst ein Dokument hoch (Büroklammer-Symbol im Eingabefeld) und stellen Sie dann Ihre Frage.",
        "no_question": "Bitte stellen Sie eine Frage zu Ihrem Dokument.",
        "reading": "Dokument wird eingelesen: {name}",
        "thinking": "Antwort wird berechnet",
        "still": "{step} (läuft seit {minutes} Min.)",
        "unreadable": "Die Datei {name} konnte nicht geöffnet werden. Bitte laden Sie sie erneut hoch.",
        "service_down": "Der Dokumenten-Dienst ist gerade nicht erreichbar. Bitte informieren Sie den Administrator (Hinweis für den Administrator: SETUP.md, Abschnitt 10).",
        "failed": "Das hat leider nicht geklappt: {detail}",
    },
    "en": {
        "no_doc": "Please attach a document first (paperclip icon in the message box), then ask your question.",
        "no_question": "Please ask a question about your document.",
        "reading": "Reading document: {name}",
        "thinking": "Calculating the answer",
        "still": "{step} (running for {minutes} min)",
        "unreadable": "The file {name} could not be opened. Please upload it again.",
        "service_down": "The document service is not reachable right now. Please tell the administrator (note for the administrator: SETUP.md, section 10).",
        "failed": "Sorry, that did not work: {detail}",
    },
}


class _NoDocument(Exception):
    pass


def question_from(body: dict) -> str:
    """The user's actual question, even if Open WebUI wrapped it.

    With "File Context" on, Open WebUI puts its retrieval template and
    excerpts of the file in front of the question, ending in </context>
    (older versions: inside <user_query> tags).
    """
    messages = body.get("messages") or []
    last_user = next((m for m in reversed(messages) if m.get("role") == "user"), None)
    content = (last_user or {}).get("content", "")
    if isinstance(content, list):
        content = " ".join(
            part.get("text", "") for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    content = _ATTACHED_FILES.sub("", str(content))
    match = _USER_QUERY.search(content)
    if match:
        content = match.group(1)
    elif "</context>" in content:
        content = content.rsplit("</context>", 1)[1]
    return content.strip()


# Open WebUI also sends its own background jobs (chat title, tags, search
# queries, follow-up suggestions) to whichever model the chat uses, this
# Pipe included. Those must never run a calculation.
_TASK_REPLIES = {
    "query_generation": '{"queries": []}',
    "tags_generation": '{"tags": []}',
    "follow_up_generation": '{"follow_ups": []}',
}
_TASK_TITLES = {"de": "Dokumentenfrage", "en": "Document question"}


def file_ref(entry: dict) -> dict | None:
    """id, file name and disk path of one entry in `__files__`."""
    if entry.get("type") not in (None, "file"):
        return None
    info = entry.get("file") if isinstance(entry.get("file"), dict) else {}
    file_id = info.get("id") or entry.get("id")
    name = info.get("filename") or entry.get("name") or (info.get("meta") or {}).get("name")
    if not file_id or not name:
        return None
    return {"id": file_id, "name": name, "path": info.get("path") or entry.get("path")}


def read_file(ref: dict) -> bytes | None:
    path = ref.get("path")
    if path and os.path.isfile(path):
        with open(path, "rb") as handle:
            return handle.read()

    directories = [
        os.environ.get("UPLOAD_DIR", ""),
        os.path.join(os.environ.get("DATA_DIR", ""), "uploads") if os.environ.get("DATA_DIR") else "",
        "/app/backend/data/uploads",
    ]
    try:
        import open_webui

        directories.append(os.path.join(os.path.dirname(open_webui.__file__), "data", "uploads"))
    except ImportError:
        pass

    for directory in directories:
        if directory and os.path.isdir(directory):
            matches = glob.glob(os.path.join(directory, f"*{ref['id']}*"))
            if matches:
                with open(matches[0], "rb") as handle:
                    return handle.read()
    return None


def _detail(error: requests.RequestException) -> str:
    response = getattr(error, "response", None)
    if response is not None:
        try:
            return str(response.json().get("detail", response.text))
        except ValueError:
            return response.text[:300]
    return str(error)


class Pipe:
    class Valves(BaseModel):
        API_BASE: str = Field(
            default="http://localhost:8080",
            description="Address of the pipeline service. If Open WebUI runs in Docker: http://host.docker.internal:8080",
        )
        LANGUAGE: str = Field(default="de", description="Language of the status messages: de or en")
        INGEST_TIMEOUT_SECONDS: int = Field(default=3600, description="Longest wait for reading one document")
        QUERY_TIMEOUT_SECONDS: int = Field(default=1800, description="Longest wait for one answer")
        STATUS_EVERY_SECONDS: int = Field(default=15, description="How often the status line updates")

    def __init__(self):
        self.valves = self.Valves()

    async def pipe(
        self,
        body: dict,
        __user__: dict = None,
        __files__: list = None,
        __chat_id__: str = None,
        __event_emitter__=None,
        __task__: str = None,
    ) -> str:
        if __task__:
            if str(__task__) == "title_generation":
                title = _TASK_TITLES.get(self.valves.LANGUAGE, _TASK_TITLES["de"])
                return '{"title": "%s"}' % title
            return _TASK_REPLIES.get(str(__task__), "")

        text = TEXT.get(self.valves.LANGUAGE, TEXT["de"])
        question = question_from(body)
        if not question:
            return text["no_question"]

        doc_id = None
        try:
            for entry in __files__ or []:
                ref = file_ref(entry)
                if ref is None:
                    continue
                step = text["reading"].format(name=ref["name"])
                await self._status(__event_emitter__, step)
                data = await asyncio.to_thread(read_file, ref)
                if data is None:
                    return text["unreadable"].format(name=ref["name"])
                result = await self._wait(__event_emitter__, text, step, self._ingest, ref, data, __chat_id__)
                doc_id = result["doc_id"]

            step = text["thinking"]
            await self._status(__event_emitter__, step)
            return await self._wait(__event_emitter__, text, step, self._query, question, doc_id, __chat_id__)
        except _NoDocument:
            return text["no_doc"]
        except requests.ConnectionError:
            return text["service_down"]
        except requests.RequestException as error:
            return text["failed"].format(detail=_detail(error))
        finally:
            await self._status(__event_emitter__, "", done=True)

    async def _wait(self, emitter, text, step, fn, *args):
        """Run a blocking call in a worker thread; refresh the status line
        while it runs."""
        task = asyncio.ensure_future(asyncio.to_thread(fn, *args))
        started = time.monotonic()
        while True:
            done, _ = await asyncio.wait({task}, timeout=self.valves.STATUS_EVERY_SECONDS)
            if done:
                return task.result()
            minutes = int((time.monotonic() - started) // 60)
            await self._status(emitter, text["still"].format(step=step, minutes=minutes))

    @staticmethod
    async def _status(emitter, description: str, done: bool = False):
        if emitter is None:
            return
        await emitter({"type": "status", "data": {"description": description, "done": done, "hidden": done}})

    def _ingest(self, ref: dict, data: bytes, chat_id: str | None) -> dict:
        response = requests.post(
            f"{self.valves.API_BASE}/ingest",
            files={"file": (ref["name"], data)},
            data={"source_id": ref["id"], "chat_id": chat_id or ""},
            timeout=self.valves.INGEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        return response.json()

    def _query(self, question: str, doc_id: str | None, chat_id: str | None) -> str:
        params = {"question": question}
        if doc_id:
            params["doc_id"] = doc_id
        if chat_id:
            params["chat_id"] = chat_id
        response = requests.post(
            f"{self.valves.API_BASE}/query",
            params=params,
            timeout=self.valves.QUERY_TIMEOUT_SECONDS,
        )
        if response.status_code == 404:
            raise _NoDocument()
        response.raise_for_status()
        return response.json()["answer"]
