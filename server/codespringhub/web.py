"""The CodeSpringHub portal: a JSON API under /api plus the built React app at /.

All endpoints take the user from identity.py and call the same service layer as the
MCP server (hub.py for reading, editing.py for changes), so permissions are identical.

Phase 2 note: the HTTPS MCP endpoint for Claude Enterprise will be mounted on this same
app (at /mcp), protected by the same SSO token check.
"""

import argparse
import atexit
import json
import os
import socket
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from mcp import Client
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel

from . import editing, hub, knowledge, pipelines
from .errors import HubError
from .identity import get_auth, redeem_download
from .permissions import acting_as, display_name, is_bsr, require_lab, require_visible_lab
from .server import mcp

DIST = Path(__file__).resolve().parents[2] / "portal" / "dist"


def create_app(auth=None) -> FastAPI:
    auth = auth or get_auth()
    app = FastAPI(title="CodeSpringHub portal", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.exception_handler(ToolError)
    async def tool_error(_, exc: ToolError):
        status = exc.status if isinstance(exc, HubError) else 400
        return JSONResponse({"detail": str(exc)}, status_code=status)

    def current(request: Request) -> str:
        header = request.headers.get("authorization", "")
        token = header[7:] if header.lower().startswith("bearer ") else None
        user = auth.user_for(token)
        if user is None:
            raise _Unauthorized()
        return user

    @app.exception_handler(_Unauthorized)
    async def unauthorized(_, __):
        return JSONResponse(
            {"detail": "This portal link is missing or out of date. Ask Claude to open CodeSpringHub for a new "
                       "link, or open the link printed when the portal started."},
            status_code=401,
        )

    U = Depends(current)

    class RedeemIn(BaseModel):
        ticket: str

    @app.post("/api/redeem")
    def redeem(body: RedeemIn):
        """A link from Claude's open_portal carries a one-time ticket, swapped here for the portal
        secret, which only the browser keeps. No login needed: the ticket is the proof."""
        swap = getattr(auth, "redeem", None)
        token = swap(body.ticket) if swap else None
        if not token:
            return JSONResponse({"detail": "This link has expired or was already used. Ask Claude to open "
                                           "CodeSpringHub again for a new one."}, status_code=401)
        return {"token": token}

    # ---- who am I / labs ----------------------------------------------------

    @app.get("/api/me")
    def me(user: str = U):
        return {"user": user, "name": display_name(user), "is_bsr": is_bsr(user), "auth": auth.name,
                "categories": editing.CATEGORIES, "assays": editing.ASSAYS}

    @app.get("/api/connect")
    def connect(user: str = U, for_user: str | None = None):
        return editing.connect_info(user, for_user)

    @app.get("/api/labs")
    def labs(user: str = U):
        return hub.list_labs(user)

    @app.get("/api/labs/{lab_id}")
    def lab_detail(lab_id: str, user: str = U):
        lab = require_visible_lab(user, knowledge.load_labs(), lab_id)
        info = next(l for l in hub.list_labs(user) if l["lab_id"] == lab_id)
        return info | {"unix_group": lab.unix_group, "pis": lab.pis,
                       "documents_list": hub.document_index(user, lab)}

    class LabIn(BaseModel):
        name: str
        description: str = ""
        projects: list[dict] = []

    @app.put("/api/labs/{lab_id}")
    def lab_update(lab_id: str, body: LabIn, user: str = U):
        editing.update_lab(user, lab_id, body.name, body.description, body.projects)
        return {"ok": True}

    @app.get("/api/labs/{lab_id}/history")
    def lab_history(lab_id: str, path: str | None = None, user: str = U):
        return editing.history(user, lab_id, path)

    # ---- documents ----------------------------------------------------------

    class DocIn(BaseModel):
        text: str

    class NewDoc(BaseModel):
        category: str
        title: str
        scope: str = "lab"

    @app.get("/api/labs/{lab_id}/docs/{rel_path:path}")
    def doc_get(lab_id: str, rel_path: str, user: str = U):
        return editing.read_document_file(user, lab_id, rel_path)

    @app.put("/api/labs/{lab_id}/docs/{rel_path:path}")
    def doc_put(lab_id: str, rel_path: str, body: DocIn, user: str = U):
        editing.save_document(user, lab_id, rel_path, body.text)
        return {"ok": True}

    @app.post("/api/labs/{lab_id}/docs")
    def doc_new(lab_id: str, body: NewDoc, user: str = U):
        return {"rel_path": editing.create_document(user, lab_id, body.category, body.title, body.scope)}

    @app.delete("/api/labs/{lab_id}/docs/{rel_path:path}")
    def doc_delete(lab_id: str, rel_path: str, user: str = U):
        editing.delete_document(user, lab_id, rel_path)
        return {"ok": True}

    # ---- datasets -----------------------------------------------------------

    @app.get("/api/labs/{lab_id}/datasets")
    def datasets(lab_id: str, user: str = U):
        # People see folder paths (they registered them); Claude never does.
        return editing.datasets_for_portal(user, lab_id)

    class DatasetIn(BaseModel):
        title: str
        path: str
        kind: str = "data"
        scope: str = "lab"
        project: str | None = None
        assay: str = ""
        description: str = ""
        sensitivity: str = "standard"

    @app.post("/api/labs/{lab_id}/datasets")
    def dataset_new(lab_id: str, body: DatasetIn, user: str = U):
        return {"id": editing.save_dataset(user, lab_id, body.model_dump())}

    # Hub IDs contain "/" and "~", so existing items are addressed with ?id=<hub id>.
    @app.put("/api/labs/{lab_id}/dataset")
    def dataset_put(lab_id: str, id: str, body: DatasetIn, user: str = U):
        return {"id": editing.save_dataset(user, lab_id, body.model_dump(), original_id=id)}

    @app.delete("/api/labs/{lab_id}/dataset")
    def dataset_delete(lab_id: str, id: str, user: str = U):
        editing.delete_dataset(user, lab_id, id)
        return {"ok": True}

    @app.get("/api/browse")
    def browse(path: str | None = None, user: str = U):
        return editing.browse(user, path)

    @app.get("/api/check-path")
    def check_path(path: str, user: str = U):
        return editing.check_dataset_path(user, path)

    # ---- workflows ----------------------------------------------------------

    class WorkflowIn(BaseModel):
        id: str | None = None
        name: str
        assays: list[str]
        status: str
        version: str = ""
        url: str = ""
        summary: str = ""

    # ---- which CodeSpringApp pipelines labs may use ---------------------------------

    @app.get("/api/pipelines")
    def pipeline_access(user: str = U):
        from . import runs
        try:
            catalog = runs.catalog()["pipelines"]
        except Exception:  # noqa: BLE001  (the shared install may be down; the settings still show)
            catalog = None
        return pipelines.for_portal(user, catalog)

    class PipelineAccessIn(BaseModel):
        access: str
        labs: list[str] = []

    @app.put("/api/pipelines/{pipeline_id}")
    def pipeline_access_put(pipeline_id: str, body: PipelineAccessIn, user: str = U):
        pipelines.save(user, pipeline_id, body.access, body.labs)
        return {"ok": True}

    @app.get("/api/workflows")
    def workflows(user: str = U):
        return hub.list_workflows()

    @app.post("/api/workflows")
    def workflow_new(body: WorkflowIn, user: str = U):
        return {"id": editing.save_workflow(user, body.model_dump())}

    @app.put("/api/workflows/{wf_id}")
    def workflow_put(wf_id: str, body: WorkflowIn, user: str = U):
        return {"id": editing.save_workflow(user, body.model_dump(), original_id=wf_id)}

    @app.delete("/api/workflows/{wf_id}")
    def workflow_delete(wf_id: str, user: str = U):
        editing.delete_workflow(user, wf_id)
        return {"ok": True}

    # ---- inbox and activity -------------------------------------------------

    class StatusIn(BaseModel):
        status: str

    @app.get("/api/inbox")
    def inbox(user: str = U):
        return editing.inbox(user)

    @app.post("/api/suggestions/{sid}")
    def suggestion_status(sid: str, body: StatusIn, user: str = U):
        editing.set_suggestion_status(user, sid, body.status)
        return {"ok": True}

    @app.post("/api/reviews/{key}")
    def review_status(key: str, body: StatusIn, user: str = U):
        editing.set_review_status(user, key, body.status)
        return {"ok": True}

    @app.get("/api/activity")
    def activity(user: str = U):
        return editing.activity(user)

    # ---- "Try it": call MCP tools exactly as Claude would --------------------

    @app.get("/api/tools")
    async def tools(user: str = U):
        with acting_as(user):
            async with Client(mcp) as c:
                listed = await c.list_tools()
        return [{"name": t.name, "description": t.description, "schema": t.input_schema} for t in listed.tools]

    class ToolCall(BaseModel):
        arguments: dict = {}

    @app.post("/api/tools/{name}")
    async def call_tool(name: str, body: ToolCall, user: str = U):
        with acting_as(user):
            async with Client(mcp) as c:
                result = await c.call_tool(name, body.arguments)
        texts = [c.text for c in result.content if getattr(c, "text", None) is not None]
        return {"is_error": result.is_error, "content": texts,
                "characters": sum(len(t) for t in texts)}

    @app.get("/api/download/{ticket}")
    def download(ticket: str):
        """A one-time link from Claude (analysis_report) for one file: no login, the ticket is the proof.
        Only report packets are ever served this way."""
        target = redeem_download(ticket)
        if target is None or not target.name.endswith("_codespring_report.html"):
            return JSONResponse({"detail": "This download link has expired or was already used. Ask Claude for "
                                           "the report again to get a new one."}, status_code=404)
        return FileResponse(target, media_type="text/html", filename=target.name)

    @app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    def api_not_found(rest: str):
        return JSONResponse({"detail": "Unknown portal address."}, status_code=404)

    # ---- the React app ------------------------------------------------------

    if DIST.exists():
        app.mount("/", StaticFiles(directory=DIST, html=True), name="portal")
    else:
        @app.get("/")
        def not_built():
            return JSONResponse({"detail": "Portal frontend not built. Run ./portal (it builds automatically)."})

    return app


class _Unauthorized(Exception):
    pass


def _free_port(preferred: int) -> int:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]


def main():
    import uvicorn

    parser = argparse.ArgumentParser(description="CodeSpringHub portal")
    parser.add_argument("--port", type=int, default=None, help="default: your own fixed port")
    parser.add_argument("--new-link", action="store_true", help="invalidate the old portal link and make a new one")
    args = parser.parse_args()

    auth = get_auth(new_link=args.new_link)
    user = auth.user
    wanted = args.port or editing.portal_port(user)
    port = _free_port(wanted)
    host = editing._laptop_hostname()
    link = f"http://localhost:{port}/#token={auth.token}"
    moved = "" if port == wanted else (
        f"\n  Note: port {wanted} was busy (do you already have a portal open?), so this one uses {port}.\n"
        f"  The one-line command won't reach it; use step 1 below.\n")
    print(f"""
  CodeSpringHub portal is running on {host} (as {user}).
{moved}
  Started with the one-line command from the Connect page? Just open the link in step 2.

  1. Otherwise, on your laptop open a terminal and run (leave it open):
       ssh -N -L {port}:localhost:{port} {user}@{host}

  2. Open this private link in your browser (don't share it):
       {link}

  Press Ctrl+C here to stop the portal.
""", flush=True)
    _record_running(port)
    uvicorn.run(create_app(auth), host="127.0.0.1", port=port, log_level="warning")


def _record_running(port: int):
    """Let Claude's open_portal find this portal (per login node: home folders are shared)."""
    from .portal_launch import state_file
    path = state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": os.getpid(), "port": port}))
    os.chmod(path, 0o600)
    atexit.register(lambda: path.unlink(missing_ok=True))


if __name__ == "__main__":
    main()
