BitMEX Manual Ordering Through API
===================

This code is a modification of the publicly available Bitmex Market Maker which can be found here https://github.com/BitMEX/sample-market-maker.

This code was created to give the user the ability to manually input orders on Bitmex through the command line using the API. This method allows for a variety of special order types and helps with order placement during times of high trading volumes where the trading interface is unusable through the web.


Order Types Currently Supported
------------------
Limit - place a single limit order buy or sell: type (l) and hit enter when prompted
Market - place a single market order buy or sell: type (m) and hit enter when prompted
Close - will market close all open positions: type (c) and hit enter when prompted
Special - this will initation the special order sequence for more complicated orders: type (x) and hit enter when prompted


Special Order Types
--------------------

Scaled - Define a price range and place an order evenly throughout that range in n chunks.
Ex: place buy order for 10,000 contracts from the range of $1,000 - $2,000 in 5 chunks. This will create the following orders 
     2,000 contracts @ $1,000
     2,000 contracts @ $1,250
     2,000 contracts @ $1,500
     2,000 contracts @ $1,750
     2,000 contracts @ $2,000

Iceberg(Not Coded) - same concept as scaled orders but with uneven order distribution i.e. smaller orders closer to current price and bigger orders further away from current price

Stop Orders
-----------
1. Simple Stop (Unsupported) - place simple stop
2. Scaled Stop - same as scaled order only for stops
3. Emergency Stop (Unsupported) - this order type is inteded to prevent getting REKT on the ocassion that one of your stop orders does not trigger in the market. This order will Create 50 stop orders in the following way
     10 orders for Total # of Contracts Input by the User
     10 orders for 1,000 Contracts
     10 orders for 100 Contracts
     10 orders for 10 Contracts
     10 orders for 1 Contracts

The purpose of this is to (A) input your stop order multiple times incase one does not get filled and (B) input stop orders that will protect you in the case that your stop order is only partially filled for whatever reason
