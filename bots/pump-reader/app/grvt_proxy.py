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
  --color-primary:#a05cf2!important;--color-primary-strong:#b985ff!important;
  --color-primary-soft:#1c1330!important;--color-info:#7c6cff!important;
  --color-chart-1:#a05cf2!important;--color-chart-4:#c9a6ff!important;--color-chart-5:#7c6cff!important;
}
/* light mode — driven by the parent (postMessage / ?theme=light) */
html.tradeos-light{
  --color-bg-base:#eef1f7!important;--color-bg-surface:#ffffff!important;
  --color-bg-elevated:#f6f8fc!important;--color-bg-muted:#eaeef6!important;
  --color-border-subtle:#dce2ec!important;--color-border-default:#d7deea!important;
  --color-border-strong:#c2cad9!important;
  --color-text-primary:#0f1622!important;--color-text-secondary:#2f3a4d!important;
  --color-text-muted:#7a869b!important;--color-text-disabled:#aab3c2!important;
  --color-primary:#7d3fc7!important;--color-primary-strong:#a05cf2!important;
  --color-primary-soft:#efe6fc!important;--color-info:#7c6cff!important;
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
/* active nav item = ScamPump look exactly (purple wash + white text + purple icon) */
#root aside a[aria-current="page"],#root aside a.active{
  background:linear-gradient(90deg,rgba(160,92,242,.16),rgba(160,92,242,.02))!important;color:#fff!important}
#root aside a[aria-current="page"] svg,#root aside a.active svg{color:#a05cf2!important}
/* Amatista brand header injected at top of the grid sidebar — IGUAL al pump:
   "Amatista" bold + "TradeOS" muted MISMO tamaño, sin uppercase/spacing/divisoria. */
#tradeos-grid-brand{display:flex!important;align-items:center;gap:9px;padding:4px 8px 16px;font-size:14px;line-height:1.1}
#tradeos-grid-brand .gem{width:26px;height:26px;flex:0 0 auto;filter:drop-shadow(0 4px 11px rgba(160,92,242,.6))}
#tradeos-grid-brand b{font-weight:600;letter-spacing:-.02em;color:var(--color-text-primary)}
#tradeos-grid-brand span{color:var(--color-text-muted);font-weight:400;margin-left:5px}
#tradeos-grid-navlabel{font-size:10px;letter-spacing:.14em;color:#4d5666;text-transform:uppercase;padding:14px 8px 6px;font-weight:600}
/* El shell de TradeOS flota su topbar (toggle Pump/Grid + acciones) sobre el ÁREA DE
   CONTENIDO del grid (no sobre el sidebar) → empuja el contenido hacia abajo para que el
   topbar no lo tape. El sidebar (aside) con el logo Amatista queda intacto arriba. */
#root #main-content{padding-top:64px!important}
/* ===== Animaciones de paridad con el Pump Reader (riseIn + lift + sheen + floaty) ===== */
@keyframes tos-rise{from{opacity:0;transform:translateY(12px)}to{opacity:1;transform:none}}
@keyframes tos-floaty{0%,100%{transform:translateY(0)}50%{transform:translateY(-2px)}}
@keyframes tos-pulse{50%{box-shadow:0 0 0 5px rgba(160,92,242,.05)}}
/* ENTRADA ESCALONADA de toda la vista (igual que .view>* del pump): cada sección de la
   página (header, strips de KPIs, cards, grid de bots) entra en cascada al cargar.
   El contenido del grid vive en #main-content > div (wrapper flex de cada página). */
#root #main-content > div > *{animation:tos-rise .55s cubic-bezier(.16,1,.3,1) both}
#root #main-content > div > *:nth-child(2){animation-delay:.05s}
#root #main-content > div > *:nth-child(3){animation-delay:.10s}
#root #main-content > div > *:nth-child(4){animation-delay:.16s}
#root #main-content > div > *:nth-child(5){animation-delay:.22s}
#root #main-content > div > *:nth-child(6){animation-delay:.28s}
/* SUPERFICIES tipo tarjeta (Card .rounded-lg + strips de KPIs .rounded-lg) = lift +
   glow morado al hover + sheen superior, como el pump. */
#root .bg-bg-elevated.rounded-lg,#root #main-content > div > .rounded-lg{position:relative;
  transition:transform .26s cubic-bezier(.16,1,.3,1),box-shadow .26s ease,border-color .26s ease!important}
#root .bg-bg-elevated.rounded-lg:hover,#root #main-content > div > .rounded-lg:hover{transform:translateY(-3px);
  border-color:rgba(160,92,242,.30)!important;box-shadow:0 32px 64px -28px rgba(0,0,0,.85),0 0 36px -12px rgba(160,92,242,.22)!important}
#root .bg-bg-elevated.rounded-lg::after,#root #main-content > div > .rounded-lg::after{content:"";position:absolute;top:0;left:14px;right:14px;height:1px;
  background:linear-gradient(90deg,transparent,rgba(255,255,255,.26),transparent);pointer-events:none;z-index:1}
/* tiles de KPI individuales (bg-bg-elevated p-4): realce sutil al pasar el cursor */
#root .bg-bg-elevated.p-4{transition:background .15s ease}
#root .bg-bg-elevated.p-4:hover{background:rgba(255,255,255,.035)}
/* tile "Crear bot nuevo" (dashed): ya tiene hover propio, solo le sumamos el lift */
#root button.border-dashed:hover{transform:translateY(-3px);transition:transform .26s cubic-bezier(.16,1,.3,1)}
/* números grandes con glow morado (igual que .kval del pump) */
#root .text-2xl,#root .text-3xl,#root .text-4xl{text-shadow:0 0 30px rgba(160,92,242,.16)}
/* gem flotante + dot de estado 'corriendo' pulsante */
#tradeos-grid-brand .gem{animation:tos-floaty 4s ease-in-out infinite}
#root .bg-success{animation:tos-pulse 1.8s infinite}
/* ===== PARIDAD DE DISEÑO con el Pump Reader (solo CSS, sin tocar lógica) =====
   Las cards del pump son glass: fondo translúcido + blur, borde claro fino, radio 14,
   sombra profunda + sheen. Replicamos EXACTO sobre las superficies del grid para que
   cambiar de tab no se note. */
#root .bg-bg-elevated.rounded-lg{
  background:rgba(16,21,30,.55)!important;
  border:1px solid rgba(255,255,255,.09)!important;
  border-radius:14px!important;
  box-shadow:inset 0 1px 0 rgba(255,255,255,.12),0 22px 54px -30px rgba(0,0,0,.85),0 0 0 1px rgba(255,255,255,.015)!important;
  backdrop-filter:blur(22px) saturate(160%);-webkit-backdrop-filter:blur(22px) saturate(160%);
}
/* Strips de KPI: de tira conectada (gap-px + divisores) → tarjetas glass SEPARADAS,
   como las KPI cards del pump (grid con gap + cada una redondeada). */
#root #main-content .grid.gap-px{gap:14px!important;background:transparent!important;overflow:visible!important}
#root #main-content .grid.gap-px > .bg-bg-elevated{
  border-radius:14px!important;border:1px solid rgba(255,255,255,.09)!important;
  background:rgba(16,21,30,.55)!important;padding:16px!important;
  box-shadow:inset 0 1px 0 rgba(255,255,255,.12),0 22px 54px -30px rgba(0,0,0,.85)!important;
  backdrop-filter:blur(22px) saturate(160%);-webkit-backdrop-filter:blur(22px) saturate(160%);
  transition:transform .26s cubic-bezier(.16,1,.3,1),box-shadow .26s ease,border-color .26s ease}
#root #main-content .grid.gap-px > .bg-bg-elevated:hover{transform:translateY(-3px);
  border-color:rgba(160,92,242,.30)!important;box-shadow:0 32px 64px -28px rgba(0,0,0,.85),0 0 36px -12px rgba(160,92,242,.22)!important}
/* Modo claro: glass blanco (no el oscuro), para no romper el tema claro del pump. */
html.tradeos-light #root .bg-bg-elevated.rounded-lg,
html.tradeos-light #root #main-content .grid.gap-px > .bg-bg-elevated{
  background:rgba(255,255,255,.72)!important;border-color:rgba(0,0,0,.06)!important;
  box-shadow:inset 0 1px 0 rgba(255,255,255,.6),0 14px 34px -22px rgba(0,0,0,.16)!important}
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
</script>
<script id="tradeos-grid-clean">
/* UNA SOLA CUENTA: el grid usa el SSO de TradeOS → no debe mostrar su propia cuenta/
   login/logout. Ocultamos la card "Cuenta" (email/rol/Cerrar sesión) del Ajustes y el
   referral del upstream, sin tocar el build (SPA → MutationObserver). El logout real
   vive en la barra superior de TradeOS, compartida por ambos bots. */
(function(){
  function cardOf(node){ return node && (node.closest('.bg-bg-elevated') || node.closest('.rounded-lg')); }
  function hideCard(node){ var c=cardOf(node); if(c) c.style.display='none'; }
  function clean(){
    try{
      // 1) ROBUSTO: ocultar la card de cuenta por su ENCABEZADO ("Cuenta"/"Account").
      // No depende del botón logout (que a veces no rinde) → la card se va siempre.
      var heads=document.querySelectorAll('h1,h2,h3');
      for(var j=0;j<heads.length;j++){
        var ht=(heads[j].textContent||'').trim().toLowerCase();
        if(ht==='cuenta'||ht==='account'){ hideCard(heads[j]); }
      }
      // 2) Respaldo: por el email de la cuenta TradeOS embebido en la card.
      var all=document.querySelectorAll('#root *');
      for(var k=0;k<all.length;k++){
        if(all[k].children.length===0 && /@tradeos\\.local/i.test(all[k].textContent||'')){ hideCard(all[k]); }
      }
      // 3) Respaldo: por el botón "Cerrar sesión" si está.
      var btns=document.querySelectorAll('button');
      for(var i=0;i<btns.length;i++){
        var t=(btns[i].textContent||'').trim().toLowerCase();
        if(t==='cerrar sesión'||t==='cerrar sesion'||t==='log out'||t==='sign out'){ hideCard(btns[i]); }
      }
      // 4) Referral del upstream (grvt.io ?ref=...).
      var ref=document.querySelector('a[href*="ref=R3WLGZS"],a[href*="grvt.io/?ref"]');
      if(ref){ hideCard(ref); }
    }catch(e){}
  }
  function start(){ clean(); try{ new MutationObserver(clean).observe(document.body,{childList:true,subtree:true}); }catch(e){} }
  if(document.body) start(); else document.addEventListener('DOMContentLoaded', start);
})();
</script>
<script id="tradeos-grid-brand-inject">
/* PARIDAD VISUAL: el sidebar del grid debe leerse igual que el del ScamPump
   (gem Amatista + label de sección). El upstream no trae header de marca → lo
   inyectamos en el <aside> sin tocar el build. Idempotente (chequea si ya existe). */
(function(){
  var GEM='<svg class="gem" viewBox="0 0 24 24"><defs><linearGradient id="amgrid" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#e7cdff"/><stop offset="55%" stop-color="#a05cf2"/><stop offset="100%" stop-color="#6a2bb0"/></linearGradient></defs><path d="M6 3.2h12l3.4 5.4L12 21.4 2.6 8.6z" fill="url(#amgrid)" stroke="rgba(255,255,255,.4)" stroke-width=".6" stroke-linejoin="round"/><path d="M6 3.2l6 5.4 6-5.4M2.6 8.6h18.8M12 8.6v12.8" fill="none" stroke="rgba(255,255,255,.5)" stroke-width=".7"/></svg>';
  function brand(){
    try{
      var aside=document.querySelector('#root aside'); if(!aside) return;
      if(!aside.querySelector('#tradeos-grid-brand')){
        var b=document.createElement('div'); b.id='tradeos-grid-brand';
        b.innerHTML=GEM+'<div><b>Amatista</b><span>TradeOS</span></div>';
        aside.insertBefore(b, aside.firstChild);
      }
      var nav=aside.querySelector('nav');
      if(nav && !aside.querySelector('#tradeos-grid-navlabel')){
        var l=document.createElement('div'); l.id='tradeos-grid-navlabel'; l.textContent='Grid Bot';
        nav.parentNode.insertBefore(l, nav);
      }
    }catch(e){}
  }
  function start(){ brand(); try{ new MutationObserver(brand).observe(document.body,{childList:true,subtree:true}); }catch(e){} }
  if(document.body) start(); else document.addEventListener('DOMContentLoaded', start);
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
    # AUTH-INJECTION: las llamadas REST del SPA embebido (/grid/api/v2/*) necesitan el
    # JWT GRVT del usuario, pero el browser a veces NO lo adjunta → 401 "unauthorized"
    # ("No se pudieron cargar los bots"). Si la llamada no trae su propia Authorization,
    # inyectamos el token del usuario logueado (lo mintea+cachea main._grid_token_for).
    # Los endpoints de auth (login/signup) se saltan — ELLOS son el login. Esto vive en
    # nuestro wrapper, NO toca los internals del grvtbot.
    # Excluir SOLO login/signup (ESOS son el login, no llevan token). El resto de
    # /auth/* (p.ej. /auth/me = "¿quién soy?") SÍ necesita el token inyectado.
    if (_token_provider is not None
            and path.startswith("api/v2/")
            and path not in ("api/v2/auth/login", "api/v2/auth/signup")
            and not any(k.lower() == "authorization" for k in headers)):
        try:
            _tok = await _token_provider(request)
            if _tok:
                headers["authorization"] = f"Bearer {_tok}"
        except Exception:  # noqa: BLE001 - inyección best-effort, nunca rompe el proxy
            logger.debug("grid token inject failed", exc_info=True)
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


# Provider que mintea/cachea el JWT GRVT del usuario logueado. main.py lo setea con
# set_grid_token_provider(_grid_token_for) tras definir la función (evita import circular).
_token_provider = None  # callable async (request) -> str | None


def set_grid_token_provider(fn) -> None:
    global _token_provider
    _token_provider = fn


async def _proxy_api_v2_root(request: Request, path: str) -> Response:
    """El SPA del grid se compiló con API base = SAME-ORIGIN → llama /api/v2/* en la
    RAÍZ del dominio (no bajo /grid/), bypaseando el proxy → 404 "No se pudieron cargar
    los bots". Lo reencaminamos al backend del grid con el MISMO handler (auth-injection
    incluida). Pump no expone /api/v2 → sin colisión. No toca internals del grvtbot."""
    return await _proxy_http(request, f"api/v2/{path}")


def register_grvt_proxy(app: FastAPI) -> None:
    """Wire the /grid/* reverse proxy onto the FastAPI app."""
    app.add_api_websocket_route("/grid/ws", _proxy_ws)
    app.add_api_route("/grid", _grid_root, methods=["GET"])
    app.add_api_route(
        "/grid/{path:path}",
        _proxy_http,
        methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
    )
    # El SPA llama same-origin /api/v2/* y /ws en la RAÍZ (su build no usa el prefijo
    # /grid). Reencaminar al backend del grid. Pump no usa /api/v2 ni /ws root.
    app.add_api_route(
        "/api/v2/{path:path}",
        _proxy_api_v2_root,
        methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
    )
    app.add_api_websocket_route("/ws", _proxy_ws)
