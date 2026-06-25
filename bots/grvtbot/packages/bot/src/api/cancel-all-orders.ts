// Cancel All Orders - Emergency Script
// Usado por stop-grid-bot.sh como SAFEGUARD

import { grvtClient } from './client.js';
import dotenv from 'dotenv';

dotenv.config();

async function cancelAllOrders() {
  try {
    console.log('🛡️  SAFEGUARD: Cancelando todas las órdenes abiertas...');
    
    // Cancelar todas las órdenes sin filtro de instrumento
    const cancelledCount = await grvtClient.cancelAllOrders();
    
    console.log(`✅ ${cancelledCount} órdenes canceladas exitosamente`);
    
    // Verificar que no queden órdenes abiertas
    const remainingOrders = await grvtClient.getOpenOrders();
    
    if (remainingOrders.length > 0) {
      console.log(`⚠️  ${remainingOrders.length} órdenes aún abiertas:`);
      for (const order of remainingOrders) {
        console.log(`   - ${order.order_id}: ${order.side} ${order.size} ${order.instrument} @ ${order.price}`);
      }
    } else {
      console.log('✅ No hay órdenes abiertas restantes');
    }
    
    process.exit(0);
    
  } catch (error) {
    console.error('❌ Error cancelando órdenes:', error instanceof Error ? error.message : error);
    process.exit(1);
  }
}

// Timeout de seguridad
setTimeout(() => {
  console.log('⏰ Timeout de seguridad alcanzado');
  process.exit(1);
}, 25000); // 25 segundos

cancelAllOrders();