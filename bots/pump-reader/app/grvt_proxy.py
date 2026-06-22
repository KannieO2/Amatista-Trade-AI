"""Same-origin reverse proxy for the REAL GRVTBot (github.com/kmanus88/GRVTBot).

The user wants ONE app / ONE host: the TradeOS dashboard at :8000 plus the
genuine GRVTBot React dashboard, not a second URL the user has to open. The
GRVTBot is a Node (express + ws) process listening on 127.0.0.1:3848 and serving
its SPA at /dashboard/, REST at /api/v2/*, and a WebSocket at /ws.

We mount everything under /grid/* on the FastAPI app and forward to the Node
backend with the /grid prefix stripped:

    /grid/dashboard/            -> :3848/dashboard/          (SPA index)
    /grid/dashboard/assets/x.js -> :3848/dashboard/assets/x.js
    /grid/api/v2/*              -> :3848/api/v2/*            (REST)
    /grid/ws?token=...          -> :3848/ws?token=...        (WebSocket)

The SPA dist is built with VITE_BASE_PATH=/grid/dashboard/ and
VITE_API_BASE_URL=/grid so every absolute URL it emits already lives under
/grid/* — no client code changes, no cross-origin requests. The Grid section of
the TradeOS dashboard simply iframes /grid/dashboard/ (same origin).

The /grid prefix is allow-listed in main.py's auth gate because the GRVTBot has
its own JWT login inside the iframe.
"""

from __future__ import annotations

import asyncio
import logging
import os

import httpx
import websockets
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from starlette.websockets import WebSocket, WebSocketDisconnect

logger = logging.getLogger("pump-reader.grvt-proxy")

# Backend del GRVTBot Node. Default 127.0.0.1:3848 = dev local (grvtbot en la misma
# máquina). En el compose raíz (los dos bots en contenedores) se sobreescribe con
# GRVT_BACKEND_HOST=grvtbot:3848 (nombre del servicio en la red compartida).
_BACKEND_HOST = os.getenv("GRVT_BACKEND_HOST", "127.0.0.1:3848")
HTTP_BACKEND = f"http://{_BACKEND_HOST}"
WS_BACKEND = f"ws://{_BACKEND_HOST}"

# Hop-by-hop headers we must not forward. Also strip accept-encoding on the way
# up (so the backend replies identity and httpx hands us decoded bytes) and
# content-encoding/length on the way down (Response recomputes the length).
_REQ_STRIP = {"host", "content-length", "connection", "accept-encoding", "transfer-encoding"}
# Also drop x-frame-options so the dashboard can be iframed by the TradeOS page.
# We own the embedding concern here (in-repo) instead of patching the upstream
# Node bot, so a vanilla GRVTBot clone works behind this proxy unmodified.
_RESP_STRIP = {
    "content-encoding", "content-length", "transfer-encoding", "connection",
    "keep-alive", "x-frame-options",
}

_client: httpx.AsyncClient | None = None

# Injected into the GRVTBot dashboard HTML so the embedded bot reads as one
# integrated app, not a separate site:
#   - matches the ScamPump palette (dark) + a light variant the parent drives,
#   - HIDES the GRVTBot's own top header bar (logo/Offline/ES-EN/theme) — the
#     TradeOS topbar already provides those, so a second bar looked like a jump
#     to a different app,
#   - drops the default scrollbar,
#   - a tiny listener flips light/dark on postMessage from the parent, so ONE
#     theme toggle in the TradeOS topbar controls both sides.
_THEME_CSS = """<style id="tradeos-theme">
@import url('https://fonts.googleapis.com/css2?family=Geist:wght@400;500;600&display=swap');
:root,.dark,html.dark{
  --color-bg-base:#070a0f!important;--color-bg-surface:#0c1018!important;
  --color-bg-elevated:#121722!important;--color-bg-muted:#161c28!important;
  --color-border-subtle:#1b2230!important;--color-border-default:#222b3a!important;
  --color-border-strong:#33405a!important;
  --color-text-primary:#e6e9ef!important;--color-text-secondary:#b6bdcc!important;
  --color-text-muted:#8b95a7!important;--color-text-disabled:#5a6477!important;
  --color-primary:#ff2f6e!important;--color-primary-strong:#ff5a86!important;
  --color-primary-soft:#2a0d17!important;--color-info:#7c6cff!important;
  --color-chart-1:#7c6cff!important;--color-chart-4:#a78bfa!important;--color-chart-5:#ff2f6e!important;
}
/* light mode — driven by the parent (postMessage / ?theme=light) */
html.tradeos-light{
  --color-bg-base:#eef1f7!important;--color-bg-surface:#ffffff!important;
  --color-bg-elevated:#f6f8fc!important;--color-bg-muted:#eaeef6!important;
  --color-border-subtle:#dce2ec!important;--color-border-default:#d7deea!important;
  --color-border-strong:#c2cad9!important;
  --color-text-primary:#0f1622!important;--color-text-secondary:#2f3a4d!important;
  --color-text-muted:#7a869b!important;--color-text-disabled:#aab3c2!important;
  --color-primary:#ff2f6e!important;--color-primary-strong:#ff5a86!important;
  --color-primary-soft:#ffe3ec!important;--color-info:#7c6cff!important;
}
html,body,#root{font-family:Geist,system-ui,-apple-system,sans-serif!important}
html.dark,html.dark body,html.dark #root,:root #root{background:var(--color-bg-base)!important}
html.tradeos-light,html.tradeos-light body,html.tradeos-light #root{background:#eef1f7!important}
/* hide the GRVTBot's own top header bar — TradeOS already shows one */
#root > div > header{display:none!important}
/* make the grid sidebar read like the ScamPump sidebar (same width + item
   sizing — the grid one looked bigger). 212px / 13px nav like the pump side. */
#root aside{width:212px!important;padding:16px 12px!important;gap:4px!important}
#root aside a,#root aside button{font-size:13px!important;font-weight:500!important;
  padding:8px 10px!important;border-radius:8px!important;gap:10px!important;letter-spacing:-.01em!important}
#root aside a svg,#root aside button svg{width:15px!important;height:15px!important}
*{scrollbar-width:none!important;-ms-overflow-style:none!important}
*::-webkit-scrollbar{width:0!important;height:0!important;display:none!important}
</style>
<script id="tradeos-theme-sync">
(function(){
  function apply(t){ try{ document.documentElement.classList.toggle('tradeos-light', t==='light'); }catch(e){} }
  window.addEventListener('message', function(e){
    if(e && e.data && e.data.tradeosTheme){ apply(e.data.tradeosTheme); }
  });
  try{ var m=String(location.search||'').match(/[?&]theme=(light|dark)/); if(m){ apply(m[1]); } }catch(e){}
})();
</script>""".encode("utf-8")


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(base_url=HTTP_BACKEND, timeout=httpx.Timeout(30.0), follow_redirects=False)
    return _client


async def _proxy_http(request: Request, path: str) -> Response:
    client = _get_client()
    body = await request.body()
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _REQ_STRIP}
    try:
        upstream = await client.request(
            request.method,
            "/" + path,
            params=request.query_params,
            content=body,
            headers=headers,
        )
    except (httpx.ConnectError, httpx.ConnectTimeout):
        return JSONResponse(
            {"error": "grid_offline", "hint": "GRVTBot no está corriendo. Ejecuta start-grvtbot.bat."},
            status_code=502,
        )
    except httpx.HTTPError as exc:
        logger.warning("grvt proxy http error: %s", exc)
        return JSONResponse({"error": "grid_proxy_error", "detail": str(exc)}, status_code=502)

    out_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in _RESP_STRIP}
    # Rewrite absolute redirects so /dashboard/ -> /grid/dashboard/ stays on host.
    loc = out_headers.get("location")
    if loc and loc.startswith("/") and not loc.startswith("/grid"):
        out_headers["location"] = "/grid" + loc

    content = upstream.content
    ctype = out_headers.get("content-type", "")
    # ASSET-BASE FIX: el dist se compiló con VITE_BASE_PATH=/dashboard/ (debió ser
    # /grid/dashboard/). Así el index.html y los chunks piden /dashboard/assets/*,
    # que NO pasan por este proxy → caen en la app pump (auth gate) → iframe NEGRO.
    # Reescribimos las refs absolutas /dashboard/ → /grid/dashboard/ en el texto
    # servido (html/js/css) para que TODO ruteé de vuelta por el proxy. La API ya
    # quedó horneada como /grid (correcta), solo el base de assets estaba mal.
    # Idempotente: el build nunca emite /grid/dashboard/, así que no hay doble prefijo.
    if any(t in ctype for t in ("text/html", "javascript", "text/css")) and b"/dashboard/" in content:
        content = content.replace(b"/dashboard/", b"/grid/dashboard/")
    # Inject the TradeOS theme into the dashboard HTML so the embedded GRVTBot
    # matches the ScamPump Radar look (same palette, no scrollbars) — done here
    # so a vanilla upstream build needs no patching.
    if "text/html" in ctype and b"</head>" in content:
        content = content.replace(b"</head>", _THEME_CSS + b"</head>", 1)
    return Response(content=content, status_code=upstream.status_code, headers=out_headers)


async def _pump_client_to_upstream(ws: WebSocket, up) -> None:
    try:
        while True:
            data = await ws.receive()
            if data.get("type") == "websocket.disconnect":
                break
            if data.get("text") is not None:
                await up.send(data["text"])
            elif data.get("bytes") is not None:
                await up.send(data["bytes"])
    except (WebSocketDisconnect, websockets.ConnectionClosed):
        pass
    except Exception as exc:  # noqa: BLE001 - proxy must not crash on either side
        logger.debug("ws client->upstream ended: %s", exc)
    finally:
        await up.close()


async def _pump_upstream_to_client(ws: WebSocket, up) -> None:
    try:
        async for message in up:
            if isinstance(message, (bytes, bytearray)):
                await ws.send_bytes(bytes(message))
            else:
                await ws.send_text(message)
    except (WebSocketDisconnect, websockets.ConnectionClosed):
        pass
    except Exception as exc:  # noqa: BLE001
        logger.debug("ws upstream->client ended: %s", exc)
    finally:
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass


async def _proxy_ws(ws: WebSocket) -> None:
    await ws.accept()
    query = ws.scope.get("query_string", b"").decode()
    target = f"{WS_BACKEND}/ws" + (f"?{query}" if query else "")
    try:
        async with websockets.connect(target, max_size=None, open_timeout=10) as up:
            t1 = asyncio.create_task(_pump_client_to_upstream(ws, up))
            t2 = asyncio.create_task(_pump_upstream_to_client(ws, up))
            _, pending = await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
    except Exception as exc:  # noqa: BLE001 - backend down / handshake failure
        logger.debug("ws proxy connect failed: %s", exc)
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass


async def _grid_root() -> RedirectResponse:
    return RedirectResponse("/grid/dashboard/", status_code=307)


def register_grvt_proxy(app: FastAPI) -> None:
    """Wire the /grid/* reverse proxy onto the FastAPI app."""
    app.add_api_websocket_route("/grid/ws", _proxy_ws)
    app.add_api_route("/grid", _grid_root, methods=["GET"])
    app.add_api_route(
        "/grid/{path:path}",
        _proxy_http,
        methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
    )
