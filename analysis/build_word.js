// Informe de Rentabilidad — Bot ScamPump (Amatista). Solo pump. Lee results.json.
const fs = require("fs");
const path = require("path");
const {
  Document, Packer, Paragraph, TextRun, Table, TableRow, TableCell, ImageRun,
  AlignmentType, HeadingLevel, BorderStyle, WidthType, ShadingType, PageBreak,
  LevelFormat,
} = require("docx");

const OUT = path.join(__dirname, "out");
const R = JSON.parse(fs.readFileSync(path.join(OUT, "results.json"), "utf-8"));
const PUR = "7C3AED", PURD = "4C1D95", GREEN = "2E7D52", RED = "B3261E", GREY = "555555";
const PRIMARY = "Ajustado (cluster ~1.3%)", WIDE = "Ancho (default 8%)";
const SC = ["Pesimista (mercado choppy / pocas señales)", "Base (mercado normal)",
            "Optimista (alt-season / pumps frecuentes)"];
const SCSHORT = { [SC[0]]: "Pesimista", [SC[1]]: "Base", [SC[2]]: "Optimista" };
const HZ = ["3 meses", "6 meses", "1 año"];

const pct = (x, d = 1) => (x * 100).toFixed(d) + "%";
const sgn = (x, d = 1) => (x >= 0 ? "+" : "") + (x * 100).toFixed(d) + "%";
const num = (x, d = 2) => x.toFixed(d);

// ---- helpers de estilo ----
const W = 9360;
const border = { style: BorderStyle.SINGLE, size: 1, color: "CCCCCC" };
const borders = { top: border, bottom: border, left: border, right: border };
const cellM = { top: 70, bottom: 70, left: 110, right: 110 };

function T(text, o = {}) { return new TextRun({ text, font: "Arial", ...o }); }
function P(children, o = {}) {
  return new Paragraph({ children: Array.isArray(children) ? children : [children], ...o });
}
function H1(t) { return new Paragraph({ heading: HeadingLevel.HEADING_1, children: [T(t, { bold: true, color: PURD, size: 30 })], spacing: { before: 280, after: 140 } }); }
function H2(t) { return new Paragraph({ heading: HeadingLevel.HEADING_2, children: [T(t, { bold: true, color: PUR, size: 25 })], spacing: { before: 220, after: 100 } }); }
function body(t, o = {}) { return P(T(t, { size: 21, ...o }), { spacing: { after: 100 }, alignment: AlignmentType.JUSTIFIED }); }
function bullet(t) { return new Paragraph({ numbering: { reference: "b", level: 0 }, children: [T(t, { size: 21 })], spacing: { after: 40 } }); }

function img(file, w = 600) {
  const data = fs.readFileSync(path.join(OUT, file));
  return new Paragraph({ alignment: AlignmentType.CENTER, spacing: { before: 80, after: 120 },
    children: [new ImageRun({ type: "png", data, transformation: { width: w, height: Math.round(w * 0.525) },
      altText: { title: file, description: file, name: file } })] });
}

function cell(text, { w, head = false, color, bold = false, fill, align = AlignmentType.LEFT } = {}) {
  return new TableCell({ borders, width: { size: w, type: WidthType.DXA }, margins: cellM,
    shading: { fill: fill || (head ? PUR : "FFFFFF"), type: ShadingType.CLEAR },
    children: [P(T(text, { size: 19, bold: head || bold, color: head ? "FFFFFF" : (color || "000000") }), { alignment: align })] });
}

function table(headRow, rows, widths) {
  const trs = [new TableRow({ tableHeader: true, children: headRow.map((h, i) =>
    cell(h, { w: widths[i], head: true, align: i === 0 ? AlignmentType.LEFT : AlignmentType.CENTER })) })];
  rows.forEach(r => trs.push(new TableRow({ children: r.map((c, i) =>
    cell(c.t, { w: widths[i], color: c.color, bold: c.bold, fill: c.fill,
      align: i === 0 ? AlignmentType.LEFT : AlignmentType.CENTER })) })));
  return new Table({ width: { size: W, type: WidthType.DXA }, columnWidths: widths, rows: trs });
}

const retColor = v => (v > 0.01 ? GREEN : v < -0.01 ? RED : GREY);

// ====================== CONTENIDO ======================
const kids = [];

// ---- PORTADA ----
kids.push(new Paragraph({ spacing: { before: 1400, after: 0 }, alignment: AlignmentType.CENTER, children: [T("AMATISTA", { bold: true, size: 64, color: PURD })] }));
kids.push(P(T("TradeOS", { size: 28, color: PUR }), { alignment: AlignmentType.CENTER, spacing: { after: 600 } }));
kids.push(P(T("Informe de Rentabilidad", { bold: true, size: 44 }), { alignment: AlignmentType.CENTER, spacing: { after: 60 } }));
kids.push(P(T("Bot detector de Scam-Pumps", { size: 30, color: GREY }), { alignment: AlignmentType.CENTER, spacing: { after: 500 } }));
kids.push(P(T("Simulación Montecarlo · 500.000 trayectorias · modelo de la estrategia", { size: 20, italics: true, color: GREY }), { alignment: AlignmentType.CENTER, spacing: { after: 40 } }));
kids.push(P(T("Horizontes: 3 meses · 6 meses · 1 año", { size: 20, color: GREY }), { alignment: AlignmentType.CENTER, spacing: { after: 1000 } }));
kids.push(P(T("25 de junio de 2026 · MODO PAPER (sin dinero real)", { size: 18, color: GREY }), { alignment: AlignmentType.CENTER }));
kids.push(P(T("Documento técnico interno — no es asesoría financiera", { size: 16, italics: true, color: GREY }), { alignment: AlignmentType.CENTER }));
kids.push(new Paragraph({ children: [new PageBreak()] }));

// ---- 1. RESUMEN EJECUTIVO ----
const be = R.breakeven.be_wr;
const baseY = R.pump[PRIMARY][SC[1]]["1 año"];
const optY = R.pump[PRIMARY][SC[2]]["1 año"];
const pesY = R.pump[PRIMARY][SC[0]]["1 año"];
kids.push(H1("1. Resumen ejecutivo"));
kids.push(body("Pregunta: ¿cuánto puede generar el bot de pump? Respuesta honesta: depende de dos cosas que ya conocemos — el win-rate (calidad de la señal) y el ancho del stop. No es una promesa de cifra: es una función de esas dos palancas. Este informe modela la GEOMETRÍA de la estrategia (sus parámetros reales de entrada/salida leídos del código), no el historial del bot, que está contaminado por el periodo mal configurado.", { }));
kids.push(P([T("Veredicto: ", { bold: true, size: 21 }), T("al win-rate medido hoy (~22%) el bot NO es rentable — pierde alrededor de −8%/año en el escenario base. Cruza a rentable al superar ~23–24% de win-rate. A partir de ahí el potencial es grande pero depende de cazar los “runners” (la cola gorda de pumps reales) y de mantener el stop ajustado.", { size: 21 })], { spacing: { after: 120 }, alignment: AlignmentType.JUSTIFIED }));
kids.push(table(
  ["Si el win-rate es…", "Retorno mediano 1 año", "Prob. de ganar", "Sharpe"],
  [
    [{ t: "~22% (medido, pre-fix)", bold: true }, { t: sgn(baseY.median_total_ret), color: RED, bold: true }, { t: pct(baseY.prob_profit), color: RED }, { t: num(baseY.median_sharpe), color: RED }],
    [{ t: "~23–24% (equilibrio)" }, { t: "≈ 0%", color: GREY }, { t: "≈ 50%", color: GREY }, { t: "≈ 0", color: GREY }],
    [{ t: "~30% (alt-season)", bold: true }, { t: sgn(optY.median_total_ret), color: GREEN, bold: true }, { t: pct(optY.prob_profit), color: GREEN }, { t: num(optY.median_sharpe), color: GREEN }],
  ],
  [3360, 2400, 1900, 1700]));
kids.push(P(T("", { size: 8 })));
kids.push(P([T("Dos condiciones duras para que sea rentable:", { bold: true, size: 21 })], { spacing: { after: 60 } }));
kids.push(bullet(`Stop ajustado (~1,3%, perfiles de cluster). Con el stop ancho por defecto (8%) el bot pierde en TODOS los escenarios — nunca rentable. El stop es la variable que más manda.`));
kids.push(bullet(`Win-rate por encima de ~23%. Es la línea de equilibrio (break-even) con el stop ajustado. Hoy está justo al filo.`));
kids.push(P([T("Riesgo: ", { bold: true, size: 21 }), T("el circuit-breaker limita la caída — en todos los escenarios el riesgo de ruina (perder >50% del capital) fue prácticamente 0% y el drawdown mediano se topó en ~10%. La estrategia protege el capital; el problema no es reventar, es no tener ventaja suficiente al win-rate actual.", { size: 21 })], { spacing: { after: 100 }, alignment: AlignmentType.JUSTIFIED }));
kids.push(new Paragraph({ children: [new PageBreak()] }));

// ---- 2. METODOLOGÍA ----
kids.push(H1("2. Metodología (y qué NO se promete)"));
kids.push(body("La simulación no usa el historial de trades del bot (vacío en local y contaminado en producción por la mala configuración previa). En su lugar modela la geometría de la estrategia: las reglas de salida y riesgo definen el PAYOFF de cada trade; lo único que la estructura no fija es el win-rate (depende de la calidad de señal). Por eso se barre el win-rate en un rango realista y se reporta el punto de equilibrio."));
kids.push(H2("Cómo se modela cada trade"));
kids.push(bullet("WIN: un pump que corre. El trailing arma casi en verde (+0,8%) y devuelve 8% del pico. Tamaño = mezcla de “pops” pequeños (mediana ~2,2%) y “runners” de cola gorda (mediana ~14%, cola a >60%)."));
kids.push(bullet("LOSS: toca el hard-stop. Su ancho es la variable pivote (se prueban 1,3% del cluster y 8% por defecto)."));
kids.push(bullet("SCRATCH: salida plana por timeout / volumen muerto (~−1,25%), medida en el código."));
kids.push(bullet("Cada trade descuenta comisión + slippage (~0,4% ida y vuelta, CEX spot). Se aplica tamaño por trade (20% del equity), tope de 4 posiciones y el circuit-breaker (corte diario −8% / drawdown −10%)."));
kids.push(H2("Lo que este informe NO hace (anti-humo)"));
kids.push(bullet("No pronostica el precio del mercado a partir de noticias: ningún modelo lo hace con fiabilidad. En su lugar usa bandas de régimen (pesimista / base / optimista) como escenarios."));
kids.push(bullet("No presenta el historial contaminado como si fuera medición real."));
kids.push(bullet("Los números son “bajo estos supuestos”, no “esto es lo que el bot hizo”. Un Sharpe optimista alto está inflado por el supuesto de trades independientes (sin rachas correlacionadas); en mercado real sería menor."));

// Parámetros de estrategia
kids.push(H2("Parámetros reales de la estrategia (del código)"));
const PP = R.params.PUMP;
kids.push(table(["Parámetro", "Valor", "Significado"],
  [
    [{ t: "hard_stop_pct" }, { t: PP.hard_stop_pct + "%" }, { t: "Pérdida tope por trade (default; cluster lo ajusta a ~1,3%)" }],
    [{ t: "breakeven_pct" }, { t: PP.breakeven_pct + "%" }, { t: "Mueve el stop a empate" }],
    [{ t: "trail_arm_pct" }, { t: PP.trail_arm_pct + "%" }, { t: "El trailing se arma casi en verde" }],
    [{ t: "trail_giveback" }, { t: PP.trail_giveback + "%" }, { t: "Devuelve 8% del pico (deja correr)" }],
    [{ t: "fee_slip_roundtrip" }, { t: PP.fee_slip_roundtrip + "%" }, { t: "Comisión + slippage ida/vuelta" }],
    [{ t: "pos_frac" }, { t: pct(PP.pos_frac, 0) }, { t: "Fracción de equity por trade" }],
    [{ t: "max_open" }, { t: String(PP.max_open) }, { t: "Posiciones simultáneas máx." }],
    [{ t: "circuit breaker" }, { t: PP.daily_loss_halt + "% / " + PP.drawdown_halt + "%" }, { t: "Corte por pérdida diaria / drawdown" }],
    [{ t: "trades_per_day" }, { t: String(PP.trades_per_day) }, { t: "Cadencia (predador: pocas, alta convicción)" }],
  ], [2600, 1700, 5060]));
kids.push(new Paragraph({ children: [new PageBreak()] }));

// ---- 3. GEOMETRÍA Y BREAK-EVEN ----
kids.push(H1("3. La pregunta clave: ¿a qué win-rate deja de perder?"));
kids.push(body(`Con el stop ajustado (1,3%), el win-rate de equilibrio es ~${be.base ? (be.base*100).toFixed(1) : "—"}% en el escenario base (${(be.pesimista*100).toFixed(1)}% pesimista, ${(be.optimista*100).toFixed(1)}% optimista). Por debajo de eso la esperanza por trade es negativa; por encima, positiva. El win-rate medido pre-fix (~22%) queda justo por debajo del equilibrio base.`));
kids.push(img("c1_breakeven.png"));
kids.push(body("Pero el equilibrio se mueve con el ancho del stop. Este es el hallazgo central: con stop ajustado basta ~20–23% de aciertos; con el stop ancho de 8% por defecto haría falta ~45% — irreal para un detector de pumps. El stop es la palanca que decide si la estrategia es viable."));
kids.push(img("c6_stopwidth.png"));
kids.push(body("Consecuencia directa: con el stop ancho (8%) el retorno mediano es negativo en todos los escenarios; con el stop ajustado se vuelve viable. La configuración del stop, por sí sola, voltea el signo del resultado."));
kids.push(img("c7_flip.png"));
kids.push(new Paragraph({ children: [new PageBreak()] }));

// ---- 4. RESULTADOS MC ----
kids.push(H1("4. Resultados Montecarlo (stop ajustado 1,3%)"));
kids.push(body("500.000 trayectorias por celda. Mediana del retorno total, probabilidad de terminar en ganancia, Sharpe y Sortino (anualizados), drawdown máximo mediano y riesgo de ruina (prob. de perder >50%)."));
const rows4 = [];
SC.forEach(sc => HZ.forEach(hz => {
  const m = R.pump[PRIMARY][sc][hz];
  rows4.push([
    { t: SCSHORT[sc] }, { t: hz },
    { t: sgn(m.median_total_ret), color: retColor(m.median_total_ret), bold: true },
    { t: pct(m.prob_profit) }, { t: num(m.median_sharpe), color: retColor(m.median_sharpe) },
    { t: num(m.median_sortino) }, { t: pct(m.median_maxdd) }, { t: pct(m.prob_ruin_50) },
  ]);
}));
kids.push(table(["Escenario", "Horizonte", "Ret. mediano", "P(ganar)", "Sharpe", "Sortino", "maxDD", "Ruina"],
  rows4, [1500, 1180, 1380, 1080, 1000, 1000, 1080, 1140]));
kids.push(P(T("", { size: 10 })));
kids.push(body("Abanico de equity (500k paths) — escenario base, 1 año. La mediana sangra hacia el corte del circuit-breaker (~−10%); la franja superior (P95) muestra el potencial cuando aparecen runners. La asimetría es la firma de la estrategia: muchas pérdidas pequeñas, pocas ganancias grandes."));
kids.push(img("c2_fan.png"));
kids.push(body("Distribución del resultado a 1 año (base). La masa está a la izquierda (pérdidas pequeñas acotadas por el stop) con una cola derecha de runners. La rentabilidad vive de que esa cola pese lo suficiente — es decir, de subir el win-rate y la frecuencia de runners."));
kids.push(img("c3_dist.png"));
kids.push(img("c4_scenarios.png"));
kids.push(new Paragraph({ children: [new PageBreak()] }));

// ---- 5. SENSIBILIDAD STOP ANCHO ----
kids.push(H1("5. Sensibilidad: el stop ancho (8%) hunde todo"));
kids.push(body("Mismos escenarios con el stop por defecto de 8% (1 año). El veredicto se invierte: incluso el escenario optimista pierde. Confirma que mantener el stop ajustado no es un detalle — es condición necesaria de rentabilidad."));
const rows5 = SC.map(sc => {
  const m = R.pump[WIDE][sc]["1 año"];
  return [{ t: SCSHORT[sc] },
    { t: sgn(m.median_total_ret), color: retColor(m.median_total_ret), bold: true },
    { t: pct(m.prob_profit) }, { t: num(m.median_sharpe), color: retColor(m.median_sharpe) },
    { t: pct(m.median_maxdd) }];
});
kids.push(table(["Escenario (stop 8%)", "Ret. mediano 1a", "P(ganar)", "Sharpe", "maxDD"],
  rows5, [2860, 1900, 1600, 1500, 1500]));

// ---- 6. MÉTRICAS / GLOSARIO ----
kids.push(H2("Glosario de las variables estadísticas"));
kids.push(bullet("Sharpe: retorno por unidad de riesgo total (volatilidad). >1 bueno; negativo = pierde ajustado a riesgo. Aquí inflado por supuesto iid."));
kids.push(bullet("Sortino: como Sharpe pero solo penaliza la volatilidad a la baja. Más justo para estrategias de cola gorda."));
kids.push(bullet("maxDD (drawdown máximo): mayor caída desde un pico. Topado en ~10% por el circuit-breaker."));
kids.push(bullet("Risk-of-ruin: probabilidad de perder >50% del capital. ~0% en todos los escenarios → el capital está protegido."));
kids.push(bullet("P(ganar): fracción de las 500k trayectorias que terminan en verde."));
kids.push(bullet("Percentiles P5/P95: el 90% de los resultados cae entre ambos — el rango realista, no el promedio aislado."));

// ---- 7. VEREDICTO ----
kids.push(H1("6. Veredicto aterrizado"));
kids.push(P([T("¿Es rentable el bot de pump en el tiempo? ", { bold: true, size: 22 }),
  T("Hoy, al win-rate medido (~22%) y si corre con el stop ajustado: NO — pierde de forma controlada (mediana ~−8%/año, sin riesgo de reventar). Se vuelve rentable al cruzar ~23–24% de win-rate. Su techo (alt-season, win-rate ~30% con muchos runners) es alto, pero es un escenario, no una expectativa, y su Sharpe está sobreestimado por el modelo.", { size: 22 })], { spacing: { after: 140 }, alignment: AlignmentType.JUSTIFIED }));
kids.push(P([T("Las 2 palancas que deciden la ganancia:", { bold: true, size: 21 })], { spacing: { after: 60 } }));
kids.push(bullet("Subir el win-rate por encima de ~23%: es el trabajo de la calidad de señal (entrar a los pumps reales, no al ruido). Es la palanca #1."));
kids.push(bullet("Mantener el stop ajustado (~1,3%): con 8% no hay rentabilidad posible. Palanca #2, gratis."));
kids.push(P([T("Honestidad sobre el “2x en un mes”: ", { bold: true, size: 21 }),
  T("solo aparece en el escenario optimista extremo y depende de cazar la cola de runners en una alt-season. Con protección de capital, lo realista a sostener es modesto y condicionado a cruzar el win-rate de equilibrio. La cifra no se promete; la mecánica para lograrla sí está clara.", { size: 21 })], { spacing: { after: 120 }, alignment: AlignmentType.JUSTIFIED }));
kids.push(P([T("Limitaciones del modelo: ", { bold: true, italics: true, size: 19, color: GREY }),
  T("trades independientes (sin rachas correlacionadas → Sharpe sobreestimado); distribución de runners asumida; win-rate barrido, no medido limpio. Para un veredicto definitivo hace falta acumular un historial LIMPIO (post-fix) de suficientes trades y recalibrar con datos reales.", { size: 19, italics: true, color: GREY })], { spacing: { after: 120 }, alignment: AlignmentType.JUSTIFIED }));

// ====================== DOC ======================
const doc = new Document({
  numbering: { config: [{ reference: "b", levels: [{ level: 0, format: LevelFormat.BULLET, text: "•",
    alignment: AlignmentType.LEFT, style: { paragraph: { indent: { left: 540, hanging: 260 } } } }] }] },
  styles: { default: { document: { run: { font: "Arial", size: 21 } } },
    paragraphStyles: [
      { id: "Heading1", name: "Heading 1", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 30, bold: true, font: "Arial", color: PURD }, paragraph: { spacing: { before: 280, after: 140 }, outlineLevel: 0 } },
      { id: "Heading2", name: "Heading 2", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 25, bold: true, font: "Arial", color: PUR }, paragraph: { spacing: { before: 220, after: 100 }, outlineLevel: 1 } },
    ] },
  sections: [{
    properties: { page: { size: { width: 12240, height: 15840 }, margin: { top: 1440, right: 1440, bottom: 1440, left: 1440 } } },
    children: kids,
  }],
});

Packer.toBuffer(doc).then(buf => {
  const out = path.join(OUT, "Informe_Rentabilidad_Pump.docx");
  fs.writeFileSync(out, buf);
  console.log("OK ->", out, "(" + (buf.length / 1024).toFixed(0) + " KB)");
});
