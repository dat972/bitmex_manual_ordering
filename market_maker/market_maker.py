from __future__ import absolute_import
from time import sleep
import sys
from datetime import datetime
from os.path import getmtime
import random
import requests
import atexit
import signal
import os
import math

from market_maker import bitmex
from market_maker.settings import settings
from market_maker.utils import log, constants, errors, botmath

# Used for reloading the bot - saves modified times of key files
import os
watched_files_mtimes = [(f, getmtime(f)) for f in settings.WATCHED_FILES]

#
# Helpers
#
logger = log.setup_custom_logger('root')


class ExchangeInterface:
    def __init__(self, dry_run=False):
        self.dry_run = dry_run
        if len(sys.argv) > 1:
            self.symbol = sys.argv[1]
        else:
            self.symbol = settings.SYMBOL
        self.bitmex = bitmex.BitMEX(base_url=settings.BASE_URL, symbol=self.symbol,
                                    apiKey=settings.API_KEY, apiSecret=settings.API_SECRET,
                                    orderIDPrefix=settings.ORDERID_PREFIX, postOnly=settings.POST_ONLY)

    def cancel_order(self, order):
        tickLog = self.get_instrument()['tickLog']
        logger.info("Canceling: %s %d @ %.*f" % (order['side'], order['orderQty'], tickLog, order['price']))
        while True:
            try:
                self.bitmex.cancel(order['orderID'])
                sleep(settings.API_REST_INTERVAL)
            except ValueError as e:
                logger.info(e)
                sleep(settings.API_ERROR_INTERVAL)
            else:
                break

    def cancel_all_orders(self):
        if self.dry_run:
            return

        logger.info("Resetting current position. Canceling all existing orders.")
        tickLog = self.get_instrument()['tickLog']

        # In certain cases, a WS update might not make it through before we call this.
        # For that reason, we grab via HTTP to ensure we grab them all.
        orders = self.bitmex.http_open_orders()
        
        # only cancel the buys and sells, do not cancel stops (stop orders dont have a ['price'] field they have ['stopPx'])
        non_stop_orders = [o for o in orders if o['price']]

        for order in non_stop_orders:
            logger.info("Canceling: %s %d @ %.*f" % (order['side'], order['orderQty'], tickLog, order['price']))

        # change this to len(non_stop_orders) to prevent canceling of stops or len(orders) to cancel all orders
        if len(non_stop_orders):
            self.bitmex.cancel([order['orderID'] for order in orders])

        sleep(settings.API_REST_INTERVAL)

    def get_portfolio(self):
        contracts = settings.CONTRACTS
        portfolio = {}
        for symbol in contracts:
            position = self.bitmex.position(symbol=symbol)
            instrument = self.bitmex.instrument(symbol=symbol)

            if instrument['isQuanto']:
                future_type = "Quanto"
            elif instrument['isInverse']:
                future_type = "Inverse"
            elif not instrument['isQuanto'] and not instrument['isInverse']:
                future_type = "Linear"
            else:
                raise NotImplementedError("Unknown future type; not quanto or inverse: %s" % instrument['symbol'])

            if instrument['underlyingToSettleMultiplier'] is None:
                multiplier = float(instrument['multiplier']) / float(instrument['quoteToSettleMultiplier'])
            else:
                multiplier = float(instrument['multiplier']) / float(instrument['underlyingToSettleMultiplier'])

            portfolio[symbol] = {
                "currentQty": float(position['currentQty']),
                "futureType": future_type,
                "multiplier": multiplier,
                "markPrice": float(instrument['markPrice']),
                "spot": float(instrument['indicativeSettlePrice'])
            }

        return portfolio

    def calc_delta(self):
        """Calculate currency delta for portfolio"""
        portfolio = self.get_portfolio()
        spot_delta = 0
        mark_delta = 0
        for symbol in portfolio:
            item = portfolio[symbol]
            if item['futureType'] == "Quanto":
                spot_delta += item['currentQty'] * item['multiplier'] * item['spot']
                mark_delta += item['currentQty'] * item['multiplier'] * item['markPrice']
            elif item['futureType'] == "Inverse":
                spot_delta += (item['multiplier'] / item['spot']) * item['currentQty']
                mark_delta += (item['multiplier'] / item['markPrice']) * item['currentQty']
            elif item['futureType'] == "Linear":
                spot_delta += item['multiplier'] * item['currentQty']
                mark_delta += item['multiplier'] * item['currentQty']
        basis_delta = mark_delta - spot_delta
        delta = {
            "spot": spot_delta,
            "mark_price": mark_delta,
            "basis": basis_delta
        }
        return delta

    def get_delta(self, symbol=None):
        if symbol is None:
            symbol = self.symbol
        return self.get_position(symbol)['currentQty']

    def get_instrument(self, symbol=None):
        if symbol is None:
            symbol = self.symbol
        return self.bitmex.instrument(symbol)

    def get_margin(self):
        if self.dry_run:
            return {'marginBalance': float(settings.DRY_BTC), 'availableFunds': float(settings.DRY_BTC)}
        return self.bitmex.funds()

    def get_orders(self):
        if self.dry_run:
            return []
        return self.bitmex.open_orders()

    def get_highest_buy(self):
        all_buys = [o for o in self.get_orders() if o['side'] == 'Buy']
        if not len(all_buys):
            return {'price': -2**32}
        # the price on stop orders is entered in the field ['stopPx'] not ['price'] so need to handle that exception
        # by creating a list of stop orders and a list of sell orders
        buys = [b for b in all_buys if b['price']]
        stops = [b for b in all_buys if not b['price']]
        if buys:
            highest_buy = max(buys or [], key=lambda o: o['price'])
        else:
            highest_buy = None
        if stops:
            highest_stop = max(stops or [], key=lambda o: o['stopPx'])
        else:
            highest_stop = None
        
        if highest_buy and highest_stop:
            if highest_buy > lowest_stop:
                return lowest_buy
            elif highest_stop > highest_buy:
                return highest_stop
        elif highest_buy:
            return highest_buy
        elif highest_stop:
            return highest_stop
        else:
            {'price': -2**32} 

    def get_lowest_sell(self):
        all_sells = [o for o in self.get_orders() if o['side'] == 'Sell']
        if not len(all_sells):
            return {'price': 2**32}
        # the price on stop orders is entered in the field ['stopPx'] not ['price'] so need to handle that exception
        # by creating a list of stop orders and a list of sell orders
        sells = [s for s in all_sells if s['price']]
        stops = [s for s in all_sells if not s['price']]
        if sells:
            lowest_sell = min(sells or [], key=lambda o: o['price'])
        else:
            lowest_sell = None
        if stops:
            lowest_stop = min(stops or [], key=lambda o: o['stopPx'])
        else:
            lowest_stop = None
        
        if lowest_sell and lowest_stop:
            if lowest_sell < lowest_stop:
                return lowest_sell 
            elif lowest_stop < lowest_sell:
                return lowest_stop
        elif lowest_sell:
            return lowest_sell
        elif lowest_stop:
            return lowest_stop
        else:
            {'price': 2**32}  # ought to be enough for anyone

    def get_position(self, symbol=None):
        if symbol is None:
            symbol = self.symbol
        return self.bitmex.position(symbol)

    def get_ticker(self, symbol=None):
        if symbol is None:
            symbol = self.symbol
        return self.bitmex.ticker_data(symbol)

    def is_open(self):
        """Check that websockets are still open."""
        return not self.bitmex.ws.exited

    def check_market_open(self):
        instrument = self.get_instrument()
        if instrument["state"] != "Open" and instrument["state"] != "Closed":
            raise errors.MarketClosedError("The instrument %s is not open. State: %s" %
                                           (self.symbol, instrument["state"]))

    def check_if_orderbook_empty(self):
        """This function checks whether the order book is empty"""
        instrument = self.get_instrument()
        if instrument['midPrice'] is None:
            raise errors.MarketEmptyError("Orderbook is empty, cannot quote")

    def amend_bulk_orders(self, orders):
        if self.dry_run:
            return orders
        return self.bitmex.amend_bulk_orders(orders)

    def create_bulk_orders(self, orders):
        if self.dry_run:
            return orders
        return self.bitmex.create_bulk_orders(orders)

    def cancel_bulk_orders(self, orders):
        if self.dry_run:
            return orders
        return self.bitmex.cancel([order['orderID'] for order in orders])

    # Section for Market Orders 
    def create_market_order(self, order, side):
        if self.dry_run:
            return order
        return self.bitmex.market_order(order, side)

    def market_close_position(self):
        if self.dry_run:
            return "Dry Run: Action = Market Close Position"
        return self.bitmex.market_close_position()

    # Limit order
    def create_limit_order(self, order, price, side):
        if self.dry_run:
            return ("Dry Run Limit order is {} {} contracts @ {}".format(side, order, price))
        return self.bitmex.place_limit_order(order, price, side)


class OrderManager:
    def __init__(self):
        self.exchange = ExchangeInterface(settings.DRY_RUN)
        # Once exchange is created, register exit handler that will always cancel orders
        # on any error.
        atexit.register(self.exit)
        signal.signal(signal.SIGTERM, self.exit)

        logger.info("Using symbol %s." % self.exchange.symbol)

    def init(self):
        if settings.DRY_RUN:
            logger.info("Initializing dry run. Orders printed below represent what would be posted to BitMEX.")
        else:
            logger.info("Connecting to BitMEX. Live Run: Ready to Trade BITCHES.")

        self.start_time = datetime.now()
        # what contract is being traded
        self.instrument = self.exchange.get_instrument()
        # contract stuff for ordering
        self.starting_qty = self.exchange.get_delta()
        self.running_qty = self.starting_qty
        self.reset()

    def reset(self):
        self.exchange.cancel_all_orders()
        self.sanity_check()
        self.print_status()

        # Create orders and converge.
        # self.place_orders()

        if settings.DRY_RUN:
            sys.exit()

    def print_status(self):
        """Print the current MM status."""
        margin = self.exchange.get_margin()
        position = self.exchange.get_position()
        self.running_qty = self.exchange.get_delta()
        tickLog = self.exchange.get_instrument()['tickLog']
        self.start_XBt = margin["marginBalance"]
        
        self.current_position = self.exchange.get_position()
        # logger.info(self.current_position)
        logger.info("Current Position: {} Contracts: {}, Avg Entry: {}, Unrealized PnL {}, Liquidation Price: {}".format(self.current_position['symbol'], self.current_position['currentQty'], self.current_position['avgEntryPrice'], round(self.current_position['unrealisedPnl'] * .00000001, 4), self.current_position['liquidationPrice']))

    def get_ticker(self):
        ticker = self.exchange.get_ticker()
        tickLog = self.exchange.get_instrument()['tickLog']

        # Set up our buy & sell positions as the smallest possible unit above and below the current spread
        # and we'll work out from there. That way we always have the best price but we don't kill wide
        # and potentially profitable spreads.
        self.start_position_buy = ticker["buy"] + self.instrument['tickSize']
        self.start_position_sell = ticker["sell"] - self.instrument['tickSize']

        # If we're maintaining spreads and we already have orders in place,
        # make sure they're not ours. If they are, we need to adjust, otherwise we'll
        # just work the orders inward until they collide.
        if settings.MAINTAIN_SPREADS:
            if ticker['buy'] == self.exchange.get_highest_buy()['price']:
                self.start_position_buy = ticker["buy"]
            if ticker['sell'] == self.exchange.get_lowest_sell()['price']:
                self.start_position_sell = ticker["sell"]

        # Back off if our spread is too small.
        if self.start_position_buy * (1.00 + settings.MIN_SPREAD) > self.start_position_sell:
            self.start_position_buy *= (1.00 - (settings.MIN_SPREAD / 2))
            self.start_position_sell *= (1.00 + (settings.MIN_SPREAD / 2))

        # Midpoint, used for simpler order placement.
        self.start_position_mid = ticker["mid"]
        logger.info(
            "%s Ticker: Buy: %.*f, Sell: %.*f" %
            (self.instrument['symbol'], tickLog, ticker["buy"], tickLog, ticker["sell"])
        )
        # Comment this out for now
        '''
        logger.info('Start Positions: Buy: %.*f, Sell: %.*f, Mid: %.*f' %
                    (tickLog, self.start_position_buy, tickLog, self.start_position_sell,
                     tickLog, self.start_position_mid))
        '''
        return ticker

    ###
    # Orders
    ###

    def place_orders(self):
        """Create order items for use in convergence."""

        buy_orders = []
        sell_orders = []
        # Create orders from the outside in. This is intentional - let's say the inner order gets taken;
        # then we match orders from the outside in, ensuring the fewest number of orders are amended and only
        # a new order is created in the inside. If we did it inside-out, all orders would be amended
        # down and a new order would be created at the outside.
        for i in reversed(range(1, settings.ORDER_PAIRS + 1)):
            if not self.long_position_limit_exceeded():
                buy_orders.append(self.prepare_order(-i, i, i))
            if not self.short_position_limit_exceeded():
                sell_orders.append(self.prepare_order(i, i, i))

        return self.converge_orders(buy_orders, sell_orders)

    def prepare_order(self, contracts, price, index):
        """Create an order object."""
        # number of contracts price and which side of book

        self.contracts = contracts
        self.price = price

        return {'price': price, 'orderQty': contracts, 'side': "Buy" if index < 0 else "Sell"}

    def converge_orders(self, buy_orders, sell_orders):
        """Converge the orders we currently have in the book with what we want to be in the book.
           This involves amending any open orders and creating new ones if any have filled completely.
           We start from the closest orders outward."""

        tickLog = self.exchange.get_instrument()['tickLog']
        to_amend = []
        to_create = []
        to_cancel = []
        buys_matched = 0
        sells_matched = 0
        existing_orders = self.exchange.get_orders()

        # Check all existing orders and match them up with what we want to place.
        # If there's an open one, we might be able to amend it to fit what we want.
        for order in existing_orders:
            try:
                if order['side'] == 'Buy':
                    desired_order = buy_orders[buys_matched]
                    buys_matched += 1
                else:
                    desired_order = sell_orders[sells_matched]
                    sells_matched += 1

                # Found an existing order. Do we need to amend it?
                if desired_order['orderQty'] != order['leavesQty'] or (
                        # If price has changed, and the change is more than our RELIST_INTERVAL, amend.
                        desired_order['price'] != order['price'] and
                        abs((desired_order['price'] / order['price']) - 1) > settings.RELIST_INTERVAL):
                    to_amend.append({'orderID': order['orderID'], 'orderQty': order['cumQty'] + desired_order['orderQty'],
                                     'price': desired_order['price'], 'side': order['side']})
            except IndexError:
                # Will throw if there isn't a desired order to match. In that case, cancel it.
                to_cancel.append(order)

        while buys_matched < len(buy_orders):
            to_create.append(buy_orders[buys_matched])
            buys_matched += 1

        while sells_matched < len(sell_orders):
            to_create.append(sell_orders[sells_matched])
            sells_matched += 1

        if len(to_amend) > 0:
            for amended_order in reversed(to_amend):
                reference_order = [o for o in existing_orders if o['orderID'] == amended_order['orderID']][0]
                logger.info("Amending %4s: %d @ %.*f to %d @ %.*f (%+.*f)" % (
                    amended_order['side'],
                    reference_order['leavesQty'], tickLog, reference_order['price'],
                    (amended_order['orderQty'] - reference_order['cumQty']), tickLog, amended_order['price'],
                    tickLog, (amended_order['price'] - reference_order['price'])
                ))
            # This can fail if an order has closed in the time we were processing.
            # The API will send us `invalid ordStatus`, which means that the order's status (Filled/Canceled)
            # made it not amendable.
            # If that happens, we need to catch it and re-tick.
            try:
                print("hello")
                # self.exchange.amend_bulk_orders(to_amend)
            except requests.exceptions.HTTPError as e:
                errorObj = e.response.json()
                if errorObj['error']['message'] == 'Invalid ordStatus':
                    logger.warn("Amending failed. Waiting for order data to converge and retrying.")
                    sleep(0.5)
                    return self.place_orders()
                else:
                    logger.error("Unknown error on amend: %s. Exiting" % errorObj)
                    sys.exit(1)

        if len(to_create) > 0:
            logger.info("Creating %d orders:" % (len(to_create)))
            for order in reversed(to_create):
                logger.info("%4s %d @ %.*f" % (order['side'], order['orderQty'], tickLog, order['price']))
            #self.exchange.create_bulk_orders(to_create)

        # Could happen if we exceed a delta limit
        if len(to_cancel) > 0:
            logger.info("Canceling %d orders:" % (len(to_cancel)))
            for order in reversed(to_cancel):
                logger.info("%4s %d @ %.*f" % (order['side'], order['leavesQty'], tickLog, order['price']))
            #self.exchange.cancel_bulk_orders(to_cancel)

    ###
    # Position Limits
    ###

    def short_position_limit_exceeded(self):
        "Returns True if the short position limit is exceeded"
        if not settings.CHECK_POSITION_LIMITS:
            return False
        position = self.exchange.get_delta()
        return position <= settings.MIN_POSITION

    def long_position_limit_exceeded(self):
        "Returns True if the long position limit is exceeded"
        if not settings.CHECK_POSITION_LIMITS:
            return False
        position = self.exchange.get_delta()
        return position >= settings.MAX_POSITION

    ###
    # Sanity
    ##

    def sanity_check(self):
        """Perform checks before placing orders."""
        
        # Check if OB is empty - if so, can't quote.
        self.exchange.check_if_orderbook_empty()

        # Ensure market is still open.
        self.exchange.check_market_open()

        # Get ticker, which sets price offsets and prints some debugging info.
        ticker = self.get_ticker()

        # Sanity check:
        """
        if self.get_price_offset(-1) >= ticker["sell"] or self.get_price_offset(1) <= ticker["buy"]:
            logger.error(self.start_position_buy, self.start_position_sell)
            logger.error("First buy position: %s\nBitMEX Best Ask: %s\nFirst sell position: %s\nBitMEX Best Bid: %s" %
                         (self.get_price_offset(-1), ticker["sell"], self.get_price_offset(1), ticker["buy"]))
            logger.error("Sanity check failed, exchange data is inconsistent")
            self.exit()"""

        # Messanging if the position limits are reached
        if self.long_position_limit_exceeded():
            logger.info("Long delta limit exceeded")
            logger.info("Current Position: %.f, Maximum Position: %.f" %
                        (self.exchange.get_delta(), settings.MAX_POSITION))

        if self.short_position_limit_exceeded():
            logger.info("Short delta limit exceeded")
            logger.info("Current Position: %.f, Minimum Position: %.f" %
                        (self.exchange.get_delta(), settings.MIN_POSITION))

    ###
    # Running
    ###

    def check_file_change(self):
        """Restart if any files we're watching have changed."""
        for f, mtime in watched_files_mtimes:
            if getmtime(f) > mtime:
                self.restart()

    def check_connection(self):
        """Ensure the WS connections are still open."""
        return self.exchange.is_open()

    def exit(self):
        logger.info("Shutting down. All open orders will be cancelled.")
        try:
            self.exchange.cancel_all_orders()
            self.exchange.bitmex.exit()
        except errors.AuthenticationError as e:
            logger.info("Was not authenticated; could not cancel orders.")
        except Exception as e:
            logger.info("WERE FUCKED!!!! Unable to cancel orders: %s" % e)

        sys.exit()

    ####
    # User Prompt Functions
    #
    # functions to help with exiting if statements when users input bad instructions
    # (program while loop won't break with bad user input)
    ###
    def limit_order_logic(self):
        # this function will return (contracts, price) after completing some sanity checks to ensure you
        # dont fat finger orders
        contracts = input("#Contracts: ")
        price = input("Bid Price: ")
        
        # Add Price Sanity Check to Prevent fat fingering large orders
        max_safety_size = 40000
        price_sanity = int(price)
        if price_sanity < max_safety_size:
            order = {
                'contracts' : contracts,
                'price' : price
            }
            return order
        else:
            print("** Safety Check Order Size **")
            double_check = int(input("Order Size Safety Check, Please Re-Enter Order Size: "))
            if price_sanity == double_check:
                print("Verified Order Size")
                order = {
                    'contracts' : contracts,
                    'price' : price
                }
                return(order)
            else:
                print("Please re-enter Order")
                contracts = input("# Contracts: ")
                price = input("Bid Price: ")
                if contracts and price:
                    order = {
                    'contracts' : contracts,
                    'price' : price
                    }
                    return(order)
                else:
                    return None

    def scaled_order_logic(self):
        order_side = input("Scaled (b)uy/(s)ell? ").lower()

        if order_side == 'b':
            side = "Buy"
        elif order_side == 's':
                side = "Sell"
        else:
            print("Invalid Order Side, please enter (b) or (s) ")
            return None

        price_range = []
        lower_bound = int(input("Please enter the lower bound of order price range: "))
        upper_bound = int(input("Please enter the upper bound of order price range: "))
        price_range.append(lower_bound)
        price_range.append(upper_bound)
        print("Price range: {} - {}".format(lower_bound, upper_bound))
        order_size = int(input("Please enter total number of contracts: "))
        chunks = int(input("These contracts should be split into how many separate orders?: "))

        if chunks > 1:
            print("{} Contracts will be split into {} separate orders over the selected range".format(order_size, chunks))
            order_chunk = math.floor(order_size / chunks) # cant enter partial contracts
            if order_chunk > 1:
                # calculate order spacing
                # math ensures orders will always be placed at the top and bottom of the range
                # 5 orders should be placed at 100, 125, 150, 175, 200 // 100/4 [100, 100+25(x1), 100+25(x2)..]
                increment = (max(price_range)-min(price_range))/(chunks - 1)
                order_prices = []
                for x in range(chunks):
                    order_prices.append(math.floor(min(price_range) + increment * x))
                for prices in order_prices:
                    print("Placing order: {} contracts @ ${}".format(order_chunk, prices))
                last_check = input("Enter y to place order, hit any other key to cancel").lower()
                if last_check == 'y':
                    print("Executing Order")
                    orders = []
                    for prices in order_prices:
                        order_obj = {
                            'side': side,
                            'orderQty': order_chunk,
                            'price': prices
                        }
                        orders.append(order_obj)
                    self.exchange.create_bulk_orders(orders)
                else:
                    print("Cancelling Scaled Order")
                    pass
            else:
                print("Error: contract order size must be greater than 1 after splitting")
                return None
        else:
            print("Error: Contracts need to be split into at least 2 orders")
            return None
    
    def simple_stop(self):
        print("Simple Stop Selected")
        side = input("Buy or Sell Stop? (b)/(s): ")
        contracts = input("# Contracts: ")
        price = input("Stop Price: ")
        sure = input("{} Stop for {} Contracts @ {} are you sure? (y)/(n)".format(side, contracts, price))
        if sure == 'y':
            print("Entering Stop Order")
        else:
            print("Stop Canceled, Refreshing Data")
            return None

    def scaled_stop(self):
        ticker = self.exchange.get_ticker()
        print("Scaled Stop Selected")
        order_side = input("Buy or Sell Stop? (b)/(s): ")
        if order_side == 'b':
            side = "Buy"
        elif order_side == 's':
            side = "Sell"
        else:
            print("No side selected, canceling order")
            return None

        # User Input for Order And Handling Null input
        order_size = int(input("# Contracts: "))
        if order_size:
            pass
        else:
            print("No Order Size Entered Cancelling Emergency Stop")
            return None
        chunks = int(input("These contracts should be split into how many separate orders?: "))
        if chunks > 1:
            pass
        else:
            print("Invalid Chunk Size Cancelling Emergency Stop")
            return None
        lower_price = int(input("Enter lower bound for price: "))
        if lower_price > 0:
            pass
        else:
            print("Invalid Lower Price Cancelling Emergency Stop")
            return None
        upper_price = int(input("Enter upper bound for price: "))
        if upper_price > 0:
            pass
        else:
            print("Invalid Upper Price Cancelling Emergency Stop")
            return None
        price_range = [lower_price, upper_price]

        # Cancel order if stops are entered for invalid prices
        if side == "Buy":
            if min(price_range) < ticker["sell"]:
                print("Buy stop must be higher than current Ask, Canceling Stops")
                return None
        elif side == "Sell":
            if max(price_range) > ticker["buy"]:
                print("Sell stop must be lower than current Bid, Canceling Stops")
                return None

        # logic to split order and create bulk orders
        if chunks > 1:
            print("{} Stop will be split into {} separate orders over the selected range".format(order_size, chunks))
            order_chunk = math.floor(order_size / chunks) # cant enter partial contracts
            if order_chunk > 1:
                # calculate order spacing
                # math ensures orders will always be placed at the top and bottom of the range
                # 5 orders should be placed at 100, 125, 150, 175, 200 // 100/4 [100, 100+25(x1), 100+25(x2)..]
                increment = (max(price_range)-min(price_range))/(chunks - 1)
                order_prices = []
                for x in range(chunks):
                    order_prices.append(math.floor(min(price_range) + increment * x))
                for prices in order_prices:
                    print("Placing stop: {} contracts @ ${}".format(order_chunk, prices))
                last_check = input("Place order? (y)/(n): ").lower()
                if last_check == 'y':
                    print("Executing Order")
                    orders = []
                    for prices in order_prices:
                        order_obj = {
                            'side': side,
                            'ordType' : 'Stop',
                            'orderQty': order_chunk,
                            'stopPx': prices
                        }
                        orders.append(order_obj)
                    self.exchange.create_bulk_orders(orders)
        else:
            print("Stop Canceled, Refreshing Data")
            return None

    def emergency_stop(self):
        min_tick = 0.5 #minimum tick size on Bitmex
        print("Emergency Stop Selected")
        order_side = input("Buy or Sell Stop? (b)/(s): ")
        if order_side == 'b':
            side = "Buy"
            index = 1
        elif order_side == 's':
            side = "Sell"
            index = -1
        else:
            print("Cancelling Emergency Stop")
            return None

        # Bailout of order if user doesnt input order size or price
        contracts = input("# Contracts: ")
        if contracts:
            pass
        else:
            print("No Contracts Entered Cancelling Emergency Stop")
            return None
        price = int(input("Stop Price: "))
        if price:
            pass
        else:
            print("No Price Entered Cancelling Emergency Stop")
            return None

        sure = input("{} Stop for {} Contracts @ {} are you sure? (y)/(n)".format(side, contracts, price))
        if sure == 'y':
            orders = []
            for x in range(50):
                order = {}
                order['side'] = side
                order['ordType'] = 'Stop'
                order['stopPx'] = str(price - (index * x * min_tick))
                if x < 10:
                    order['orderQty'] = contracts
                elif 10 <= x < 20:
                    order['orderQty'] = 1000
                elif 20 <= x < 30:
                    order['orderQty'] = 100
                elif 30 <= x < 40:
                    order['orderQty'] = 10
                elif x >= 40:
                    order['orderQty'] = 1
                orders.append(order)
            for y in orders:
                print("Entering {} Stop @ {}".format(y['orderQty'], y['stopPx']))
        else:
            print("Stop Canceled, Refreshing Data")
            return None

    def exit_peacefully(self):
        # gracefully exit if statements
        return None

    def run_loop(self):
        while True:
            sys.stdout.write("-----\n")
            sys.stdout.flush()

            self.check_file_change()
            sleep(settings.LOOP_INTERVAL)

            # This will restart on very short downtime, but if it's longer,
            # the MM will crash entirely as it is unable to connect to the WS on boot.
            if not self.check_connection():
                logger.error("Realtime data connection unexpectedly closed, restarting.")
                self.restart()

            self.sanity_check()  # Ensures health of mm - several cut-out points here
            self.print_status()  # Print skew, delta, etc

            # Wait for user input to begin forming orders
            order = input("\nPlease enter order type, (b)uy/(s)ell/(c)lose/(x)special or (t) for stops: ").lower()

            if order == 'b':
                side = "Buy"
                order_type = input("Limit or Market order (l)/(m): ").lower()

                if order_type == 'l':
                    limit_order = self.limit_order_logic()
                    sure = input("Are you sure you want to order (y)/(n): ")
                    if limit_order and sure == 'y':
                        print ("Executing Buy Order for {} Contracts @ {}".format(limit_order['contracts'], limit_order['price']))
                        self.exchange.create_limit_order(limit_order['contracts'], limit_order['price'], side)
                    else:
                        print("Cancelling Order and Refreshing Data")

                elif order_type == 'm':
                    contracts = input("# Contracts: ")
                    # Sanity Check To Prevent fat finger
                    safety_size = 40000 # if orders are bigger than this initiatie safety checks
                    if int(contracts) > safety_size:
                        print("*** Fat Finger Safety Check ***")
                        contracts_double_check = int(input("Please re-enter # Contracts: "))
                        if contracts_double_check == contracts:
                            print("Order Size Successfully Confirmed")
                            sure = input("Are you sure you want to order {:,} Contracts (y)/(n): ".format(int(contracts)))
                            if sure == 'y':
                                print ("Executing Market Sell for {} Contracts".format(contracts))
                                self.exchange.create_market_order(contracts, side)
                        else:
                            print("Order Size Verification Failure: Refreshing Loop, Please Try Again.")
                    elif int(contracts) < safety_size:
                        sure = input("Are you sure you want to order {:,} Contracts (y)/(n): ".format(int(contracts)))
                        if sure == 'y':
                            print ("Executing Market Sell for {} Contracts".format(contracts))
                            self.exchange.create_market_order(contracts, side)
                    else:
                        print("Cancelling Order and Refreshing Data")
                else:
                    pass

            elif order =='s':
                side = "Sell"
                order_type = input("Limit or Market order (l)/(m): ").lower()

                if order_type == 'l':
                    limit_order = self.limit_order_logic()
                    sure = input("Are you sure you want to order (y)/(n): ")
                    if limit_order and sure == 'y':
                        print ("Executing Buy Order for {} Contracts @ {}".format(limit_order['contracts'], limit_order['price']))
                        self.exchange.create_limit_order(limit_order['contracts'], limit_order['price'], side)
                    else:
                        print("Cancelling Order and Refreshing Data")

                elif order_type == 'm':
                    contracts = input("# Contracts: ")
                    # Sanity Check To Prevent fat finger
                    safet_size = 40000 # if orders are bigger than this initiatie safety checks
                    if int(contracts) > safety_size:
                        print("*** Fat Finger Safety Check ***")
                        contracts_double_check = int(input("Please re-enter # Contracts: "))
                        if contracts_double_check == contracts:
                            print("Order Size Successfully Confirmed")
                            sure = input("Are you sure you want to order {:,} Contracts (y)/(n): ".format(int(contracts)))
                            if sure == 'y':
                                print ("Executing Market Sell for {} Contracts".format(contracts))
                                self.exchange.create_market_order(contracts, side)
                        else:
                            print("Order Size Verification Failure: Refreshing Loop, Please Try Again.")
                    elif int(contracts) < safety_size:
                        sure = input("Are you sure you want to order {:,} Contracts (y)/(n): ".format(int(contracts)))
                        if sure == 'y':
                            print ("Executing Market Sell for {} Contracts".format(contracts))
                            self.exchange.create_market_order(contracts, side)
                    else:
                        print("Cancelling Order and Refreshing Data")
                else:
                    pass

            elif order == 'c':
                sure = input("Do you want to Market Close all Positions? (y/n) ").lower()
                if sure == 'y':
                    print ("Closing all positions")
                    self.exchange.market_close_position()
                else:
                    print("Canceling Order Close Command")

            # - - - - - - - 
            # Special Order: types like waterfall and scaled etc
            # - - - - - - - 
            elif order == 'x':
                special = input("Special Order Types: (s)caled/(i)ceberg: ").lower()
                if special == 's':
                    self.scaled_order_logic()
                elif special == 'i':
                    print("You selected iceberg order")
                else:
                    print("Invalid Order Type. Updating Price Data. Try Again After Update")
            elif order == 't':
                stop_type = input("Stop Options - (1)Simple/(2)Scaled/(3)emergency: ")
                # error handling bad user input
                try:
                    stop_type = int(stop_type)
                    error_code = 200
                except:
                    error_code = 400
                    pass
                if error_code == 400:
                    self.exit_peacefully()
                if stop_type == 1:
                    self.simple_stop()
                elif stop_type == 2:
                    self.scaled_stop()
                elif stop_type ==3:
                    self.emergency_stop()
                else:
                    print("No Stops Chosen, Refreshing Price Data")
                    pass
            else:
                print("No Orders Placed, Updating Price Data")
                pass
                
            #self.place_orders()  # Creates desired orders and converges to existing orders

    def restart(self):
        logger.info("Restarting the market maker...")
        os.execv(sys.executable, [sys.executable] + sys.argv)

#
# Helpers
#

#testing github commits

def XBt_to_XBT(XBt):
    return float(XBt) / constants.XBt_TO_XBT


def cost(instrument, quantity, price):
    mult = instrument["multiplier"]
    P = mult * price if mult >= 0 else mult / price
    return abs(quantity * P)


def margin(instrument, quantity, price):
    return cost(instrument, quantity, price) * instrument["initMargin"]


def run():
    logger.info('BitMEX Market Maker Version: %s\n' % constants.VERSION)

    om = OrderManager()
    # Try/except just keeps ctrl-c from printing an ugly stacktrace
    try:
        om.init()
        om.run_loop()
    except (KeyboardInterrupt, SystemExit):
        sys.exit()
