#!/usr/bin/env python3
import platform
import logging
import logging.handlers
import time
import os
import asyncio
import json
from collections import deque
from binance import AsyncClient
from binance.enums import *
from binance.exceptions import BinanceAPIException
import websockets
from tenacity import retry, wait_exponential, stop_after_attempt
from rich.live import Live
from rich.table import Table
from rich.text import Text
from rich.layout import Layout
from rich.panel import Panel
from rich.console import Console
from rich import box
from colorama import init
import psutil
from telegram.ext import Updater
import telegram

console = Console()

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
file_handler = logging.handlers.RotatingFileHandler("risk_management_bot.log", maxBytes=5*1024*1024, backupCount=3)
stream_handler = logging.StreamHandler()
file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
stream_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logger.handlers = [file_handler, stream_handler]
logger.info("Logging configurado correctamente.")

TELEGRAM_CONFIG = {
    'ENABLED': True,
    'BOT_TOKEN': '7740238959:AAE3zxuYVZt-R85ti4xddLhnw0GVdnBJUyI',  # REEMPLAZA con tu token
    'CHAT_ID': '6118142446'  # REEMPLAZA con tu chat_id
}

async def send_telegram_notification(message):
    try:
        bot = telegram.Bot(token=TELEGRAM_CONFIG['BOT_TOKEN'])
        await bot.send_message(
            chat_id=TELEGRAM_CONFIG['CHAT_ID'],
            text=message,
            parse_mode='HTML'
        )
    except Exception as e:
        logger.error(f"Error enviando notificación Telegram: {e}")

CONFIG = {
    'SYMBOL': 'API3USDT',                     # Símbolo de trading en Binance Futures
    'MAX_LEVERAGE': 25,                      # Apalancamiento máximo permitido
    'WEBSOCKET_URL': 'wss://fstream.binance.com/ws',  # URL del WebSocket para datos en tiempo real
    'TESTNET': False,                        # Indica si se usa la red de prueba (False = red real)
    'RECV_WINDOW': 15000,                    # Ventana de recepción en milisegundos para solicitudes API
    'FEE_RATE_MAKER': 0.0002,                # Tasa de comisión para órdenes tipo maker
    'TAKE_PROFIT_LEVELS': [0.4, 0.4, 0.2],   # PORCENTAJE DE CIERRE EN CADA TAKE PROFIT
    'TP_MULTIPLIERS': [4.0, 7.0, 9.0],       # DISTANCIA ENTRE PRECIO ENTRADA Y TAKE PROFITS
    'SL_ATR_MULTIPLIER': 2.0,                # DISTANCIA ENTRE PRECIO ENTRADA Y STOP LOSS INICIAL
    'ATR_PERIOD_LONG': 14,                   # Período para calcular ATR largo
    'ATR_PERIOD_SHORT': 7,                   # Período para calcular ATR corto
    'STOP_LOSS_FLUCTUATION_MARGIN': 0.0020,  # Margen adicional para el stop loss
    'MIN_TRAILING_STOP_MOVE': 0.0002,        # Movimiento mínimo del trailing stop para actualización
    'TRAILING_STOP_SENSITIVITY': 3.0,        # Sensibilidad para ajustes del trailing stop
    'TRAILING_STOP_MULTIPLIER': 3.0,         # Multiplicador de ATR para el trailing stop
    'WEBSOCKET_TIMEOUT': 10.0,               # Tiempo máximo de espera para mensajes WebSocket (segundos)
    'PING_INTERVAL': 10.0,                   # Intervalo de envío de pings al WebSocket (segundos)
    'MAX_DRAWDOWN': 0.10,                    # Máximo drawdown permitido antes de cerrar posición
    'POSITION_CHECK_INTERVAL': 0.5,          # Intervalo de verificación de posiciones (segundos)
    'ATR_INTERVAL': 10.0,                    # Intervalo para calcular ATR (segundos)
    'SYNC_INTERVAL': 30,                     # Intervalo de sincronización de tiempo (segundos)
    'EQUITY_UPDATE_INTERVAL': 10.0,          # Intervalo de actualización del equity (segundos)
    'UI_UPDATE_MIN_INTERVAL': 0.1,           # Intervalo mínimo de actualización de la interfaz (segundos)
    'ORDER_CHECK_INTERVAL': 1.0              # Intervalo de verificación de órdenes (segundos)
}

symbol_info = {}
symbol_info_lock = asyncio.Lock()
bot = None
websocket_status = "Disconnected"
server_time_offset = 0
last_sync_time = 0
initial_balance = None
initial_available_balance = None
cached_equity = None
last_equity_update = 0
ui = None
last_ui_update = 0
metrics = None

init()
if platform.system() == "Windows":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

class BotMetrics:
    def __init__(self):
        self.websocket_reconnects = 0
        self.api_calls = 0
        self.api_errors = 0
        self.last_price_updates = deque(maxlen=100)
        self.memory_samples = deque(maxlen=60)

    def add_price_update(self, price):
        self.last_price_updates.append((time.time(), price))

    def get_price_update_frequency(self):
        if len(self.last_price_updates) < 2:
            return 0
        times = [t for t, _ in self.last_price_updates]
        return len(times) / (times[-1] - times[0]) if times[-1] != times[0] else 0

    def record_api_call(self, success=True):
        self.api_calls += 1
        if not success:
            self.api_errors += 1

class HeartbeatMonitor:
    def __init__(self):
        self.last_heartbeats = {}
        self.thresholds = {'websocket': 60, 'position_monitor': 5, 'order_monitor': 5}

    def update(self, component):
        self.last_heartbeats[component] = time.time()

    def check(self):
        current_time = time.time()
        return [f"{comp} no responde por {current_time - last:.1f}s" 
                for comp, last in self.last_heartbeats.items() 
                if current_time - last > self.thresholds[comp]]

def create_init_table():
    table = Table(title="Inicializando Risk Management Bot v4.0.7", box=box.ROUNDED, border_style="cyan", show_header=True)
    table.add_column("Parámetro", style="bold white", justify="left")
    table.add_column("Valor", style="white", justify="right")
    return table

async def display_init_progress(client):
    with Live(console=console, refresh_per_second=4) as live:
        init_table = create_init_table()
        init_table.add_row("Símbolo", Text(CONFIG['SYMBOL'], style="bold cyan"))
        init_table.add_row("Apalancamiento", Text(f"{CONFIG['MAX_LEVERAGE']}x", style="bold cyan"))
        init_table.add_row("Cliente Binance", Text("Conectando...", style="yellow"))
        live.update(init_table)
        await asyncio.sleep(0.5)

        init_table = create_init_table()
        init_table.add_row("Símbolo", Text(CONFIG['SYMBOL'], style="bold cyan"))
        init_table.add_row("Apalancamiento", Text(f"{CONFIG['MAX_LEVERAGE']}x", style="bold cyan"))
        init_table.add_row("Cliente Binance", Text("✔ Conectado", style="green"))
        init_table.add_row("Sincronización Tiempo", Text("Sincronizando...", style="yellow"))
        live.update(init_table)
        await synchronize_time(client)
        init_table.add_row("Sincronización Tiempo", Text(f"✔ Offset: {server_time_offset}ms", style="green"))
        live.update(init_table)
        await asyncio.sleep(0.5)

        init_table.add_row("Configuración Futuros", Text("Configurando...", style="yellow"))
        live.update(init_table)
        await configure_futures(client)
        init_table.add_row("Configuración Futuros", Text("✔ Listo", style="green"))
        live.update(init_table)
        await asyncio.sleep(0.5)

        init_table.add_row("Equity Inicial", Text("Calculando...", style="yellow"))
        live.update(init_table)
        global initial_balance, initial_available_balance
        account_info = await client.futures_account(timestamp=int(time.time() * 1000) + server_time_offset, recvWindow=CONFIG['RECV_WINDOW'])
        initial_balance = await RiskManagementBot().get_equity(client) or 0.0
        initial_available_balance = float(account_info['availableBalance'])
        init_table.add_row("Equity Inicial", Text(f"{initial_balance:.2f} USDT", style="bold green" if initial_balance > 0 else "red"))
        live.update(init_table)
        await asyncio.sleep(0.5)

    console.print(Panel(Text("¡Bot Iniciado - Gestión de Riesgos Activa!", style="bold green", justify="center"), border_style="green", expand=False))

class TerminalUI:
    def __init__(self):
        self.live = Live(auto_refresh=False)
        self.theme = {
            "header": "bold white on #1A237E", "text": "white", "positive": "#00C853", "negative": "#D81B60",
            "border": "#3F51B5", "highlight": "#FFD600", "info": "#0288D1", "warning": "#F57C00",
            "neutral": "#B0BEC5", "secured": "#AB47BC", "session_loss": "#F57C00", "session_gain": "#AB47BC"
        }

    def create_table(self, title, columns, highlight=False):
        table = Table(title=Text(title, style=self.theme["header"]), box=box.MINIMAL_DOUBLE_HEAD, 
                      border_style=self.theme["border"], show_header=True, header_style="bold white", padding=(0, 1))
        for col in columns:
            table.add_column(col, style=self.theme["text"], justify="right" if col == "Valor" else "left")
        if highlight:
            table.title_style = f"bold {self.theme['highlight']} on #1A237E"
        return table

    def generate_layout(self, bot, equity, websocket_status, notifications):
        layout = Layout()
        layout.split_column(Layout(name="top", ratio=2), Layout(name="bottom", ratio=5))
        layout["top"].split_row(Layout(name="position", ratio=1), Layout(name="price", ratio=1), Layout(name="indicators", ratio=1))
        layout["bottom"].split_row(Layout(name="notifications", ratio=1), Layout(name="status", ratio=1))

        table_position = self.create_table("Posición Activa", ["Parámetro", "Valor"], highlight=True)
        if bot.in_position:
            position_side_symbol = "↑ " if bot.position_side == 'LONG' else "↓ "
            table_position.add_row("Estado", Text("True", style=self.theme["info"]))
            table_position.add_row("Lado", Text(f"{position_side_symbol}{bot.position_side}", style=self.theme["positive"]))
            table_position.add_row("Cantidad", Text(f"{bot.trade_quantity:.{symbol_info.get('quantity_precision', 0)}f}", style=self.theme["info"]))
            table_position.add_row("Entrada", Text(f"{bot.entry_price:.4f}", style=self.theme["info"]))
            table_position.add_row("Trailing Stop", Text(f"{bot.trailing_stop:.4f}" if bot.trailing_stop else "N/A", style=self.theme["negative"]))
            float_style = self.theme["positive"] if bot.floating_profit >= 0 else self.theme["negative"]
            table_position.add_row("Ganancia Flotante", Text(f"{bot.floating_profit:.4f}", style=float_style))
            for i, (tp_price, percentage, reached) in enumerate(zip(bot.take_profit_levels, CONFIG['TAKE_PROFIT_LEVELS'], bot.tp_reached)):
                checkmark = " ✔" if reached else ""
                table_position.add_row(f"TP {i+1}", Text(f"{tp_price:.4f} ({percentage*100:.0f}%){checkmark}", style=self.theme["positive"] if reached else self.theme["info"]))
            secured_style = self.theme["secured"] if bot.secured_profit > 0 else self.theme["negative"]
            table_position.add_row("Ganancia Asegurada", Text(f"${bot.secured_profit:.4f}", style=secured_style))
        else:
            for param in ["Estado", "Lado", "Cantidad", "Entrada", "Trailing Stop", "Ganancia Flotante", "Ganancia Asegurada"]:
                table_position.add_row(param, Text("False" if param == "Estado" else "N/A", style=self.theme["neutral"]))
        layout["position"].update(Panel(table_position, border_style=self.theme["border"], padding=(0, 1)))

        table_price = self.create_table("Precio Actual", ["Símbolo", "Valor"])
        table_price.add_row(CONFIG['SYMBOL'], Text(f"{bot.current_price:.4f}" if bot.current_price else "N/A", style=self.theme["info"]))
        layout["price"].update(Panel(table_price, border_style=self.theme["border"], padding=(0, 1)))

        table_indicators = self.create_table("Indicadores", ["Indicador", "Valor"])
        atr_long_text = f"{len(bot.prices_long)}/{CONFIG['ATR_PERIOD_LONG']}" if bot.atr_long == 0 else f"{bot.atr_long:.6f}"
        atr_short_text = f"{len(bot.prices_short)}/{CONFIG['ATR_PERIOD_SHORT']}" if bot.atr_short == 0 else f"{bot.atr_short:.6f}"
        table_indicators.add_row("ATR Largo", Text(atr_long_text, style=self.theme["info"]))
        table_indicators.add_row("ATR Corto", Text(atr_short_text, style=self.theme["info"]))
        table_indicators.add_row("Tiempo Activo", Text(f"{time.time() - bot.start_time:.1f}s", style=self.theme["info"]))
        layout["indicators"].update(Panel(table_indicators, border_style=self.theme["border"], padding=(0, 1)))

        table_status = self.create_table("Estado", ["Parámetro", "Valor"])
        ws_style = self.theme["positive"] if websocket_status == "Connected" else self.theme["negative"] if websocket_status == "Disconnected" else self.theme["warning"]
        ws_symbol = "✔ " if websocket_status == "Connected" else "✖ " if websocket_status == "Disconnected" else "⟳ "
        table_status.add_row("WebSocket", Text(f"{ws_symbol}{websocket_status}", style=ws_style))
        table_status.add_row("Equity USDT", Text(f"{equity:.2f}", style=self.theme["info"]))
        table_status.add_row("Frecuencia Precios", Text(f"{metrics.get_price_update_frequency():.2f} Hz", style=self.theme["info"]))
        table_status.add_row("Reconexiones WS", Text(f"{metrics.websocket_reconnects}", style=self.theme["warning"] if metrics.websocket_reconnects > 0 else self.theme["info"]))
        if bot.closed_operations:
            closed_ops_text = Text()
            for i, op in enumerate(bot.closed_operations):
                profit_style = self.theme["session_gain"] if op['profit'] >= 0 else self.theme["session_loss"]
                profit_sign = '+' if op['profit'] >= 0 else '-'
                closed_ops_text.append(f"Operación {i+1}: {op['timestamp']} ", style=self.theme["info"])
                closed_ops_text.append(f"{profit_sign}{abs(op['profit']):.4f}\n", style=profit_style)
            table_status.add_row("Operaciones cerradas", closed_ops_text)
        else:
            table_status.add_row("Operaciones cerradas", Text("Ninguna", style=self.theme["neutral"]))
        total_pnl_style = self.theme["session_gain"] if bot.total_profit >= 0 else self.theme["session_loss"]
        total_pnl_sign = "+" if bot.total_profit >= 0 else "-"
        table_status.add_row("Ganancia/Pérdida Total", Text(f"{total_pnl_sign}${abs(bot.total_profit):.2f}", style=total_pnl_style))
        layout["status"].update(Panel(table_status, border_style=self.theme["border"], padding=(0, 1)))

        table_notifications = self.create_table("Notificaciones", ["Mensaje"])
        if not notifications:
            table_notifications.add_row(Text("Sin notificaciones recientes", style=self.theme["neutral"]))
        else:
            for msg in list(notifications)[-20:]:
                style = self.theme["warning"] if "Error" in msg else self.theme["neutral"]
                table_notifications.add_row(Text(msg, style=style))
        layout["notifications"].update(Panel(table_notifications, border_style=self.theme["border"], padding=(0, 1)))

        return layout

    async def update(self, bot, equity, websocket_status, notifications, force=False):
        global last_ui_update
        current_time = time.time()
        if force or (current_time - last_ui_update >= CONFIG['UI_UPDATE_MIN_INTERVAL']):
            try:
                self.live.update(self.generate_layout(bot, equity, websocket_status, notifications), refresh=True)
                last_ui_update = current_time
            except Exception as e:
                logger.error(f"Error al actualizar UI: {e}")

    def start(self):
        self.live.start()

    def stop(self):
        self.live.stop()

class AsyncClientManager:
    async def __aenter__(self):
        api_key, api_secret = os.getenv('BINANCE_API_KEY'), os.getenv('BINANCE_API_SECRET')
        if not api_key or not api_secret:
            logger.critical("Claves de API de Binance no configuradas.")
            raise ValueError("BINANCE_API_KEY y BINANCE_API_SECRET deben estar definidas en variables de entorno.")
        self.client = await AsyncClient.create(api_key=api_key, api_secret=api_secret, testnet=CONFIG['TESTNET'])
        logger.info(f"Cliente inicializado en red real | Leverage: {CONFIG['MAX_LEVERAGE']}x")
        return self.client

    async def __aexit__(self, exc_type, exc, tb):
        await self.client.close_connection()
        logger.info("Conexión cerrada.")

class RiskManagementBot:
    def __init__(self):
        self.bid = self.ask = self.current_price = 0.0
        self.in_position = False
        self.position_side = None
        self.trade_quantity = self.initial_quantity = self.entry_price = self.sl_price = self.trailing_stop = 0.0
        self.stop_loss_order_id = None
        self.take_profit_order_ids = []
        self.total_profit = self.floating_profit = self.secured_profit = 0.0
        self.prices_long = deque(maxlen=CONFIG['ATR_PERIOD_LONG'])
        self.prices_short = deque(maxlen=CONFIG['ATR_PERIOD_SHORT'])
        self.atr_long = self.atr_short = 0.0
        self.start_time = time.time()
        self.last_price_update = self.start_time
        self.position_lock = asyncio.Lock()
        self.trailing_update_count = 0
        self.take_profit_levels = []
        self.tp_reached = [False] * len(CONFIG['TAKE_PROFIT_LEVELS'])
        self.tp_quantities = []
        self.notifications = deque(maxlen=1000)
        self.interval_duration = CONFIG['ATR_INTERVAL']
        self.last_interval_time = self.start_time
        self.high_interval = None
        self.low_interval = None
        self.close_prev = None
        self.closed_operations = []

    def add_notification(self, message):
        self.notifications.append(f"{time.strftime('%H:%M:%S')} - {message}")
        logger.info(f"Notificación: {message}")

    async def reset_position(self, client=None):
        logger.info("Reseteando posición.")
        if self.stop_loss_order_id and client:
            try:
                await client.futures_cancel_order(symbol=CONFIG['SYMBOL'], orderId=self.stop_loss_order_id, 
                                                  timestamp=int(time.time() * 1000) + server_time_offset, 
                                                  recvWindow=CONFIG['RECV_WINDOW'])
                self.add_notification(f"Stop Loss cancelado: OrderID {self.stop_loss_order_id}")
            except BinanceAPIException:
                pass
        if self.take_profit_order_ids and client:
            for tp_id in self.take_profit_order_ids[:]:
                try:
                    await client.futures_cancel_order(symbol=CONFIG['SYMBOL'], orderId=tp_id, 
                                                      timestamp=int(time.time() * 1000) + server_time_offset, 
                                                      recvWindow=CONFIG['RECV_WINDOW'])
                    self.add_notification(f"Take Profit cancelado: OrderID {tp_id}")
                except BinanceAPIException:
                    pass
        self.in_position = False
        self.position_side = None
        self.trade_quantity = self.initial_quantity = self.entry_price = self.sl_price = self.trailing_stop = 0.0
        self.stop_loss_order_id = None
        self.take_profit_order_ids.clear()
        self.floating_profit = self.secured_profit = 0.0
        self.take_profit_levels.clear()
        self.tp_reached = [False] * len(CONFIG['TAKE_PROFIT_LEVELS'])
        self.tp_quantities.clear()
        self.trailing_update_count = 0
        self.add_notification("Posición reseteada - Bot en modo espera")
        logger.info("Reseteo completado: Bot listo para nueva posición.")
        if client:
            equity = await self.get_equity(client) or 0.0
            await ui.update(self, equity, websocket_status, self.notifications, force=True)

    def update_indicators(self):
        if self.bid <= 0 or self.ask <= 0:
            return
        new_price = (self.bid + self.ask) / 2
        if new_price == self.current_price:
            return
        self.current_price = new_price
        metrics.add_price_update(new_price)
        current_time = time.time()
        self.last_price_update = current_time

        if self.high_interval is None or self.low_interval is None:
            self.high_interval = self.low_interval = self.current_price

        self.high_interval = max(self.high_interval, self.current_price)
        self.low_interval = min(self.low_interval, self.current_price)

        if current_time - self.last_interval_time >= self.interval_duration:
            tr = (max(self.high_interval - self.low_interval,
                      abs(self.high_interval - self.close_prev),
                      abs(self.low_interval - self.close_prev)) 
                  if self.close_prev is not None 
                  else self.high_interval - self.low_interval)
            if tr > 0:
                self.prices_long.append(tr)
                self.prices_short.append(tr)
            self.atr_long = sum(self.prices_long) / min(len(self.prices_long), CONFIG['ATR_PERIOD_LONG']) if self.prices_long else 0
            self.atr_short = sum(self.prices_short) / min(len(self.prices_short), CONFIG['ATR_PERIOD_SHORT']) if self.prices_short else 0
            self.close_prev = self.current_price
            self.high_interval = self.low_interval = self.current_price
            self.last_interval_time = current_time

        if self.in_position:
            profit = (self.current_price - self.entry_price if self.position_side == 'LONG' 
                      else self.entry_price - self.current_price) * self.trade_quantity
            self.floating_profit = profit - (self.trade_quantity * (self.entry_price + self.current_price) * CONFIG['FEE_RATE_MAKER'])

    async def get_equity(self, client):
        global cached_equity, last_equity_update
        current_time = time.time()
        if current_time - last_equity_update < CONFIG['EQUITY_UPDATE_INTERVAL'] and cached_equity is not None:
            return cached_equity
        try:
            timestamp = int(time.time() * 1000) + server_time_offset
            account_info = await client.futures_account(timestamp=timestamp, recvWindow=CONFIG['RECV_WINDOW'])
            balance = float(account_info['availableBalance'])
            positions = await client.futures_position_information(symbol=CONFIG['SYMBOL'], timestamp=timestamp, recvWindow=CONFIG['RECV_WINDOW'])
            unrealized_pnl = sum(float(pos.get('unrealizedProfit', 0.0)) for pos in positions if pos['symbol'] == CONFIG['SYMBOL'])
            cached_equity = balance + unrealized_pnl
            last_equity_update = current_time
            metrics.record_api_call(True)
            return cached_equity
        except BinanceAPIException as e:
            logger.error(f"Error al obtener equity: {e}")
            metrics.record_api_call(False)
            return cached_equity or 0.0

    async def manage_manual_position(self, client):
        async with self.position_lock:
            position = await get_open_positions(client)
            if not position or float(position['positionAmt']) == 0:
                if self.in_position:
                    await self.close_position(client, "Cerrada manualmente o por SL/TP")
                return

            equity = await self.get_equity(client)
            if equity <= 0:
                self.add_notification("Equity insuficiente")
                return

            if equity < initial_balance * 0.10 and self.in_position:
                await self.close_position(client, "Margen mínimo alcanzado")
                return

            unrealized_pnl = float(position.get('unrealizedProfit', 0.0))
            current_drawdown = -unrealized_pnl / initial_available_balance if initial_available_balance > 0 and unrealized_pnl < 0 else 0
            if current_drawdown > CONFIG['MAX_DRAWDOWN'] and self.in_position:
                await self.close_position(client, "Drawdown máximo excedido")
                return

            if not self.in_position:
                if self.current_price <= 0 or time.time() - self.last_price_update > CONFIG['WEBSOCKET_TIMEOUT']:
                    self.current_price = await get_current_price(client)
                    self.last_price_update = time.time()
                    self.add_notification(f"Precio actualizado: {self.current_price:.4f}")
                self.position_side = 'LONG' if float(position['positionAmt']) > 0 else 'SHORT'
                self.entry_price = float(position['entryPrice'])
                self.initial_quantity = self.trade_quantity = round(abs(float(position['positionAmt'])), symbol_info.get('quantity_precision', 0))
                sl_distance = max(self.atr_long * CONFIG['SL_ATR_MULTIPLIER'] if self.atr_long > 0 else 0.002, 
                                  symbol_info.get('tick_size', 0.0001) * 10)
                self.sl_price = (self.entry_price - sl_distance - CONFIG['STOP_LOSS_FLUCTUATION_MARGIN'] 
                                 if self.position_side == 'LONG' 
                                 else self.entry_price + sl_distance + CONFIG['STOP_LOSS_FLUCTUATION_MARGIN'])
                self.sl_price = round(self.sl_price, symbol_info.get('price_precision', 4))
                self.trailing_stop = self.sl_price
                self.in_position = True
                await self.configure_take_profit(client)
                await self.configure_stop_loss(client)
                self.add_notification(f"Nueva posición detectada: {self.position_side} @ {self.entry_price:.4f}, Cantidad: {self.trade_quantity}")

    async def close_position(self, client, reason="Manual"):
        if not self.in_position:
            return
        try:
            timestamp = int(time.time() * 1000) + server_time_offset
            logger.info(f"Cerrando posición: {self.position_side}, Cantidad: {self.trade_quantity}, Motivo: {reason}")
            open_orders = await client.futures_get_open_orders(symbol=CONFIG['SYMBOL'], timestamp=timestamp, recvWindow=CONFIG['RECV_WINDOW'])
            for order in open_orders:
                await client.futures_cancel_order(symbol=CONFIG['SYMBOL'], orderId=order['orderId'], timestamp=timestamp, recvWindow=CONFIG['RECV_WINDOW'])
                self.add_notification(f"Orden cancelada: {order['orderId']}")

            position = await get_open_positions(client)
            if position and abs(float(position['positionAmt'])) > symbol_info.get('min_quantity', 0.001):
                close_side = 'SELL' if self.position_side == 'LONG' else 'BUY'
                quantity = round(self.trade_quantity, symbol_info.get('quantity_precision', 0))
                order = await client.futures_create_order(
                    symbol=CONFIG['SYMBOL'], side=close_side, type=ORDER_TYPE_MARKET, quantity=quantity,
                    timestamp=timestamp, recvWindow=CONFIG['RECV_WINDOW'], reduceOnly=True)
                await asyncio.sleep(1)
                order_status = await client.futures_get_order(symbol=CONFIG['SYMBOL'], orderId=order['orderId'], timestamp=timestamp, recvWindow=CONFIG['RECV_WINDOW'])
                exit_price = float(order_status.get('avgPrice', 0.0)) or await get_current_price(client)
            else:
                exit_price = await get_current_price(client)

            profit = (exit_price - self.entry_price if self.position_side == 'LONG' else self.entry_price - exit_price) * self.trade_quantity
            fees = self.trade_quantity * (self.entry_price + exit_price) * CONFIG['FEE_RATE_MAKER']
            operation_profit = profit - fees + self.secured_profit
            self.total_profit += operation_profit
            self.closed_operations.append({'timestamp': time.strftime('%H:%M:%S'), 'profit': operation_profit})
            self.add_notification(f"Posición cerrada @ {exit_price:.4f} | {reason} | Ganancia: {operation_profit:.4f}")
            logger.info(f"Posición cerrada exitosamente: Profit {operation_profit:.4f}")

            if TELEGRAM_CONFIG['ENABLED']:
                message = (
                    f"🔔 <b>Operación Cerrada</b>\n\n"
                    f"📊 Par: {CONFIG['SYMBOL']}\n"
                    f"📈 Lado: {self.position_side}\n"
                    f"💰 Entrada: {self.entry_price:.4f}\n"
                    f"💱 Salida: {exit_price:.4f}\n"
                    f"💵 Ganancia: {operation_profit:.4f} USDT\n"
                    f"📝 Razón: {reason}\n"
                    f"⏰ Hora: {time.strftime('%H:%M:%S')}"
                )
                await send_telegram_notification(message)
        
        except Exception as e:
            self.add_notification(f"Error al cerrar posición: {e}")
            logger.error(f"Error al cerrar posición: {e}")
        finally:
            await self.reset_position(client)

    def adjust_to_tick_size(self, price, tick_size, direction='up'):
        if tick_size <= 0:
            return price
        factor = price / tick_size
        adjusted = round(factor + 0.5 if direction == 'up' else factor - 0.5) * tick_size
        return round(adjusted, symbol_info.get('price_precision', 4))

    async def configure_take_profit(self, client):
        if not self.in_position or self.atr_long == 0:
            return
        tick_size = symbol_info.get('tick_size', 0.0001)
        self.take_profit_levels = [self.adjust_to_tick_size(
            (self.entry_price + multiplier * self.atr_long) if self.position_side == 'LONG' 
            else (self.entry_price - multiplier * self.atr_long), 
            tick_size, 'up' if self.position_side == 'LONG' else 'down') 
            for multiplier in CONFIG['TP_MULTIPLIERS']]
        self.tp_reached = [False] * len(self.take_profit_levels)
        self.add_notification(f"TPs calculados: {self.take_profit_levels}")

        side = 'SELL' if self.position_side == 'LONG' else 'BUY'
        timestamp = int(time.time() * 1000) + server_time_offset
        self.take_profit_order_ids.clear()
        quantities = [round(self.initial_quantity * perc) for perc in CONFIG['TAKE_PROFIT_LEVELS']]
        if sum(quantities) != self.initial_quantity:
            quantities[-1] += self.initial_quantity - sum(quantities)
        self.tp_quantities = quantities

        for i, (tp_price, quantity) in enumerate(zip(self.take_profit_levels, self.tp_quantities)):
            if quantity <= 0:
                continue
            try:
                order = await client.futures_create_order(
                    symbol=CONFIG['SYMBOL'], side=side, type="LIMIT", timeInForce="GTC",
                    price=tp_price, quantity=quantity, reduceOnly=True,
                    timestamp=timestamp, recvWindow=CONFIG['RECV_WINDOW'])
                self.take_profit_order_ids.append(order['orderId'])
                self.add_notification(f"TP {i+1} colocado: {quantity} @ {tp_price:.4f}, OrderID: {order['orderId']}")
            except BinanceAPIException as e:
                self.add_notification(f"Error al colocar TP {i+1}: {e}")

    async def configure_stop_loss(self, client):
        if not self.in_position or self.sl_price <= 0:
            return
        timestamp = int(time.time() * 1000) + server_time_offset
        sl_side = 'SELL' if self.position_side == 'LONG' else 'BUY'
        safe_sl_price = get_safe_stop_price(self.position_side, self.sl_price, self.current_price, 
                                            symbol_info.get('tick_size', 0.0001), symbol_info.get('price_precision', 4))
        try:
            sl_order = await client.futures_create_order(
                symbol=CONFIG['SYMBOL'], side=sl_side, type="STOP_MARKET", stopPrice=safe_sl_price,
                closePosition=True, timestamp=timestamp, recvWindow=CONFIG['RECV_WINDOW'])
            self.stop_loss_order_id = sl_order['orderId']
            self.add_notification(f"Stop-Loss configurado: {safe_sl_price:.4f}, OrderID: {sl_order['orderId']}")
        except BinanceAPIException as e:
            self.add_notification(f"Error al configurar SL: {e}")

    async def update_stop_loss_order(self, client):
        if not self.in_position or self.sl_price <= 0:
            return
        timestamp = int(time.time() * 1000) + server_time_offset
        if self.stop_loss_order_id:
            try:
                await client.futures_cancel_order(symbol=CONFIG['SYMBOL'], orderId=self.stop_loss_order_id, 
                                                  timestamp=timestamp, recvWindow=CONFIG['RECV_WINDOW'])
                self.add_notification(f"SL anterior cancelado: OrderID {self.stop_loss_order_id}")
            except BinanceAPIException:
                pass
        sl_side = 'SELL' if self.position_side == 'LONG' else 'BUY'
        safe_sl_price = get_safe_stop_price(self.position_side, self.sl_price, self.current_price, 
                                            symbol_info.get('tick_size', 0.0001), symbol_info.get('price_precision', 4))
        try:
            sl_order = await client.futures_create_order(
                symbol=CONFIG['SYMBOL'], side=sl_side, type="STOP_MARKET", stopPrice=safe_sl_price,
                closePosition=True, timestamp=timestamp, recvWindow=CONFIG['RECV_WINDOW'])
            self.stop_loss_order_id = sl_order['orderId']
            self.add_notification(f"Trailing Stop actualizado: {safe_sl_price:.4f}, OrderID: {sl_order['orderId']}")
        except BinanceAPIException as e:
            self.add_notification(f"Error al actualizar SL: {e}")

    async def update_trailing_stop(self, client):
        async with self.position_lock:
            if not self.in_position or self.atr_short == 0 or self.current_price == 0:
                return
            position = await get_open_positions(client)
            if not position or abs(float(position['positionAmt'])) == 0:
                await self.close_position(client, "Posición cerrada detectada en trailing stop")
                return

            fluctuation_margin = CONFIG['STOP_LOSS_FLUCTUATION_MARGIN']
            multiplier = CONFIG['TRAILING_STOP_MULTIPLIER']
            new_stop = (self.current_price - (self.atr_short * multiplier) - fluctuation_margin 
                        if self.position_side == 'LONG' 
                        else self.current_price + (self.atr_short * multiplier) + fluctuation_margin)
            new_stop = round(new_stop, symbol_info.get('price_precision', 4))

            profit_potential = (self.current_price - self.entry_price if self.position_side == 'LONG' 
                                else self.entry_price - self.current_price)
            break_even_reached = (self.position_side == 'LONG' and new_stop >= self.entry_price) or \
                                 (self.position_side == 'SHORT' and new_stop <= self.entry_price)
            sensitivity = CONFIG['TRAILING_STOP_SENSITIVITY'] / 2 if not break_even_reached else CONFIG['TRAILING_STOP_SENSITIVITY']
            should_update = (profit_potential > 0 and 
                             ((self.position_side == 'LONG' and new_stop > self.trailing_stop + CONFIG['MIN_TRAILING_STOP_MOVE']) or 
                              (self.position_side == 'SHORT' and new_stop < self.trailing_stop - CONFIG['MIN_TRAILING_STOP_MOVE'])))

            if should_update and self.trailing_update_count >= sensitivity:
                self.trailing_stop = self.sl_price = new_stop
                await self.update_stop_loss_order(client)
                self.trailing_update_count = 0
                if not break_even_reached:
                    self.add_notification(f"Trailing Stop ajustado rápido pre-break-even: {new_stop:.4f}")
            elif profit_potential > 0:
                self.trailing_update_count += 1

    async def check_take_profit(self, client):
        async with self.position_lock:
            if not self.in_position or self.current_price == 0:
                return
            position = await get_open_positions(client)
            if not position or abs(float(position['positionAmt'])) == 0:
                self.total_profit += self.secured_profit
                self.add_notification(f"Posición cerrada por TP completo | Ganancia Total: {self.total_profit:.4f}")
                await self.close_position(client, "Posición cerrada por TP completo")
                return
            self.trade_quantity = abs(float(position['positionAmt']))
            if self.trade_quantity <= symbol_info.get('min_quantity', 0.001):
                self.total_profit += self.secured_profit
                self.add_notification(f"Posición cerrada por cantidad mínima | Ganancia Total: {self.total_profit:.4f}")
                await self.close_position(client, "Posición cerrada por cantidad mínima")
                return

            timestamp = int(time.time() * 1000) + server_time_offset
            try:
                open_orders = await client.futures_get_open_orders(symbol=CONFIG['SYMBOL'], timestamp=timestamp, recvWindow=CONFIG['RECV_WINDOW'])
                open_order_ids = {order['orderId'] for order in open_orders}
            except BinanceAPIException as e:
                self.add_notification(f"Error al obtener órdenes abiertas: {e}")
                return

            for i, (tp_price, percentage) in enumerate(zip(self.take_profit_levels, CONFIG['TAKE_PROFIT_LEVELS'])):
                if not self.tp_reached[i]:
                    tp_triggered = (self.position_side == 'LONG' and self.current_price >= tp_price) or \
                                   (self.position_side == 'SHORT' and self.current_price <= tp_price)
                    if tp_triggered or (i < len(self.take_profit_order_ids) and self.take_profit_order_ids[i] not in open_order_ids):
                        self.tp_reached[i] = True
                        quantity = self.tp_quantities[i]
                        profit = (tp_price - self.entry_price if self.position_side == 'LONG' 
                                  else self.entry_price - tp_price) * quantity
                        fees = quantity * (self.entry_price + tp_price) * CONFIG['FEE_RATE_MAKER']
                        self.secured_profit += profit - fees
                        self.add_notification(f"TP {i+1} alcanzado: {tp_price:.4f} (Cantidad: {quantity})")
            if all(self.tp_reached) and self.in_position:
                self.total_profit += self.secured_profit
                self.add_notification(f"Todos los TPs alcanzados | Ganancia Total: {self.total_profit:.4f}")
                await self.close_position(client, "Todos los TPs alcanzados")

    async def check_orders_status(self, client):
        async with self.position_lock:
            if not self.in_position or not (self.stop_loss_order_id or self.take_profit_order_ids):
                return
            timestamp = int(time.time() * 1000) + server_time_offset
            try:
                if self.stop_loss_order_id:
                    sl_order = await client.futures_get_order(symbol=CONFIG['SYMBOL'], orderId=self.stop_loss_order_id, 
                                                              timestamp=timestamp, recvWindow=CONFIG['RECV_WINDOW'])
                    if sl_order['status'] == 'FILLED':
                        self.total_profit += self.secured_profit
                        sl_price = float(sl_order['stopPrice'])
                        self.add_notification(f"Posición cerrada por Stop-Loss @ {sl_price:.4f} | Ganancia Total: {self.total_profit:.4f}")
                        await self.close_position(client, "Cerrada por Stop-Loss")

                for i, tp_id in enumerate(self.take_profit_order_ids[:]):
                    if tp_id:
                        tp_order = await client.futures_get_order(symbol=CONFIG['SYMBOL'], orderId=tp_id, 
                                                                  timestamp=timestamp, recvWindow=CONFIG['RECV_WINDOW'])
                        if tp_order['status'] == 'FILLED' and not self.tp_reached[i]:
                            self.tp_reached[i] = True
                            quantity = self.tp_quantities[i]
                            tp_price = float(tp_order['price'])
                            profit = (tp_price - self.entry_price if self.position_side == 'LONG' 
                                      else self.entry_price - tp_price) * quantity
                            fees = quantity * (self.entry_price + tp_price) * CONFIG['FEE_RATE_MAKER']
                            self.secured_profit += profit - fees
                            self.add_notification(f"TP {i+1} ejecutado: {tp_price:.4f} (Cantidad: {quantity})")

                position = await get_open_positions(client)
                if not position or abs(float(position['positionAmt'])) == 0:
                    if self.in_position:
                        self.total_profit += self.secured_profit
                        self.add_notification(f"Posición cerrada completamente | Ganancia Total: {self.total_profit:.4f}")
                        await self.close_position(client, "Cierre detectado por API")
            except BinanceAPIException as e:
                self.add_notification(f"Error al verificar órdenes: {e}")
                logger.error(f"Error al verificar órdenes: {e}")

@retry(wait=wait_exponential(multiplier=1, min=2, max=10), stop=stop_after_attempt(5))
async def synchronize_time(client):
    global server_time_offset, last_sync_time
    if time.time() - last_sync_time < CONFIG['SYNC_INTERVAL']:
        return
    try:
        server_time = (await client.futures_time())['serverTime']
        server_time_offset = server_time - int(time.time() * 1000)
        last_sync_time = time.time()
        logger.info(f"Tiempo sincronizado | Offset: {server_time_offset}ms")
        metrics.record_api_call(True)
    except Exception as e:
        logger.error(f"Error al sincronizar tiempo: {e}")
        metrics.record_api_call(False)

async def get_symbol_info(client):
    try:
        info = await client.futures_exchange_info()
        for s in info['symbols']:
            if s['symbol'] == CONFIG['SYMBOL']:
                filters = {f['filterType']: f for f in s['filters']}
                return {
                    'price_precision': s['pricePrecision'],
                    'quantity_precision': s['quantityPrecision'],
                    'tick_size': float(filters['PRICE_FILTER']['tickSize']),
                    'min_quantity': float(filters['LOT_SIZE']['minQty']),
                    'min_notional': float(filters['MIN_NOTIONAL']['notional'])
                }
        logger.error(f"Símbolo {CONFIG['SYMBOL']} no encontrado.")
        return {'price_precision': 4, 'quantity_precision': 0, 'tick_size': 0.0001, 'min_quantity': 0.001, 'min_notional': 5.0}
    except BinanceAPIException as e:
        logger.error(f"Error al obtener info del símbolo: {e}")
        metrics.record_api_call(False)
        return {'price_precision': 4, 'quantity_precision': 0, 'tick_size': 0.0001, 'min_quantity': 0.001, 'min_notional': 5.0}

async def configure_futures(client):
    timestamp = int(time.time() * 1000) + server_time_offset
    try:
        await client.futures_change_leverage(symbol=CONFIG['SYMBOL'], leverage=CONFIG['MAX_LEVERAGE'], 
                                             timestamp=timestamp, recvWindow=CONFIG['RECV_WINDOW'])
        logger.info(f"Apalancamiento configurado: {CONFIG['MAX_LEVERAGE']}x")
        metrics.record_api_call(True)
    except BinanceAPIException as e:
        logger.warning(f"Apalancamiento ya configurado o no aplicable: {e}")
        metrics.record_api_call(False)
    try:
        await client.futures_change_margin_type(symbol=CONFIG['SYMBOL'], marginType='ISOLATED', 
                                                timestamp=timestamp, recvWindow=CONFIG['RECV_WINDOW'])
        logger.info("Margen configurado: ISOLATED")
        metrics.record_api_call(True)
    except BinanceAPIException as e:
        logger.warning(f"Margen ya configurado o no aplicable: {e}")
        metrics.record_api_call(False)

@retry(wait=wait_exponential(multiplier=1, min=2, max=10), stop=stop_after_attempt(5))
async def get_current_price(client):
    try:
        ticker = await client.futures_symbol_ticker(symbol=CONFIG['SYMBOL'])
        metrics.record_api_call(True)
        return float(ticker['price'])
    except BinanceAPIException as e:
        logger.error(f"Error al obtener precio actual: {e}")
        metrics.record_api_call(False)
        raise

async def initialize_prices(client, bot):
    try:
        klines = await client.futures_historical_klines(
            symbol=CONFIG['SYMBOL'], interval="1m", start_str="1 hour ago UTC", 
            limit=max(CONFIG['ATR_PERIOD_LONG'], CONFIG['ATR_PERIOD_SHORT']) * 2)
        prev_close = None
        for i, kline in enumerate(klines):
            high, low, close = float(kline[2]), float(kline[3]), float(kline[4])
            tr = high - low if i == 0 else max(high - low, abs(high - prev_close), abs(low - prev_close))
            bot.prices_long.append(tr)
            bot.prices_short.append(tr)
            prev_close = close
        bot.atr_long = sum(bot.prices_long) / CONFIG['ATR_PERIOD_LONG'] if len(bot.prices_long) >= CONFIG['ATR_PERIOD_LONG'] else 0
        bot.atr_short = sum(bot.prices_short) / CONFIG['ATR_PERIOD_SHORT'] if len(bot.prices_short) >= CONFIG['ATR_PERIOD_SHORT'] else 0
        logger.info(f"Precios inicializados: ATR Largo {bot.atr_long:.6f}, ATR Corto {bot.atr_short:.6f}")
        metrics.record_api_call(True)
    except BinanceAPIException as e:
        logger.error(f"Error al inicializar precios: {e}")
        metrics.record_api_call(False)

async def get_open_positions(client):
    try:
        positions = await client.futures_position_information(symbol=CONFIG['SYMBOL'], 
                                                              timestamp=int(time.time() * 1000) + server_time_offset, 
                                                              recvWindow=CONFIG['RECV_WINDOW'])
        metrics.record_api_call(True)
        return next((pos for pos in positions if float(pos['positionAmt']) != 0), None)
    except BinanceAPIException as e:
        logger.error(f"Error al obtener posiciones: {e}")
        metrics.record_api_call(False)
        return None

def get_safe_stop_price(position_side, desired_sl_price, current_price, tick_size, price_precision):
    desired_sl_price = round(desired_sl_price, price_precision)
    if position_side == 'LONG' and desired_sl_price >= current_price:
        return round(current_price - tick_size, price_precision)
    if position_side == 'SHORT' and desired_sl_price <= current_price:
        return round(current_price + tick_size, price_precision)
    return desired_sl_price

async def handle_websocket_message(ws, bot):
    try:
        data = await asyncio.wait_for(ws.recv(), timeout=CONFIG['WEBSOCKET_TIMEOUT'])
        event = json.loads(data)
        if event.get('e') == 'bookTicker':
            bot.bid = float(event['b'])
            bot.ask = float(event['a'])
            bot.update_indicators()
        return True
    except asyncio.TimeoutError:
        logger.warning("Timeout en WebSocket recv")
        return False
    except json.JSONDecodeError:
        logger.error("Error al decodificar mensaje WebSocket")
        return False
    except Exception as e:
        logger.error(f"Error inesperado en WebSocket: {e}")
        return False

async def start_websocket(client):
    global websocket_status
    ws_url = CONFIG['WEBSOCKET_URL']
    retry_count = 0
    max_retries = 5
    heartbeat = HeartbeatMonitor()
    while True:
        try:
            if retry_count >= max_retries:
                await asyncio.sleep(60)
                retry_count = 0
            websocket_status = "Connecting"
            logger.info(f"Iniciando conexión WebSocket (intento {retry_count + 1}/{max_retries})...")
            async with websockets.connect(ws_url, ping_interval=CONFIG['PING_INTERVAL'], ping_timeout=30, close_timeout=10, max_size=10*1024*1024) as ws:
                retry_count = 0
                subscription = {"method": "SUBSCRIBE", "params": [f"{CONFIG['SYMBOL'].lower()}@bookTicker"], "id": 1}
                await ws.send(json.dumps(subscription))
                await asyncio.wait_for(ws.recv(), timeout=5.0)
                bot.add_notification(f"WebSocket suscrito a {CONFIG['SYMBOL']}")
                websocket_status = "Connected"
                while True:
                    if await handle_websocket_message(ws, bot):
                        heartbeat.update('websocket')
                    else:
                        metrics.websocket_reconnects += 1
                        break
        except Exception as e:
            retry_count += 1
            metrics.websocket_reconnects += 1
            logger.error(f"WebSocket error (intento {retry_count}/{max_retries}): {e}")
            bot.add_notification(f"WebSocket error: {e}")
            websocket_status = "Disconnected"
            bot.current_price = await get_current_price(client)
            bot.bid = bot.current_price - symbol_info.get('tick_size', 0.0001)
            bot.ask = bot.current_price + symbol_info.get('tick_size', 0.0001)
            bot.update_indicators()
            await asyncio.sleep(min(5 * retry_count, 30))

async def monitor_positions(client):
    heartbeat = HeartbeatMonitor()
    while True:
        try:
            await bot.manage_manual_position(client)
            if bot.in_position:
                await bot.check_take_profit(client)
                await bot.update_trailing_stop(client)
            equity = await bot.get_equity(client) or 0.0
            await ui.update(bot, equity, websocket_status, bot.notifications)
            heartbeat.update('position_monitor')
            await asyncio.sleep(CONFIG['POSITION_CHECK_INTERVAL'])
        except Exception as e:
            bot.add_notification(f"Error en monitor_positions: {e}")
            logger.error(f"Error en monitor_positions: {e}")
            await asyncio.sleep(1)

async def monitor_orders(client):
    heartbeat = HeartbeatMonitor()
    while True:
        try:
            await bot.check_orders_status(client)
            heartbeat.update('order_monitor')
            await asyncio.sleep(CONFIG['ORDER_CHECK_INTERVAL'])
        except Exception as e:
            bot.add_notification(f"Error en monitor_orders: {e}")
            logger.error(f"Error en monitor_orders: {e}")
            await asyncio.sleep(1)

async def health_check(client):
    heartbeat = HeartbeatMonitor()
    while True:
        try:
            current_time = time.time()
            if current_time - bot.last_price_update > 60:
                logger.warning("No se han recibido actualizaciones de precio por 1 minuto")
                bot.add_notification("Alerta: Sin actualizaciones de precio")
                bot.current_price = await get_current_price(client)
                bot.update_indicators()
            process = psutil.Process(os.getpid())
            memory_usage = process.memory_info().rss / 1024 / 1024
            metrics.memory_samples.append((current_time, memory_usage))
            if memory_usage > 500:
                logger.warning(f"Alto uso de memoria: {memory_usage:.2f}MB")
                bot.add_notification(f"Alerta: Alto uso de memoria ({memory_usage:.2f}MB)")
            for issue in heartbeat.check():
                logger.warning(issue)
                bot.add_notification(f"Alerta: {issue}")
            await asyncio.sleep(30)
        except Exception as e:
            logger.error(f"Error en health_check: {e}")
            await asyncio.sleep(5)

async def monitor_latency(client):
    while True:
        try:
            start_time = time.time()
            await client.futures_ping()
            latency = (time.time() - start_time) * 1000
            if latency > 1000:
                logger.warning(f"Alta latencia detectada: {latency:.2f}ms")
                bot.add_notification(f"Alerta: Alta latencia API ({latency:.2f}ms)")
            metrics.record_api_call(True)
            await asyncio.sleep(30)
        except Exception as e:
            logger.error(f"Error en monitor_latency: {e}")
            metrics.record_api_call(False)
            await asyncio.sleep(5)

async def main():
    global bot, ui, metrics
    ui = TerminalUI()
    bot = RiskManagementBot()
    metrics = BotMetrics()
    async with AsyncClientManager() as client:
        await display_init_progress(client)
        async with symbol_info_lock:
            symbol_info.update(await get_symbol_info(client))
        await initialize_prices(client, bot)
        bot.add_notification(f"Bot iniciado | Equity inicial: {initial_balance:.2f} USDT")
        ui.start()
        tasks = [
            asyncio.create_task(start_websocket(client)),
            asyncio.create_task(monitor_positions(client)),
            asyncio.create_task(monitor_orders(client)),
            asyncio.create_task(health_check(client)),
            asyncio.create_task(monitor_latency(client))
        ]
        try:
            await asyncio.gather(*tasks)
        except KeyboardInterrupt:
            logger.info("Interrupción por usuario (Ctrl+C).")
            console.print(Panel(Text(f"Resumen: Operaciones cerradas: {len(bot.closed_operations)}, Ganancia Total: {bot.total_profit:.4f} USDT", 
                                     style="bold yellow", justify="center"), border_style="yellow", expand=False))
            if bot.in_position:
                async with bot.position_lock:
                    await bot.close_position(client, "Cierre al apagar")
        except Exception as e:
            logger.error(f"Error crítico en main: {e}")
            bot.add_notification(f"Error crítico: {e}")
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            ui.stop()

if __name__ == "__main__":
    asyncio.run(main())