import upstox_client
from upstox_client.rest import ApiException
from datetime import datetime, time, timedelta, date
import pytz
import pandas as pd
import threading
from dotenv import load_dotenv
import os
import time as tm


class AlgoKM:
    def __init__(self,instrument_key='BSE_INDEX|SENSEX',access_token=None,tick_size=False):
        self.configuration = upstox_client.Configuration(sandbox=False)
        self.configuration.access_token = access_token
        self.api_instance = upstox_client.OrderApiV3(upstox_client.ApiClient(self.configuration))
        self.instrument_key = instrument_key
        self.tick_size = tick_size
        self.option_contracts = None
        
        threading.Thread(
            target=self.get_option_contracts_response,
            daemon=True
        ).start()
        

        

    def round_to_tick_size(self, price, tick_size=0.05):
        """Round price to the nearest tick size."""
        return round(price / tick_size) * tick_size


    def buyStock(self,quantity,buy_price,instrument_key=None):

        if instrument_key is None:
            instrument_key = self.instrument_key

        # Round buy_price to tick size
        if self.tick_size:
            buy_price = self.round_to_tick_size(buy_price)
        
        stop_loss_value = buy_price + ((buy_price * - 1.25) / 100)
        if self.tick_size:
            stop_loss_value = self.round_to_tick_size(stop_loss_value)
        
        target_value = buy_price + ((buy_price * 2.5) / 100)
        if self.tick_size:
            target_value = self.round_to_tick_size(target_value)

        entry_rule = upstox_client.GttRule(
            strategy="ENTRY",           # ENTRY / STOPLOSS / TARGET
            trigger_type="ABOVE",
            trigger_price=buy_price         # Trigger price for the condition
        )


        stop_loss = upstox_client.GttRule(
            strategy="STOPLOSS",           # ENTRY / STOPLOSS / TARGET
            trigger_type="IMMEDIATE",
            trigger_price=stop_loss_value
        )

        target = upstox_client.GttRule(
             strategy="TARGET",           # ENTRY / STOPLOSS / TARGET
            trigger_type="IMMEDIATE",
            trigger_price=target_value
        )

        body = upstox_client.GttPlaceOrderRequest(
            type="MULTIPLE",                         # SINGLE or MULTIPLE
            quantity=quantity,
            product="I",                           # D=Delivery, I=Intraday, etc.
            rules=[entry_rule,stop_loss,target],                          # list of GttRule
            instrument_token=instrument_key,# Example instrument
            transaction_type="BUY"                 # BUY or SELL
        )


        try:
            api_response = self.api_instance.place_gtt_order(body)
            print("Order Placed:", api_response)
            return api_response.data.gtt_order_ids[0]
        except ApiException as e:
            print("Exception when calling OrderApi->place_order: %s\n" % e)


    def sellStock(self,quantity,sell_price):
        # Round sell_price to tick size
        if self.tick_size:
            sell_price = self.round_to_tick_size(sell_price)
        
        sell_order = upstox_client.PlaceOrderV3Request(
            quantity=quantity,
            product="D",
            validity="DAY",
            price=sell_price,
            tag="sell_at_50",
            instrument_token="NSE_EQ|INE669E01016",
            order_type="LIMIT",
            transaction_type="SELL",
            disclosed_quantity=0,
            trigger_price=0.0,
            is_amo=False,
            slice=False
    )
        try:
            api_response = self.api_instance.place_order(body)
            print("Order Placed:", api_response)
        except ApiException as e:
            print("Exception when calling OrderApi->place_order: %s\n" % e)

    def highMarketValue(self,current,high):
        if current == 0:
            return high
        elif current < high:
            return high
        else:
            return current

    def when_to_sell(self,selled_price,closed_price):
        target_price = self.round_to_tick_size(selled_price + (selled_price * 2.5 / 100))
        stop_loss_price = self.round_to_tick_size(selled_price + (selled_price * -1.5 / 100))
        
        if closed_price >= target_price:
            print(f'it have been 2.5% hooray {target_price} of {closed_price}')
            self.sellStock(quantity=1,sell_price=target_price)

        elif closed_price <= stop_loss_price:
            print(f'oh sorry we are loosing {stop_loss_price} of {closed_price}')
            self.sellStock(quantity=1,sell_price=stop_loss_price)
        else:
            print(f'not reached the expect value expecting values are top {target_price} low {stop_loss_price} cr {closed_price}')

    def get_gtt_order_details(self,order_id):
        gtt_detail = self.api_instance.get_gtt_order_details(gtt_order_id=order_id)
        # The SDK returns a model instance; convert it to dict for easier downstream handling
        return gtt_detail.to_dict()

    def check_gtt_status(self,api_response):
        """Check if GTT order hit stop loss, target, or got cancelled."""
        try:
            data_list = api_response.get("data", [])
            if not data_list:
                return "No data"

            for order in data_list:
                rules = order.get("rules", [])
                # Flags to track rule states
                target_hit = False
                stoploss_hit = False
                all_cancelled = True
                bought_it = False

                for rule in rules:
                    status = rule.get("status")
                    strategy = rule.get("strategy")

                    if status != "CANCELLED":
                        all_cancelled = False

                    if strategy == "TARGET" and status == "TRIGGERED":
                        target_hit = True
                    elif strategy == "STOPLOSS" and status == "TRIGGERED":
                        stoploss_hit = True
                    elif strategy == "ENTRY" and status == "TRIGGERED":
                        bought_it = True

                # Decision logic
                if stoploss_hit:
                    return "Stop Loss Hit"
                elif target_hit:
                    return "Target Hit"
                elif all_cancelled:
                    return "Cancelled"
                elif bought_it:
                    return 'Entry Has been triggered'
                else:
                    return "Active"

        except Exception as e:
            return f"Error: {e}"

    def extract_l1_ohlc(self, data_dict):
        """
        Extract LTP, CP, and LTT from the new data_dict structure and return as OHLC-like dict.
        """
        try:
            ist = pytz.timezone("Asia/Kolkata")
            if 'feeds' in data_dict:
                feed = data_dict['feeds'].get(self.instrument_key, {})
                if 'ltpc' in feed:
                    ltpc = feed['ltpc']
                    ltp = float(ltpc.get('ltp', 0))
                    cp = float(ltpc.get('cp', 0))
                    ltt = ltpc.get('ltt')
                    # Use ltt as timestamp if available, else fallback to currentTs
                    ts_ms = int(ltt) if ltt is not None else int(data_dict.get('currentTs', 0))
                    timestamp = datetime.fromtimestamp(ts_ms / 1000, tz=ist)
                    return {
                        'cp': cp,    # Using cp as open (since only ltp/cp available)
                        'ltp': ltp,   # Using ltp as high (since only ltp/cp available)
                        'ltt': timestamp.strftime("%A, %d %B %Y %H:%M:%S %Z")

                    }
            return None
        except Exception as e:
            print(f"Error extracting L1 OHLC: {e}")
            return None

    def extract_i1_ohlc(self, data_dict,instrument_key):
        """
        Extract OHLC interval 1 (I1) data and LTPC from the fullFeed structure.
        Returns a dict with OHLC I1 data and LTPC data.
        """
        try:
            ist = pytz.timezone("Asia/Kolkata")
            if 'feeds' in data_dict:
                feed = data_dict['feeds'].get(instrument_key, {})
                if 'fullFeed' in feed:
                    full_feed = feed['fullFeed']
                    if 'marketFF' in full_feed:
                        market_ff = full_feed['marketFF']
                        
                        # Extract LTPC data
                        ltpc_data = {}
                        if 'ltpc' in market_ff:
                            ltpc = market_ff['ltpc']
                            ltpc_data = {
                                'ltp': float(ltpc.get('ltp', 0)),
                                'ltt': ltpc.get('ltt'),
                                'ltq': ltpc.get('ltq', '0'),
                                'cp': float(ltpc.get('cp', 0))
                            }
                        
                        # Extract OHLC I1 data
                        ohlc_i1_data = {}
                        if 'marketOHLC' in market_ff:
                            market_ohlc = market_ff['marketOHLC']
                            if 'ohlc' in market_ohlc:
                                ohlc_array = market_ohlc['ohlc']
                                # Find the entry with interval "I1"
                                for ohlc_entry in ohlc_array:
                                    if ohlc_entry.get('interval') == 'I1':
                                        ts_ms = int(ohlc_entry.get('ts', 0))
                                        timestamp = datetime.fromtimestamp(ts_ms / 1000, tz=ist)
                                        ohlc_i1_data = {
                                            'interval': ohlc_entry.get('interval'),
                                            'open': float(ohlc_entry.get('open', 0)),
                                            'high': float(ohlc_entry.get('high', 0)),
                                            'low': float(ohlc_entry.get('low', 0)),
                                            'close': float(ohlc_entry.get('close', 0)),
                                            'vol': ohlc_entry.get('vol', '0'),
                                            'ts': ohlc_entry.get('ts'),
                                            'ts_formatted': timestamp.strftime("%A, %d %B %Y %H:%M:%S %Z")
                                        }
                                        break
                        
                        # Return combined data if we found both
                        if ohlc_i1_data and ltpc_data:
                            return {
                                'ohlc_i1': ohlc_i1_data,
                                'ltpc': ltpc_data
                            }
                        elif ohlc_i1_data:
                            return {
                                'ohlc_i1': ohlc_i1_data,
                                'ltpc': None
                            }
                        elif ltpc_data:
                            return {
                                'ohlc_i1': None,
                                'ltpc': ltpc_data
                            }
            return None
        except Exception as e:
            print(f"Error extracting I1 OHLC: {e}")
            return None

    def is_price_near_trigger(self,current_price,buy_price,threshold_percent=0.5):
        # Round buy_price to tick size
        if self.tick_size:
            buy_price = self.round_to_tick_size(buy_price)
        
        target_price = buy_price + (buy_price * 2.5 / 100)
        if self.tick_size:
            target_price = self.round_to_tick_size(target_price)
        
        stop_loss_price = buy_price + (buy_price * -1.25 / 100)
        if self.tick_size:
            stop_loss_price = self.round_to_tick_size(stop_loss_price)

        if current_price >= target_price - (target_price * threshold_percent / 100):
            return True, "TARGET", target_price
        elif current_price <= stop_loss_price + (stop_loss_price * threshold_percent / 100):
            return True, "STOPLOSS", stop_loss_price
        else:
            return False, None, None

    def when_to_buy(self,start,end,data_dict,tracked_high):
        ist = pytz.timezone("Asia/Kolkata")
        now = datetime.now(ist).time()



        while start <= now <= end:
            result = self.extract_l1_ohlc(data_dict)
            if result:
                cp,ltp,ltt = result.values()
                print(f"📊 L1 HIGH: ₹{ltp} | L1 CURRENT: ₹{cp}")

                # Pass to when_to_buy - using L1 data
                tracked_high = self.highMarketValue(
                    tracked_high,
                    ltp
                )
                print(tracked_high,'this is tracked high')
                price=tracked_high if tracked_high else ltp
                print(f'price {price}')

                # ✅ Update time on each iteration to check if we're still in window
                now = datetime.now(ist).time()
                print(f'⏰ Current time: {now} | Window: {start} - {end}')
                return tracked_high
            else:
                return 0

    def get_token(self):
        pass

    def get_thursday_date(self):
        """
        Checks if today is Thursday and returns today's date if it is,
        otherwise returns the date of the next coming Thursday.

        Returns:
        str: The date of the relevant Thursday in 'YYYY-MM-DD' format.
        """
        today = date.today()
        # Weekday Monday is 0 and Sunday is 6. Thursday is 3.
        days_until_thursday = (3 - today.weekday() + 7) % 7

        if days_until_thursday == 0:
            # Today is Thursday
            thursday_date = today
        else:
            # Find the next Thursday
            thursday_date = today + timedelta(days=days_until_thursday)

        return thursday_date.strftime('%Y-%m-%d')
    

    def get_option_contracts_response(self):
        options_instance = upstox_client.OptionsApi(
            upstox_client.ApiClient(self.configuration)
        )
            
            # Get option contracts for the expiry date
        self.option_contracts = options_instance.get_option_contracts(
            instrument_key=self.instrument_key,
            expiry_date=self.get_thursday_date()
        )
        

    def get_option_contracts(self, sensex_price, instrument_key=None):
        """
        Get option contracts (CE and PE) for given expiry and strike price.
        
        Args:
            expiry_date: Expiry date in format 'YYYY-MM-DD'
            sensex_price: Current Sensex price to round for strike selection
            instrument_key: Underlying instrument key (defaults to Sensex)
            
        Returns:
            tuple: (ce_instrument_key, pe_instrument_key)
        """
        if instrument_key is None:
            instrument_key = self.instrument_key
        
        try:
            # Round price to nearest 100 for strike selection
            rounded_strike = round(sensex_price / 100) * 100
            
            
            if not self.option_contracts:
                self.get_option_contracts_response()
            
            # Initialize OptionsApi with configuration
           
            # Convert response to dict if needed
            if hasattr(self.option_contracts, 'to_dict'):
                response_dict = self.option_contracts.to_dict()
            else:
                response_dict = self.option_contracts
            
            # Extract data
            data = response_dict.get('data', [])
            if not data:
                raise ValueError(f"No option contracts found for {instrument_key} on {expiry_date}")
            
            # Create DataFrame
            df = pd.DataFrame(data)
            
            # Filter by strike price and instrument type
            ce_df = df[(df['strike_price'] == rounded_strike) & (df['instrument_type'] == 'CE')]
            pe_df = df[(df['strike_price'] == rounded_strike) & (df['instrument_type'] == 'PE')]
            
            if ce_df.empty:
                raise ValueError(f"No CE option found for strike {rounded_strike}")
            if pe_df.empty:
                raise ValueError(f"No PE option found for strike {rounded_strike}")
            
            ce_ik = ce_df['instrument_key'].iloc[0]
            pe_ik = pe_df['instrument_key'].iloc[0]
            
            return ce_ik, pe_ik
            
        except ApiException as e:
            print(f"Exception when calling OptionsApi->get_option_contracts: {e}")
            raise
        except Exception as e:
            print(f"Error getting option contracts: {e}")
            raise

    def time_range(self, ts, ltp):
        # Convert milliseconds to seconds and then to a datetime object
        dt_object = datetime.fromtimestamp(ts / 1000)

        # Define the target time range
        target_start = dt_object.replace(hour=9, minute=16, second=50, microsecond=0).time()
        target_end = dt_object.replace(hour=9, minute=17, second=0, microsecond=0).time()

        # Get the time part of the input timestamp
        input_time = dt_object.time()

        # Check if the input time is within the range
        if target_start <= input_time <= target_end:
            rounded_ltp = round(ltp / 100) * 100
            
            expiry_date = self.get_thursday_date()
            return self.get_option_contracts(expiry_date, ltp)
        # add the logic later

    def get_instrument_token(self):
        pass
    
    def get_today_start_timestamp_ms(self,time,ist=pytz.timezone("Asia/Kolkata")):
        today = datetime.now(ist).date()
        start_datetime = datetime.combine(today, time)
        start_datetime = ist.localize(start_datetime)
        start_timestamp_ms = int(start_datetime.timestamp() * 1000)
        return start_timestamp_ms
        
    
# load_dotenv()
# my_access_token = os.getenv('access_token')  
# # print(my_access_token,'this is the access_token')
# am = AlgoKM(access_token=my_access_token,tick_size=False)
# data = {'feeds': {'BSE_FO|1127928': {'fullFeed': {'marketFF': {'ltpc': {'ltp': 255.0, 'ltt': '1763529366611', 'ltq': '40', 'cp': 386.2}, 'marketLevel': {'bidAskQuote': [{'bidQ': '80', 'bidP': 254.6, 'askQ': '60', 'askP': 254.9}, {'bidQ': '20', 'bidP': 254.55, 'askQ': '40', 'askP': 255.0}, {'bidQ': '20', 'bidP': 254.5, 'askQ': '1080', 'askP': 255.05}, {'bidQ': '160', 'bidP': 254.4, 'askQ': '340', 'askP': 255.1}, {'bidQ': '340', 'bidP': 254.35, 'askQ': '340', 'askP': 255.15}]}, 'optionGreeks': {'delta': -0.4796, 'theta': -112.3274, 'gamma': 0.0006, 'vega': 19.3795, 'rho': -1.3444}, 'marketOHLC': {'ohlc': [{'interval': '1d', 'open': 385.95, 'high': 488.35, 'low': 235.6, 'close': 255.0, 'vol': '9027620', 'ts': '1763490600000'}, {'interval': 'I1', 'open': 260.5, 'high': 261.35, 'low': 255.4, 'close': 255.8, 'vol': '142360', 'ts': '1763529300000'}]}, 'atp': 296.86, 'vtt': '9027620', 'oi': 1416020.0, 'iv': 0.1387786865234375, 'tbq': 188240.0, 'tsq': 263140.0}}, 'requestMode': 'full_d5'}, 'BSE_FO|1128472': {'fullFeed': {'marketFF': {'ltpc': {'ltp': 228.1, 'ltt': '1763529365954', 'ltq': '40', 'cp': 239.35}, 'marketLevel': {'bidAskQuote': [{'bidQ': '160', 'bidP': 227.8, 'askQ': '20', 'askP': 228.3}, {'bidQ': '80', 'bidP': 227.75, 'askQ': '600', 'askP': 228.35}, {'bidQ': '220', 'bidP': 227.7, 'askQ': '160', 'askP': 228.4}, {'bidQ': '260', 'bidP': 227.65, 'askQ': '220', 'askP': 228.45}, {'bidQ': '680', 'bidP': 227.6, 'askQ': '180', 'askP': 228.5}]}, 'optionGreeks': {'delta': 0.5234, 'theta': -88.2644, 'gamma': 0.0008, 'vega': 19.3711, 'rho': 1.4507}, 'marketOHLC': {'ohlc': [{'interval': '1d', 'open': 239.2, 'high': 249.05, 'low': 125.75, 'close': 228.1, 'vol': '18783540', 'ts': '1763490600000'}, {'interval': 'I1', 'open': 219.35, 'high': 226.75, 'low': 219.35, 'close': 226.75, 'vol': '126180', 'ts': '1763529300000'}]}, 'atp': 191.32, 'vtt': '18783540', 'oi': 1438320.0, 'iv': 0.109100341796875, 'tbq': 238620.0, 'tsq': 226160.0}}, 'requestMode': 'full_d5'}}, 'currentTs': '1763529366451'}
# dat = am.extract_i1_ohlc(data,'BSE_FO|1127928')
# # dat.get('ltpc', {}).get('ltp', 0) if data.get('ltpc') else 0
# print(dat.get('ltpc', {})['ltp'])







                
