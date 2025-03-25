#!/usr/bin/env python3
"""
Bot de Señales de Entrada para Binance Futures
Versión 1.26.14: Control de posiciones únicas, EMAs, RSI SMA, órdenes automáticas 75x 30 ADA, verificación de tendencia.
Última actualización: 2025-03-23
Autor: h0ry79
"""

import asyncio
import os
import time
import json
import logging
from collections import deque
from binance import AsyncClient
from binance.enums import *
from binance.exceptions import BinanceAPIException
import websockets
from rich.live import Live
from rich.table import Table
from rich.text import Text
from rich.layout import Layout
from rich.panel import Panel
from rich.console import Console
from rich import box

console = Console()
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s',
    handlers=[
        logging.FileHandler(f'bot_signals_{time.strftime("%Y%m%d")}.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)

CONFIG = {
    'SYMBOL': 'API3USDT',                            # Símbolo del par de trading en Binance Futures (ej. LAYER/USDT)
    'LEVERAGE': 25,                                   # Nivel de apalancamiento para las órdenes de futuros
    'QUANTITY': 10,                                    # Cantidad de contratos/unidades por orden
    'EMA_SHORT_PERIOD': 5,                            # Período de la EMA corta para cálculos (7 velas)
    'EMA_LONG_PERIOD': 10,                            # Período de la EMA larga para cálculos (21 velas)
    'DELAY_MINUTES': 2,                               # Retraso en minutos para verificar cruces de EMAs
    'DISTANCE_THRESHOLD': 0.00033,                    # Umbral mínimo de distancia entre EMAs para confirmar señales
    'RSI_PERIOD': 7,                                 # Período del RSI base para cálculos (14 velas)
    'RSI_SMA_PERIOD': 7,                             # Período de la SMA aplicada al RSI (14 valores)
    'RSI_SMA_OVERBOUGHT': 60,                         # Umbral superior del RSI SMA para señal de sobrecompra
    'RSI_SMA_OVERSOLD': 40,                           # Umbral inferior del RSI SMA para señal de sobreventa
    'UPDATE_INTERVAL': 10.0,                          # Intervalo en segundos para actualizar el balance
    'UI_UPDATE_INTERVAL': 0.2,                        # Intervalo en segundos para refrescar la interfaz de usuario
    'RECV_WINDOW': 15000,                             # Ventana de recepción en milisegundos para solicitudes API
    'WEBSOCKET_TIMEOUT': 10.0,                        # Tiempo máximo en segundos para esperar datos del WebSocket
    'PING_INTERVAL': 10.0,                            # Intervalo en segundos para enviar pings al WebSocket
    'KLINE_INTERVAL': '1m',                           # Intervalo de velas para datos históricos y WebSocket (1 minuto)
    'CROSS_DETECTION_THRESHOLD': 0.00001,             # Umbral mínimo para detectar cruces iniciales de EMAs
    'MIN_TIME_BETWEEN_SIGNALS': 3,        	      # Tiempo mínimo en segundos entre señales consecutivas
    'WEBSOCKET_URL': 'wss://fstream.binance.com/ws',  # URL del WebSocket para datos en tiempo real de Binance Futures
    'TESTNET': False,                                 # Activa/desactiva el modo testnet (False para producción real)
    'VERSION': '1.26.13',                             # Versión actual del bot
    'LAST_UPDATE': '2025-03-22',                      # Fecha de la última actualización del código
    'AUTHOR': 'h0ry79'                                # Autor del bot
}

bot = None
ui = None
websocket_status = "Disconnected"
symbol_info = {}
balance = 0.0

class TerminalUI:
    def __init__(self):
        self.live = Live(auto_refresh=True)
        self.theme = {
            "header": "bold white on #1A237E",
            "text": "white",
            "positive": "#00C853",
            "negative": "#D81B60",
            "border": "#3F51B5",
            "info": "#0288D1",
            "neutral": "#B0BEC5",
            "warning": "#FFA000"
        }

    def create_table(self, title, columns):
        table = Table(title=Text(title, style=self.theme["header"]), box=box.MINIMAL_DOUBLE_HEAD,
                      border_style=self.theme["border"], show_header=True, header_style="bold white")
        for col in columns:
            table.add_column(col, style=self.theme["text"], justify="right" if col == "Valor" else "left")
        return table

    def get_rsi_style(self, rsi_value):
        if rsi_value >= CONFIG['RSI_SMA_OVERBOUGHT']:
            return self.theme["negative"]
        elif rsi_value <= CONFIG['RSI_SMA_OVERSOLD']:
            return self.theme["positive"]
        return self.theme["neutral"]

    def generate_layout(self, bot, balance):
        layout = Layout()
        layout.split_column(Layout(name="top", ratio=2), Layout(name="bottom", ratio=5))
        layout["top"].split_row(Layout(name="price", ratio=1), Layout(name="indicators", ratio=1), Layout(name="stats", ratio=1))

        table_price = self.create_table("Precio Actual", ["Símbolo", "Valor"])
        price_str = f"{bot.current_price:.{symbol_info.get('price_precision', 4)}f}"
        table_price.add_row(CONFIG['SYMBOL'], Text(price_str, style=self.theme["info"]))
        layout["price"].update(Panel(table_price, border_style=self.theme["border"]))

        table_indicators = self.create_table("Indicadores", ["Indicador", "Valor"])
        table_indicators.add_row(f"EMA {CONFIG['EMA_SHORT_PERIOD']}", Text(f"{bot.real_time_ema_short:.5f}", style=self.theme["info"]))
        table_indicators.add_row(f"EMA {CONFIG['EMA_LONG_PERIOD']}", Text(f"{bot.real_time_ema_long:.5f}", style=self.theme["info"]))
        difference = bot.real_time_ema_short - bot.real_time_ema_long
        diff_style = "positive" if difference > 0 else "negative"
        table_indicators.add_row("Diferencia", Text(f"{difference:.5f}", style=self.theme[diff_style]))
        rsi_style = self.get_rsi_style(bot.rsi_sma_value)
        table_indicators.add_row("RSI", Text(f"{bot.rsi_sma_value:.2f}", style=rsi_style))
        layout["indicators"].update(Panel(table_indicators, border_style=self.theme["border"]))

        table_stats = self.create_table("Estadísticas", ["Métrica", "Valor"])
        table_stats.add_row("Señales", Text(f"{bot.stats['signals_generated']}", style=self.theme["info"]))
        table_stats.add_row("Órdenes OK", Text(f"{bot.stats['orders_executed']}", style=self.theme["positive"]))
        table_stats.add_row("Órdenes Fallidas", Text(f"{bot.stats['failed_orders']}", style=self.theme["negative"]))
        table_stats.add_row("Reconexiones", Text(f"{bot.stats['reconnections']}", style=self.theme["warning"]))
        layout["stats"].update(Panel(table_stats, border_style=self.theme["border"]))

        table_notifications = self.create_table("Notificaciones", ["Mensaje"])
        if not bot.notifications:
            table_notifications.add_row(Text("Sin notificaciones", style=self.theme["neutral"]))
        else:
            for msg in bot.notifications:
                table_notifications.add_row(msg)
        layout["bottom"].update(Panel(table_notifications, border_style=self.theme["border"]))

        return layout

    async def update(self, bot, balance):
        self.live.update(self.generate_layout(bot, balance))

    def start(self):
        self.live.start()

    def stop(self):
        self.live.stop()

class AsyncClientManager:
    async def __aenter__(self):
        api_key, api_secret = os.getenv('BINANCE_API_KEY'), os.getenv('BINANCE_API_SECRET')
        if not api_key or not api_secret:
            raise ValueError("BINANCE_API_KEY y BINANCE_API_SECRET deben estar definidas.")
        self.client = await AsyncClient.create(api_key=api_key, api_secret=api_secret, testnet=CONFIG['TESTNET'])
        return self.client

    async def __aexit__(self, exc_type, exc, tb):
        try:
            await self.client.close_connection()
        except Exception as e:
            logging.error(f"Error al cerrar conexión: {e}")

class SignalBot:
    def __init__(self, theme, client=None):
        self.client = client
        self.current_price = 0.0
        self.ema_short = 0.0
        self.ema_long = 0.0
        self.prev_ema_short = 0.0
        self.prev_ema_long = 0.0
        self.real_time_ema_short = 0.0
        self.real_time_ema_long = 0.0
        self.last_real_time_ema_short = 0.0
        self.last_real_time_ema_long = 0.0
        self.last_cross_check_price = 0.0
        self.last_cross_time = 0
        self.last_cross_type = None
        self.rsi_period = CONFIG['RSI_PERIOD']
        self.rsi_values = deque(maxlen=self.rsi_period)
        self.rsi_gains = deque(maxlen=self.rsi_period)
        self.rsi_losses = deque(maxlen=self.rsi_period)
        self.rsi_value = 0.0
        self.last_rsi_price = 0.0
        self.rsi_sma_values = deque(maxlen=CONFIG['RSI_SMA_PERIOD'])
        self.rsi_sma_value = 0.0
        self.short_closes = deque(maxlen=CONFIG['EMA_SHORT_PERIOD'])
        self.long_closes = deque(maxlen=CONFIG['EMA_LONG_PERIOD'])
        self.notifications = deque(maxlen=30)
        self.k_short = 2 / (CONFIG['EMA_SHORT_PERIOD'] + 1)
        self.k_long = 2 / (CONFIG['EMA_LONG_PERIOD'] + 1)
        self.last_kline_time = 0
        self.theme = theme
        self.initialized = False
        self.stats = {
            'signals_generated': 0,
            'orders_executed': 0,
            'failed_orders': 0,
            'reconnections': 0
        }
        self.last_successful_update = time.time()

    def add_notification(self, message):
        console.print(f"[yellow]Notificación añadida: {message}[/yellow]")
        logging.info(f"Notificación: {message}")
        self.notifications.append(message)

    async def reconnect_client(self):
        try:
            api_key, api_secret = os.getenv('BINANCE_API_KEY'), os.getenv('BINANCE_API_SECRET')
            self.client = await AsyncClient.create(api_key=api_key, api_secret=api_secret, testnet=CONFIG['TESTNET'])
            console.print("[cyan]Cliente reconectado con éxito[/cyan]")
            logging.info("Cliente reconectado con éxito")
            self.stats['reconnections'] += 1
            return True
        except Exception as e:
            console.print(f"[red]Error al reconectar cliente: {e}[/red]")
            logging.error(f"Error al reconectar cliente: {e}")
            return False

    async def validate_client(self):
        try:
            if not self.client:
                return await self.reconnect_client()
            await asyncio.wait_for(self.client.ping(), timeout=5.0)
            return True
        except (asyncio.TimeoutError, Exception):
            return await self.reconnect_client()

    async def check_open_positions(self):
        try:
            if not await self.validate_client():
                return False, None
            position_info = await self.client.futures_position_information(
                symbol=CONFIG['SYMBOL'],
                timestamp=int(time.time() * 1000),
                recvWindow=CONFIG['RECV_WINDOW']
            )
            for pos in position_info:
                if pos['symbol'] == CONFIG['SYMBOL']:
                    amt = float(pos['positionAmt'])
                    if amt != 0:
                        side = "LONG" if amt > 0 else "SHORT"
                        return True, side
            return False, None
        except Exception as e:
            logging.error(f"Error verificando posiciones: {e}")
            return False, None

    def calculate_rsi(self, close_price, is_closed_candle=False):
        if self.last_rsi_price > 0:
            change = close_price - self.last_rsi_price
            gain = max(change, 0)
            loss = abs(min(change, 0))
            if is_closed_candle:
                self.rsi_gains.append(gain)
                self.rsi_losses.append(loss)
                if len(self.rsi_gains) == self.rsi_period:
                    avg_gain = sum(self.rsi_gains) / self.rsi_period
                    avg_loss = sum(self.rsi_losses) / self.rsi_period
                    if avg_loss == 0:
                        self.rsi_value = 100
                    else:
                        rs = avg_gain / avg_loss
                        self.rsi_value = 100 - (100 / (1 + rs))
                    self.rsi_sma_values.append(self.rsi_value)
                    self.rsi_sma_value = sum(self.rsi_sma_values) / len(self.rsi_sma_values)
            else:
                if len(self.rsi_gains) == self.rsi_period:
                    alpha = 2 / (self.rsi_period + 1)
                    smoothed_gain = (gain * alpha) + (self.rsi_gains[-1] * (1 - alpha))
                    smoothed_loss = (loss * alpha) + (self.rsi_losses[-1] * (1 - alpha))
                    if smoothed_loss == 0:
                        real_time_rsi = 100
                    else:
                        rs = smoothed_gain / smoothed_loss
                        real_time_rsi = 100 - (100 / (1 + rs))
                    self.rsi_value = real_time_rsi
                    temp_sma_values = list(self.rsi_sma_values)
                    if temp_sma_values:
                        temp_sma_values[-1] = real_time_rsi
                        self.rsi_sma_value = sum(temp_sma_values) / len(temp_sma_values)
        if is_closed_candle:
            self.last_rsi_price = close_price

    async def initialize_emas(self):
        try:
            klines = await self.client.futures_historical_klines(
                symbol=CONFIG['SYMBOL'],
                interval=CONFIG['KLINE_INTERVAL'],
                start_str="120 minutes ago UTC"
            )
            if not klines:
                raise ValueError("No se obtuvieron datos históricos.")
            closes = [float(kline[4]) for kline in klines]
            console.print(f"[yellow]Se obtuvieron {len(closes)} velas históricas.[/yellow]")
            logging.info(f"Se obtuvieron {len(closes)} velas históricas")
            self.short_closes.extend(closes[-CONFIG['EMA_SHORT_PERIOD']:])
            self.long_closes.extend(closes[-CONFIG['EMA_LONG_PERIOD']:])
            self.ema_short = sum(self.short_closes) / len(self.short_closes)
            self.ema_long = sum(self.long_closes) / len(self.long_closes)
            self.last_rsi_price = closes[0]
            for price in closes[1:]:
                self.calculate_rsi(price, is_closed_candle=True)
            self.real_time_ema_short = self.ema_short
            self.real_time_ema_long = self.ema_long
            self.last_real_time_ema_short = self.ema_short
            self.last_real_time_ema_long = self.ema_long
            self.prev_ema_short = self.ema_short
            self.prev_ema_long = self.ema_long
            self.current_price = closes[-1]
            self.last_kline_time = int(klines[-1][6])
            await self.client.futures_change_leverage(
                symbol=CONFIG['SYMBOL'],
                leverage=CONFIG['LEVERAGE'],
                timestamp=int(time.time() * 1000),
                recvWindow=CONFIG['RECV_WINDOW']
            )
            console.print(f"[cyan]Apalancamiento configurado a {CONFIG['LEVERAGE']}x[/cyan]")
            logging.info(f"Apalancamiento configurado a {CONFIG['LEVERAGE']}x")
            console.print(f"[cyan]Inicialización completa - EMAs, RSI y apalancamiento configurados[/cyan]")
            logging.info("Inicialización completa")
            self.initialized = True
        except BinanceAPIException as e:
            console.print(f"[red]Error al inicializar: {e}[/red]")
            logging.error(f"Error al inicializar: {e}")
            raise ValueError("Fallo en la API de Binance.")

    def update_indicators(self, close_price, timestamp):
        if timestamp > self.last_kline_time:
            self.short_closes.append(close_price)
            self.long_closes.append(close_price)
            self.last_kline_time = timestamp
            self.prev_ema_short = self.ema_short
            self.prev_ema_long = self.ema_long
            self.ema_short = close_price * self.k_short + self.prev_ema_short * (1 - self.k_short)
            self.ema_long = close_price * self.k_long + self.prev_ema_long * (1 - self.k_long)
            self.calculate_rsi(close_price, is_closed_candle=True)
            self.real_time_ema_short = self.ema_short
            self.real_time_ema_long = self.ema_long
            self.last_real_time_ema_short = self.ema_short
            self.last_real_time_ema_long = self.ema_long
            self.last_successful_update = time.time()

    def update_real_time_emas(self, current_price):
        if not self.initialized or current_price == self.last_cross_check_price:
            return
        self.last_cross_check_price = current_price
        self.last_real_time_ema_short = self.real_time_ema_short
        self.last_real_time_ema_long = self.real_time_ema_long
        self.real_time_ema_short = current_price * self.k_short + self.ema_short * (1 - self.k_short)
        self.real_time_ema_long = current_price * self.k_long + self.ema_long * (1 - self.k_long)
        self.calculate_rsi(current_price, is_closed_candle=False)
        self.check_real_time_cross(current_price)
        self.last_successful_update = time.time()

    def check_connection_health(self):
        elapsed = time.time() - self.last_successful_update
        if elapsed > 300:  # 5 minutos
            msg = f"Alerta: Sin actualizaciones por {elapsed:.0f} segundos"
            if elapsed > 600:  # 10 minutos
                msg = f"CRÍTICO: Sin actualizaciones por {elapsed/60:.1f} minutos"
            logging.warning(msg)
            self.add_notification(Text(msg, style=self.theme["warning"]))
            return False
        return True

    def check_real_time_cross(self, current_price):
        current_time = time.time()
        if current_time - self.last_cross_time < CONFIG['MIN_TIME_BETWEEN_SIGNALS']:
            return
        if abs(self.real_time_ema_short - self.real_time_ema_long) < CONFIG['CROSS_DETECTION_THRESHOLD']:
            return
        cross_type = None
        if (self.last_real_time_ema_short <= self.last_real_time_ema_long and 
            self.real_time_ema_short > self.real_time_ema_long and
            self.rsi_sma_value <= CONFIG['RSI_SMA_OVERSOLD']):
            cross_type = "LONG"
        elif (self.last_real_time_ema_short >= self.last_real_time_ema_long and 
              self.real_time_ema_short < self.real_time_ema_long and
              self.rsi_sma_value >= CONFIG['RSI_SMA_OVERBOUGHT']):
            cross_type = "SHORT"
        if cross_type and cross_type != self.last_cross_type:
            self.last_cross_type = cross_type
            self.last_cross_time = current_time
            self.stats['signals_generated'] += 1
            msg = Text(f"{time.strftime('%H:%M:%S')} - SEÑAL EN TIEMPO REAL - ")
            if cross_type == "LONG":
                msg.append("CRUCE ALCISTA", style=self.theme["positive"])
                msg.append(f" RSI: {self.rsi_sma_value:.2f} (Sobreventa)")
            else:
                msg.append("CRUCE BAJISTA", style=self.theme["negative"])
                msg.append(f" RSI: {self.rsi_sma_value:.2f} (Sobrecompra)")
            msg.append(f" Precio: {current_price:.4f}")
            self.add_notification(msg)
            logging.info(f"Señal detectada: {cross_type} - RSI: {self.rsi_sma_value:.2f} - Precio: {current_price:.4f}")
            asyncio.create_task(self.verify_cross(cross_type))

    async def place_futures_order(self, side):
        max_retries = 3
        retry_count = 0
        while retry_count < max_retries:
            try:
                if not await self.validate_client():
                    raise ValueError("No se pudo establecer conexión con el cliente")
                order = await self.client.futures_create_order(
                    symbol=CONFIG['SYMBOL'],
                    side=side,
                    type=ORDER_TYPE_MARKET,
                    quantity=CONFIG['QUANTITY'],
                    timestamp=int(time.time() * 1000),
                    recvWindow=CONFIG['RECV_WINDOW']
                )
                console.print(f"[green]Orden {side} de {CONFIG['QUANTITY']} {CONFIG['SYMBOL']} a mercado ejecutada con éxito[/green]")
                logging.info(f"Orden {side} ejecutada: {CONFIG['QUANTITY']} {CONFIG['SYMBOL']} @ Mercado")
                self.stats['orders_executed'] += 1
                self.add_notification(Text(f"{time.strftime('%H:%M:%S')} - Orden {side} ejecutada - {CONFIG['QUANTITY']} {CONFIG['SYMBOL']} @ Mercado", style=self.theme["info"]))
                return order
            except Exception as e:
                retry_count += 1
                self.stats['failed_orders'] += 1
                if retry_count == max_retries:
                    console.print(f"[red]Error final al colocar orden {side} tras {max_retries} intentos: {e}[/red]")
                    logging.error(f"Error final en orden {side} tras {max_retries} intentos: {e}")
                    self.add_notification(Text(f"{time.strftime('%H:%M:%S')} - Error al ejecutar orden {side}: {e}", style=self.theme["warning"]))
                    raise
                console.print(f"[yellow]Reintento {retry_count} de orden {side}: {e}[/yellow]")
                logging.warning(f"Reintento {retry_count} de orden {side}: {e}")
                await asyncio.sleep(1)

    async def verify_cross(self, cross_type):
        initial_price = self.current_price
        initial_ema_direction = "LONG" if self.real_time_ema_short > self.real_time_ema_long else "SHORT"
        await asyncio.sleep(CONFIG['DELAY_MINUTES'] * 60)

        current_distance = abs(self.real_time_ema_short - self.real_time_ema_long)
        price_change = abs((self.current_price - initial_price) / initial_price) * 100 if initial_price > 0 else 0
        current_ema_direction = "LONG" if self.real_time_ema_short > self.real_time_ema_long else "SHORT"

        if current_distance < CONFIG['DISTANCE_THRESHOLD']:
            self.add_notification(
                Text(f"{time.strftime('%H:%M:%S')} - Señal cancelada - Distancia insuficiente: {current_distance:.5f}",
                     style=self.theme["neutral"])
            )
            logging.info(f"Señal {cross_type} cancelada - Distancia insuficiente: {current_distance:.5f}")
            return

        if cross_type != current_ema_direction:
            self.add_notification(
                Text(f"{time.strftime('%H:%M:%S')} - Señal cancelada - Dirección de EMAs invertida: "
                     f"Señal inicial {cross_type}, Dirección actual {current_ema_direction}",
                     style=self.theme["warning"])
            )
            logging.warning(f"Señal {cross_type} cancelada - Dirección invertida - "
                           f"Inicial: {initial_ema_direction}, Actual: {current_ema_direction}")
            return

        if cross_type == "LONG" and self.current_price < initial_price:
            self.add_notification(
                Text(f"{time.strftime('%H:%M:%S')} - Señal cancelada - Precio cayó durante confirmación",
                     style=self.theme["warning"])
            )
            logging.warning(f"Señal {cross_type} cancelada - Precio cayó: Inicial {initial_price:.4f}, Actual {self.current_price:.4f}")
            return
        elif cross_type == "SHORT" and self.current_price > initial_price:
            self.add_notification(
                Text(f"{time.strftime('%H:%M:%S')} - Señal cancelada - Precio subió durante confirmación",
                     style=self.theme["warning"])
            )
            logging.warning(f"Señal {cross_type} cancelada - Precio subió: Inicial {initial_price:.4f}, Actual {self.current_price:.4f}")
            return

        has_position, position_side = await self.check_open_positions()
        msg = Text(f"{time.strftime('%H:%M:%S')} - Confirmación de ")
        side = SIDE_BUY if cross_type == "LONG" else SIDE_SELL
        msg.append("COMPRA (LONG)" if cross_type == "LONG" else "VENTA (SHORT)",
                   style=self.theme["positive"] if cross_type == "LONG" else self.theme["negative"])
        msg.append(f" - Distancia: {current_distance:.5f} - Δ Precio: {price_change:.2f}%")

        if has_position:
            msg.append(f" [Ignorada: Posición {position_side} abierta]", style=self.theme["warning"])
            self.add_notification(msg)
            logging.info(f"Señal {cross_type} ignorada - Posición {position_side} abierta")
            return

        self.add_notification(msg)
        logging.info(f"Confirmación de {cross_type} - Distancia: {current_distance:.5f} - Δ Precio: {price_change:.2f}%")
        await self.place_futures_order(side)

    async def get_balance(self):
        try:
            if not await self.validate_client():
                raise ValueError("No se pudo obtener balance: cliente no conectado")
            account_info = await self.client.futures_account(timestamp=int(time.time() * 1000), recvWindow=CONFIG['RECV_WINDOW'])
            return float(account_info['availableBalance'])
        except (BinanceAPIException, asyncio.TimeoutError) as e:
            console.print(f"[red]Error al obtener balance: {e}[/red]")
            logging.error(f"Error al obtener balance: {e}")
            return balance

async def get_current_price(client):
    try:
        ticker = await client.futures_symbol_ticker(symbol=CONFIG['SYMBOL'])
        return float(ticker['price'])
    except Exception as e:
        console.print(f"[red]Error al obtener precio actual: {e}[/red]")
        logging.error(f"Error al obtener precio actual: {e}")
        return 0.0

async def get_symbol_info(client):
    try:
        info = await client.futures_exchange_info()
        for s in info['symbols']:
            if s['symbol'] == CONFIG['SYMBOL']:
                return {
                    'price_precision': s['pricePrecision'],
                    'quantity_precision': s['quantityPrecision'],
                    'tick_size': float(s['filters'][0]['tickSize'])
                }
    except Exception as e:
        console.print(f"[red]Error al obtener información del símbolo: {e}[/red]")
        logging.error(f"Error al obtener información del símbolo: {e}")
    return {'price_precision': 4, 'quantity_precision': 0, 'tick_size': 0.0001}

async def start_websocket(client):
    global websocket_status, balance
    ws_url = CONFIG['WEBSOCKET_URL']
    last_ui_update = 0
    reconnect_delay = 5
    max_reconnect_delay = 300
    while True:
        try:
            websocket_status = "Connecting"
            async with websockets.connect(ws_url, ping_interval=CONFIG['PING_INTERVAL']) as ws:
                subscription = {
                    "method": "SUBSCRIBE",
                    "params": [
                        f"{CONFIG['SYMBOL'].lower()}@kline_{CONFIG['KLINE_INTERVAL']}",
                        f"{CONFIG['SYMBOL'].lower()}@bookTicker"
                    ],
                    "id": 1
                }
                await ws.send(json.dumps(subscription))
                await asyncio.wait_for(ws.recv(), timeout=5.0)
                websocket_status = "Connected"
                console.print("[green]Conexión WebSocket establecida correctamente[/green]")
                logging.info("Conexión WebSocket establecida")
                while True:
                    try:
                        data = await asyncio.wait_for(ws.recv(), timeout=CONFIG['WEBSOCKET_TIMEOUT'])
                        event = json.loads(data)
                        current_time = time.time()
                        if 'e' in event:
                            if event['e'] == 'kline' and event['k']['x']:
                                close_price = float(event['k']['c'])
                                timestamp = int(event['k']['T'])
                                bot.update_indicators(close_price, timestamp)
                                await ui.update(bot, balance)
                                last_ui_update = current_time
                            elif event['e'] == 'bookTicker':
                                tick_price = (float(event['b']) + float(event['a'])) / 2
                                bot.current_price = tick_price
                                bot.update_real_time_emas(tick_price)
                                if current_time - last_ui_update >= CONFIG['UI_UPDATE_INTERVAL']:
                                    await ui.update(bot, balance)
                                    last_ui_update = current_time
                        if not bot.check_connection_health():
                            console.print("[yellow]Advertencia: conexión potencialmente inactiva[/yellow]")
                    except asyncio.TimeoutError:
                        console.print("[yellow]Timeout en WebSocket, reconectando...[/yellow]")
                        logging.warning("Timeout en WebSocket, intentando reconectar")
                        break
            reconnect_delay = 5
        except Exception as e:
            websocket_status = "Disconnected"
            console.print(f"[red]Error en WebSocket: {e}[/red]")
            logging.error(f"Error en WebSocket: {e}")
            bot.current_price = await get_current_price(client)
            await ui.update(bot, balance)
            await asyncio.sleep(min(reconnect_delay, max_reconnect_delay))
            reconnect_delay *= 2

async def update_balance(client):
    global balance
    while True:
        try:
            balance = await bot.get_balance()
            await ui.update(bot, balance)
        except Exception as e:
            console.print(f"[red]Error en actualización de balance: {e}[/red]")
            logging.error(f"Error en actualización de balance: {e}")
        await asyncio.sleep(CONFIG['UPDATE_INTERVAL'])

async def main():
    global bot, ui
    console.print(f"""
[bold cyan]╔══════════════════════════════════════════════╗
║      Bot de Señales Binance Futures          ║
║     SMA RSI 7 - 40/60 || EMAs 5/10 - 1m     ║
║           Version: {CONFIG['VERSION']}                   ║ 
║    Última Actualización: {CONFIG['LAST_UPDATE']}          ║ 
║            Autor: {CONFIG['AUTHOR']}                     ║ 
╚══════════════════════════════════════════════╝[/bold cyan]
""")    
    logging.info("Iniciando bot")
    try:
        ui = TerminalUI()
        async with AsyncClientManager() as client:
            console.print("[cyan]Inicializando bot...[/cyan]")
            symbol_info.update(await get_symbol_info(client))
            bot = SignalBot(theme=ui.theme, client=client)
            await bot.initialize_emas()
            ui.start()
            console.print("[cyan]Iniciando tareas principales...[/cyan]")
            logging.info("Tareas principales iniciadas")
            websocket_task = asyncio.create_task(start_websocket(client))
            balance_task = asyncio.create_task(update_balance(client))
            try:
                await asyncio.gather(websocket_task, balance_task)
            except KeyboardInterrupt:
                console.print("[yellow]Bot detenido por usuario.[/yellow]")
                logging.info("Bot detenido por usuario")
            except Exception as e:
                console.print(f"[red]Error en la ejecución principal: {e}[/red]")
                logging.error(f"Error en ejecución principal: {e}")
            finally:
                websocket_task.cancel()
                balance_task.cancel()
                ui.stop()
                console.print("[cyan]Cerrando conexiones y limpiando recursos...[/cyan]")
                logging.info("Conexiones cerradas")
    except Exception as e:
        console.print(f"[red]Error fatal en la inicialización: {e}[/red]")
        logging.error(f"Error fatal en inicialización: {e}")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        console.print("\n[yellow]Programa terminado por el usuario.[/yellow]")
        logging.info("Programa terminado por usuario")
    except Exception as e:
        console.print(f"\n[red]Error fatal: {e}[/red]")
        logging.error(f"Error fatal: {e}")
    finally:
        console.print("[cyan]Bot detenido correctamente.[/cyan]")
        logging.info("Bot detenido correctamente")