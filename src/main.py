from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.ask import ask, ingest_ticket
from app.graph import connect
from app.seed import reset_and_seed

app = FastAPI(title="Company Brain")
STATIC = Path(__file__).parent / "static"


class AskBody(BaseModel):
    question: str


class TicketBody(BaseModel):
    title: str
    about: str = ""


@app.on_event("startup")
def seed_if_empty() -> None:
    _, graph = connect()
    count = graph.query("MATCH (n) RETURN count(n) AS nodes").result_set
    nodes = count[0][0] if count else 0
    if nodes == 0:
        reset_and_seed()


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.post("/api/ask")
def ask_route(body: AskBody):
    return ask(body.question)


@app.post("/api/tickets")
def ticket_route(body: TicketBody):
    created = ingest_ticket(body.title, body.about)
    follow = ask(f"who should look at {created['about_name']}")
    return {
        "ticket": created["id"],
        "about": created["about_name"],
        "answer": f"Added {created['id']} about {created['about_name']}. {follow['answer']}",
        "path": created["path"] + follow["path"],
    }


@app.get("/api/health")
def health():
    _, graph = connect()
    nodes = graph.query("MATCH (n) RETURN count(n)").result_set[0][0]
    return {"ok": True, "nodes": nodes}
