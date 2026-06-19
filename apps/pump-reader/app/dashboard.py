"""Self-contained dashboard served by the FastAPI app at '/'.

No build step, no external JS libs. Charts are hand-built inline SVG. Polls the
JSON endpoints (/overview, /grvt/status, /allocation) and renders the
ScamPump Radar view plus a separate GRVTBot grid-trading view.
"""

DASHBOARD_HTML = r"""<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Amatista · TradeOS</title>
<link rel="preconnect" href="https://fonts.googleapis.com" />
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
<link href="https://fonts.googleapis.com/css2?family=Geist:wght@400;500;600;700&family=Geist+Mono:wght@400;500;600&display=swap" rel="stylesheet" />
<style>
  :root{
    --bg:#080b11; --panel:#0c1018; --panel-2:#0f141d; --inset:#131a25;
    --border:#1b2333; --border-soft:#151c28; --text:#e7ebf2; --muted:#6f7a8e;
    --muted-2:#9aa5b8; --pink:#ff2f6e; --pink-soft:#f4789a; --green:#2fd08a;
    --purple:#8b86f2; --amber:#e6a23c; --red:#e8556a; --blue:#3d7bff;
  }
  /* light mode: override surfaces/text, keep the accent palette */
  html[data-theme="light"]{
    --bg:#eef1f7; --panel:#ffffff; --panel-2:#f6f8fc; --inset:#eaeef6;
    --border:#d7deea; --border-soft:#e4e9f2; --text:#0f1622; --muted:#7a869b;
    --muted-2:#4d5973; --green:#10a86a; --red:#d83a52;
  }
  html[data-theme="light"] body{
    background:
      radial-gradient(1000px 540px at 10% -8%, rgba(255,47,110,.08), transparent 58%),
      radial-gradient(900px 620px at 102% -4%, rgba(61,123,255,.07), transparent 55%),
      radial-gradient(700px 700px at 85% 110%, rgba(139,134,242,.06), transparent 60%),
      var(--bg);
  }
  html[data-theme="light"] .sidebar{background:rgba(255,255,255,.7);border-right:1px solid var(--border)}
  html[data-theme="light"] .nav a.active{background:linear-gradient(90deg,rgba(255,47,110,.10),rgba(255,47,110,.02));color:#1a1f2b}
  html[data-theme="light"] .card,html[data-theme="light"] .panel,html[data-theme="light"] .modal,
  html[data-theme="light"] .topbar,html[data-theme="light"] .pill{background:rgba(255,255,255,.75);border-color:var(--border)}
  *{box-sizing:border-box}
  /* hide scrollbars everywhere (scroll still works) */
  *{scrollbar-width:none;-ms-overflow-style:none}
  *::-webkit-scrollbar{width:0;height:0;display:none}
  html,body{margin:0;height:100%}
  body{
    background:
      radial-gradient(1000px 540px at 10% -8%, rgba(255,47,110,.12), transparent 58%),
      radial-gradient(900px 620px at 102% -4%, rgba(61,123,255,.11), transparent 55%),
      radial-gradient(700px 700px at 85% 110%, rgba(139,134,242,.10), transparent 60%),
      var(--bg);
    background-attachment:fixed;
    color:var(--text);
    font-family:"Geist",system-ui,-apple-system,Segoe UI,sans-serif;
    font-size:13px; -webkit-font-smoothing:antialiased;
  }
  /* liquid glass surfaces */
  .card,.panel,.modal,.statusbadge,.sbox,.exbox,.sidebar,.topbar,.pill,.scoreb{
    backdrop-filter:blur(16px) saturate(130%); -webkit-backdrop-filter:blur(16px) saturate(130%);
  }
  .mono{font-family:"Geist Mono",ui-monospace,SFMono-Regular,Menlo,monospace;font-variant-numeric:tabular-nums}
  .app{display:grid;grid-template-columns:212px 1fr;min-height:100dvh}

  /* equity curve hover tooltip */
  .eqwrap{position:relative;cursor:crosshair}
  .eqline{position:absolute;top:0;width:1px;background:var(--green);opacity:0;pointer-events:none;transition:opacity .1s}
  .eqdot{position:absolute;width:8px;height:8px;border-radius:50%;background:var(--green);box-shadow:0 0 0 3px rgba(47,208,138,.25);transform:translate(-50%,-50%);opacity:0;pointer-events:none}
  .eqtip{position:absolute;pointer-events:none;opacity:0;transform:translate(-50%,-130%);background:var(--inset);border:1px solid var(--border);border-radius:8px;padding:6px 9px;font-size:11px;white-space:nowrap;z-index:5;box-shadow:0 8px 24px -8px rgba(0,0,0,.5)}
  .eqtip .d{color:var(--muted);font-size:10px;margin-bottom:2px}
  .eqtip .v{font-weight:600}
  .eqtip .dl{font-family:"Geist Mono",monospace}
  /* theme toggle */
  .pill.icon{padding:6px 8px;display:inline-flex;align-items:center;justify-content:center}
  .pill.icon svg{width:15px;height:15px}
  /* pnl breakdown rows */
  .pnlrow{display:grid;grid-template-columns:1fr auto;gap:4px 12px;align-items:center;padding:9px 11px;border:1px solid var(--border-soft);border-radius:9px;margin-bottom:7px;background:var(--panel-2)}
  .pnlrow .sym{font-weight:600}
  .pnlrow .sub{font-size:11px;color:var(--muted)}
  .pnlrow .tot{font-family:"Geist Mono",monospace;font-weight:600;text-align:right}
  .pnlrow .br{font-family:"Geist Mono",monospace;font-size:11px;color:var(--muted);text-align:right}
  .pnl-sec{display:flex;justify-content:space-between;align-items:center;margin:14px 2px 8px;font-size:10px;letter-spacing:.12em;text-transform:uppercase}
  .pnl-sec:first-child{margin-top:2px}
  .pnl-sec-t{font-weight:600}

  /* sidebar */
  .sidebar{background:rgba(12,16,24,.6);border-right:1px solid rgba(255,255,255,.06);padding:18px 14px;display:flex;flex-direction:column;gap:6px}
  .brand{display:flex;align-items:center;gap:9px;padding:4px 8px 16px}
  .brand .dot{width:22px;height:22px;border-radius:7px;background:radial-gradient(circle at 30% 30%,#ff5c8a,#ff2f6e 60%,#b3134a);box-shadow:0 0 0 1px rgba(255,47,110,.35),0 4px 14px -4px rgba(255,47,110,.5)}
  .brand b{font-weight:600;letter-spacing:-.02em}
  .brand span{color:var(--muted)}
  .navlabel{font-size:10px;letter-spacing:.14em;color:#4d5666;text-transform:uppercase;padding:14px 8px 6px}
  .nav a{display:flex;align-items:center;gap:10px;padding:8px 9px;border-radius:8px;color:var(--muted-2);text-decoration:none;cursor:pointer;font-weight:500;transition:background .15s ease,color .15s ease}
  .nav a svg{width:15px;height:15px;opacity:.8;flex:none}
  .nav a:hover{background:var(--panel-2);color:var(--text)}
  .nav a.active{background:linear-gradient(90deg,rgba(255,47,110,.14),rgba(255,47,110,.02));color:#fff}
  .nav a.active svg{opacity:1;color:var(--pink)}
  .nav a .badge{margin-left:auto;font-size:9px;background:var(--inset);color:var(--muted);padding:1px 6px;border-radius:5px}
  .badge{display:inline-block;font-size:11px;padding:3px 8px;border-radius:6px;background:var(--inset);color:var(--muted-2);font-weight:600}
  .stat{font-size:11px;white-space:nowrap}
  .stat.sw{color:var(--pink);font-weight:600}

  /* topbar */
  .main{display:flex;flex-direction:column;min-width:0}
  .topbar{display:flex;align-items:center;gap:12px;padding:11px 18px;border-bottom:1px solid var(--border-soft);background:linear-gradient(180deg,var(--panel),rgba(12,16,24,.6))}
  .search{flex:1;max-width:420px;position:relative}
  .search input{width:100%;background:var(--panel-2);border:1px solid var(--border);border-radius:9px;color:var(--text);padding:8px 12px 8px 32px;font-family:inherit;font-size:12px;outline:none}
  .search input:focus{border-color:#2a3447}
  .search svg{position:absolute;left:10px;top:9px;width:14px;height:14px;color:var(--muted)}
  .tb-actions{display:flex;align-items:center;gap:8px;margin-left:auto}
  .pill{display:inline-flex;align-items:center;gap:6px;background:var(--panel-2);border:1px solid var(--border);border-radius:8px;padding:6px 10px;font-size:11px;color:var(--muted-2);cursor:pointer;font-weight:500;transition:transform .12s ease,border-color .15s ease}
  .pill:hover{border-color:#2a3447}
  .pill:active{transform:translateY(1px)}
  .pill svg{width:13px;height:13px}
  .pill b{color:var(--text);font-weight:600}
  .pill.green{color:var(--green)}
  .pill.live .ldot{width:7px;height:7px;border-radius:50%;background:var(--green);box-shadow:0 0 0 3px rgba(47,208,138,.18);animation:pulse 2s infinite}
  @keyframes pulse{50%{box-shadow:0 0 0 5px rgba(47,208,138,.05)}}

  /* views */
  .view{padding:20px 22px;display:flex;flex-direction:column;gap:16px}
  .view.hidden{display:none}
  .vhead{display:flex;align-items:flex-start;justify-content:space-between}
  .vhead h1{margin:0;font-size:21px;font-weight:600;letter-spacing:-.02em}
  .vhead p{margin:4px 0 0;color:var(--muted);font-size:12px}
  .vhead .ts{color:var(--muted);font-size:11px}

  .grid-kpi{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}
  .grid-2{display:grid;grid-template-columns:1fr 1.32fr;gap:14px}
  .grid-2b{display:grid;grid-template-columns:1.5fr 1fr;gap:14px}
  @media(max-width:1100px){.grid-kpi{grid-template-columns:repeat(2,1fr)}.grid-2,.grid-2b{grid-template-columns:1fr}}
  /* ---- mobile / phone (matches the responsive grid bot) ---- */
  @media(max-width:860px){
    .app{grid-template-columns:1fr}
    /* sidebar becomes a sticky horizontal nav strip */
    .sidebar{flex-direction:row;align-items:center;gap:8px;padding:8px 10px;border-right:0;border-bottom:1px solid var(--border-soft);overflow-x:auto;position:sticky;top:0;z-index:30;-webkit-backdrop-filter:blur(16px);backdrop-filter:blur(16px)}
    .sidebar .navlabel{display:none}
    .sidebar .brand{flex:0 0 auto;margin:0}
    .nav{flex-direction:row;gap:4px;flex:1;overflow-x:auto;scrollbar-width:none}
    .nav::-webkit-scrollbar{display:none}
    .nav a{white-space:nowrap;padding:8px 11px;font-size:12px}
    .topbar{flex-wrap:wrap;gap:8px;padding:9px 12px}
    .search{display:none}
    .tb-actions{flex-wrap:wrap;gap:6px;margin-left:auto}
    .view{padding:12px}
    .grid-kpi{grid-template-columns:1fr 1fr;gap:10px}
    .vhead{flex-direction:column;align-items:flex-start;gap:6px}
    .vhead h1{font-size:18px}
    table{font-size:11.5px}
    .modal{width:100%!important;max-width:100vw;border-radius:14px 14px 0 0;max-height:88vh}
    .modal-overlay{align-items:flex-end}
  }
  @media(max-width:480px){
    body{font-size:12.5px}
    .grid-kpi{grid-template-columns:1fr}
    .sidebar .brand b+span,.sidebar .brand span{display:none}
    .nav a svg{width:14px;height:14px}
    .pill{padding:6px 9px;font-size:11px}
    .modeswitch button{padding:7px 10px;font-size:11px}
    .modeswitch button svg{display:none}
    .vhead h1{font-size:17px}
  }
  body{overflow-x:hidden}
  /* wide tables scroll inside their panel instead of breaking the page */
  .panel{overflow-x:auto}
  table{width:100%;min-width:max-content}

  .card{background:rgba(16,21,30,.55);border:1px solid rgba(255,255,255,.07);border-radius:14px;padding:16px;box-shadow:inset 0 1px 0 rgba(255,255,255,.05),0 14px 34px -22px rgba(0,0,0,.7)}
  .card .klabel{display:flex;align-items:center;gap:7px;color:var(--muted);font-size:10px;letter-spacing:.1em;text-transform:uppercase}
  .card .klabel svg{width:13px;height:13px}
  .card .kval{font-size:30px;font-weight:600;margin-top:10px;letter-spacing:-.02em}
  .card .ksub{color:var(--muted);font-size:11px;margin-top:6px}
  .kval.pink{color:var(--pink)}

  .panel{background:rgba(16,21,30,.55);border:1px solid rgba(255,255,255,.07);border-radius:14px;padding:16px;min-width:0;box-shadow:inset 0 1px 0 rgba(255,255,255,.05),0 14px 34px -22px rgba(0,0,0,.7)}
  .phead{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px}
  .phead .pt{font-weight:600;font-size:13px}
  .phead .px{color:var(--muted);font-size:11px}

  /* cluster split */
  .clrow{display:flex;align-items:center;justify-content:space-between;margin-bottom:7px}
  .clrow .cl{display:flex;align-items:center;gap:8px;font-size:12px}
  .cdot{width:8px;height:8px;border-radius:50%}
  .cdot.green{background:var(--green)} .cdot.purple{background:var(--purple)}
  .ctrack{height:5px;border-radius:4px;background:var(--inset);overflow:hidden;margin-bottom:16px}
  .ctrack i{display:block;height:100%;border-radius:4px}
  .ctrack i.green{background:linear-gradient(90deg,#1c7d54,var(--green))}
  .ctrack i.purple{background:linear-gradient(90deg,#5650b0,var(--purple))}
  .statgrid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}
  .statgrid.split{margin-top:10px}
  .sbox{background:var(--inset);border-radius:8px;padding:9px 10px}
  .sbox .l{font-size:9px;letter-spacing:.1em;color:var(--muted);text-transform:uppercase}
  .sbox .v{font-size:16px;font-weight:600;margin-top:3px}
  .sbox .v.green{color:var(--green)} .sbox .v.purple{color:var(--purple)}

  /* table */
  table{width:100%;border-collapse:collapse}
  thead th{text-align:left;font-size:9.5px;letter-spacing:.1em;color:var(--muted);text-transform:uppercase;font-weight:500;padding:0 8px 9px}
  tbody td{padding:8px;border-top:1px solid var(--border-soft);font-size:12px;vertical-align:middle}
  .scoreb{display:inline-block;min-width:42px;text-align:center;padding:3px 6px;border-radius:6px;font-weight:600;font-size:11px}
  .tag{display:inline-flex;align-items:center;gap:6px;font-size:11px;color:var(--muted-2)}
  .bar{display:flex;align-items:center;gap:8px}
  .bar .bt{flex:1;height:5px;border-radius:4px;background:var(--inset);overflow:hidden;min-width:54px}
  .bar .bt i{display:block;height:100%;background:linear-gradient(90deg,#7a1f33,var(--red))}
  .delta.up{color:var(--green)} .delta.down{color:var(--red)}
  .sym{font-weight:600}

  /* alerts */
  .alert{display:flex;align-items:center;gap:11px;padding:11px 4px;border-top:1px solid var(--border-soft)}
  .alert:first-child{border-top:none}
  .alert .meta{flex:1;min-width:0}
  .alert .meta .top{display:flex;align-items:center;gap:8px}
  .alert .meta .sub{color:var(--muted);font-size:11px;margin-top:2px}
  .alert .ago{color:var(--muted);font-size:11px}

  /* grvt */
  .feat{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:14px}
  .feat .f{background:var(--inset);border:1px solid var(--border-soft);border-radius:10px;padding:13px}
  .feat .f .ft{font-weight:600;font-size:12px}
  .feat .f .fd{color:var(--muted);font-size:11px;margin-top:4px;line-height:1.5}
  .kv{display:flex;justify-content:space-between;padding:9px 0;border-top:1px solid var(--border-soft);font-size:12px}
  .kv:first-child{border-top:none}
  .kv .k{color:var(--muted)}
  .statusbadge{display:inline-flex;align-items:center;gap:6px;font-size:11px;padding:3px 9px;border-radius:20px;background:rgba(230,162,60,.12);color:var(--amber);border:1px solid rgba(230,162,60,.25)}
  .statusbadge.run{background:rgba(47,208,138,.12);color:var(--green);border-color:rgba(47,208,138,.28)}
  .gform{display:grid;grid-template-columns:1fr 1fr;gap:10px 14px}
  .gf{display:flex;flex-direction:column;gap:5px}
  .gf label{font-size:11px;color:var(--muted)}
  .gf input{background:var(--bg);border:1px solid var(--border);border-radius:8px;color:var(--text);padding:8px 10px;font-family:inherit;font-size:12px;outline:none}
  .gf input:focus{border-color:#2a3447}
  .gactions{display:flex;align-items:center;gap:10px;margin-top:14px}
  .ladder{display:flex;flex-direction:column;gap:2px;max-height:320px;overflow:auto}
  .lvl{display:flex;align-items:center;gap:10px;font-size:11px;padding:3px 4px;border-radius:6px}
  .lvl .ld{width:8px;height:8px;border-radius:50%;background:var(--inset);border:1px solid var(--border);flex:none}
  .lvl .ld.held{background:var(--green);border-color:var(--green)}
  .lvl .lp{font-family:"Geist Mono",monospace;color:var(--muted-2);min-width:96px}
  .lvl .lbar{flex:1;height:4px;border-radius:3px;background:var(--inset);overflow:hidden}
  .lvl .lbar i{display:block;height:100%;background:var(--blue)}
  .lvl.cur{background:rgba(61,123,255,.1)}
  .lvl.cur .lp{color:var(--blue);font-weight:600}

  /* modal */
  .modal-overlay{position:fixed;inset:0;background:rgba(4,6,10,.66);backdrop-filter:blur(3px);display:flex;align-items:center;justify-content:center;z-index:50}
  .modal-overlay.hidden{display:none}
  .modal{width:520px;max-width:92vw;background:rgba(18,24,34,.72);border:1px solid rgba(255,255,255,.1);border-radius:16px;box-shadow:inset 0 1px 0 rgba(255,255,255,.08),0 40px 90px -20px rgba(0,0,0,.8);overflow:hidden;display:flex;flex-direction:column;max-height:94vh}
  .modal .mh{display:flex;align-items:flex-start;justify-content:space-between;padding:14px 18px 0}
  .modal .mh h3{margin:0;font-size:16px;font-weight:600}
  .modal .mh p{margin:5px 0 0;color:var(--muted);font-size:12px}
  .modal .mx{cursor:pointer;color:var(--muted);background:none;border:none;font-size:18px;line-height:1}
  .modal .mb{padding:12px 18px;overflow-y:auto;flex:1;min-height:0}
  .modal .mh,.modal .mfoot{flex:0 0 auto}
  .mfield{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:12px}
  .mfield label{color:var(--muted-2);font-size:12px}
  .mfield input[type=number]{width:130px;background:var(--bg);border:1px solid var(--border);border-radius:8px;color:var(--text);padding:8px 10px;text-align:right;font-family:"Geist Mono",monospace;outline:none}
  .exbox{background:var(--inset);border:1px solid var(--border-soft);border-radius:11px;padding:13px;margin-bottom:11px}
  .exbox .top{display:flex;justify-content:space-between;align-items:center;margin-bottom:9px}
  .exbox .top b{font-weight:600}
  .exbox .top .bal{color:var(--muted);font-size:11px}
  .exbox .row{display:flex;align-items:center;gap:12px}
  .exbox input[type=range]{flex:1;accent-color:var(--pink);height:4px}
  .exbox .pct{width:46px;text-align:right;font-family:"Geist Mono",monospace;font-weight:600}
  .exbox .cap{color:var(--muted);font-size:10.5px;margin-top:7px}
  .exbox .cap .ok{color:var(--green)}
  .sumbar{display:flex;justify-content:space-between;align-items:center;background:var(--inset);border-radius:9px;padding:11px 13px;font-size:12px}
  .sumbar .v{font-family:"Geist Mono",monospace;font-weight:600}
  .sumbar .ok{color:var(--green)} .sumbar .bad{color:var(--red)}
  .mfoot{display:flex;justify-content:flex-end;gap:10px;padding:14px 18px;border-top:1px solid var(--border-soft)}
  .btn{border:1px solid var(--border);background:var(--panel);color:var(--text);padding:9px 16px;border-radius:9px;font-family:inherit;font-size:12px;font-weight:600;cursor:pointer;transition:transform .12s ease}
  .btn:active{transform:translateY(1px)}
  .btn.primary{background:var(--pink);border-color:var(--pink);color:#fff}
  .btn.primary:disabled{opacity:.4;cursor:not-allowed}
  .empty{color:var(--muted);text-align:center;padding:26px;font-size:12px}
  /* top-level mode switch (Pump <-> Grid) */
  .modeswitch{display:inline-flex;background:rgba(12,16,24,.7);border:1px solid var(--border);border-radius:11px;padding:3px;gap:3px}
  .modeswitch button{display:inline-flex;align-items:center;gap:6px;border:0;background:transparent;color:var(--muted);font-family:inherit;font-size:12px;font-weight:600;padding:7px 14px;border-radius:8px;cursor:pointer;transition:all .14s ease}
  .modeswitch button svg{width:14px;height:14px}
  .modeswitch button.on{background:var(--pink);color:#fff;box-shadow:0 6px 18px -8px var(--pink)}
  .modeswitch button:not(.on):hover{color:var(--text)}
  .app.grid-mode{grid-template-columns:1fr}
  .app.grid-mode .sidebar{display:none}
  .app.grid-mode .search{display:none}
  #tb-pump-only{display:inline-flex;align-items:center;gap:8px}
  .app.grid-mode #tb-pump-only{display:none}
  /* grid mode = full-bleed iframe, no chrome, fills under the top bar */
  #view-grvt{padding:0;gap:0;flex:1;min-height:0}
  #grvt-frame{width:100%;flex:1;min-height:0;border:0;display:block;background:#0b0e14}
</style>
</head>
<body>
<div class="app">
  <aside class="sidebar">
    <div class="brand"><div class="dot"></div><div><b>Amatista</b> <span>TradeOS</span></div></div>
    <div class="navlabel">Pump Reader</div>
    <nav class="nav">
      <a class="active" data-view="pump"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 12h4l3 8 4-16 3 8h4"/></svg>Overview</a>
      <a data-view="tokens"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>Tokens</a>
      <a data-view="alerts"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M6 8a6 6 0 0112 0c0 7 3 7 3 7H3s3 0 3-7"/></svg>Alerts</a>
      <a data-view="learning"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 3l9 5-9 5-9-5 9-5z"/><path d="M21 8v6"/></svg>Learning</a>
      <a data-view="trades"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 17l6-6 4 4 7-8"/></svg>Trades<span class="badge">P6</span></a>
      <a data-view="pipeline"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="5" cy="6" r="2"/><circle cx="5" cy="18" r="2"/><circle cx="19" cy="12" r="2"/><path d="M7 6h6a4 4 0 014 4M7 18h6a4 4 0 004-4"/></svg>Fase 2<span class="badge">FSM</span></a>
      <a data-view="settings"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3"/></svg>Settings</a>
    </nav>
  </aside>

  <div class="main">
    <header class="topbar">
      <div class="modeswitch">
        <button data-mode="pump" class="on"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 12h4l3 8 4-16 3 8h4"/></svg>Pump Reader</button>
        <button data-mode="grid"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 3v18h18M8 17V9M13 17V5M18 17v-6"/></svg>Grid Bot</button>
      </div>
      <div class="search">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4-4"/></svg>
        <input id="tok-search" placeholder="Buscar token por símbolo, cluster o tipo..." autocomplete="off" />
      </div>
      <div class="tb-actions">
        <span id="tb-pump-only">
          <button class="pill" id="btn-discover"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4-4"/></svg>Discover</button>
          <button class="pill" id="btn-update"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 11-3-6.7L21 8"/><path d="M21 3v5h-5"/></svg>Update</button>
          <button class="pill" id="btn-balance">Balance <b id="tb-balance" class="mono">$0.0K</b></button>
          <button class="pill" id="btn-pnl" title="Ver qué tokens ganan o pierden">PNL 7D <b id="tb-pnl" class="mono">+$0.00</b></button>
        </span>
        <span class="pill live green"><span class="ldot"></span>Live</span>
        <button class="pill icon" id="btn-theme" title="Modo claro / oscuro"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg></button>
        <a class="pill" id="link-admin" href="/admin" title="Crear y gestionar cuentas" style="text-decoration:none;display:none"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="vertical-align:-2px;margin-right:4px"><circle cx="9" cy="7" r="3"/><path d="M2 21v-1a6 6 0 016-6h2"/><circle cx="18" cy="16" r="3"/><path d="M18 13v.5M18 19v-.5M21 16h-.5M15 16h.5"/></svg>Cuentas</a>
        <a class="pill" href="/logout" style="text-decoration:none">Logout</a>
      </div>
    </header>

    <!-- ============ PUMP VIEW ============ -->
    <section class="view" id="view-pump">
      <div class="vhead">
        <div>
          <h1>Overview</h1>
          <p id="pump-sub">Real-time pump &amp; squeeze surveillance</p>
        </div>
        <div class="ts mono" id="pump-ts">—</div>
      </div>

      <div class="grid-kpi">
        <div class="card">
          <div class="klabel"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4-4"/></svg>Tokens monitored</div>
          <div class="kval mono" id="k-monitored">—</div>
          <div class="ksub" id="k-exchanges">—</div>
        </div>
        <div class="card">
          <div class="klabel"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2l2.5 7H22l-6 4.5L18 22l-6-4.5L6 22l2-8.5L2 9h7.5z"/></svg>Score max · live</div>
          <div class="kval pink mono" id="k-scoremax">—</div>
          <div class="ksub" id="k-scoremax-sub">—</div>
        </div>
        <div class="card">
          <div class="klabel"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M6 8a6 6 0 0112 0c0 7 3 7 3 7H3s3 0 3-7"/></svg>Alerts · 24h</div>
          <div class="kval mono" id="k-alerts">—</div>
          <div class="ksub" id="k-alerts-sub">—</div>
        </div>
        <div class="card">
          <div class="klabel"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 17l6-6 4 4 7-8"/></svg>Open positions</div>
          <div class="kval mono" id="k-positions">—</div>
          <div class="ksub" id="k-positions-sub">executor armed</div>
        </div>
      </div>

      <div class="grid-2">
        <div class="panel">
          <div class="phead"><span class="pt">Cluster split</span><span class="px">last 24h</span></div>
          <div class="clrow"><span class="cl"><span class="cdot green"></span>Classic</span><span class="px mono" id="cl-classic-n">0 candidates</span></div>
          <div class="ctrack"><i class="green" id="cl-classic-bar" style="width:0%"></i></div>
          <div class="clrow"><span class="cl"><span class="cdot purple"></span>Long pump</span><span class="px mono" id="cl-long-n">0 candidates</span></div>
          <div class="ctrack"><i class="purple" id="cl-long-bar" style="width:0%"></i></div>
          <div class="statgrid split">
            <div class="sbox"><div class="l">Avg</div><div class="v green mono" id="cl-classic-avg">0</div></div>
            <div class="sbox"><div class="l">Median</div><div class="v mono" id="cl-classic-med">0</div></div>
            <div class="sbox"><div class="l">Max</div><div class="v green mono" id="cl-classic-max">0</div></div>
          </div>
          <div class="statgrid split">
            <div class="sbox"><div class="l">Avg</div><div class="v mono" id="cl-long-avg">0</div></div>
            <div class="sbox"><div class="l">Median</div><div class="v mono" id="cl-long-med">0</div></div>
            <div class="sbox"><div class="l">Max</div><div class="v purple mono" id="cl-long-max">0</div></div>
          </div>
        </div>
        <div class="panel">
          <div class="phead"><span class="pt">Equity curve · 7d</span><span class="px">live</span></div>
          <div id="equity-chart"></div>
        </div>
      </div>

      <div class="grid-2b">
        <div class="panel">
          <div class="phead"><span class="pt">Candidatos pre-estallido</span><span class="px">FSM · antes del pump</span></div>
          <table>
            <thead><tr><th>Token</th><th>Estado</th><th>Acc</th><th>Pers</th><th>Rug</th><th>Conf</th></tr></thead>
            <tbody id="tbl-body"><tr><td colspan="6" class="empty">Analizando acumulación…</td></tr></tbody>
          </table>
        </div>
        <div class="panel">
          <div class="phead"><span class="pt">Latest alerts</span><span class="px">stream</span></div>
          <div id="alerts-body"><div class="empty">No alerts yet</div></div>
        </div>
      </div>
    </section>

    <!-- ============ GRVT VIEW (full-bleed embed) ============ -->
    <section class="view hidden" id="view-grvt">
      <iframe id="grvt-frame" title="GRVTBot"></iframe>
      <div class="empty" id="grvt-offline" style="display:none;text-align:left;padding:14px 18px">
        GRVTBot no responde. Inicia el proceso Node: ejecuta <b>start-grvtbot.bat</b> y recarga la página.
      </div>
    </section>

    <!-- ============ TOKENS VIEW ============ -->
    <section class="view hidden" id="view-tokens">
      <div class="vhead"><div><h1>Tokens</h1><p>Monitoreo de cada token escaneado · análisis, volumen, gráfico (clic en una fila)</p></div><div class="ts mono" id="tok-ts">—</div></div>
      <div class="panel">
        <div class="phead"><span class="pt">⚡ Live volume-acceleration watch</span><span class="px" id="vel-meta">—</span></div>
        <div id="vel-body"><div class="empty">No hot symbols being watched right now</div></div>
      </div>
      <div class="panel">
        <div class="phead"><span class="pt">All candidates</span><span class="px" id="tok-count">0</span></div>
        <table><thead><tr><th>Score</th><th>Conf</th><th>Token</th><th>Exch</th><th>Cluster</th><th>Class</th><th>&Delta;24h</th><th>Vol&times;</th><th>Liquidity</th><th>Status</th><th></th></tr></thead>
        <tbody id="tok-body"><tr><td colspan="11" class="empty">Loading…</td></tr></tbody></table>
      </div>
    </section>

    <!-- ============ ALERTS VIEW ============ -->
    <section class="view hidden" id="view-alerts">
      <div class="vhead"><div><h1>Alerts</h1><p>Candidates that crossed the confirmation threshold</p></div><div class="ts mono" id="al-ts">—</div></div>
      <div class="panel">
        <div class="phead"><span class="pt">Confirmation queue</span><span class="px" id="al-count">0</span></div>
        <div id="al-body"><div class="empty">No alerts yet</div></div>
      </div>
    </section>

    <!-- ============ LEARNING VIEW ============ -->
    <section class="view hidden" id="view-learning">
      <div class="vhead"><div><h1>Learning</h1><p id="lrn-sub">Feedback loop · did alerts fire before the pump?</p></div><div class="ts mono" id="le-ts">—</div></div>
      <div class="grid-kpi">
        <div class="card"><div class="klabel">Precision · 30d</div><div class="kval" id="lrn-prec">—</div><div class="ksub" id="lrn-prec-sub">alerts that pumped</div></div>
        <div class="card"><div class="klabel">Recall (est.)</div><div class="kval" id="lrn-rec">—</div><div class="ksub" id="lrn-rec-sub">pumps caught</div></div>
        <div class="card"><div class="klabel">Avg lead time</div><div class="kval" id="lrn-lead">—</div><div class="ksub">alert &rarr; peak</div></div>
        <div class="card"><div class="klabel">Pending proposals</div><div class="kval" id="lrn-prop-n">0</div><div class="ksub" id="lrn-prop-sub">threshold tweaks</div></div>
      </div>
      <div class="panel">
        <div class="phead"><span class="pt">Pending proposals</span><span class="px" id="lrn-prop-c">0</span></div>
        <div id="lrn-proposals"><div class="empty">The analyzer needs settled outcomes (7-day horizon) before it recommends changes. Detection-only learning starts ~7 days after deploy.</div></div>
      </div>
      <div class="grid-2" style="grid-template-columns:1fr 1fr">
        <div class="panel"><div class="phead"><span class="pt">Component contributions · classic</span><span class="px">lift ≥ outcome</span></div><div id="lrn-comp-classic"><div class="empty">—</div></div></div>
        <div class="panel"><div class="phead"><span class="pt">Component contributions · long_pump</span><span class="px">lift ≥ outcome</span></div><div id="lrn-comp-long"><div class="empty">—</div></div></div>
      </div>
      <div class="panel">
        <div class="phead"><span class="pt">Outcomes</span>
          <span style="display:flex;gap:6px"><input id="lrn-missed" placeholder="symbol e.g. BSB" style="background:var(--panel-2);border:1px solid var(--border);border-radius:7px;color:var(--text);padding:5px 9px;font-size:12px;width:140px" /><button class="btn" id="lrn-missed-btn">Report missed pump</button></span>
        </div>
        <div class="px" style="margin:-6px 0 10px">Tracks max favorable/adverse excursion (MFE/MAE) and lead time for up to 30 days.</div>
        <table><thead><tr><th>Token</th><th>Cluster</th><th>Score</th><th>Label</th><th>MFE 24h</th><th>MFE 7d</th><th>MAE 7d</th><th>Lead</th></tr></thead>
        <tbody id="lrn-body"><tr><td colspan="8" class="empty">No outcomes yet — alerts become outcomes the moment they fire</td></tr></tbody></table>
      </div>
    </section>

    <!-- ============ TRADES VIEW ============ -->
    <section class="view hidden" id="view-trades">
      <div class="vhead"><div><h1>Trades</h1><p>Auto-entry &rarr; two-phase exit (60/40) &rarr; trailing &amp; dump detector</p></div><div class="ts mono" id="tr-ts">—</div></div>
      <div class="grid-2b">
        <div class="panel">
          <div class="phead"><span class="pt">Managed positions</span><span class="px" id="mg-count">0</span></div>
          <table><thead><tr><th>Token</th><th>Entry</th><th>Last</th><th>Gain</th><th>Phase</th><th>Unreal.</th></tr></thead>
          <tbody id="mg-body"><tr><td colspan="6" class="empty">No open positions · bot auto-enters on confirmed signals</td></tr></tbody></table>
        </div>
        <div class="panel">
          <div class="phead"><span class="pt">Recent exits</span><span class="px" id="mg-thr">thr —</span></div>
          <table><thead><tr><th>Token</th><th>Reason</th><th>%</th><th>Price</th><th>PnL</th></tr></thead>
          <tbody id="mg-exits"><tr><td colspan="5" class="empty">No exits yet</td></tr></tbody></table>
        </div>
      </div>
      <div class="panel">
        <div class="phead"><span class="pt">Positions &amp; fills</span><span class="px" id="tr-count">0</span></div>
        <table><thead><tr><th>Time</th><th>Mode</th><th>Exch</th><th>Token</th><th>Side</th><th>Notional</th><th>Fill</th><th>Amount</th><th>SL</th><th>TP</th></tr></thead>
        <tbody id="tr-body"><tr><td colspan="10" class="empty">No trades yet · paper executor armed</td></tr></tbody></table>
      </div>
    </section>

    <!-- ============ FASE 2 / PIPELINE VIEW ============ -->
    <section class="view hidden" id="view-pipeline">
      <div class="vhead"><div><h1>Fase 2 · Máquina de estados</h1><p>Candidate &rarr; Watchlist &rarr; Monitor &rarr; Confirmation &rarr; Entry · detección de PREPARACIÓN</p></div><div class="ts mono" id="pl-ts">—</div></div>
      <div class="grid-2b">
        <div class="panel">
          <div class="phead"><span class="pt">Estado del pipeline</span><span class="px" id="pl-mode">—</span></div>
          <div id="pl-status"><div class="empty">cargando…</div></div>
        </div>
        <div class="panel">
          <div class="phead"><span class="pt">Decisiones recientes (Decision Log)</span><span class="px" id="pl-dec-count">0</span></div>
          <table><thead><tr><th>Hora</th><th>Token</th><th>De&rarr;A</th><th>Acción</th><th>Acc</th><th>Pers</th><th>Rug</th></tr></thead>
          <tbody id="pl-decisions"><tr><td colspan="7" class="empty">Sin decisiones todavía</td></tr></tbody></table>
        </div>
      </div>
      <div class="panel">
        <div class="phead"><span class="pt">Tablero de símbolos en la FSM</span><span class="px" id="pl-board-count">0</span></div>
        <table><thead><tr><th>Token</th><th>Exch</th><th>Estado</th><th>Accumulation</th><th>Persistence</th><th>RugRisk</th><th>Seq</th><th>Confirm</th></tr></thead>
        <tbody id="pl-board"><tr><td colspan="8" class="empty">Ningún símbolo observado aún · alimenta el scan</td></tr></tbody></table>
      </div>
    </section>

    <!-- ============ SETTINGS VIEW ============ -->
    <section class="view hidden" id="view-settings">
      <div class="vhead"><div><h1>Settings</h1><p>Engine status, risk controls &amp; capital allocation</p></div></div>
      <div class="grid-2">
        <div class="panel">
          <div class="phead"><span class="pt">Engine</span><span class="px" id="set-mode-badge">—</span></div>
          <div class="kv"><span class="k">Execution mode</span><span class="mono" id="set-mode">—</span></div>
          <div class="kv"><span class="k">Exchanges</span><span class="mono" id="set-exch">—</span></div>
          <div class="kv"><span class="k">Scan interval</span><span class="mono" id="set-interval">—</span></div>
          <div class="kv"><span class="k">Candidates</span><span class="mono" id="set-count">—</span></div>
          <div class="kv"><span class="k">Open positions</span><span class="mono" id="set-pos">—</span></div>
          <div class="kv"><span class="k">Last scan</span><span class="mono" id="set-last">—</span></div>
          <div class="kv"><span class="k">Persistence</span><span class="mono" id="set-persist">—</span></div>
          <div class="kv"><span class="k">Real account</span><span class="mono" id="set-account">—</span></div>
          <div class="empty" style="text-align:left;padding:12px 0 0">Live trading requires your exchange API keys (no withdrawal permission) and is opt-in. See note below.</div>
        </div>
        <div class="panel">
          <div class="phead"><span class="pt">Risk controls</span><span class="px">capital protection first</span></div>
          <div class="kv"><span class="k">Kill switch</span><span class="mono" id="set-kill">—</span></div>
          <div class="gactions" style="margin-top:4px">
            <button class="btn" id="set-kill-on" style="border-color:rgba(232,85,106,.4);color:var(--red)">Activate kill switch</button>
            <button class="btn" id="set-kill-off">Deactivate</button>
          </div>
          <div class="kv" style="margin-top:14px"><span class="k">Bot total (USDT)</span><span class="mono" id="set-alloc-total">—</span></div>
          <div class="kv"><span class="k">Split</span><span class="mono" id="set-alloc-split">—</span></div>
          <div class="gactions"><button class="btn primary" id="set-alloc-btn">Edit capital allocation</button></div>
        </div>
      </div>

      <div class="panel" style="margin-top:16px">
        <div class="phead"><span class="pt">Bot configuration</span><span class="px">Applies live</span></div>
        <div class="gform">
          <div class="gf"><label>Confirmation threshold</label><input id="cfg-thr" type="number" min="1" max="100" step="1" class="mono" /></div>
          <div class="gf"><label>Auto-entry (paper)</label><select id="cfg-auto"><option value="true">Enabled</option><option value="false">Disabled</option></select></div>
          <div class="gf"><label>Auto-entry size (USDT)</label><input id="cfg-size" type="number" min="1" step="10" class="mono" /></div>
          <div class="gf"><label>Velocity trigger (× volume)</label><input id="cfg-accel" class="mono" disabled /></div>
        </div>
        <div class="gactions">
          <button class="btn primary" id="cfg-save">Save configuration</button>
          <button class="btn" id="cfg-reset" style="border-color:rgba(232,85,106,.4);color:var(--red)">Reset mi bot</button>
          <span class="px" id="cfg-msg"></span>
        </div>
        <div class="empty" style="text-align:left;padding:10px 0 0">Lower the confirmation threshold to make the bot more sensitive — more alerts and paper auto-entries. 75 = strict (default). Live real-money trading still requires your API keys and explicit opt-in.</div>
      </div>

      <div class="panel" style="margin-top:16px">
        <div class="phead"><span class="pt">Telegram notifications</span><span class="px" id="tg-status">checking…</span></div>
        <div class="gform">
          <div class="gf" style="grid-column:1/-1"><label>Bot token (de @BotFather)</label><input id="tg-token" class="mono" placeholder="123456:ABC-DEF..." /></div>
          <div class="gf"><label>Group chat ID</label><input id="tg-chat" class="mono" placeholder="-1001234567890" /></div>
          <div class="gf"><label>&nbsp;</label><button class="btn" id="tg-find">Find group ID</button></div>
        </div>
        <div class="gactions">
          <button class="btn primary" id="tg-save">Save</button>
          <button class="btn" id="tg-test">Send test</button>
          <span class="px" id="tg-msg"></span>
        </div>
        <div id="tg-chats" style="margin-top:8px"></div>
        <div class="empty" style="text-align:left;padding:10px 0 0;line-height:1.7">
          <b>Conectar tu grupo:</b> 1) En Telegram abre <b>@BotFather</b> → <b>/newbot</b> → copia el <b>token</b>. 2) Crea un grupo, agrega tu bot y envía cualquier mensaje. 3) Pega el token arriba → <b>Find group ID</b> → elige tu grupo → <b>Save</b> → <b>Send test</b>.
        </div>
      </div>
    </section>
  </div>
</div>

<!-- ============ ALLOCATION MODAL ============ -->
<div class="modal-overlay hidden" id="alloc-modal">
  <div class="modal">
    <div class="mh">
      <div><h3>Capital allocation</h3><p>Bot total + per-exchange split. Position size = % of effective equity.</p></div>
      <button class="mx" id="alloc-close">&times;</button>
    </div>
    <div class="mb">
      <div class="mfield"><label>Bot total (USDT)</label><input type="number" id="alloc-total" value="1000" min="0" step="50" /></div>
      <div class="exbox" data-ex="mexc">
        <div class="top"><b>MEXC</b><span class="bal">balance <span class="mono" id="bal-mexc">$0.0K</span></span></div>
        <div class="row"><input type="range" min="0" max="100" value="100" id="r-mexc" /><span class="pct mono" id="p-mexc">100%</span></div>
        <div class="cap mono">cap <span id="cap-mexc">$0.0K</span> · in open $0.00 · <span class="ok">OK</span></div>
      </div>
      <div class="exbox" data-ex="bitget">
        <div class="top"><b>BITGET</b><span class="bal">balance <span class="mono" id="bal-bitget">$0.00</span></span></div>
        <div class="row"><input type="range" min="0" max="100" value="0" id="r-bitget" /><span class="pct mono" id="p-bitget">0%</span></div>
        <div class="cap mono">cap <span id="cap-bitget">$0.00</span> · in open $0.00 · <span class="ok">OK</span></div>
      </div>
      <div class="sumbar"><span>sum allocation: <span class="v" id="sum-val">100.0%</span></span><span id="sum-flag" class="ok">&check; valid</span></div>
    </div>
    <div class="mfoot">
      <button class="btn" id="alloc-cancel">Cancel</button>
      <button class="btn primary" id="alloc-save">Save allocation</button>
    </div>
  </div>
</div>

<!-- ============ CANDIDATE DETAIL MODAL ============ -->
<div class="modal-overlay hidden" id="cand-modal">
  <div class="modal" style="width:720px;max-width:94vw">
    <div class="mh">
      <div>
        <h3 id="cd-symbol">—</h3>
        <p id="cd-sub">—</p>
      </div>
      <button class="mx" id="cd-close">&times;</button>
    </div>
    <div class="mb">
      <div style="display:flex;gap:18px;align-items:flex-start;justify-content:space-between">
        <div style="flex:1">
          <div id="cd-chips" style="display:flex;flex-wrap:wrap;gap:6px"></div>
          <div id="cd-tags" style="display:flex;flex-wrap:wrap;gap:8px;margin-top:10px"></div>
        </div>
        <div id="cd-ring"></div>
      </div>
      <div id="cd-tabs" style="display:flex;gap:16px;border-bottom:1px solid var(--border-soft);margin:10px 0 8px"></div>
      <div id="cd-tabbody"></div>
    </div>
    <div class="mfoot" style="justify-content:space-between;flex-wrap:wrap">
      <div style="display:flex;gap:8px;flex-wrap:wrap">
        <button class="btn" id="cd-onchain">⛓ View on chain</button>
        <button class="btn" id="cd-tg">✈ Telegram threads</button>
      </div>
      <div style="display:flex;gap:8px">
        <button class="btn" id="cd-cancel">Close</button>
        <button class="btn primary" id="cd-act">Act paper · $100</button>
      </div>
    </div>
  </div>
</div>

<div class="modal-overlay hidden" id="bal-modal">
  <div class="modal" style="width:520px;max-width:94vw">
    <div class="mh">
      <div><h3>Balance</h3><p id="bal-sub">—</p></div>
      <button class="mx" id="bal-close">&times;</button>
    </div>
    <div class="mb">
      <div class="card" style="margin-bottom:14px">
        <div class="klabel">Total equity</div>
        <div class="kval" id="bal-total">$0</div>
        <div class="ksub" id="bal-source">—</div>
      </div>
      <div class="navlabel" style="padding:4px 0 8px">Holdings</div>
      <div id="bal-holdings"><div class="empty">—</div></div>
    </div>
    <div class="mfoot">
      <button class="btn" id="bal-cancel">Close</button>
      <button class="btn primary" id="bal-alloc">Edit allocation</button>
    </div>
  </div>
</div>

<div class="modal-overlay hidden" id="pnl-modal">
  <div class="modal" style="width:560px;max-width:94vw">
    <div class="mh">
      <div><h3>PnL 7 días · por token</h3><p id="pnl-sub">—</p></div>
      <button class="mx" id="pnl-close">&times;</button>
    </div>
    <div class="mb">
      <div class="statgrid split" style="margin-bottom:14px">
        <div class="sbox"><div class="l">Total 7d</div><div class="v mono" id="pnl-total">$0</div></div>
        <div class="sbox"><div class="l">Ganando</div><div class="v green mono" id="pnl-win">0</div></div>
        <div class="sbox"><div class="l">Perdiendo</div><div class="v mono" id="pnl-lose" style="color:var(--red)">0</div></div>
      </div>
      <div id="pnl-rows"><div class="empty">—</div></div>
    </div>
    <div class="mfoot">
      <button class="btn" id="pnl-cancel">Cerrar</button>
    </div>
  </div>
</div>

<script>
const $ = (id) => document.getElementById(id);
const fmtK = (n) => "$" + (Number(n)/1000).toFixed(1) + "K";
const clusterColor = (c) => c === "classic" ? "var(--green)" : "var(--purple)";
// Text formatting: Title Case for clusters/classifications/statuses, UPPER for
// exchange ids (display only — the raw id is still used for API calls).
const tcase = (s) => String(s||"").replace(/_/g," ").replace(/\b\w/g, m => m.toUpperCase());
const upx = (s) => String(s||"").toUpperCase();

// ---- nav view switching ----
let activeView = "pump";
document.querySelectorAll(".nav a[data-view]").forEach(a => {
  a.addEventListener("click", () => {
    document.querySelectorAll(".nav a").forEach(x => x.classList.remove("active"));
    a.classList.add("active");
    document.querySelectorAll(".view").forEach(v => v.classList.add("hidden"));
    activeView = a.dataset.view;
    $("view-" + activeView).classList.remove("hidden");
    const loaders = {pump:loadOverview, tokens:loadTokens, alerts:loadAlerts, learning:loadLearning, trades:loadTrades, pipeline:loadPipeline, settings:loadSettings};
    if (loaders[activeView]) loaders[activeView]();
  });
});

// ---- top-level mode switch: Pump Reader <-> Grid Bot (same app) ----
let appMode = "pump";
function setMode(m){
  appMode = m;
  document.querySelector(".app").classList.toggle("grid-mode", m === "grid");
  document.querySelectorAll(".modeswitch button").forEach(b => b.classList.toggle("on", b.dataset.mode === m));
  document.querySelectorAll(".view").forEach(v => v.classList.add("hidden"));
  if (m === "grid"){
    $("view-grvt").classList.remove("hidden");
    loadGrvt();
  } else {
    $("view-" + activeView).classList.remove("hidden");
    const loaders = {pump:loadOverview, tokens:loadTokens, alerts:loadAlerts, learning:loadLearning, trades:loadTrades, pipeline:loadPipeline, settings:loadSettings};
    if (loaders[activeView]) loaders[activeView]();
  }
}
document.querySelectorAll(".modeswitch button").forEach(b => b.addEventListener("click", () => setMode(b.dataset.mode)));

// ---- inline SVG charts ----
function sparkSvg(vals){
  if(!vals || vals.length < 2) return '<span class="px">—</span>';
  const w=64,h=22,min=Math.min(...vals),max=Math.max(...vals),rng=(max-min)||1;
  const pts=vals.map((v,i)=>`${(i/(vals.length-1)*w).toFixed(1)},${(h-((v-min)/rng)*(h-4)-2).toFixed(1)}`).join(" ");
  const up=vals[vals.length-1]>=vals[0];
  const col=up?"var(--green)":"var(--red)";
  return `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}"><polyline points="${pts}" fill="none" stroke="${col}" stroke-width="1.5" stroke-linejoin="round"/></svg>`;
}
const EQ_H=170, EQ_PAD=8;
function equitySvg(curve){
  const w=600,h=EQ_H,pad=EQ_PAD;
  const vals=(curve&&curve.length?curve:[{v:0},{v:0}]).map(p=>Number(p.v));
  const min=Math.min(...vals),max=Math.max(...vals),rng=(max-min)||Math.max(max,1);
  const n=vals.length;
  const x=i=>n<2?w/2:(i/(n-1))*(w-pad*2)+pad;
  const y=v=>h-pad-((v-min)/rng)*(h-pad*2);
  let line=vals.map((v,i)=>`${i?'L':'M'}${x(i).toFixed(1)} ${y(v).toFixed(1)}`).join(" ");
  if(n<2){const yy=y(vals[0]).toFixed(1);line=`M${pad} ${yy} L${w-pad} ${yy}`;}
  const area=`${line} L${w-pad} ${h-pad} L${pad} ${h-pad} Z`;
  const up=vals[vals.length-1]>=vals[0];
  const col=up?"var(--green)":"var(--red)";
  return `<div class="eqwrap" id="eqwrap" style="height:${h}px">
    <svg width="100%" height="${h}" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
      <defs><linearGradient id="eg" x1="0" x2="0" y1="0" y2="1">
        <stop offset="0" stop-color="rgba(47,208,138,.28)"/><stop offset="1" stop-color="rgba(47,208,138,0)"/>
      </linearGradient></defs>
      <path d="${area}" fill="url(#eg)"/>
      <path d="${line}" fill="none" stroke="${col}" stroke-width="1.8"/>
    </svg>
    <div class="eqline" id="eqline" style="height:${h}px"></div>
    <div class="eqdot" id="eqdot"></div>
    <div class="eqtip" id="eqtip"></div>
  </div>
  <div class="px mono" style="margin-top:6px">${fmtK(max)}</div>`;
}
// Hover tooltip: shows the $value at the cursor + the gain/loss vs start and vs
// the previous point, so you see exactly where the money moved.
function wireEquityHover(curve){
  const wrap=$("eqwrap"); if(!wrap) return;
  const tip=$("eqtip"),dot=$("eqdot"),gl=$("eqline");
  const vals=(curve&&curve.length?curve:[]); if(vals.length<2) return;
  const nums=vals.map(p=>Number(p.v));
  const min=Math.min(...nums),max=Math.max(...nums),rng=(max-min)||Math.max(max,1);
  const n=vals.length,h=EQ_H,pad=EQ_PAD,start=nums[0];
  wrap.onmousemove=(ev)=>{
    const r=wrap.getBoundingClientRect();
    let fx=(ev.clientX-r.left)/r.width; fx=Math.max(0,Math.min(1,fx));
    const i=Math.round(fx*(n-1)), v=nums[i];
    const px=(i/(n-1))*r.width, py=h-pad-((v-min)/rng)*(h-pad*2);
    dot.style.left=px+"px"; dot.style.top=py+"px"; dot.style.opacity=1;
    gl.style.left=px+"px"; gl.style.opacity=.45;
    // keep the tooltip fully inside the panel: clamp horizontally, and flip it
    // BELOW the point when near the top so it never gets clipped ("se come").
    const tw=tip.offsetWidth||130, th=tip.offsetHeight||52;
    const lx=Math.max(tw/2+4, Math.min(r.width-tw/2-4, px));
    const above = py > th+8;
    tip.style.left=lx+"px"; tip.style.top=py+"px";
    tip.style.transform = above ? "translate(-50%,calc(-100% - 12px))" : "translate(-50%,16px)";
    tip.style.opacity=1;
    const dStart=v-start, dPrev=i>0?v-nums[i-1]:0;
    const dt=vals[i].t?new Date(vals[i].t):null;
    const dlab=dt?dt.toLocaleString([],{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}):("punto "+(i+1));
    const cS=dStart>=0?"var(--green)":"var(--red)", cP=dPrev>=0?"var(--green)":"var(--red)";
    tip.innerHTML=`<div class="d">${dlab}</div><div class="v">$${v.toFixed(2)}</div>`+
      `<div class="dl" style="color:${cS}">${dStart>=0?'▲ +':'▼ '}${dStart.toFixed(2)} vs inicio</div>`+
      `<div class="dl" style="color:${cP}">${dPrev>=0?'+':''}${dPrev.toFixed(2)} vs anterior</div>`;
  };
  wrap.onmouseleave=()=>{tip.style.opacity=0;dot.style.opacity=0;gl.style.opacity=0;};
}

// ---- render overview ----
async function loadOverview(){
  let d; try{ d=await (await fetch("/overview")).json(); }catch(e){ return; }
  $("pump-sub").textContent = `Real-time pump & squeeze surveillance · ${d.monitored} tokens monitored`;
  $("pump-ts").textContent = new Date(d.now).toLocaleString();
  $("k-monitored").textContent = d.monitored;
  $("k-exchanges").textContent = "across " + (d.exchanges||[]).join(" · ");
  if(d.score_max){ $("k-scoremax").textContent = d.score_max.value.toFixed(2);
    $("k-scoremax-sub").textContent = `${d.score_max.symbol} · ${tcase(d.score_max.cluster)}`; }
  $("k-alerts").textContent = d.alerts_24h.total;
  $("k-alerts-sub").textContent = `${d.alerts_24h.classic} classic · ${d.alerts_24h.long_pump} long_pump`;
  $("k-positions").textContent = d.open_positions;
  $("k-positions-sub").textContent = (d.open_positions===0?"no positions open · ":"") + "executor armed (" + d.exec_mode + ")";
  $("tb-balance").textContent = fmtK(d.balance);
  const pnlEl=$("tb-pnl"); pnlEl.textContent=(d.pnl_7d>=0?"+":"-")+"$"+Math.abs(d.pnl_7d).toFixed(2);
  pnlEl.style.color=d.pnl_7d>0?"var(--green)":d.pnl_7d<0?"var(--red)":"";

  const cs=d.cluster_split.classic, lp=d.cluster_split.long_pump, tot=(cs.count+lp.count)||1;
  $("cl-classic-n").textContent = cs.count + " candidates";
  $("cl-long-n").textContent = lp.count + " candidates";
  $("cl-classic-bar").style.width = (cs.count/tot*100)+"%";
  $("cl-long-bar").style.width = (lp.count/tot*100)+"%";
  $("cl-classic-avg").textContent=cs.avg.toFixed(1); $("cl-classic-med").textContent=cs.median.toFixed(2); $("cl-classic-max").textContent=cs.max.toFixed(2);
  $("cl-long-avg").textContent=lp.avg.toFixed(1); $("cl-long-med").textContent=lp.median.toFixed(2); $("cl-long-max").textContent=lp.max.toFixed(2);

  $("equity-chart").innerHTML = equitySvg(d.equity_curve);
  wireEquityHover(d.equity_curve);

  const PP_STATE={entry:"var(--green)",confirmation:"var(--pink)",monitor:"var(--amber)",watchlist:"var(--muted)"};
  const PP_LBL={entry:"ENTRY · comprado",confirmation:"confirmando",monitor:"analizando",watchlist:"en cola"};
  $("tbl-body").innerHTML = (d.prepump||[]).length ? d.prepump.map(r=>{
    const col=PP_STATE[r.state]||"var(--muted)";
    return `<tr style="cursor:pointer" onclick="openCandidate('${r.symbol}','${r.exchange}')">
      <td><span class="sym">${r.symbol}</span> <span class="px">${upx(r.exchange)}</span></td>
      <td><span class="px" style="color:${col};font-weight:600">${PP_LBL[r.state]||r.state}</span></td>`
      +plScoreCell(r.acc,false)+plScoreCell(r.pers,false)+plScoreCell(r.rug,true)
      +`<td class="mono">${r.confirm_count||0}</td>
    </tr>`;
  }).join("") : `<tr><td colspan="6" class="empty">Sin candidatos en análisis · alimentando el detector…</td></tr>`;

  $("alerts-body").innerHTML = (d.latest_alerts||[]).length ? d.latest_alerts.map(a=>{
    return `<div class="alert" style="cursor:pointer" onclick="openCandidate('${a.symbol}','')">
      <span class="scoreb mono" style="color:var(--pink);background:rgba(255,47,110,.14)">${a.score}</span>
      <div class="meta"><div class="top"><b>${a.symbol}</b><span class="tag"><span class="cdot" style="background:${clusterColor(a.cluster)}"></span>${tcase(a.cluster)}</span></div>
      <div class="sub">ScamPump candidate: ${a.symbol}</div></div>
      <span class="ago mono">${a.ago}</span>
    </div>`;
  }).join("") : `<div class="empty">No alerts yet</div>`;
}

// ---- grvt ----
let grvtFormInit=false;
function money(n){return (n<0?"-$":"$")+Math.abs(Number(n)).toLocaleString(undefined,{maximumFractionDigits:2});}
// Compact, locale-proof money: $14.8K / $14.9M / $80.9M (avoids ambiguous separators).
function moneyC(n){n=Number(n)||0;const a=Math.abs(n),s=n<0?"-":"";
  if(a>=1e9)return s+"$"+(a/1e9).toFixed(2)+"B";
  if(a>=1e6)return s+"$"+(a/1e6).toFixed(2)+"M";
  if(a>=1e3)return s+"$"+(a/1e3).toFixed(1)+"K";
  return s+"$"+a.toFixed(a<1?6:2);}
async function loadGrvt(){
  // The real GRVTBot renders itself inside the iframe (#grvt-frame), served
  // same-origin through the /grid/* reverse proxy. Nothing to render here — we
  // only probe the Node backend once so we can show a friendly note if it's
  // not running (otherwise the iframe just shows a blank/error page).
  const off=$("grvt-offline"), fr=$("grvt-frame");
  try{
    const r=await fetch("/grid/api/health",{cache:"no-store"});
    if(!r.ok) throw 0;
    // Single sign-on: the TradeOS login is the ONLY login. Mint a GRVT session
    // on the backend (owner creds never touch the browser) and stash the JWT
    // where the embedded SPA reads it (same-origin localStorage). We store the
    // token FIRST, then (re)load the iframe with a cache-buster so the SPA always
    // boots fresh with the token present — no GRVT login ever shows.
    const s=await fetch("/grid-sso",{cache:"no-store"});
    const j=await s.json().catch(()=>null);
    if(j && j.ok && j.token){ localStorage.setItem(j.key||"grvt-grid-token", j.token); }
    if(off) off.style.display="none";
    if(fr){
      fr.style.display="block";
      const t=document.documentElement.getAttribute("data-theme")==="light"?"light":"dark";
      fr.onload=()=>postGridTheme();           // sync theme once the SPA boots
      fr.src="/grid/dashboard/?theme="+t+"&sso="+Date.now();
    }
  }catch(e){
    if(off) off.style.display="block";
    if(fr) fr.style.display="none";
  }
}

// ---- update button ----
$("btn-update").addEventListener("click", async ()=>{
  $("btn-update").textContent="Updating…";
  try{ await fetch("/scan",{method:"POST"}); await loadOverview(); }catch(e){}
  $("btn-update").innerHTML='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 11-3-6.7L21 8"/><path d="M21 3v5h-5"/></svg>Update';
});
$("btn-discover").addEventListener("click", ()=>$("btn-update").click());

// ---- allocation modal ----
async function openAlloc(){
  $("alloc-modal").classList.remove("hidden");
  try{
    const a=await (await fetch("/allocation")).json();
    $("alloc-total").value=a.bot_total_usdt;
    $("r-mexc").value=a.splits.mexc??100; $("r-bitget").value=a.splits.bitget??0;
  }catch(e){}
  syncAlloc();
}
function syncAlloc(){
  const total=Number($("alloc-total").value)||0;
  const m=Number($("r-mexc").value), b=Number($("r-bitget").value);
  $("p-mexc").textContent=m+"%"; $("p-bitget").textContent=b+"%";
  $("bal-mexc").textContent=fmtK(total*m/100); $("cap-mexc").textContent=fmtK(total*m/100);
  $("bal-bitget").textContent="$"+(total*b/100).toFixed(2); $("cap-bitget").textContent="$"+(total*b/100).toFixed(2);
  const sum=m+b;
  $("sum-val").textContent=sum.toFixed(1)+"%";
  const ok=Math.abs(sum-100)<0.01;
  $("sum-flag").textContent=ok?"✓ valid":"✗ must equal 100%";
  $("sum-flag").className=ok?"ok":"bad";
  $("alloc-save").disabled=!ok;
}
// Linked sliders: the two splits always sum to 100, so it is impossible to set >100%.
$("r-mexc").addEventListener("input",()=>{ $("r-bitget").value = 100 - Number($("r-mexc").value); syncAlloc(); });
$("r-bitget").addEventListener("input",()=>{ $("r-mexc").value = 100 - Number($("r-bitget").value); syncAlloc(); });
$("alloc-total").addEventListener("input", syncAlloc);
async function openBalance(){
  $("bal-modal").classList.remove("hidden");
  $("bal-sub").textContent="loading…"; $("bal-holdings").innerHTML='<div class="empty">loading…</div>';
  let a; try{ a=await (await fetch("/account")).json(); }catch(e){ $("bal-sub").textContent="error"; return; }
  $("bal-total").textContent=money(a.total_usdt);
  const live=a.has_keys;
  $("bal-source").textContent = live ? `Live account · ${(a.connected||[]).map(upx).join(", ")}` : "Paper balance · no exchange keys set";
  $("bal-sub").textContent = live ? "Real read-only balance" : "Add read-only spot keys (no withdrawal) to see your real balance";
  const snaps=a.snapshots||[];
  let rows="";
  snaps.forEach(s=>{
    const vals=s.values_usdt||{};
    rows+=`<div class="px" style="margin:6px 0 2px;color:var(--muted-2)">${upx(s.exchange)} · ${money(s.total_usdt)}</div>`;
    rows+=Object.keys(vals).map(k=>`<div style="display:flex;justify-content:space-between;gap:10px;font-size:12px;padding:2px 0">
        <span class="mono">${k}</span><span class="mono px">${(s.balances||{})[k]??""}</span><span class="mono" style="color:var(--green)">${money(vals[k])}</span></div>`).join("");
  });
  $("bal-holdings").innerHTML = rows || (live?'<div class="empty">No holdings</div>':`<div class="empty">Paper mode — ${money(a.total_usdt)} virtual. ${a.note||""}</div>`);
}
$("btn-balance").addEventListener("click",openBalance);
$("bal-close").addEventListener("click",()=>$("bal-modal").classList.add("hidden"));
$("bal-cancel").addEventListener("click",()=>$("bal-modal").classList.add("hidden"));
$("bal-modal").addEventListener("click",(e)=>{if(e.target.id==="bal-modal")$("bal-modal").classList.add("hidden")});
$("bal-alloc").addEventListener("click",()=>{$("bal-modal").classList.add("hidden");openAlloc();});
$("alloc-close").addEventListener("click",()=>$("alloc-modal").classList.add("hidden"));
$("alloc-cancel").addEventListener("click",()=>$("alloc-modal").classList.add("hidden"));
$("alloc-modal").addEventListener("click",(e)=>{if(e.target.id==="alloc-modal")$("alloc-modal").classList.add("hidden")});
$("alloc-save").addEventListener("click", async ()=>{
  const body={bot_total_usdt:Number($("alloc-total").value)||0,splits:{mexc:Number($("r-mexc").value),bitget:Number($("r-bitget").value)}};
  try{ const r=await fetch("/allocation",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
    if(r.ok){$("alloc-modal").classList.add("hidden"); loadOverview();} }catch(e){}
});

// ---- tokens / alerts / learning / trades / settings ----
function scoreBadge(s){
  const c=s>=70?"var(--pink)":s>=40?"var(--amber)":"var(--muted)";
  const bg=s>=70?"rgba(255,47,110,.14)":s>=40?"rgba(230,162,60,.14)":"rgba(255,255,255,.05)";
  return `<span class="scoreb mono" style="color:${c};background:${bg}">${s}</span>`;
}
let TOKENS=[];
function renderTokens(list){
  $("tok-body").innerHTML = list.length ? list.map(t=>{
    const up=t.price_change_pct_24h>=0;
    return `<tr style="cursor:pointer" onclick="openCandidate('${t.symbol}','${t.exchange}')"><td>${scoreBadge(t.pump_score)}</td><td class="mono px">${t.confidence_score}</td>
      <td><span class="sym">${t.symbol}</span></td><td class="px">${upx(t.exchange)}</td>
      <td><span class="tag"><span class="cdot" style="background:${clusterColor(t.cluster)}"></span>${tcase(t.cluster)}</span></td>
      <td class="px">${tcase(t.classification)}</td>
      <td class="mono delta ${up?'up':'down'}">${up?'+':''}${t.price_change_pct_24h}%</td>
      <td class="mono">${t.volume_spike}x</td><td class="mono">${moneyC(t.liquidity_usd)}</td>
      <td class="px"><span class="stat ${t.status==='waiting_confirmation'?'sw':''}">${tcase(t.status)}</span></td>
      <td><button class="btn" style="padding:4px 10px" onclick="event.stopPropagation();actToken('${t.symbol}','${t.exchange}',this)">Act</button></td></tr>`;
  }).join("") : `<tr><td colspan="11" class="empty">Sin coincidencias · prueba otro término o corre Update</td></tr>`;
}
function filterTokens(){
  const q=(($("tok-search")||{}).value||"").trim().toLowerCase();
  const list = !q ? TOKENS : TOKENS.filter(t =>
    (t.symbol||"").toLowerCase().includes(q) ||
    (t.cluster||"").toLowerCase().includes(q) ||
    (t.classification||"").toLowerCase().includes(q) ||
    (t.exchange||"").toLowerCase().includes(q));
  $("tok-count").textContent = q ? `${list.length} / ${TOKENS.length} tokens` : `${TOKENS.length} tokens`;
  renderTokens(list);
}
async function loadTokens(){
  let c; try{ c=await (await fetch("/candidates")).json(); }catch(e){ return; }
  TOKENS=c; $("tok-ts").textContent=new Date().toLocaleTimeString();
  filterTokens();   // render with the current search box applied (survives polling)
  loadVelocity();
}
async function loadVelocity(){
  let v; try{ v=await (await fetch("/velocity")).json(); }catch(e){ return; }
  const w=v.watching||[];
  $("vel-meta").textContent = `Fire ≥ ${v.accel_factor}x · ${w.length} watched`;
  $("vel-body").innerHTML = w.length ? w.map(x=>{
    const hot=Number(x.last_accel)>=Number(v.accel_factor);
    return `<div class="alert">
      <span class="badge" style="background:${hot?'#3a1414':'#16241a'};color:${hot?'#ff6b6b':'#7CFFB2'}">${x.last_accel}x</span>
      <div class="meta"><div class="top"><b>${x.symbol}</b><span class="px">${upx(x.exchange)}</span>
        ${hot?'<span class="tag" style="color:#ff6b6b">Accelerating</span>':'<span class="tag">Priming</span>'}</div>
        <div class="sub">Baseline vol ${x.baseline_vol} · ${x.primed?'Primed':'Warming up'}</div></div>
      <span class="ago mono">${hot?'TRIGGER':'Watch'}</span></div>`;
  }).join("") : `<div class="empty">No hot symbols (score ≥ watch min) right now</div>`;
}
async function actToken(sym, exch, btn){
  if(btn){ btn.disabled=true; btn.textContent="…"; }
  try{
    const r=await fetch(`/act?symbol=${encodeURIComponent(sym)}&exchange=${encodeURIComponent(exch)}&capital_usd=100`,{method:"POST"});
    const j=await r.json().catch(()=>null);
    let ok=false, msg="no action";
    if(j && j.fills && j.fills.length){ ok=true; msg="✓ bought (paper)"; }
    else if(j && j.rejected && j.rejected.length){ msg="rejected: "+String(j.rejected[0]).split(":").pop().trim(); }
    else if(!r.ok){ msg = (j && typeof j.detail==="string") ? j.detail : "error"; }
    if(ok){ if(typeof loadOverview==="function") loadOverview(); if(typeof loadTrades==="function") loadTrades(); }
    if(btn){ btn.textContent=msg; setTimeout(()=>{ btn.disabled=false; btn.textContent="Act $100"; }, 2600); }
    return {ok, msg};
  }catch(e){ if(btn){ btn.textContent="err"; btn.disabled=false; } return {ok:false, msg:"err"}; }
}
async function loadAlerts(){
  let c; try{ c=await (await fetch("/candidates")).json(); }catch(e){ return; }
  const al=c.filter(t=>t.status==="waiting_confirmation");
  $("al-ts").textContent=new Date().toLocaleTimeString(); $("al-count").textContent=al.length+" active";
  $("al-body").innerHTML = al.length ? al.map(a=>`
    <div class="alert" style="cursor:pointer" onclick="openCandidate('${a.symbol}','${a.exchange}')">${scoreBadge(a.pump_score)}
      <div class="meta"><div class="top"><b>${a.symbol}</b><span class="px">${upx(a.exchange)}</span>
        <span class="tag"><span class="cdot" style="background:${clusterColor(a.cluster)}"></span>${tcase(a.cluster)}</span></div>
        <div class="sub">ScamPump candidate: ${a.symbol} · ${(a.flags||[]).map(tcase).join(", ")||"no flags"}</div></div>
      <span class="ago mono">${tcase(a.classification)}</span></div>`).join("")
    : `<div class="empty">No candidates above the confirmation threshold right now</div>`;
}
function leadFmt(secs){ if(secs==null) return "—"; const m=secs/60; if(m<60) return Math.round(m)+"m"; if(m<1440) return (m/60).toFixed(1)+"h"; return (m/1440).toFixed(1)+"d"; }
function pctCell(v){ if(v==null) return '<td class="px">—</td>'; const up=v>=0; return `<td class="mono delta ${up?'up':'down'}">${up?'+':''}${v}%</td>`; }
function labelPill(lab){ const m={confirmed_pump:["#15301f","#7CFFB2"],no_pump:["#2a1414","#ff8a8a"],pending:["var(--inset)","var(--muted)"],missed:["#33240a","#f6c177"]}; const c=m[lab]||m.pending; return `<span class="badge" style="background:${c[0]};color:${c[1]}">${tcase(lab)}</span>`; }
function compHtml(c){
  if(!c) return '<div class="empty">—</div>';
  if(!c.ready) return `<div class="empty">Not enough samples yet (have ${c.have||0}, need ${c.need}).</div>`;
  return (c.contrib||[]).map(x=>{ const up=x.lift>=0; return `<div style="display:flex;align-items:center;gap:10px;margin:7px 0">
    <span style="width:150px;font-size:12px;color:var(--muted-2)">${tcase(x.signal)}</span>
    <div style="flex:1;height:6px;border-radius:4px;background:var(--inset);overflow:hidden"><i style="display:block;height:100%;width:${Math.min(Math.abs(x.lift)*100,100)}%;background:${up?'var(--green)':'var(--red)'}"></i></div>
    <span class="mono" style="width:60px;text-align:right;color:${up?'var(--green)':'var(--red)'}">${up?'+':''}${x.lift}</span></div>`; }).join("") || '<div class="empty">—</div>';
}
async function loadLearning(){
  let d; try{ d=await (await fetch("/learning")).json(); }catch(e){ return; }
  $("le-ts").textContent=new Date().toLocaleTimeString();
  $("lrn-sub").textContent=`Feedback loop · ${d.window_days}d window · ${d.n_alerts} alerts · ${d.n_settled} settled outcomes`;
  $("lrn-prec").textContent = d.precision==null?"—":Math.round(d.precision*100)+"%";
  $("lrn-prec-sub").textContent = `${d.n_settled} alerts evaluated`;
  $("lrn-rec").textContent = d.recall==null?"—":Math.round(d.recall*100)+"%";
  $("lrn-rec-sub").textContent = `${d.n_missed} missed reported`;
  $("lrn-lead").textContent = leadFmt(d.avg_lead_secs);
  const props=d.proposals||[];
  $("lrn-prop-n").textContent=props.length; $("lrn-prop-c").textContent=props.length;
  $("lrn-prop-sub").textContent = props.length?"action suggested":"needs settled outcomes (7d)";
  $("lrn-proposals").innerHTML = props.length
    ? props.map(p=>`<div class="alert"><span class="badge" style="background:#1a2030">${tcase(p.kind)}</span><div class="meta"><div class="sub" style="color:var(--text)">${p.text}</div></div></div>`).join("")
    : `<div class="empty">The analyzer needs settled outcomes (7-day horizon) before it recommends changes. Detection-only learning starts ~7 days after deploy.</div>`;
  $("lrn-comp-classic").innerHTML=compHtml((d.components||{}).classic);
  $("lrn-comp-long").innerHTML=compHtml((d.components||{}).long_pump);
  const t=d.table||[];
  $("lrn-body").innerHTML = t.length ? t.map(r=>`
    <tr><td class="sym">${r.symbol} <span class="px">${upx(r.exchange)}</span></td>
    <td><span class="tag"><span class="cdot" style="background:${clusterColor(r.cluster)}"></span>${tcase(r.cluster)}</span></td>
    <td>${r.source==='missed'?'<span class="px">—</span>':scoreBadge(r.pump_score)}</td>
    <td>${labelPill(r.label)}</td>${pctCell(r.mfe_24h)}${pctCell(r.mfe_7d)}${pctCell(r.mae_7d)}
    <td class="mono px">${r.lead_mins==null?'—':leadFmt(r.lead_mins*60)}</td></tr>`).join("")
    : `<tr><td colspan="8" class="empty">No outcomes yet — alerts become outcomes the moment they fire</td></tr>`;
}
async function reportMissed(){
  const s=$("lrn-missed").value.trim(); if(!s) return;
  $("lrn-missed-btn").textContent="…";
  try{ await fetch("/learning/missed",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({symbol:s})}); $("lrn-missed").value=""; $("lrn-missed-btn").textContent="Reported ✓"; loadLearning(); }
  catch(e){ $("lrn-missed-btn").textContent="Error"; }
  setTimeout(()=>{$("lrn-missed-btn").textContent="Report missed pump";},1500);
}
async function loadTrades(){
  let p, m; try{ p=await (await fetch("/positions")).json(); m=await (await fetch("/managed")).json(); }catch(e){ return; }
  // managed open positions
  $("mg-count").textContent=(m.open||[]).length+" open";
  $("mg-thr").textContent="thr "+m.adaptive_threshold;
  $("mg-body").innerHTML=(m.open||[]).length ? m.open.map(o=>{
    const up=o.gain_pct>=0;
    return `<tr><td class="sym">${o.symbol} <span class="px">${upx(o.exchange)}</span></td><td class="mono px">${o.entry_price}</td>
      <td class="mono">${o.last_price}</td><td class="mono delta ${up?'up':'down'}">${up?'+':''}${o.gain_pct}%</td>
      <td><span class="px">phase ${o.phase}</span></td>
      <td class="mono" style="color:${o.unrealized_pnl>=0?'var(--green)':'var(--red)'}">${o.unrealized_pnl>=0?'+':''}${o.unrealized_pnl}</td></tr>`;
  }).join("") : `<tr><td colspan="6" class="empty">No open positions · bot auto-enters on confirmed signals</td></tr>`;
  $("mg-exits").innerHTML=(m.exits||[]).length ? m.exits.map(e=>
    `<tr><td class="sym">${e.symbol}</td><td><span class="px">${e.reason}</span></td><td class="mono">${Math.round(e.fraction*100)}%</td>
     <td class="mono">${e.price}</td><td class="mono" style="color:${e.pnl>=0?'var(--green)':'var(--red)'}">${e.pnl>=0?'+':''}${e.pnl}</td></tr>`
  ).join("") : `<tr><td colspan="5" class="empty">No exits yet</td></tr>`;
  $("tr-ts").textContent=new Date().toLocaleTimeString(); $("tr-count").textContent=p.length+" fills";
  $("tr-body").innerHTML = p.length ? p.slice().reverse().map(f=>`
    <tr><td class="mono px">${new Date(f.created_at).toLocaleTimeString()}</td><td class="mono">${f.mode}</td>
    <td class="px">${upx(f.exchange)}</td><td class="sym">${f.symbol}</td>
    <td style="color:${f.side==='buy'?'var(--green)':'var(--red)'};font-weight:600">${f.side}</td>
    <td class="mono">${money(f.notional_usd)}</td><td class="mono">${f.fill_price}</td><td class="mono">${f.amount}</td>
    <td class="mono px">${f.stop_loss}</td><td class="mono px">${f.take_profit}</td></tr>`).join("")
    : `<tr><td colspan="10" class="empty">No trades yet · paper executor armed</td></tr>`;
}
function plScoreCell(v, invert){
  const n=Number(v)||0; const good = invert ? n<=40 : n>=55;
  const col = invert ? (n>=55?'var(--red)':(n>=40?'var(--amber,#b56a00)':'var(--green)'))
                     : (n>=60?'var(--green)':(n>=40?'var(--amber,#b56a00)':'var(--muted-2)'));
  return `<td class="mono" style="color:${col};font-weight:600">${n}</td>`;
}
async function loadPipeline(){
  let st,b,d;
  try{ st=await (await fetch("/pipeline/status")).json(); b=await (await fetch("/pipeline/board")).json(); d=await (await fetch("/pipeline/decisions")).json(); }
  catch(e){ return; }
  if(st.enabled===false){ $("pl-status").innerHTML='<div class="empty">Pipeline no iniciado</div>'; return; }
  const thr=st.thresholds||{}; const stc=st.states||{};
  $("pl-mode").textContent=(st.mode||"—").toUpperCase();
  $("pl-status").innerHTML=
    `<div class="pnlrow"><span>Modo</span><b>${st.mode==='enforcing'?'ENFORCING · gobierna entradas':'SHADOW · solo observa'}</b></div>`
   +`<div class="pnlrow"><span>Ventana de scoring</span><b class="mono">${st.window_min} min</b></div>`
   +`<div class="pnlrow"><span>Umbrales</span><b class="mono">Acc≥${thr.acc_min} · Pers≥${thr.pers_min} · Rug≤${thr.rug_max} · ${thr.confirm_ticks} ticks</b></div>`
   +`<div class="pnlrow"><span>Por estado</span><b class="mono">`+Object.entries(stc).map(([k,v])=>`${k}:${v}`).join(" · ")+`</b></div>`;
  const dec=(d.rows||[]); $("pl-dec-count").textContent=dec.length;
  $("pl-decisions").innerHTML=dec.length ? dec.map(r=>
    `<tr><td class="mono px">${new Date(r.ts_ms).toLocaleTimeString()}</td><td class="sym">${r.symbol} <span class="px">${upx(r.exchange)}</span></td>
     <td class="px">${r.from_state}&rarr;${r.to_state}</td><td><span class="px">${r.action}</span></td>
     <td class="mono">${r.acc==null?'—':r.acc}</td><td class="mono">${r.pers==null?'—':r.pers}</td><td class="mono">${r.rug==null?'—':r.rug}</td></tr>`
  ).join("") : `<tr><td colspan="7" class="empty">Sin decisiones todavía</td></tr>`;
  const rows=(b.rows||[]); $("pl-board-count").textContent=rows.length+" símbolos";
  $("pl-board").innerHTML=rows.length ? rows.map(r=>
    `<tr><td class="sym">${r.symbol}</td><td class="px">${upx(r.exchange)}</td><td><span class="px">${r.state}</span></td>`
    +plScoreCell(r.acc,false)+plScoreCell(r.pers,false)+plScoreCell(r.rug,true)
    +`<td class="mono px">${r.seq}</td><td class="mono">${r.confirm_count||0}</td></tr>`
  ).join("") : `<tr><td colspan="8" class="empty">Ningún símbolo observado aún · alimenta el scan</td></tr>`;
  $("pl-ts").textContent=new Date().toLocaleTimeString();
}
async function loadSettings(){
  let s,a; try{ s=await (await fetch("/status")).json(); a=await (await fetch("/allocation")).json(); }catch(e){ return; }
  $("set-mode").textContent=s.exec_mode;
  $("set-mode-badge").innerHTML = s.exec_mode==="live" ? '<span class="statusbadge" style="background:rgba(232,85,106,.12);color:var(--red);border-color:rgba(232,85,106,.3)">LIVE · real money</span>' : '<span class="statusbadge run">paper</span>';
  $("set-exch").textContent=(s.exchanges||[]).join(", ");
  $("set-interval").textContent=Math.round(s.scan_interval_seconds/60)+" min";
  $("set-count").textContent=s.candidate_count;
  $("set-pos").textContent=s.open_positions;
  $("set-last").textContent=s.last_scan_at?new Date(s.last_scan_at).toLocaleTimeString():"—";
  $("set-kill").innerHTML=s.kill_switch_active?'<span style="color:var(--red)">ACTIVE</span>':'<span style="color:var(--green)">off</span>';
  $("set-alloc-total").textContent=money(a.bot_total_usdt);
  $("set-alloc-split").textContent=Object.entries(a.splits).map(([k,v])=>`${k} ${v}%`).join(" · ");
  $("set-persist").innerHTML = s.persistence==="supabase"
    ? '<span style="color:var(--green)">Supabase</span>' : '<span style="color:var(--muted)">in-memory</span>';
  try{
    const acct=await (await fetch("/account")).json();
    $("set-account").innerHTML = acct.has_keys
      ? `<span style="color:var(--green)">${acct.connected.map(upx).join(", ")} · ${money(acct.total_usdt)}</span>`
      : '<span style="color:var(--muted)">Paper (no keys)</span>';
  }catch(e){ $("set-account").textContent="—"; }
  try{
    const cfg=await (await fetch("/settings")).json();
    $("cfg-thr").value=cfg.confirmation_threshold;
    // Confirmation threshold is the SHARED brain — only admins may change it.
    $("cfg-thr").disabled = !cfg.threshold_editable;
    $("cfg-thr").title = cfg.threshold_editable ? "" : "Umbral del cerebro compartido — solo admin";
    $("cfg-auto").value=String(cfg.auto_entry);
    $("cfg-size").value=cfg.auto_entry_usd;
    $("cfg-accel").value=cfg.velocity_accel_factor+"x";
  }catch(e){}
  loadTelegram();
}
async function loadTelegram(){
  try{ const s=await (await fetch("/telegram")).json();
    const el=$("tg-status");
    el.textContent = s.configured?"connected ✓":(s.has_token?"token set · pick a group":"not configured");
    el.style.color = s.configured?"var(--green)":"var(--muted)";
    if(s.chat_id && !$("tg-chat").value) $("tg-chat").value=s.chat_id;
  }catch(e){}
}
async function tgPost(path, params){
  return (await fetch(path,{method:"POST",headers:{"Content-Type":"application/x-www-form-urlencoded"},body:new URLSearchParams(params||{})})).json();
}
function wireTelegram(){
  const tk=()=>$("tg-token").value.trim(), ch=()=>$("tg-chat").value.trim();
  $("tg-save").addEventListener("click", async ()=>{ $("tg-msg").textContent="saving…"; await tgPost("/telegram/config",{token:tk(),chat_id:ch()}); $("tg-msg").textContent="saved ✓"; loadTelegram(); });
  $("tg-find").addEventListener("click", async ()=>{
    $("tg-msg").textContent="reading Telegram…";
    if(tk()) await tgPost("/telegram/config",{token:tk()});
    const r=await (await fetch("/telegram/updates")).json();
    $("tg-msg").textContent="";
    if(!r.ok){ $("tg-chats").innerHTML=`<div class="empty" style="text-align:left">No pude leer (${r.error||"error"}). Pega el token y envía un mensaje en el grupo, luego reintenta.</div>`; return; }
    if(!r.chats||!r.chats.length){ $("tg-chats").innerHTML='<div class="empty" style="text-align:left">Sin chats todavía. Agrega el bot al grupo y envía un mensaje, luego reintenta.</div>'; return; }
    $("tg-chats").innerHTML=r.chats.map(c=>`<div style="display:flex;align-items:center;gap:8px;margin:5px 0">
       <button class="btn" onclick="document.getElementById('tg-chat').value='${c.id}';document.getElementById('tg-msg').textContent='ID elegido — pulsa Save';">Use</button>
       <span class="px">${tcase(c.type||"")} · <b>${c.title||"(sin título)"}</b> · <span class="mono">${c.id}</span></span></div>`).join("");
  });
  $("tg-test").addEventListener("click", async ()=>{ $("tg-msg").textContent="sending…"; const r=await (await fetch("/telegram/test",{method:"POST"})).json(); $("tg-msg").textContent=r.ok?"✓ enviado — revisa tu grupo":"falló (revisa token + chat id)"; });
}
async function saveConfig(){
  const body={
    confirmation_threshold:Number($("cfg-thr").value),
    auto_entry:$("cfg-auto").value==="true",
    auto_entry_usd:Number($("cfg-size").value),
  };
  $("cfg-msg").textContent="saving…";
  try{
    const r=await fetch("/settings",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
    if(!r.ok) throw 0;
    $("cfg-msg").textContent="saved ✓"; loadSettings();
  }catch(e){ $("cfg-msg").textContent="error"; }
}
async function killSwitch(active){ try{ await fetch(`/risk/kill-switch?active=${active}&reason=manual`,{method:"POST"}); loadSettings(); }catch(e){} }
async function resetBot(){
  if(!confirm("¿Reset de TU bot? Cierra tus posiciones abiertas y limpia tu curva de equidad. El aprendizaje compartido se conserva. No afecta a otras cuentas.")) return;
  $("cfg-msg").textContent="reset…";
  try{ const r=await (await fetch("/reset",{method:"POST"})).json(); $("cfg-msg").textContent=`reset ✓ (${r.closed} cerradas)`; loadSettings(); }
  catch(e){ $("cfg-msg").textContent="error"; }
}
$("set-kill-on").addEventListener("click",()=>killSwitch(true));
$("set-kill-off").addEventListener("click",()=>killSwitch(false));
$("set-alloc-btn").addEventListener("click",openAlloc);
$("cfg-save").addEventListener("click",saveConfig);
$("cfg-reset").addEventListener("click",resetBot);
wireTelegram();
$("lrn-missed-btn").addEventListener("click",reportMissed);
$("lrn-missed").addEventListener("keydown",(e)=>{if(e.key==="Enter")reportMissed();});

// ---- candidate detail modal ----
function ringSvg(score, color){
  const r=46,cx=58,cy=58,circ=2*Math.PI*r,off=circ*(1-Math.min(score,100)/100);
  return `<svg width="116" height="116" viewBox="0 0 116 116">
    <circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="rgba(255,255,255,.08)" stroke-width="8"/>
    <circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="${color}" stroke-width="8" stroke-linecap="round"
      stroke-dasharray="${circ}" stroke-dashoffset="${off}" transform="rotate(-90 ${cx} ${cy})"/>
    <text x="${cx}" y="${cy-1}" text-anchor="middle" font-size="26" font-weight="600" fill="#fff" font-family="Geist Mono,monospace">${score}</text>
    <text x="${cx}" y="${cy+17}" text-anchor="middle" font-size="10" fill="#6f7a8e">/ 100</text>
  </svg>`;
}
function contributions(c){
  const vs=c.volume_spike, pc=c.price_change_pct_24h, imb=c.orderbook_imbalance, liq=c.liquidity_usd;
  let v=0; if(vs>=10)v=45;else if(vs>=6)v=35;else if(vs>=3)v=25;
  let p=0; if(pc>=50)p=35;else if(pc>=25)p=25;else if(pc>=10)p=15;
  let i=0; if(imb>=0.8)i=20;else if(imb>=0.65)i=10;
  let l=0; if(liq<75000 && vs>=3)l=15;
  return [
    ["Volume spike", v, 45, vs+"x", true],
    ["Price Δ24h", p, 35, (pc>=0?"+":"")+pc+"%", true],
    ["Book pressure", i, 20, (imb*100).toFixed(0)+"%", true],
    ["Low-liquidity trap", l, 15, moneyC(liq), true],
    ["OI / MCap", 0, 20, "n/a", false],
    ["L/S Ratio", 0, 10, "n/a", false],
    ["Concentration", 0, 20, "n/a", false],
    ["CEX Inflows", 0, 13, "n/a", false],
  ];
}
// Component radar (like the source tool). Axes normalised 0..1; on-chain axes
// we cannot source are drawn at 0 and labelled n/a — not faked.
function radarSvg(c){
  const vs=c.volume_spike, pc=c.price_change_pct_24h, imb=c.orderbook_imbalance, liq=c.liquidity_usd;
  const trap = liq<75000 ? Math.min((75000-liq)/75000,1) : 0;
  const axes=[
    ["Vol Spike", Math.min(vs/10,1), true],
    ["Price Δ24h", Math.min(Math.max(pc,0)/100,1), true],
    ["Book press.", imb, true],
    ["Liq. trap", trap, true],
    ["OI/MCap", 0, false],
    ["L/S Ratio", 0, false],
    ["Concentr.", 0, false],
    ["CEX Inflow", 0, false],
  ];
  const cx=150,cy=116,R=74,n=axes.length;
  const ang=i=>(-90 + i*360/n)*Math.PI/180;
  const pt=(i,r)=>[cx+Math.cos(ang(i))*R*r, cy+Math.sin(ang(i))*R*r];
  let rings="";
  [0.25,0.5,0.75,1].forEach(r=>{
    const p=axes.map((_,i)=>pt(i,r).map(x=>x.toFixed(1)).join(",")).join(" ");
    rings+=`<polygon points="${p}" fill="none" stroke="rgba(255,255,255,.07)" stroke-width="1"/>`;
  });
  let spokes="", labels="";
  axes.forEach((a,i)=>{
    const co=Math.cos(ang(i));
    const [ex,ey]=pt(i,1), [lx,ly]=pt(i,1.13);
    const anchor=co>0.3?"start":co<-0.3?"end":"middle";
    spokes+=`<line x1="${cx}" y1="${cy}" x2="${ex.toFixed(1)}" y2="${ey.toFixed(1)}" stroke="rgba(255,255,255,.07)"/>`;
    labels+=`<text x="${lx.toFixed(1)}" y="${ly.toFixed(1)}" text-anchor="${anchor}" dominant-baseline="middle" font-size="10" fill="${a[2]?'#9aa4b5':'#4d5566'}">${a[0]}</text>`;
  });
  const poly=axes.map((a,i)=>pt(i,Math.max(a[1],0.001)).map(x=>x.toFixed(1)).join(",")).join(" ");
  return `<svg width="100%" viewBox="0 0 300 232" style="display:block;max-width:248px;margin:0 auto">${rings}${spokes}
    <polygon points="${poly}" fill="rgba(124,108,255,.22)" stroke="var(--purple)" stroke-width="1.6"/>
    ${axes.map((a,i)=>{const[px,py]=pt(i,Math.max(a[1],0.001));return `<circle cx="${px.toFixed(1)}" cy="${py.toFixed(1)}" r="2.4" fill="${a[2]?'var(--purple)':'#4d5566'}"/>`}).join("")}
    ${labels}</svg>`;
}
let candCache=[], cdActive="Scoring", cdCur=null, cdDetail=null;
async function openCandidate(sym, exch){
  $("cand-modal").classList.remove("hidden");
  $("cd-symbol").textContent=sym; $("cd-sub").textContent="loading…"; cdActive="Scoring"; cdDetail=null;
  try{ candCache=await (await fetch("/candidates")).json(); }catch(e){}
  const c=candCache.find(x=>x.symbol===sym && x.exchange===exch) || candCache.find(x=>x.symbol===sym);
  if(!c){ $("cd-sub").textContent="not found · run Update"; return; }
  renderCandidate(c);
}
function ago(ts){ const s=(Date.now()-new Date(ts).getTime())/1000; if(s<90)return"now"; if(s<3600)return Math.floor(s/60)+"m"; return Math.floor(s/3600)+"h"; }
async function loadMarketChip(sym){
  const el=$("cd-mcap"); if(!el) return;
  try{
    const m=await (await fetch(`/token/market?symbol=${encodeURIComponent(sym)}`)).json();
    if(!el) return;
    if(m.found){
      const mc=m.market_cap_usd?moneyC(m.market_cap_usd):"n/a", fdv=m.fdv_usd?moneyC(m.fdv_usd):"n/a";
      el.style.color="var(--muted-2)";
      el.textContent=`MCap ${mc} · FDV ${fdv}${m.approx?" ~":""}`;
      el.title=m.name?`CoinGecko: ${m.name}${m.approx?" (ticker shared — largest mcap)":""}`:"CoinGecko";
    } else { el.textContent="MCap n/a · FDV n/a"; el.title="No CoinGecko match for this ticker"; }
  }catch(e){ el.textContent="MCap n/a · FDV n/a"; }
}
const CD_TABS=["Scoring","Timeline","Holders","Inflows","Alerts"];
async function cdTab(name){ cdActive=name; if(cdCur) await renderCandidate(cdCur); }
async function loadDetail(c){
  const k=c.exchange+":"+c.symbol;
  if(cdDetail && cdDetail._k===k) return cdDetail;
  try{
    const d=await (await fetch(`/token/detail?symbol=${encodeURIComponent(c.symbol)}&exchange=${encodeURIComponent(c.exchange)}`)).json();
    d._k=k; cdDetail=d; return d;
  }catch(e){ return null; }
}
async function renderCandidate(c){
  cdCur=c;
  $("cd-symbol").textContent=c.symbol;
  $("cd-sub").textContent=`${upx(c.exchange)} · ${tcase(c.classification)} · last signal ${ago(c.updated_at)}`;
  const col=c.pump_score>=70?"var(--pink)":c.pump_score>=40?"var(--amber)":"var(--muted)";
  $("cd-ring").innerHTML=ringSvg(c.pump_score,col);
  $("cd-chips").innerHTML=`
    <span class="tag"><span class="cdot" style="background:${clusterColor(c.cluster)}"></span>${tcase(c.cluster)}</span>
    <span class="badge" style="background:var(--inset)">age ${ago(c.updated_at)}</span>
    <span class="badge" style="background:var(--inset)">Top20% ${(c.orderbook_imbalance*100).toFixed(1)}%</span>
    <span class="badge" style="background:var(--inset)">Liq ${moneyC(c.liquidity_usd)}</span>
    <span class="badge" id="cd-mcap" style="background:var(--inset);color:var(--muted)">MCap … · FDV …</span>`;
  loadMarketChip(c.symbol);
  $("cd-tags").innerHTML=`<span class="px">confidence <b class="mono">${c.confidence_score}</b>/100</span>
    <span class="px">flags: ${(c.flags||[]).map(tcase).join(", ")||"none"}</span>`;
  const alertsN = c.status==="waiting_confirmation"?1:0;
  $("cd-tabs").innerHTML=CD_TABS.map(t=>{
    const on=t===cdActive, lbl=t==="Alerts"?`Alerts · ${alertsN}`:t;
    return `<span onclick="cdTab('${t}')" style="cursor:pointer;padding:0 0 9px;font-size:12.5px;border-bottom:2px solid ${on?'var(--purple)':'transparent'};color:${on?'#fff':'var(--muted)'}">${lbl}</span>`;
  }).join("");
  $("cd-act").disabled=false; $("cd-act").textContent="Act paper · $100";
  $("cd-act").onclick=async ()=>{ $("cd-act").disabled=true; $("cd-act").textContent="…"; const res=await actToken(c.symbol,c.exchange,null); $("cd-act").textContent=res.ok?"✓ bought (paper)":(res.msg||"done"); setTimeout(()=>{ $("cd-act").disabled=false; $("cd-act").textContent="Act paper · $100"; }, 2800); };
  const base=(c.symbol||"").split("/")[0];
  $("cd-onchain").onclick=()=>window.open("https://dexscreener.com/search?q="+encodeURIComponent(base),"_blank","noopener");
  $("cd-tg").onclick=()=>window.open("https://www.google.com/search?q="+encodeURIComponent('"'+base+'" telegram pump'),"_blank","noopener");
  const body=$("cd-tabbody");
  if(cdActive==="Scoring"){ body.innerHTML=scoringTab(c); return; }
  if(cdActive==="Alerts"){ body.innerHTML=alertsTab(c, alertsN); return; }
  body.innerHTML='<div class="empty">loading live market data…</div>';
  const d=await loadDetail(c);
  if(cdCur!==c) return;               // user switched candidate while loading
  if(!d || d.detail){ body.innerHTML='<div class="empty">could not load market data for this token</div>'; return; }
  if(cdActive==="Timeline") body.innerHTML=timelineTab(d);
  else if(cdActive==="Holders") body.innerHTML=holdersTab(d);
  else if(cdActive==="Inflows") body.innerHTML=inflowsTab(d);
}
// --- real charts (live CCXT data) ---
function priceVolChart(cd){
  if(!cd.length) return '<div class="empty">no candles</div>';
  const w=620,h=180,pad=8,vh=46;
  const cs=cd.map(x=>x.c), vs=cd.map(x=>x.v);
  const cmin=Math.min(...cs),cmax=Math.max(...cs),cr=(cmax-cmin)||1,vmax=Math.max(...vs)||1,ph=h-vh-pad*2;
  const x=i=>pad+i*(w-2*pad)/((cd.length-1)||1), yP=v=>pad+ph-((v-cmin)/cr)*ph;
  const line=cs.map((v,i)=>`${i?'L':'M'}${x(i).toFixed(1)},${yP(v).toFixed(1)}`).join(" ");
  const area=line+` L${x(cs.length-1).toFixed(1)},${(pad+ph).toFixed(1)} L${pad},${(pad+ph).toFixed(1)} Z`;
  const bw=Math.max((w-2*pad)/cd.length*0.6,1);
  const bars=vs.map((v,i)=>{const bh=(v/vmax)*vh,bx=x(i)-bw/2,by=h-pad-bh;return `<rect x="${bx.toFixed(1)}" y="${by.toFixed(1)}" width="${bw.toFixed(1)}" height="${bh.toFixed(1)}" fill="rgba(124,108,255,.4)"/>`}).join("");
  return `<svg width="100%" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" style="display:block;height:180px">
    <path d="${area}" fill="rgba(124,108,255,.10)"/><path d="${line}" fill="none" stroke="var(--purple)" stroke-width="1.6"/>${bars}</svg>`;
}
function depthLadder(dp){
  const bids=dp.bids||[],asks=dp.asks||[];
  const mx=Math.max(...bids.map(([p,a])=>p*a),...asks.map(([p,a])=>p*a),1);
  const row=(p,a,side)=>{const not=p*a;return `<div style="display:flex;align-items:center;gap:8px;margin:2px 0">
     <span class="mono px" style="width:96px;text-align:right;color:${side==='bid'?'var(--green)':'var(--pink)'}">${p}</span>
     <div style="flex:1;height:13px;background:var(--inset);border-radius:3px;overflow:hidden"><i style="display:block;height:100%;width:${(not/mx*100).toFixed(0)}%;background:${side==='bid'?'rgba(70,220,160,.45)':'rgba(255,90,120,.45)'}"></i></div>
     <span class="mono px" style="width:74px;text-align:right">${money(not)}</span></div>`;};
  return asks.slice().reverse().map(([p,a])=>row(p,a,'ask')).join("")
    +`<div class="px" style="text-align:center;margin:6px 0;color:var(--muted)">— spread —</div>`
    +bids.map(([p,a])=>row(p,a,'bid')).join("");
}
function timelineTab(d){
  const cd=d.candles||[],s=d.stats||{};
  return `<div class="navlabel" style="padding:0 0 8px">Price &amp; volume · last ${cd.length} × ${d.timeframe} (live CCXT)</div>
    ${priceVolChart(cd)}
    <div class="statgrid" style="margin-top:12px">
      <div class="sbox"><div class="l">Last</div><div class="v mono">${s.last}</div></div>
      <div class="sbox"><div class="l">Δ24h</div><div class="v mono">${(s.price_change_pct_24h>=0?'+':'')}${s.price_change_pct_24h}%</div></div>
      <div class="sbox"><div class="l">Vol spike</div><div class="v mono">${s.vol_spike}x</div></div>
      <div class="sbox"><div class="l">Quote vol 24h</div><div class="v mono">${moneyC(s.quote_volume_24h)}</div></div>
    </div>`;
}
function holdersTab(d){
  const dp=d.depth||{};
  return `<div class="navlabel" style="padding:0 0 8px">Orderbook depth · CEX proxy for concentration · live (on-chain holder list needs a provider)</div>
    ${depthLadder(dp)}
    <div class="statgrid" style="margin-top:12px">
      <div class="sbox"><div class="l">Bid imbalance (Top20%)</div><div class="v mono">${(dp.imbalance*100).toFixed(1)}%</div></div>
      <div class="sbox"><div class="l">Resting liquidity ±2%</div><div class="v mono">${moneyC(dp.liquidity_usd)}</div></div>
    </div>`;
}
function inflowsTab(d){
  const cd=d.candles||[],s=d.stats||{},vmax=Math.max(...cd.map(x=>x.v),1);
  const bars=cd.map(x=>`<div title="${x.v}" style="flex:1;display:flex;flex-direction:column;justify-content:flex-end;height:110px">
     <div style="height:${(x.v/vmax*100+3).toFixed(0)}px;background:rgba(124,108,255,.5);border-radius:2px"></div></div>`).join("");
  return `<div class="navlabel" style="padding:0 0 8px">Volume inflow per ${d.timeframe} · CEX proxy · live (on-chain CEX deposits need a provider)</div>
    <div style="display:flex;gap:3px;align-items:flex-end">${bars}</div>
    <div class="statgrid" style="margin-top:12px">
      <div class="sbox"><div class="l">Vol spike (last closed)</div><div class="v mono">${s.vol_spike}x</div></div>
      <div class="sbox"><div class="l">Quote vol 24h</div><div class="v mono">${moneyC(s.quote_volume_24h)}</div></div>
    </div>`;
}
function alertsTab(c, alertsN){
  return alertsN
    ? `<div class="alert">${scoreBadge(c.pump_score)}<div class="meta"><div class="top"><b>${c.symbol}</b><span class="px">crossed confirmation</span></div><div class="sub">${(c.flags||[]).map(tcase).join(", ")||"no flags"} · Telegram alert sent</div></div><span class="ago mono">${tcase(c.classification)}</span></div>`
    : `<div class="empty">No alert yet · score ${c.pump_score} below the confirmation threshold</div>`;
}
function scoringTab(c){
  const contrib=contributions(c).map(x=>`
    <div style="display:flex;align-items:center;gap:10px;margin:4px 0;opacity:${x[4]?1:.5}">
      <span style="width:120px;font-size:12px;color:var(--muted-2)">${x[0]}</span>
      <div style="flex:1;height:6px;border-radius:4px;background:var(--inset);overflow:hidden"><i style="display:block;height:100%;width:${Math.min(x[1]/x[2]*100,100)}%;background:${x[1]>0?'var(--purple)':'var(--inset)'}"></i></div>
      <span class="mono px" style="width:52px;text-align:right">w ${x[2]}</span>
      <span class="mono px" style="width:64px;text-align:right">${x[3]}</span>
      <span class="mono" style="width:40px;text-align:right;color:${x[1]>0?'var(--green)':'var(--muted)'}">+${x[1].toFixed(1)}</span>
    </div>`).join("");
  const signals=[
    ["Δ24h",(c.price_change_pct_24h>=0?"+":"")+c.price_change_pct_24h+"%"],
    ["Volume",c.volume_spike+"x"],["Top20%",(c.orderbook_imbalance*100).toFixed(1)+"%"],
    ["Liquidity",moneyC(c.liquidity_usd)],
    ["Last price",c.last_price],["Status",tcase(c.status)],
  ].map(s=>`<div class="sbox"><div class="l">${s[0]}</div><div class="v mono" style="font-size:14px">${s[1]}</div></div>`).join("");
  const lp=c.score_long_pump||0, cls=c.score_classic||0, classicWins=cls>lp;
  const winner=classicWins?"Classic":"Long Pump", wcol=classicWins?"var(--amber)":"var(--purple)";
  const critBanner=`<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin:0 0 14px;padding:10px 12px;background:var(--inset);border-radius:10px">
      <span class="px" style="color:var(--muted)">Criterion firing</span>
      <span style="font-weight:700;color:${wcol}">▸ ${winner}</span>
      <div style="flex:1;min-width:8px"></div>
      <span class="mono px">Long Pump <b style="color:${classicWins?'var(--muted)':'#fff'}">${lp}</b>/100</span>
      <span class="px" style="color:var(--muted)">·</span>
      <span class="mono px">Classic <b style="color:${classicWins?'#fff':'var(--muted)'}">${cls}</b>/100</span>
    </div>`;
  return `
    ${critBanner}
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px;align-items:start">
      <div>
        <div class="navlabel" style="padding:0 0 2px">Component radar</div>
        ${radarSvg(c)}
      </div>
      <div>
        <div class="navlabel" style="padding:0 0 4px">Weighted contributions</div>
        ${contrib}
        <div class="px" style="margin-top:4px;color:var(--muted)">Dim = on-chain/futures (not in public CCXT, not faked).</div>
      </div>
    </div>
    <div class="navlabel" style="padding:10px 0 6px">Signals</div>
    <div class="statgrid">${signals}</div>`;
}
$("cd-close").addEventListener("click",()=>$("cand-modal").classList.add("hidden"));
$("cd-cancel").addEventListener("click",()=>$("cand-modal").classList.add("hidden"));
$("cand-modal").addEventListener("click",(e)=>{if(e.target.id==="cand-modal")$("cand-modal").classList.add("hidden")});

// ---- PNL 7D breakdown: which tokens win / lose ----
async function openPnl(){
  $("pnl-modal").classList.remove("hidden");
  $("pnl-rows").innerHTML='<div class="empty">Cargando…</div>';
  let d; try{ d=await (await fetch("/pnl/breakdown")).json(); }
  catch(e){ $("pnl-rows").innerHTML='<div class="empty">Error al cargar</div>'; return; }
  const sign=v=>(v>=0?"+":"")+"$"+Number(v).toFixed(2);
  $("pnl-total").textContent=sign(d.total); $("pnl-total").style.color=d.total>=0?"var(--green)":"var(--red)";
  $("pnl-win").textContent=d.winners; $("pnl-lose").textContent=d.losers;
  $("pnl-sub").textContent=`${d.rows.length} tokens · realizado + flotante (7 días)`;
  const row=(r)=>{
    const col=r.total>=0?"var(--green)":"var(--red)";
    const open=r.open?' <span class="tag" style="color:var(--green)">abierta</span>':'';
    return `<div class="pnlrow">
      <div class="pl">
        <div class="sym">${r.symbol} <span class="px">${upx(r.exchange)}</span>${open}</div>
        <div class="sub">${r.trades} trade(s) · realizado ${sign(r.realized)} · flotante ${sign(r.unrealized)}</div>
      </div>
      <div class="tot" style="color:${col}">${sign(r.total)}</div>
    </div>`;
  };
  const win=d.rows.filter(r=>r.total>0), lose=d.rows.filter(r=>r.total<0), flat=d.rows.filter(r=>r.total===0);
  const section=(label,color,arr)=> arr.length
    ? `<div class="pnl-sec"><span class="pnl-sec-t" style="color:${color}">${label}</span><span class="px mono">${arr.length}</span></div>`+arr.map(row).join("")
    : "";
  $("pnl-rows").innerHTML = d.rows.length
    ? section("▲ Ganando","var(--green)",win)+section("▼ Perdiendo","var(--red)",lose)+section("• Neutro","var(--muted)",flat)
    : '<div class="empty">Sin operaciones en los últimos 7 días</div>';
}
$("btn-pnl").addEventListener("click",openPnl);
$("pnl-close").addEventListener("click",()=>$("pnl-modal").classList.add("hidden"));
$("pnl-cancel").addEventListener("click",()=>$("pnl-modal").classList.add("hidden"));
$("pnl-modal").addEventListener("click",(e)=>{if(e.target.id==="pnl-modal")$("pnl-modal").classList.add("hidden")});

// ---- light / dark theme (applies to BOTH sides: pump + embedded grid) ----
function postGridTheme(){
  const fr=document.getElementById("grvt-frame");
  const t=document.documentElement.getAttribute("data-theme")==="light"?"light":"dark";
  if(fr&&fr.contentWindow){ try{ fr.contentWindow.postMessage({tradeosTheme:t},"*"); }catch(e){} }
}
function applyTheme(t){
  document.documentElement.setAttribute("data-theme", t==="light"?"light":"dark");
  postGridTheme();   // push the same theme into the embedded grid iframe
}
applyTheme(localStorage.getItem("tradeos-theme")||"dark");
$("btn-theme").addEventListener("click",()=>{
  const cur=document.documentElement.getAttribute("data-theme")==="light"?"light":"dark";
  const nxt=cur==="light"?"dark":"light";
  localStorage.setItem("tradeos-theme",nxt); applyTheme(nxt);
});

// ---- current user: reveal admin-only tools (Cuentas, Grid) for admins ----
let CURRENT_USER = null;
async function loadMe(){
  try{
    const r = await fetch('/me'); CURRENT_USER = await r.json();
    if(CURRENT_USER && CURRENT_USER.role === 'admin'){
      const a = document.getElementById('link-admin'); if(a) a.style.display = '';
    }
  }catch(e){}
}
loadMe();

// ---- token search box (filters the Tokens table live) ----
(function(){ const s=$("tok-search"); if(s) s.addEventListener("input", filterTokens); })();

// ---- boot + active-view polling ----
const viewLoaders = {pump:loadOverview, tokens:loadTokens, alerts:loadAlerts, learning:loadLearning, trades:loadTrades, pipeline:loadPipeline, settings:loadSettings, grvt:loadGrvt};
loadOverview();
setInterval(()=>{ const fn=viewLoaders[activeView]; if(fn) fn(); }, 15000);
</script>
</body>
</html>
"""
