#!/usr/bin/env ts-node --esm

// Test de firma EIP-712 para órdenes GRVT
// Test: BUY 0.01 ETH @ $1700 (debajo del mercado), luego cancelar

import dns from 'dns';
import { grvtClient } from './api/client.js';

// Forzar IPv4 globalmente
dns.setDefaultResultOrder('ipv4first');

async function testEIP712Order() {
  console.log('🧪 Testing EIP-712 Order Signing...');
  console.log('==========================================');

  try {
    // 1. Verificar autenticación
    console.log('1. Verificando autenticación...');
    const balance = await grvtClient.getBalance();
    console.log(`✅ Balance disponible: $${balance.available_balance}`);

    // 2. Crear orden de prueba (BUY 0.02 ETH @ $1700 = $34 notional - bien debajo del mercado)
    console.log('\n2. Creando orden de prueba con firma EIP-712...');
    const testOrder = {
      sub_account_id: process.env.GRVT_TRADING_ACCOUNT_ID!,
      instrument: 'ETH_USDT_Perp',
      size: '0.02',
      price: '1700',
      side: 'buy' as const,
      type: 'limit' as const,
      time_in_force: 'gtc' as const,
      metadata: 'EIP712-test'
    };

    let orderId: string | null = null;

    try {
      const createdOrder = await grvtClient.createOrder(testOrder);
      orderId = createdOrder.order_id;
      
      console.log('✅ ORDEN CREADA EXITOSAMENTE!');
      console.log(`📦 Order ID: ${orderId}`);
      console.log(`🎯 Instrumento: ${testOrder.instrument}`);
      console.log(`📏 Size: ${testOrder.size} ETH`);
      console.log(`💰 Price: $${testOrder.price}`);
      console.log(`🔄 Status: ${createdOrder.status}`);

      // 3. Verificar que aparece en open_orders
      console.log('\n3. Verificando orden en open_orders...');
      const openOrders = await grvtClient.getOpenOrders('ETH_USDT_Perp');
      const ourOrder = openOrders.find((o: any) => o.order_id === orderId);
      
      if (ourOrder) {
        console.log('✅ Orden visible en open_orders');
        console.log(`📊 Status: ${ourOrder.status}`);
      } else {
        console.log('⚠️  Orden no encontrada en open_orders');
      }

    } catch (createError) {
      console.error('❌ ERROR CREANDO ORDEN:', createError);
      
      // Loguear error completo para diagnóstico
      if (createError instanceof Error) {
        console.error('Error message:', createError.message);
      }
      
      return false;
    }

    // 4. Cancelar la orden de prueba
    if (orderId) {
      console.log('\n4. Cancelando orden de prueba...');
      try {
        const cancelled = await grvtClient.cancelOrder(orderId, testOrder.instrument);
        if (cancelled) {
          console.log('✅ Orden cancelada exitosamente');
        } else {
          console.log('⚠️  No se pudo cancelar la orden');
        }
      } catch (cancelError) {
        console.error('❌ Error cancelando orden:', cancelError);
      }
    }

    console.log('\n🎉 TEST COMPLETADO - FIRMA EIP-712 FUNCIONA!');
    return true;

  } catch (error) {
    console.error('❌ TEST FAILED:', error);
    return false;
  }
}

// Ejecutar test si se llama directamente
if (import.meta.url === `file://${process.argv[1]}`) {
  testEIP712Order()
    .then(success => {
      process.exit(success ? 0 : 1);
    })
    .catch(error => {
      console.error('Fatal error:', error);
      process.exit(1);
    });
}

export default testEIP712Order;