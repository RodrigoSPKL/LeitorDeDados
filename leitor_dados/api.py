"""API de consulta local: valores e credenciais nunca entram na URL."""

from contextlib import asynccontextmanager
import os
from pathlib import Path
import secrets
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictStr

from .query import CursorExpired, DatasetStore, DatasetUnavailable, QueryBusy, QueryTimeout, ResultTooLarge


MAX_REQUEST_BODY_BYTES = 65_536


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: Literal["cpf", "email", "phone", "code"]
    value: Annotated[StrictStr, Field(min_length=1, max_length=4096)]
    limit: Annotated[int, Field(strict=True, ge=1, le=100)] = 50
    cursor: Annotated[StrictStr, Field(max_length=4096)] | None = None


class SearchBodyLimit:
    """Limita também corpos enviados em fluxo, antes de interpretar JSON."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] != "http"
            or scope.get("method") != "POST"
            or scope.get("path") != "/v1/search"
        ):
            await self.app(scope, receive, send)
            return

        chunks = []
        size = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body = message.get("body", b"")
            size += len(body)
            if size > MAX_REQUEST_BODY_BYTES:
                await JSONResponse(
                    status_code=413, content={"detail": "Solicitação muito grande."}
                )(scope, receive, send)
                return
            chunks.append(body)
            if not message.get("more_body", False):
                break

        consumed = False

        async def replay():
            nonlocal consumed
            if not consumed:
                consumed = True
                return {"type": "http.request", "body": b"".join(chunks)}
            return await receive()

        await self.app(scope, replay, send)


def _secret(explicit: str | None, name: str, minimum: int) -> str:
    value = explicit
    if value is None:
        secret_file = os.environ.get(f"{name}_FILE")
        if secret_file:
            try:
                value = Path(secret_file).read_text(encoding="utf-8").strip()
            except (OSError, UnicodeError):
                raise RuntimeError(f"Não foi possível ler a configuração de {name}.") from None
        else:
            value = os.environ.get(name, "")
    if not isinstance(value, str) or len(value) < minimum or not value.strip():
        raise RuntimeError(f"Configure {name} com pelo menos {minimum} caracteres.")
    return value


def _query_failure(exc: Exception) -> HTTPException:
    if isinstance(exc, CursorExpired):
        return HTTPException(409, "A base mudou ou o cursor expirou. Inicie uma nova consulta.")
    if isinstance(exc, ResultTooLarge):
        return HTTPException(413, "Reduza a quantidade de registros por página.")
    if isinstance(exc, ValueError):
        return HTTPException(400, "Parâmetros de consulta inválidos.")
    if isinstance(exc, QueryTimeout):
        return HTTPException(503, "A consulta excedeu o tempo disponível. Tente novamente.")
    if isinstance(exc, QueryBusy):
        return HTTPException(503, "O serviço está ocupado. Tente novamente.")
    if isinstance(exc, DatasetUnavailable):
        return HTTPException(503, "Base de dados indisponível.")
    return HTTPException(503, "Consulta indisponível no momento.")


def create_app(
    data_dir: Path | None = None,
    api_key: str | None = None,
    cursor_secret: str | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.api_key = _secret(api_key, "API_KEY", 24)
        signing_key = _secret(cursor_secret, "CURSOR_SECRET", 32)
        root = Path(data_dir) if data_dir is not None else Path(os.environ.get("DATA_DIR", "/data"))
        app.state.store = DatasetStore(root=root, cursor_secret=signing_key)
        try:
            yield
        finally:
            app.state.api_key = ""
            app.state.store = None

    app = FastAPI(
        title="Leitor de Dados",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.add_middleware(SearchBodyLimit)

    @app.middleware("http")
    async def privacy_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if "Content-Security-Policy" not in response.headers:
            response.headers["Content-Security-Policy"] = (
                "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"
            )
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):
        # A resposta padrão do Pydantic pode conter o valor pesquisado.
        return JSONResponse(status_code=422, content={"detail": "Solicitação inválida."})

    def authenticated(
        request: Request,
        supplied: Annotated[str | None, Header(alias="X-API-Key")] = None,
    ) -> DatasetStore:
        configured = request.app.state.api_key
        if supplied is None or not secrets.compare_digest(
            supplied.encode("utf-8"), configured.encode("utf-8")
        ):
            raise HTTPException(401, "Credencial inválida.")
        return request.app.state.store

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def index():
        nonce = secrets.token_urlsafe(24)
        template = Path(__file__).with_name("static").joinpath("index.html").read_text(encoding="utf-8")
        return HTMLResponse(
            template.replace("__CSP_NONCE__", nonce),
            headers={
                "Content-Security-Policy": (
                    f"default-src 'none'; script-src 'nonce-{nonce}'; "
                    f"style-src 'nonce-{nonce}'; connect-src 'self'; "
                    "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
                )
            },
        )

    @app.get("/health/live")
    def live():
        return {"status": "alive"}

    @app.get("/health/ready")
    def ready(store: Annotated[DatasetStore, Depends(authenticated)]):
        try:
            available = store.ready()
        except Exception as exc:
            raise _query_failure(exc) from None
        if not available:
            raise HTTPException(503, "Base de dados indisponível.")
        return {"status": "ready"}

    @app.get("/v1/info")
    def info(store: Annotated[DatasetStore, Depends(authenticated)]):
        try:
            return store.info()
        except Exception as exc:
            raise _query_failure(exc) from None

    @app.post("/v1/search")
    def search(body: SearchRequest, store: Annotated[DatasetStore, Depends(authenticated)]):
        try:
            return store.search(
                field=body.field,
                value=body.value,
                limit=body.limit,
                cursor=body.cursor,
            )
        except Exception as exc:
            raise _query_failure(exc) from None

    return app
