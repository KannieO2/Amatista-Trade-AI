# Guía simple — Pump Reader (para empezar sin saber nada técnico)

Esta guía te explica, en palabras normales, qué es el bot, cómo encenderlo y
cómo leerlo. No necesitas saber programar.

---

## ¿Qué hace este bot?

Vigila cientos de monedas cripto en **Binance, MEXC y Bitget** las 24 horas y
busca señales de **"scam pumps"**: cuando alguien infla el precio de una moneda
barata de golpe (de centavos a mucho más) para luego venderla y dejar tirados a
los demás.

El bot intenta **detectarlos pronto**. Hace el trabajo que tú no podrías hacer a
mano: mirar miles de monedas cada pocos minutos.

> Ahora mismo el bot está en **MODO PAPEL**: solo *simula* compras con dinero
> falso. **No toca tu dinero real.** Eso es a propósito, para que aprendas sin
> riesgo.

---

## Cómo encenderlo (1 paso)

1. Abre la carpeta del proyecto:
   `C:\Users\osval\OneDrive\Documentos\Trading IA`
2. Haz **doble clic en `start.bat`**.
   - La **primera vez** tardará 1–2 minutos (instala lo que necesita). Solo pasa
     la primera vez.
   - Se abrirá una ventana negra. **No la cierres** mientras uses el bot.
3. Se abrirá tu navegador en `http://localhost:8000`. Ahí está el panel.
   - Si no se abre solo, abre el navegador y escribe esa dirección a mano.

**Para apagarlo:** cierra la ventana negra. Ya está.

---

## Cómo leer el panel

Verás una tabla. Cada fila es una moneda sospechosa. Columnas:

| Columna     | Qué significa |
|-------------|---------------|
| **Exchange**| En qué casa de cambio está (binance / mexc / bitget). |
| **Token**   | El nombre de la moneda (ej. `BR/USDT`). |
| **Score**   | Qué tan fuerte es la señal de pump, de 0 a 100. Más alto = más sospechoso. Rojo = muy alto. |
| **Tipo**    | Qué clase de patrón es (ver abajo). |
| **Δ 24h**   | Cuánto subió en 24 horas (ej. +43%). |
| **Volumen x**| Cuántas veces se disparó el volumen vs. lo normal (ej. 9x = nueve veces más). |
| **Liquidez**| Cuánto dinero hay puesto en el libro. **Poca liquidez + mucho volumen = trampa típica.** |
| **Señales** | Las pistas concretas que encontró. |
| **Estado**  | `watching` = vigilando. `waiting_confirmation` = señal fuerte, ojo aquí. |

### Los "Tipos" en cristiano
- **`criminal_pump_suspect`** → el más sospechoso: poca liquidez + volumen
  fabricado + precio disparado. El clásico pump-and-dump.
- **`active_pump`** → un pump en marcha ahora mismo.
- **`accumulation_imbalance`** → muchas órdenes de compra apiladas; alguien
  acumulando.
- **`volume_anomaly`** → algo raro en el volumen, menos claro.

### El botón "Escanear ahora"
El bot ya escanea solo cada 5 minutos. Si quieres forzar un escaneo en el
momento, pulsa **"Escanear ahora"** (tarda ~15 segundos).

---

## ⚠️ Sobre el dinero real (lee esto)

El bot **puede** operar solo (comprar y vender), pero está **apagado** esa parte
a propósito. Para encenderla harían falta tus claves (API keys) de Binance/MEXC,
y aún **no deberías hacerlo**. Por qué:

- El que inventó esta estrategia (KManuS88) dice que él mismo **acaba de
  inventarla** y que hay que observarla **entre 7 y 30 días** para saber si de
  verdad funciona o fue suerte.
- Operar dinero real con una estrategia sin probar puede **vaciarte la cuenta**.

**Plan sano:** déjalo en modo papel varios días. Mira si las monedas que marca
de verdad suben después. Cuando confíes, hablamos de activar dinero real **con
límites pequeños y topes de seguridad** (ya están programados: tope por
operación, freno de emergencia, y nunca se permiten claves que puedan retirar
fondos).

---

## Avisos por Telegram (opcional, para después)

Se puede hacer que el bot te mande un mensaje a Telegram cuando detecte algo
fuerte. Requiere crear un "bot de Telegram" (te guío paso a paso cuando
quieras). No es necesario para empezar.

---

## Si algo no funciona

- La ventana negra se cerró sola → vuelve a hacer doble clic en `start.bat` y
  mira si sale algún texto en rojo; cópiamelo.
- El navegador dice "no se puede conectar" → asegúrate de que la ventana negra
  sigue abierta, y espera 15–20 segundos tras encenderlo.
- La tabla está vacía → espera al primer escaneo o pulsa "Escanear ahora".
