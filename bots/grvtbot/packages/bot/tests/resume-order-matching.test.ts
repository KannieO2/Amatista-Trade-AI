// Resume order-matching tests
//
// resumeBotInstance() corre SOLO al arrancar el proceso con un bot en 'running'
// (loadActiveBots) o al reanudarlo (startBot). Ningún test lo cubría, y por eso
// nunca se ejercitó: un bot que opera sin interrupciones jamás pasa por acá.
//
// Lo que hace es reconstruir qué orden de GRVT corresponde a qué nivel de la
// grilla. La identidad del nivel NO viaja al exchange — el motor la estampa como
// `grid_<bot>_<nivel>` (grid-engine.ts:2757) pero client.ts:480 no se la pasa a
// formatSignedOrderForAPI, que manda `client_order_id = String(Date.now())`
// (order-signer.ts:316). Así que al volver solo queda el precio para desempatar,
// y el desempate usa una tolerancia fija de $0.50.
//
// Estos tests documentan el comportamiento REAL de hoy, sin tocar el motor.

import { describe, it, expect, beforeEach, vi } from 'vitest';

const { mockGrvtClient, mockDb } = vi.hoisted(() => ({
  mockGrvtClient: {
    getOpenOrders: vi.fn(),
    getFillHistory: vi.fn(),
    getTicker: vi.fn(),
    getAccountSummary: vi.fn(),
    createOrder: vi.fn(),
    cancelOrder: vi.fn(),
    cancelAllOrders: vi.fn(),
    getInstruments: vi.fn(),
    login: vi.fn(),
  },
  mockDb: {
    getBot: vi.fn(),
    getGridLevels: vi.fn(),
    updateGridLevel: vi.fn(),
    updateBot: vi.fn(),
    getOrders: vi.fn(),
    close: vi.fn(),
  },
}));

vi.mock('../src/api/client.js', () => ({
  grvtClient: mockGrvtClient,
  GRVTClient: vi.fn(),
}));

vi.mock('../src/database/db.js', () => ({
  db: mockDb,
}));

import { GridEngine, GridBotInstance } from '../src/bot/grid-engine.js';

/** Niveles equiespaciados, como los genera calculateGridLevels(). */
function buildLevels(lower: number, upper: number, numGrids: number) {
  const spacing = (upper - lower) / numGrids;
  return Array.from({ length: numGrids }, (_, i) => ({
    id: i + 1,
    bot_id: 1,
    level_index: i,
    // grid-engine.ts:1349 redondea a 2 decimales
    price: Math.round((lower + i * spacing) * 100) / 100,
    side: 'buy' as const,
    quantity: 0.05,
    is_filled: false,
    order_id: null,
    state: 'active' as const,
  }));
}

/** Orden abierta con la forma que devuelve GRVT (legs[0].limit_price). */
function grvtOrder(price: number, orderId: string) {
  return {
    order_id: orderId,
    legs: [{ limit_price: String(price), is_buying_asset: true }],
    metadata: { client_order_id: orderId },
  };
}

function makeBot(overrides: Record<string, unknown> = {}) {
  return {
    id: 1,
    user_id: 1,
    pair: 'BNB_USDT_Perp',
    direction: 'long',
    status: 'running',
    leverage: 10,
    investment_usdt: 23,
    quantity_per_level: 0.05,
    virtual_enabled: 0,
    ...overrides,
  };
}

describe('resumeBotInstance — reasociación de órdenes tras reiniciar', () => {
  let engine: InstanceType<typeof GridEngine>;

  beforeEach(() => {
    vi.clearAllMocks();
    engine = new GridEngine();
    mockDb.updateGridLevel.mockResolvedValue(undefined);
  });

  it('espaciado ANCHO: cada orden vuelve a su nivel correcto', async () => {
    // ETH 1800–2450 en 94 grillas ≈ $6.91 de espaciado: muy por encima de $0.50,
    // así que solo un nivel cae dentro de la tolerancia. Este es el caso sano.
    const levels = buildLevels(1800, 2450, 94);
    mockDb.getGridLevels.mockResolvedValue(levels);

    const bot = makeBot({ pair: 'ETH_USDT_Perp', lower_price: 1800, upper_price: 2450, num_grids: 94 });
    const instance = new GridBotInstance(bot as any, mockGrvtClient as any);

    const target = levels[40]!;
    await (engine as any).resumeBotInstance(bot, instance, [grvtOrder(target.price, 'ord-A')]);

    const mapped = (instance as any).activeOrders.get('ord-A');
    expect(mapped).toBeDefined();
    expect(mapped.grid_level_id).toBe(target.id);
    expect(mapped.price).toBe(target.price);
  });

  it('espaciado ESTRECHO: la orden se asocia al nivel equivocado', async () => {
    // Rango de $10 en 40 grillas = $0.25 por escalón. Nada en validateGridConfig
    // (grid-engine.ts:1399-1438) impide esta configuración: valida orden del
    // rango, cantidad de grillas, leverage, inversión y min_notional — no el
    // espaciado.
    const levels = buildLevels(630, 640, 40);
    const spacing = (640 - 630) / 40;
    expect(spacing).toBeLessThan(0.5); // premisa del test

    mockDb.getGridLevels.mockResolvedValue(levels);

    const bot = makeBot({ lower_price: 630, upper_price: 640, num_grids: 40 });
    const instance = new GridBotInstance(bot as any, mockGrvtClient as any);

    // La orden real está en el nivel 3 ($630.75).
    const realLevel = levels[3]!;
    expect(realLevel.price).toBe(630.75);

    await (engine as any).resumeBotInstance(bot, instance, [grvtOrder(realLevel.price, 'ord-B')]);

    const mapped = (instance as any).activeOrders.get('ord-B');
    expect(mapped).toBeDefined();

    // find() devuelve el PRIMER nivel dentro de $0.50, no el más cercano
    // (grid-engine.ts:970 — el comentario de arriba dice "closest", el código no
    // lo es). Recorriendo de abajo hacia arriba, $630.50 entra antes que $630.75.
    expect(mapped.grid_level_id).not.toBe(realLevel.id);
    expect(mapped.grid_level_id).toBe(levels[2]!.id);
    expect(mapped.price).toBe(630.5);
  });

  it('espaciado ESTRECHO: el nivel real queda marcado como libre → segunda orden encima', async () => {
    // Consecuencia del desajuste anterior. Con virtual_enabled, todo nivel
    // 'active' que no quedó emparejado se devuelve a 'virtual' (grid-engine.ts:987-993)
    // para que rotateVirtualWindow lo reactive — o sea, coloca OTRA orden en un
    // precio que ya tenía una viva en el exchange.
    const levels = buildLevels(630, 640, 40);
    mockDb.getGridLevels.mockResolvedValue(levels);

    const bot = makeBot({ lower_price: 630, upper_price: 640, num_grids: 40, virtual_enabled: 1 });
    const instance = new GridBotInstance(bot as any, mockGrvtClient as any);

    const realLevel = levels[3]!; // $630.75, con orden viva en GRVT
    await (engine as any).resumeBotInstance(bot, instance, [grvtOrder(realLevel.price, 'ord-C')]);

    const resetIds = mockDb.updateGridLevel.mock.calls
      .filter(([, patch]: any[]) => patch?.state === 'virtual')
      .map(([id]: any[]) => id);

    // El nivel que SÍ tiene orden viva en el exchange se marca como libre.
    expect(resetIds).toContain(realLevel.id);
    // Y el que se quedó la orden prestada NO se libera.
    expect(resetIds).not.toContain(levels[2]!.id);
  });

  it('el vínculo exacto existe en la base local, pero el resume no lo consulta', async () => {
    // grid-engine.ts:2786 guarda cada orden con su grid_level_id, y
    // database.ts:1227 expone getOrdersByBot(). El resume reconstruye por precio
    // y nunca toca esa tabla: por eso puede equivocarse teniendo el dato exacto.
    const levels = buildLevels(630, 640, 40);
    mockDb.getGridLevels.mockResolvedValue(levels);
    mockDb.getOrders.mockResolvedValue([
      { order_id: 'ord-D', grid_level_id: levels[3]!.id, price: 630.75, status: 'pending' },
    ]);

    const bot = makeBot({ lower_price: 630, upper_price: 640, num_grids: 40 });
    const instance = new GridBotInstance(bot as any, mockGrvtClient as any);

    await (engine as any).resumeBotInstance(bot, instance, [grvtOrder(630.75, 'ord-D')]);

    expect(mockDb.getOrders).not.toHaveBeenCalled();
    expect((instance as any).activeOrders.get('ord-D').grid_level_id).toBe(levels[2]!.id);
  });
});
