"""
Trading Strategy Implementation
Main strategy execution that:
1. Connects to Upstox websocket at 9:17 AM
2. Captures Sensex price and gets CE/PE option contracts
3. Tracks highest prices for both options between 9:17-9:30
4. Places buy orders at highest price with -1.25% stop loss and +2.5% target
5. Manages re-entry logic (one additional order after stop loss)
6. Tracks order status via PortfolioDataStreamer
"""

import asyncio
import json
import ssl
import websockets
import requests
# from google.protobuf.json_format import MessageToDict
# import MarketDataFeed_pb2 as pb
import os
from dotenv import load_dotenv
from mani import AlgoKM
import pytz
from datetime import datetime, time as time_class
import time as time_module
import threading
import queue
import upstox_client

# Load environment variables
load_dotenv()
my_access_token = os.getenv('access_token')


def get_market_data_feed_authorize_v3():
    """Get authorization for market data feed."""
    headers = {
        'Accept': 'application/json',
        'Authorization': f'Bearer {my_access_token}'
    }
    url = 'https://api.upstox.com/v3/feed/market-data-feed/authorize'
    api_response = requests.get(url=url, headers=headers)
    return api_response.json()


def decode_protobuf(buffer):
    """Decode protobuf message."""
    feed_response = pb.FeedResponse()
    feed_response.ParseFromString(buffer)
    return feed_response


class TradingStrategy:
    """Main trading strategy class that orchestrates the entire flow."""
    
    def __init__(self, access_token, quantity=1, start_time=None, end_time=None, exit_time=None,tick_size=False):
        """
        Initialize trading strategy.
        
        Args:
            access_token: Upstox access token
            quantity: Quantity for option orders (default: 1)
            start_time: Start time as time object (default: 9:17 AM)
            end_time: End time as time object (default: 9:30 AM)
            exit_time: Exit time as time object (default: 3:30 PM)
        """
        self.access_token = access_token
        self.quantity = quantity
        self.ist = pytz.timezone("Asia/Kolkata")
        
        # Sensex instrument key
        self.sensex_instrument_key = 'BSE_INDEX|SENSEX'
        self.tick_size = tick_size
        
        # Initialize AlogKM instance for Sensex
        self.sensex_trader = AlgoKM(
            access_token=access_token,
            instrument_key=self.sensex_instrument_key,
            tick_size=self.tick_size
        )
        
        # Pre-fetch option contracts in background for faster lookup later
        # self._pre_fetch_options()
        
        # State variables
        self.sensex_price_at_917 = None
        self.ce_instrument_key = None
        self.pe_instrument_key = None
        self.ce_high_price = 0
        self.pe_high_price = 0
        self.total_high = 0
        self.ce_trader = None
        self.pe_trader = None
        
        # Order tracking
        self.ce_gtt_order_id = None
        self.pe_gtt_order_id = None
        self.ce_stoploss_hit_count = 0
        self.pe_stoploss_hit_count = 0
        self.ce_reentry_placed = False
        self.pe_reentry_placed = False
        self.reentry_placed = False
        
        # Time windows - use provided times or defaults
        if start_time is None:
            start_time = time_class(9, 17)  # 9:17 AM
        if end_time is None:
            end_time = time_class(9, 30)    # 9:30 AM
        if exit_time is None:
            exit_time = time_class(15, 30)   # 3:30 PM
        
        self.start_time = start_time
        self.end_time = end_time
        self.exit_time = exit_time
        print('self.end_time',self.end_time)
        
        # Portfolio streamer for order tracking
        self.portfolio_streamer = None
        self.portfolio_thread = None
        self.portfolio_update_queue = queue.Queue()
    
    def _pre_fetch_options(self):
        """Pre-fetch option contracts in background thread for faster lookup."""
        def fetch_in_background():
            try:
                expiry_date = self.sensex_trader.get_thursday_date()
                print(f"🔄 Pre-fetching option contracts for expiry {expiry_date} in background...")
                self.sensex_trader.pre_fetch_option_contracts(
                    expiry_date=expiry_date,
                    instrument_key=self.sensex_instrument_key
                )
            except Exception as e:
                print(f"⚠️ Warning: Could not pre-fetch option contracts: {e}")
                print("   Will fetch when needed (may be slower)")
        
        # Start background thread
        fetch_thread = threading.Thread(target=fetch_in_background, daemon=True)
        fetch_thread.start()
        print("✅ Option contracts pre-fetch started in background")
        
    def wait_until_time(self, target_time):
        """
        Wait until target time is reached.
        
        Args:
            target_time: time object representing target time
        """
        while True:
            now = datetime.now(self.ist).time()
            if now >= target_time:
                print(f"⏰ Target time {target_time.strftime('%H:%M')} reached!")
                return
            time_module.sleep(1)
    
    def setup_portfolio_streamer(self):
        """Set up PortfolioDataStreamer to track GTT order updates."""
        try:
            configuration = upstox_client.Configuration(sandbox=False)
            configuration.access_token = self.access_token
            
            self.portfolio_streamer = upstox_client.PortfolioDataStreamer(
                upstox_client.ApiClient(configuration),
                order_update=True,
                position_update=False,
                holding_update=False,
                gtt_update=True
            )
            
            def on_portfolio_message(message):
                """Handle portfolio/order update messages."""
                self.handle_order_updates(json.loads(message))
            
            def on_portfolio_open():
                print("✅ Portfolio streamer connected")
            
            def on_portfolio_error(error):
                print(f"❌ Portfolio streamer error: {error}")
            
            self.portfolio_streamer.on("message", on_portfolio_message)
            self.portfolio_streamer.on("open", on_portfolio_open)
            self.portfolio_streamer.on("error", on_portfolio_error)
            
            # Connect in a separate thread
            def run_portfolio_streamer():
                self.portfolio_streamer.connect()
            
            self.portfolio_thread = threading.Thread(target=run_portfolio_streamer, daemon=True)
            self.portfolio_thread.start()
            
            print("📡 Portfolio streamer setup complete")
            
        except Exception as e:
            print(f"Error setting up portfolio streamer: {e}")
    
    def handle_order_updates(self, message):
        """
        Process order status updates from PortfolioDataStreamer and add to queue.
        
        Args:
            message: Order update message from websocket
        """
        try:
            # Parse the message (format depends on Upstox API)
            if isinstance(message, dict):
                order_data = message
                
            else:
                # Try to convert to dict if it's a model instance
                order_data = message.to_dict() if hasattr(message, 'to_dict') else str(message)
            
            print(f"📨 Order update received: {json.dumps(order_data, indent=2) if isinstance(order_data, dict) else order_data}")
            
            # Add to queue for async processing
            self.portfolio_update_queue.put(order_data)
                
        except Exception as e:
            print(f"Error handling order update: {e}")
    
    async def monitor_portfolio_updates(self):
        """
        Monitor portfolio streamer updates and handle re-entry logic based on GTT order status.
        This function processes real-time updates from the portfolio streamer websocket.
        Similar to monitor_orders but uses portfolio streamer data instead of polling.
        """
        print("📡 Monitoring portfolio streamer for order updates...")
        
        while True:
            now = datetime.now(self.ist).time()
            
            # Check exit time
            if now >= self.exit_time:
                print(f"⏰ Exit time {self.exit_time.strftime('%H:%M')} reached. Stopping portfolio monitoring.")
                break
            
            try:
                # Wait for message with timeout
                try:
                    order_data = self.portfolio_update_queue.get(timeout=1.0)
                    await self.process_portfolio_update(order_data)
                except queue.Empty:
                    # Timeout is expected, continue monitoring
                    continue
                    
            except Exception as e:
                print(f"Error in portfolio monitoring loop: {e}")
                continue
            
            # Small sleep to prevent tight loop
            await asyncio.sleep(0.1)
    
    async def process_portfolio_update(self, order_data):
        """
        Process a single portfolio update message and handle re-entry if needed.
        
        Args:
            order_data: Order update data from portfolio streamer
        """
        try:
            update_type = order_data.get('update_type', '')
            
            # Process GTT order updates
            if update_type == 'gtt_order':
                gtt_order_id = order_data.get('gtt_order_id', '')
                rules = order_data.get('rules', [])
                
                # Check if this GTT order matches our CE or PE order
                is_ce_order = (self.ce_gtt_order_id and gtt_order_id == self.ce_gtt_order_id)
                is_pe_order = (self.pe_gtt_order_id and gtt_order_id == self.pe_gtt_order_id)
                
                if not (is_ce_order or is_pe_order):
                    # Not our order, skip
                    return
                
                # Check rules for ENTRY status
                for rule in rules:
                    strategy = rule.get('strategy', '')
                    status = rule.get('status', '')
                    
                    # Check if ENTRY rule failed
                    if strategy == 'ENTRY' and status == 'FAILED':
                        message = rule.get('message', '')
                        print(f"❌ GTT Order {gtt_order_id} ENTRY rule FAILED: {message}")
                        
                        # Handle re-entry based on which order failed
                        if is_ce_order and not self.reentry_placed:
                            print(f"🔄 Attempting CE re-entry for failed order...")
                            try:
                                self.ce_gtt_order_id = self.ce_trader.buyStock(
                                    quantity=self.quantity,
                                    buy_price=self.ce_high_price,
                                    instrument_key=self.ce_instrument_key
                                )
                                print(f"✅ CE re-entry order placed. GTT Order ID: {self.ce_gtt_order_id}")
                                self.reentry_placed = True
                            except Exception as e:
                                print(f"❌ Error placing CE re-entry order: {e}")
                        
                        elif is_pe_order and not self.reentry_placed:
                            print(f"🔄 Attempting PE re-entry for failed order...")
                            try:
                                self.pe_gtt_order_id = self.pe_trader.buyStock(
                                    quantity=self.quantity,
                                    buy_price=self.pe_high_price,
                                    instrument_key=self.pe_instrument_key
                                )
                                print(f"✅ PE re-entry order placed. GTT Order ID: {self.pe_gtt_order_id}")
                                self.reentry_placed = True
                            except Exception as e:
                                print(f"❌ Error placing PE re-entry order: {e}")
                    
                    # Check if STOPLOSS rule triggered
                    elif strategy == 'STOPLOSS' and status in ['ACTIVE', 'TRIGGERED']:
                        if is_ce_order and not self.ce_reentry_placed:
                            print(f"🛑 CE Stop Loss Hit! Placing re-entry order...")
                            self.ce_stoploss_hit_count += 1
                            try:
                                self.ce_gtt_order_id = self.ce_trader.buyStock(
                                    quantity=self.quantity,
                                    buy_price=self.ce_high_price,
                                    instrument_key=self.ce_instrument_key
                                )
                                print(f"✅ CE re-entry order placed. GTT Order ID: {self.ce_gtt_order_id}")
                                self.ce_reentry_placed = True
                            except Exception as e:
                                print(f"❌ Error placing CE re-entry order: {e}")
                        
                        elif is_pe_order and not self.pe_reentry_placed:
                            print(f"🛑 PE Stop Loss Hit! Placing re-entry order...")
                            self.pe_stoploss_hit_count += 1
                            try:
                                self.pe_gtt_order_id = self.pe_trader.buyStock(
                                    quantity=self.quantity,
                                    buy_price=self.pe_high_price,
                                    instrument_key=self.pe_instrument_key
                                )
                                print(f"✅ PE re-entry order placed. GTT Order ID: {self.pe_gtt_order_id}")
                                self.pe_reentry_placed = True
                            except Exception as e:
                                print(f"❌ Error placing PE re-entry order: {e}")
                    
                    # Check if TARGET rule triggered
                    elif strategy == 'TARGET' and status in ['ACTIVE', 'TRIGGERED']:
                        if is_ce_order:
                            print(f"🎯 CE Target Hit! Order completed.")
                            self.ce_gtt_order_id = None  # Stop monitoring
                        elif is_pe_order:
                            print(f"🎯 PE Target Hit! Order completed.")
                            self.pe_gtt_order_id = None  # Stop monitoring
            
            # Process regular order updates
            elif update_type == 'order':
                order_ref_id = order_data.get('order_ref_id', '')
                status = order_data.get('status', '').lower()
                
                # Check if this order matches our GTT orders
                is_ce_order = (self.ce_gtt_order_id and order_ref_id == self.ce_gtt_order_id)
                is_pe_order = (self.pe_gtt_order_id and order_ref_id == self.pe_gtt_order_id)
                
                if not (is_ce_order or is_pe_order):
                    # Not our order, skip
                    return
                
                # Check if order was rejected
                if status == 'rejected':
                    status_message = order_data.get('status_message', '')
                    print(f"❌ Order {order_ref_id} REJECTED: {status_message}")
                    
                    # Handle re-entry for rejected orders
                    if is_ce_order and not self.ce_reentry_placed:
                        print(f"🔄 Attempting CE re-entry for rejected order...")
                        try:
                            self.ce_gtt_order_id = self.ce_trader.buyStock(
                                quantity=self.quantity,
                                buy_price=self.ce_high_price,
                                instrument_key=self.ce_instrument_key
                            )
                            print(f"✅ CE re-entry order placed. GTT Order ID: {self.ce_gtt_order_id}")
                            self.ce_reentry_placed = True
                        except Exception as e:
                            print(f"❌ Error placing CE re-entry order: {e}")
                    
                    elif is_pe_order and not self.pe_reentry_placed:
                        print(f"🔄 Attempting PE re-entry for rejected order...")
                        try:
                            self.pe_gtt_order_id = self.pe_trader.buyStock(
                                quantity=self.quantity,
                                buy_price=self.pe_high_price,
                                instrument_key=self.pe_instrument_key
                            )
                            print(f"✅ PE re-entry order placed. GTT Order ID: {self.pe_gtt_order_id}")
                            self.pe_reentry_placed = True
                        except Exception as e:
                            print(f"❌ Error placing PE re-entry order: {e}")
                
        except Exception as e:
            print(f"Error processing portfolio update: {e}")
            import traceback
            traceback.print_exc()
    
    async def capture_sensex_price_at_917(self, websocket):
        """
        Wait until 9:17 AM and capture Sensex price.
        
        Args:
            websocket: WebSocket connection
            
        Returns:
            float: Sensex price at 9:17
        """
        print("⏳ Waiting until 9:17 AM to capture Sensex price...")
        
        # Wait until 9:17 AM
        self.wait_until_time(self.start_time)
        
        print("📊 Capturing Sensex price at 9:17 AM...")
        
        # Subscribe to Sensex feed if not already subscribed
        data = {
            "guid": "sensex_sub",
            "method": "sub",
            "data": {
                "mode": "ltpc",
                "instrumentKeys": [self.sensex_instrument_key]
            }
        }
        binary_data = json.dumps(data).encode('utf-8')
        await websocket.send(binary_data)
        
        # Receive messages until we get a valid price
        max_attempts = 10
        attempt = 0
        
        while attempt < max_attempts:
            try:
                message = await asyncio.wait_for(websocket.recv(), timeout=5.0)
                decoded_data = decode_protobuf(message)
                # data_dict = MessageToDict(decoded_data)
                result = self.sensex_trader.extract_l1_ohlc(decoded_data)
                if result and result.get('ltp', 0) > 0:
                    sensex_price = result['ltp']
                    self.sensex_price_at_917 = sensex_price
                    print(f"✅ Sensex price at 9:17 AM: ₹{sensex_price}")
                    return sensex_price
                    
            except asyncio.TimeoutError:
                attempt += 1
                print(f"⏳ Waiting for Sensex price data... (attempt {attempt}/{max_attempts})")
            except Exception as e:
                print(f"Error capturing Sensex price: {e}")
                attempt += 1
        
        raise ValueError("Failed to capture Sensex price at 9:17 AM")
    
    def get_option_contracts_for_price(self, sensex_price):
        """
        Get CE and PE option contracts for the given Sensex price.
        
        Args:
            sensex_price: Current Sensex price
            
        Returns:
            tuple: (ce_instrument_key, pe_instrument_key)
        """
        print(f"🔍 Getting option contracts for Sensex price: ₹{sensex_price}")
        
        # Get Thursday expiry date
        expiry_date = self.sensex_trader.get_thursday_date()
        print(f"📅 Expiry date: {expiry_date}")
        
        # Get option contracts
        ce_ik, pe_ik = self.sensex_trader.get_option_contracts(
            sensex_price=sensex_price,
        )
        
        self.ce_instrument_key = ce_ik
        self.pe_instrument_key = pe_ik
        
        print(f"✅ CE Instrument Key: {ce_ik}")
        print(f"✅ PE Instrument Key: {pe_ik}")
        
        # Initialize traders for CE and PE
        self.ce_trader = AlgoKM(
            access_token=self.access_token,
            instrument_key=ce_ik,
            tick_size=True
        )
        self.pe_trader = AlgoKM(
            access_token=self.access_token,
            instrument_key=pe_ik,
            tick_size=True
        )
        
        return ce_ik, pe_ik
    
    async def track_high_prices(self, websocket, ce_ik, pe_ik):
        """
        Track highest prices for CE and PE options between 9:17-9:30.
        
        Args:
            websocket: WebSocket connection
            ce_ik: CE instrument key
            pe_ik: PE instrument key
        """
        print(f"📈 Tracking high prices for CE and PE from 9:17 to 9:30...")
        
        # Subscribe to both CE and PE feeds
        data = {
            "guid": "options_sub",
            "method": "sub",
            "data": {
                "mode": "full",
                "instrumentKeys": [ce_ik, pe_ik]
            }
        }
        binary_data = json.dumps(data).encode('utf-8')
        await websocket.send(binary_data)
        
        self.ce_high_price = 0
        self.pe_high_price = 0
        current_time_ms = self.sensex_trader.get_today_start_timestamp_ms(self.start_time) + 60000
        
        
        # Track until 9:30
        while True:
            now = datetime.now(self.ist).time()
            
            # Check if we've passed 9:30
            if now > self.end_time:
                print(f"⏰ 9:30 AM reached. Final high prices:")
                print(f"   CE High: ₹{self.ce_high_price}")
                print(f"   PE High: ₹{self.pe_high_price}")
                break
            
            # Check exit time
            if now >= self.exit_time:
                print(f"⏰ Exit time {self.exit_time.strftime('%H:%M')} reached.")
                break
            
            try:
                # Receive message with timeout
                message = await asyncio.wait_for(websocket.recv(), timeout=1.0)
                decoded_data = decode_protobuf(message)
                data_dict = MessageToDict(decoded_data)
                
                now_ms = self.sensex_trader.get_today_start_timestamp_ms(now)
                
                
                # Extract prices for CE
                if 'feeds' in data_dict and ce_ik in data_dict['feeds']:
                    
                    data_ce_ik = self.sensex_trader.extract_i1_ohlc(data_dict,ce_ik)
                    ce_ltp = data_ce_ik.get('ltpc', {})['ltp']
                    
                    
                    
                    
                    if int(data_ce_ik['ohlc_i1']['ts'])<= now_ms <= current_time_ms:
                        self.ce_high_price = self.sensex_trader.highMarketValue(
                            self.ce_high_price, data_ce_ik['ohlc_i1']['high'])  
                        

                    if ce_ltp > 0:
                        self.ce_high_price = self.sensex_trader.highMarketValue(
                            self.ce_high_price, ce_ltp
                        )
                
                # Extract prices for PE
                if 'feeds' in data_dict and pe_ik in data_dict['feeds']:
                    
                    data_pe_ik = self.sensex_trader.extract_i1_ohlc(data_dict,pe_ik)
                    pe_ltp = data_pe_ik.get('ltpc', {})['ltp']
                    
                
                
                    if int(data_pe_ik['ohlc_i1']['ts']) <= now_ms <= current_time_ms:
                        self.pe_high_price = self.sensex_trader.highMarketValue(
                            self.pe_high_price, data_pe_ik['ohlc_i1']['high'])  
                         
                         
                    if pe_ltp > 0:
                        self.pe_high_price = self.sensex_trader.highMarketValue(
                            self.pe_high_price, pe_ltp
                        )
                
            except asyncio.TimeoutError:
                # Timeout is expected, continue tracking
                continue
            except Exception as e:
                print(f"Error tracking prices: {e}")
                continue
    
    def place_option_orders(self):
        """
        Place buy orders for both CE and PE at their respective highest prices.
        """
        if self.ce_high_price <= 0 or self.pe_high_price <= 0:
            print("❌ Cannot place orders: high prices not available")
            return
        
        self.total_high = self.sensex_trader.highMarketValue(
            self.pe_high_price, self.ce_high_price
        )
    
        
        print(f"📝 Placing orders at high prices:")
        print(f"   CE: ₹{self.ce_high_price} (quantity: {self.quantity})")
        print(f"   PE: ₹{self.pe_high_price} (quantity: {self.quantity})")
        
        try:
            # Place CE order
            if self.ce_instrument_key and self.total_high > 0:
                self.ce_gtt_order_id = self.ce_trader.buyStock(
                    quantity=self.quantity,
                    buy_price=self.ce_high_price,
                    instrument_key=self.ce_instrument_key
                )['data']['gtt_order_ids'][0]
                print(f"✅ CE order placed. GTT Order ID: {self.ce_gtt_order_id}")
            
            # Place PE order
            if self.pe_instrument_key and self.total_high > 0:
                self.pe_gtt_order_id = self.pe_trader.buyStock(
                    quantity=self.quantity,
                    buy_price=self.pe_high_price,
                    instrument_key=self.pe_instrument_key
                )['data']['gtt_order_ids'][0]
                print(f"✅ PE order placed. GTT Order ID: {self.pe_gtt_order_id}")
                
        except Exception as e:
            print(f"❌ Error placing orders: {e}")
    
    async def monitor_orders(self, websocket):
        """
        Monitor order status and handle re-entry logic.
        
        Args:
            websocket: WebSocket connection
        """
        print("👀 Monitoring orders for stop loss and target hits...")
        
        while True:
            now = datetime.now(self.ist).time()
            
            # Check exit time
            if now >= self.exit_time:
                print(f"⏰ Exit time {self.exit_time.strftime('%H:%M')} reached. Stopping monitoring.")
                break
            
            # Check CE order status
            if self.ce_gtt_order_id:
                try:
                    response = self.ce_trader.get_gtt_order_details(self.ce_gtt_order_id)
                    status = self.ce_trader.check_gtt_status(response)
                    
                    if status == "Stop Loss Hit" and not self.ce_reentry_placed:
                        print(f"🛑 CE Stop Loss Hit! Placing re-entry order...")
                        self.ce_stoploss_hit_count += 1
                        self.ce_reentry_placed = True
                        
                        # Place one more order
                        self.ce_gtt_order_id = self.ce_trader.buyStock(
                            quantity=self.quantity,
                            buy_price=self.ce_high_price,
                            instrument_key=self.ce_instrument_key
                        )
                        print(f"✅ CE re-entry order placed. GTT Order ID: {self.ce_gtt_order_id}")
                    
                    elif status == "Target Hit":
                        print(f"🎯 CE Target Hit! Order completed.")
                        self.ce_gtt_order_id = None  # Stop monitoring
                    
                except Exception as e:
                    print(f"Error checking CE order status: {e}")
            
            # Check PE order status
            if self.pe_gtt_order_id:
                try:
                    response = self.pe_trader.get_gtt_order_details(self.pe_gtt_order_id)
                    status = self.pe_trader.check_gtt_status(response)
                    
                    if status == "Stop Loss Hit" and not self.pe_reentry_placed:
                        print(f"🛑 PE Stop Loss Hit! Placing re-entry order...")
                        self.pe_stoploss_hit_count += 1
                        self.pe_reentry_placed = True
                        
                        # Place one more order
                        self.pe_gtt_order_id = self.pe_trader.buyStock(
                            quantity=self.quantity,
                            buy_price=self.pe_high_price,
                            instrument_key=self.pe_instrument_key
                        )
                        print(f"✅ PE re-entry order placed. GTT Order ID: {self.pe_gtt_order_id}")
                    
                    elif status == "Target Hit":
                        print(f"🎯 PE Target Hit! Order completed.")
                        self.pe_gtt_order_id = None  # Stop monitoring
                    
                except Exception as e:
                    print(f"Error checking PE order status: {e}")
            
            # Sleep before next check
            await asyncio.sleep(5)  # Check every 5 seconds
    
    async def execute_strategy(self):
        """Main strategy execution function."""
        print("=" * 60)
        print("🚀 Starting Trading Strategy")
        print("=" * 60)
        
        # Setup portfolio streamer for order tracking
        self.setup_portfolio_streamer()
        
        # Wait a bit for portfolio streamer to connect
        time_module.sleep(2)
        
        # Create SSL context
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
        
        # Get market data feed authorization
        response = get_market_data_feed_authorize_v3()
        
        try:
            # Connect to WebSocket
            async with websockets.connect(
                response["data"]["authorized_redirect_uri"],
                ssl=ssl_context
            ) as websocket:
                print("✅ WebSocket connected")
                
                # Step 1: Capture Sensex price at 9:17
                sensex_price = await self.capture_sensex_price_at_917(websocket)
                
                # # Step 2: Get option contracts
                ce_ik, pe_ik = self.get_option_contracts_for_price(sensex_price)
                
                # # Step 3: Track high prices from 9:17 to 9:30
                await self.track_high_prices(websocket, ce_ik, pe_ik)
                
                # Step 4: Place orders after 9:30
                self.place_option_orders()
                
                 # Step 5: Monitor orders for stop loss/target hits (run both monitoring functions concurrently)
                await asyncio.gather(
                    self.monitor_portfolio_updates()
                )
                
        except Exception as e:
            print(f"❌ Error in strategy execution: {e}")
            import traceback
            traceback.print_exc()
        
        print("=" * 60)
        print("🏁 Trading Strategy Completed")
        print("=" * 60)


def main():
    """Main entry point."""
    if not my_access_token:
        print("❌ Error: access_token not found in environment variables")
        return
    
    strategy = TradingStrategy(access_token=my_access_token, quantity=1)
    asyncio.run(strategy.execute_strategy())
    # strategy.run_portfolio_streamer()
    # strategy = TradingStrategy(access_token=my_access_token, quantity=1)
    # await strategy.execute_strategy()  # This will run run_portfolio_streamer automatically
    # time_module.sleep(60)  # Keep it running for testing


if __name__ == "__main__":
    main()

