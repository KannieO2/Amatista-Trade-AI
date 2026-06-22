"""
Simulador de Ejecución: Lógica Actual vs Nueva Precisión
Este script demuestra cómo se comporta el bot con las reglas actuales (rígidas)
vs las nuevas reglas propuestas (Score de Calidad Compuesto), sin modificar el código real.
"""

import time

# --- MOCK DATA: Simulación de 3 monedas detectadas por el escáner ---
candidates = [
    {
        "symbol": "TOKEN_A",
        "description": "El alumno perfecto (100% de todo, poco probable en la vida real)",
        "vol_spike": 5.0,        # Muy alto
        "runup_pct": 2.0,        # Breakout claro
        "heat": 85,              # Presión on-chain masiva
        "fsm_score": 80,         # Acumulación excelente
    },
    {
        "symbol": "TOKEN_B",
        "description": "La oportunidad real (Súper volumen y compras, pero base casi plana)",
        "vol_spike": 4.5,        # Buen volumen
        "runup_pct": 0.8,        # <-- FALLA EL GATE ANTIGUO (FSM_MIN_BREAKOUT_PCT = 1.5)
        "heat": 75,              # Alta presión compradora real
        "fsm_score": 70,         # Buena acumulación
    },
    {
        "symbol": "TOKEN_C",
        "description": "La trampa (Falsa ruptura sin dinero real respaldando)",
        "vol_spike": 1.1,        # <-- FALLA EL GATE ANTIGUO (FSM_MIN_ENTRY_VOL_SPIKE = 4.0)
        "runup_pct": 2.5,        # Sube rápido (fake pump)
        "heat": 10,              # Nadie está comprando realmente
        "fsm_score": 30,         # Acumulación pobre
    }
]

# --- LÓGICA ANTIGUA (La que tienes actualmente en main.py y estanca al bot) ---
def vieja_logica_rigida(cand):
    # Compuertas binarias: Pasan TODAS o se rechaza.
    if cand["runup_pct"] < 1.5:
        return False, "Rechazado: Base plana (runup < 1.5%)"
    if cand["vol_spike"] < 4.0:
        return False, "Rechazado: Sin volumen suficiente (spike < 4.0x)"
    if cand["fsm_score"] < 65:
        return False, "Rechazado: Puntuación de acumulación baja (< 65)"
    
    return True, "APROBADO (Pasó todos los filtros rígidos)"

# --- NUEVA LÓGICA PROPUESTA (Sistema de Precisión 0-100) ---
def nueva_logica_precision(cand):
    score = 0
    max_score = 100
    reasons = []

    # 1. Breakout (25 pts)
    if cand["runup_pct"] >= 1.5: score += 25; reasons.append("Breakout fuerte (+25)")
    elif cand["runup_pct"] >= 0.5: score += 15; reasons.append("Micro-arranque aceptable (+15)")
    
    # 2. Volumen (30 pts)
    if cand["vol_spike"] >= 4.0: score += 30; reasons.append("Volumen excelente (+30)")
    elif cand["vol_spike"] >= 2.0: score += 15; reasons.append("Volumen moderado (+15)")
    
    # 3. Heat On-chain (20 pts)
    if cand["heat"] >= 70: score += 20; reasons.append("Presión compradora masiva (+20)")
    elif cand["heat"] >= 40: score += 10; reasons.append("Compras reales activas (+10)")

    # 4. Acumulación Estructural (25 pts)
    if cand["fsm_score"] >= 70: score += 25; reasons.append("Acumulación sólida (+25)")
    elif cand["fsm_score"] >= 50: score += 10; reasons.append("Acumulación moderada (+10)")

    # Evaluación Final
    aprobado = score >= 80 # <-- Precisión 80-90% exigida
    estado = f"Calidad: {score}/100"
    
    if aprobado:
        return True, f"APROBADO con {estado} -> " + " | ".join(reasons)
    else:
        return False, f"DESCARTADO por baja calidad ({estado}) -> " + " | ".join(reasons)

# --- EJECUCIÓN ---
def ejecutar_demostracion():
    print("="*70)
    print(" SIMULADOR DE LÓGICA DE ENTRADAS: VIEJA VS NUEVA".center(70))
    print("="*70 + "\n")

    for c in candidates:
        print(f"🪙  ANALIZANDO {c['symbol']}")
        print(f"   Contexto: {c['description']}")
        print(f"   Datos -> Vol: {c['vol_spike']}x | Runup: {c['runup_pct']}% | Heat: {c['heat']} | FSM: {c['fsm_score']}")
        
        # Test Vieja Lógica
        old_pass, old_msg = vieja_logica_rigida(c)
        print(f"   [Vieja Lógica] -> {old_msg}")
        
        # Test Nueva Lógica
        new_pass, new_msg = nueva_logica_precision(c)
        print(f"   [Nueva Lógica] -> {new_msg}")
        print("-" * 70)

    print("\n" + "="*70)
    print(" SIMULADOR DE LOGIN DE DASHBOARD".center(70))
    print("="*70)
    print("Configuración actual (.env en AWS): APP_PASSWORD=Ojmp*.*1066867590")
    print("  -> Resultado: Pantalla de login obligatoria para entrar al Grid Bot.")
    print("Configuración propuesta (auth_enabled = False permanente en código):")
    print("  -> Resultado: Bypass automático. El usuario es inyectado como admin sin pedir contraseña.")
    print("="*70 + "\n")

if __name__ == "__main__":
    ejecutar_demostracion()
