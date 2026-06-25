"""
PALMERO Paper Trading Bot — Binance Futures Testnet
====================================================
Recibe señales de PCELER v2 (webhook o polling) y ejecuta trades
automáticamente en Binance Futures Testnet (dinero ficticio).

Gestión de posición:
  - Entry: al recibir señal LONG o SHORT
  - SL: -3% desde entrada
  - TP1: +1.5% → cierra 40%, stop sube a breakeven
  - TP2: +2.5% → cierra 30%
  - Caballo: 30% restante con trailing stop 1.5%

Desplegable en Railway.
"""

import os
import time
import json
import threading
from datetime import datetime, timezone
from flask import Flask, jsonify, request
from binance.client import Client
from binance.enums import *
from binance.exceptions import BinanceAPIException

app = Flask(__name__)

# ─── CONFIGURACIÓN ───
API_KEY = os.environ.get("BINANCE_TESTNET_KEY", "")
API_SECRET = os.environ.get("BINANCE_TESTNET_SECRET", "")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "5448802464")

# SL/TP configuración (Escala XL)
SL_PCT = 0.03        # -3%
TP1_PCT = 0.015      # +1.5%
TP1_PESO = 0.40      # 40%
TP2_PCT = 0.025      # +2.5%
TP2_PESO = 0.30      # 30%
TRAILING_PCT = 0.015  # 1.5% trailing para caballo

# Tamaño de posición
POSITION_USDT = 100  # $100 por trade en testnet

# Símbolos soportados
SYMBOLS_CONFIG = {
    "XRPUSDT": {"precision_qty": 0, "precision_price": 4, "min_qty": 1},
    "SOLUSDT": {"precision_qty": 1, "precision_price": 2, "min_qty": 0.1},
}

# Estado
_client = None
_trades = []  # historial de trades
_active_positions = {}  # posiciones activas: {symbol: {...}}
_lock = threading.Lock()

POLL_INTERVAL = 30  # segundos entre checks de posiciones abiertas


def get_client():
    global _client
    if _client is None and API_KEY:
        _client = Client(API_KEY, API_SECRET, testnet=True)
        _client.FUTURES_URL = "https://testnet.binancefuture.com/fapi"
    return _client


def enviar_telegram(mensaje):
    """Envia mensaje a Telegram."""
    if not TELEGRAM_TOKEN:
        return
    try:
        import requests as req
        req.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": mensaje, "parse_mode": "HTML"},
            timeout=10
        )
    except Exception as e:
        print(f"[TG] Error: {e}")


def calcular_cantidad(symbol, precio):
    """Calcula cantidad a comprar con POSITION_USDT."""
    cfg = SYMBOLS_CONFIG.get(symbol, {})
    qty = POSITION_USDT / precio
    precision = cfg.get("precision_qty", 0)
    qty = round(qty, precision)
    if precision == 0:
        qty = int(qty)
    return max(qty, cfg.get("min_qty", 1))


def abrir_posicion(symbol, tipo, precio_senal):
    """Abre una posición en el testnet."""
    client = get_client()
    if not client:
        return {"error": "sin conexion a Binance testnet"}

    with _lock:
        if symbol in _active_positions:
            return {"error": f"ya hay posicion abierta en {symbol}"}

    try:
        # Obtener precio actual
        ticker = client.futures_symbol_ticker(symbol=symbol)
        precio_actual = float(ticker["price"])

        # Calcular cantidad
        qty_total = calcular_cantidad(symbol, precio_actual)
        cfg = SYMBOLS_CONFIG.get(symbol, {})
        prec_qty = cfg.get("precision_qty", 0)
        prec_price = cfg.get("precision_price", 4)

        # Abrir posición market
        side = SIDE_BUY if tipo == "LONG" else SIDE_SELL
        order = client.futures_create_order(
            symbol=symbol,
            side=side,
            type=ORDER_TYPE_MARKET,
            quantity=qty_total,
        )

        precio_entrada = precio_actual

        # Calcular niveles
        if tipo == "LONG":
            sl_price = round(precio_entrada * (1 - SL_PCT), prec_price)
            tp1_price = round(precio_entrada * (1 + TP1_PCT), prec_price)
            tp2_price = round(precio_entrada * (1 + TP2_PCT), prec_price)
        else:
            sl_price = round(precio_entrada * (1 + SL_PCT), prec_price)
            tp1_price = round(precio_entrada * (1 - TP1_PCT), prec_price)
            tp2_price = round(precio_entrada * (1 - TP2_PCT), prec_price)

        # Calcular cantidades parciales
        qty_tp1 = round(qty_total * TP1_PESO, prec_qty)
        qty_tp2 = round(qty_total * TP2_PESO, prec_qty)
        qty_caballo = qty_total - qty_tp1 - qty_tp2
        if prec_qty == 0:
            qty_tp1 = int(qty_tp1)
            qty_tp2 = int(qty_tp2)
            qty_caballo = int(qty_caballo)

        posicion = {
            "symbol": symbol,
            "tipo": tipo,
            "precio_entrada": precio_entrada,
            "precio_senal": precio_senal,
            "qty_total": qty_total,
            "qty_tp1": qty_tp1,
            "qty_tp2": qty_tp2,
            "qty_caballo": qty_caballo,
            "sl_price": sl_price,
            "tp1_price": tp1_price,
            "tp2_price": tp2_price,
            "tp1_tocado": False,
            "tp2_tocado": False,
            "breakeven": False,
            "trailing_activado": False,
            "trailing_stop": None,
            "mejor_precio": precio_entrada,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "order_id": order.get("orderId"),
            "resultado": None,
        }

        with _lock:
            _active_positions[symbol] = posicion

        msg = (f"🟢 <b>PAPER TRADE ABIERTO</b>\n"
               f"📊 {symbol} {tipo}\n"
               f"💰 Entrada: {precio_entrada}\n"
               f"📏 Cantidad: {qty_total}\n"
               f"🛑 SL: {sl_price} (-3%)\n"
               f"🎯 TP1: {tp1_price} (+1.5%) → 40%\n"
               f"🎯 TP2: {tp2_price} (+2.5%) → 30%\n"
               f"🐎 Caballo: {qty_caballo} con trailing {TRAILING_PCT*100}%")
        enviar_telegram(msg)
        print(f"[TRADE] Abierto {symbol} {tipo} @ {precio_entrada}")

        return posicion

    except BinanceAPIException as e:
        return {"error": f"Binance API: {e}"}
    except Exception as e:
        return {"error": str(e)}


def cerrar_parcial(symbol, qty, razon):
    """Cierra una parte de la posición."""
    client = get_client()
    if not client:
        return
    try:
        pos = _active_positions.get(symbol)
        if not pos:
            return
        side = SIDE_SELL if pos["tipo"] == "LONG" else SIDE_BUY
        cfg = SYMBOLS_CONFIG.get(symbol, {})
        prec_qty = cfg.get("precision_qty", 0)
        if prec_qty == 0:
            qty = int(qty)
        if qty <= 0:
            return
        client.futures_create_order(
            symbol=symbol,
            side=side,
            type=ORDER_TYPE_MARKET,
            quantity=qty,
        )
        print(f"[TRADE] Cerrado parcial {symbol} qty={qty} razon={razon}")
    except Exception as e:
        print(f"[TRADE] Error cerrando parcial {symbol}: {e}")


def cerrar_total(symbol, razon):
    """Cierra toda la posición."""
    client = get_client()
    if not client:
        return
    try:
        pos = _active_positions.get(symbol)
        if not pos:
            return
        # Calcular qty restante
        qty_restante = pos["qty_total"]
        if pos["tp1_tocado"]:
            qty_restante -= pos["qty_tp1"]
        if pos["tp2_tocado"]:
            qty_restante -= pos["qty_tp2"]
        if qty_restante > 0:
            cerrar_parcial(symbol, qty_restante, razon)
    except Exception as e:
        print(f"[TRADE] Error cerrando total {symbol}: {e}")


def gestionar_posicion(symbol):
    """Revisa una posición abierta y gestiona SL/TP/trailing."""
    client = get_client()
    if not client:
        return

    with _lock:
        pos = _active_positions.get(symbol)
        if not pos:
            return

    try:
        ticker = client.futures_symbol_ticker(symbol=symbol)
        precio_actual = float(ticker["price"])
    except:
        return

    tipo = pos["tipo"]
    entrada = pos["precio_entrada"]

    if tipo == "LONG":
        cambio_pct = (precio_actual - entrada) / entrada
    else:
        cambio_pct = (entrada - precio_actual) / entrada

    # Actualizar mejor precio
    if tipo == "LONG":
        pos["mejor_precio"] = max(pos["mejor_precio"], precio_actual)
    else:
        pos["mejor_precio"] = min(pos["mejor_precio"], precio_actual)

    # CHECK SL
    if cambio_pct <= -SL_PCT and not pos["tp1_tocado"]:
        cerrar_total(symbol, "SL")
        pos["resultado"] = round(cambio_pct * 100, 2)
        msg = f"🔴 <b>PAPER SL</b> {symbol} {tipo} @ {precio_actual}\nResultado: {pos['resultado']}%"
        enviar_telegram(msg)
        with _lock:
            _trades.append(pos.copy())
            del _active_positions[symbol]
        return

    # CHECK BREAKEVEN SL (despues de TP1)
    if pos["breakeven"] and cambio_pct <= 0:
        cerrar_total(symbol, "BREAKEVEN")
        pos["resultado"] = 0
        msg = f"⚪ <b>PAPER BREAKEVEN</b> {symbol} {tipo} @ {precio_actual}"
        enviar_telegram(msg)
        with _lock:
            _trades.append(pos.copy())
            del _active_positions[symbol]
        return

    # CHECK TRAILING STOP (caballo)
    if pos["trailing_activado"] and pos["trailing_stop"]:
        if tipo == "LONG" and precio_actual <= pos["trailing_stop"]:
            cerrar_parcial(symbol, pos["qty_caballo"], "TRAILING")
            trail_pct = round((pos["trailing_stop"] - entrada) / entrada * 100, 2)
            pos["resultado"] = trail_pct
            msg = f"🐎 <b>PAPER TRAILING</b> {symbol} {tipo} @ {precio_actual}\nCaballo cerrado: +{trail_pct}%"
            enviar_telegram(msg)
            with _lock:
                _trades.append(pos.copy())
                del _active_positions[symbol]
            return
        elif tipo == "SHORT" and precio_actual >= pos["trailing_stop"]:
            cerrar_parcial(symbol, pos["qty_caballo"], "TRAILING")
            trail_pct = round((entrada - pos["trailing_stop"]) / entrada * 100, 2)
            pos["resultado"] = trail_pct
            msg = f"🐎 <b>PAPER TRAILING</b> {symbol} {tipo} @ {precio_actual}\nCaballo cerrado: +{trail_pct}%"
            enviar_telegram(msg)
            with _lock:
                _trades.append(pos.copy())
                del _active_positions[symbol]
            return

    # CHECK TP1
    if not pos["tp1_tocado"] and cambio_pct >= TP1_PCT:
        cerrar_parcial(symbol, pos["qty_tp1"], "TP1")
        pos["tp1_tocado"] = True
        pos["breakeven"] = True
        msg = f"🎯 <b>PAPER TP1</b> {symbol} {tipo} @ {precio_actual}\n40% cerrado a +1.5%. Stop → breakeven"
        enviar_telegram(msg)

    # CHECK TP2
    if not pos["tp2_tocado"] and cambio_pct >= TP2_PCT:
        cerrar_parcial(symbol, pos["qty_tp2"], "TP2")
        pos["tp2_tocado"] = True
        pos["trailing_activado"] = True
        # Activar trailing stop
        cfg = SYMBOLS_CONFIG.get(symbol, {})
        prec = cfg.get("precision_price", 4)
        if tipo == "LONG":
            pos["trailing_stop"] = round(precio_actual * (1 - TRAILING_PCT), prec)
        else:
            pos["trailing_stop"] = round(precio_actual * (1 + TRAILING_PCT), prec)
        msg = f"🎯 <b>PAPER TP2</b> {symbol} {tipo} @ {precio_actual}\n30% cerrado a +2.5%. 🐎 Trailing activado ({TRAILING_PCT*100}%)"
        enviar_telegram(msg)

    # ACTUALIZAR TRAILING
    if pos["trailing_activado"]:
        cfg = SYMBOLS_CONFIG.get(symbol, {})
        prec = cfg.get("precision_price", 4)
        if tipo == "LONG":
            nuevo_trail = round(precio_actual * (1 - TRAILING_PCT), prec)
            if nuevo_trail > (pos["trailing_stop"] or 0):
                pos["trailing_stop"] = nuevo_trail
        else:
            nuevo_trail = round(precio_actual * (1 + TRAILING_PCT), prec)
            if pos["trailing_stop"] is None or nuevo_trail < pos["trailing_stop"]:
                pos["trailing_stop"] = nuevo_trail


def monitor_loop():
    """Bucle que revisa posiciones abiertas cada N segundos."""
    time.sleep(15)
    print(f"[MONITOR] Arrancando monitor de posiciones (cada {POLL_INTERVAL}s)")
    while True:
        try:
            symbols = list(_active_positions.keys())
            for symbol in symbols:
                gestionar_posicion(symbol)
        except Exception as e:
            print(f"[MONITOR] Error: {e}")
        time.sleep(POLL_INTERVAL)


# ─── ENDPOINTS ───

@app.route("/")
def home():
    return jsonify({
        "servicio": "PALMERO Paper Trading Bot",
        "version": "1.0",
        "testnet": True,
        "posiciones_activas": len(_active_positions),
        "trades_completados": len(_trades),
        "config": {
            "sl": f"-{SL_PCT*100}%",
            "tp1": f"+{TP1_PCT*100}% (40%)",
            "tp2": f"+{TP2_PCT*100}% (30%)",
            "trailing": f"{TRAILING_PCT*100}%",
            "position_usdt": POSITION_USDT,
        },
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
    })


@app.route("/webhook", methods=["POST"])
def webhook():
    """Recibe señales de TradingView o PCELER."""
    try:
        body = request.get_data(as_text=True)
        print(f"[WEBHOOK] Recibido: {body}")

        # Parsear: "XRPUSDT PCELER LONG 1.085" o "SOLUSDT PCELER SHORT 68.5"
        partes = body.strip().split()
        if len(partes) < 3:
            return jsonify({"error": "formato invalido", "body": body}), 400

        symbol = partes[0].upper()
        tipo = None
        precio = 0

        for p in partes:
            if "LONG" in p.upper():
                tipo = "LONG"
            elif "SHORT" in p.upper():
                tipo = "SHORT"
            try:
                precio = float(p)
            except:
                pass

        if not tipo:
            return jsonify({"error": "no se encontro LONG o SHORT"}), 400

        if symbol not in SYMBOLS_CONFIG:
            return jsonify({"error": f"simbolo no soportado: {symbol}"}), 400

        resultado = abrir_posicion(symbol, tipo, precio)
        return jsonify({"ok": True, "trade": resultado})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/abrir/<symbol>/<tipo>")
def abrir_manual(symbol, tipo):
    """Abrir posición manualmente (para testing)."""
    symbol = symbol.upper()
    tipo = tipo.upper()
    if symbol not in SYMBOLS_CONFIG:
        return jsonify({"error": f"simbolo no soportado: {symbol}"}), 400
    if tipo not in ("LONG", "SHORT"):
        return jsonify({"error": "tipo debe ser LONG o SHORT"}), 400
    resultado = abrir_posicion(symbol, tipo, 0)
    return jsonify(resultado)


@app.route("/posiciones")
def posiciones():
    """Ver posiciones activas."""
    client = get_client()
    precios = {}
    if client:
        for s in _active_positions:
            try:
                t = client.futures_symbol_ticker(symbol=s)
                precios[s] = float(t["price"])
            except:
                pass

    pos_info = {}
    for s, p in _active_positions.items():
        precio_actual = precios.get(s, 0)
        if p["tipo"] == "LONG":
            pnl = (precio_actual - p["precio_entrada"]) / p["precio_entrada"] * 100 if precio_actual else 0
        else:
            pnl = (p["precio_entrada"] - precio_actual) / p["precio_entrada"] * 100 if precio_actual else 0
        pos_info[s] = {
            "tipo": p["tipo"],
            "entrada": p["precio_entrada"],
            "precio_actual": precio_actual,
            "pnl_pct": round(pnl, 3),
            "sl": p["sl_price"],
            "tp1": p["tp1_price"],
            "tp1_tocado": p["tp1_tocado"],
            "tp2": p["tp2_price"],
            "tp2_tocado": p["tp2_tocado"],
            "trailing": p["trailing_stop"],
            "caballo_activo": p["trailing_activado"],
            "timestamp": p["timestamp"],
        }

    return jsonify({
        "posiciones_activas": pos_info,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
    })


@app.route("/historial")
def historial():
    """Ver trades completados."""
    resumen = []
    for t in _trades:
        resumen.append({
            "symbol": t["symbol"],
            "tipo": t["tipo"],
            "entrada": t["precio_entrada"],
            "resultado_pct": t.get("resultado", 0),
            "timestamp": t["timestamp"],
        })

    total = sum(t.get("resultado", 0) or 0 for t in _trades)
    ganadoras = sum(1 for t in _trades if (t.get("resultado") or 0) > 0)
    n = len(_trades)

    return jsonify({
        "n_trades": n,
        "ganadoras": ganadoras,
        "winrate": round(ganadoras / n * 100, 1) if n > 0 else 0,
        "total_pct": round(total, 2),
        "media_pct": round(total / n, 3) if n > 0 else 0,
        "trades": resumen,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
    })


@app.route("/balance")
def balance():
    """Ver balance del testnet."""
    client = get_client()
    if not client:
        return jsonify({"error": "sin conexion"}), 500
    try:
        balances = client.futures_account_balance()
        result = {}
        for b in balances:
            if float(b["balance"]) > 0:
                result[b["asset"]] = {
                    "balance": float(b["balance"]),
                    "available": float(b.get("availableBalance", b["balance"])),
                }
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/cerrar/<symbol>")
def cerrar_manual(symbol):
    """Cerrar posición manualmente."""
    symbol = symbol.upper()
    if symbol not in _active_positions:
        return jsonify({"error": f"no hay posicion abierta en {symbol}"}), 404
    cerrar_total(symbol, "MANUAL")
    pos = _active_positions.pop(symbol, {})
    _trades.append(pos)
    return jsonify({"ok": True, "cerrado": symbol})


# ─── ARRANQUE ───

_monitor_thread = threading.Thread(target=monitor_loop, daemon=True)
_monitor_thread.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    app.run(host="0.0.0.0", port=port)
