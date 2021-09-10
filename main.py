import json, config, requests, traceback
from datetime import datetime
from flask import Flask, request
from binance.client import Client
from binance.enums import *
from binance.exceptions import *

app = Flask(__name__)

# Binance Client
client = Client(config.API_KEY, config.API_SECRET)

# Transaction List
transactions = []


# *********************************************************************************************
# FUNCTIONS
# *********************************************************************************************


def order_to_str(order, asset_name):
    price = order['fills'][0]['price']
    quantity = order['fills'][0]['qty']
    balance = str(round(float(price) * float(quantity), 2))
    side = order['side'].casefold().capitalize()
    string = side + ' ' + quantity + " " + asset_name + " for $" + price + " ($" + balance + ")"
    return string


def current_time():
    now = datetime.now()
    time_string = now.strftime("%m/%d/%Y, %H:%M:%S")
    return "Time: " + time_string


# Send an error report to the specified Discord Group
def send_report(report):
    if config.REPORT:
        message = "@everyone " + report
        requests.post(config.DISCORD_ERROR_LINK,
                      data={"content": message},
                      headers=config.DISCORD_HEADER)


def report_transaction(transaction):
    if config.REPORT:
        requests.post(config.DISCORD_TRANSACTION_LINK,
                      data={"content": transaction},
                      headers=config.DISCORD_HEADER)


# Repay Loan
def repay_loan(asset, amount, symbol):
    try:
        # Cross margin function
        client.repay_margin_loan(asset=asset, amount=amount)

    # Error
    except BinanceAPIException as e:
            print(e)
            send_report(str(e.message) + "During Repay Loan")


# Get a loan
def take_loan(asset, amount):
    try:
        max = float(client.get_max_margin_loan(asset='BTC')['amount'])
        if max > amount:
            amount = max
        client.create_margin_loan(asset=asset, amount=amount)

    # Error
    except BinanceAPIException as e:
        print(e)
        send_report(str(e.message) + "During Take Loan")


# Margin Order
def margin_order(side, quantity, symbol, precision, step, price_precision, order_type=ORDER_TYPE_MARKET):
    order = False
    # Round the numbers to required decimal places
    if precision >= 0:
        quantity = round(quantity, precision)

    # Or round up the integer
    elif precision < 0:
        quantity = quantity // (10 ** -precision) * (10 ** -precision)
    try:
        # Execute the order
        order = client.create_margin_order(symbol=symbol, side=side, type=order_type, quantity=quantity)
    # Exit if an error occurs
    except BinanceAPIException as e:
        if e.message == "Account has insufficient balance for requested action.":
            order = margin_order(side, quantity - step, symbol, precision,
                                 step, price_precision, order_type=ORDER_TYPE_MARKET)
        else:
            print(e.message)
            send_report(str(e.message) + "During Margin Order")
            return False
    return order


# *********************************************************************************************
# ROUTES
# *********************************************************************************************


# Index page
@app.route('/')
def hello_world():
    return 'Hello, World!'


# Address for receiving pings to avoid idling
@app.route('/ping', methods=['POST'])
def ping():
    return "Pinged!"


# Address for receiving webhooks
@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        transactions.append(current_time())

        # *********************************************************************************************
        # MAIN ROUTE
        # *********************************************************************************************
        # READING RECEIVED DATA
        # ---------------------------------------------------------------------------------------------

        # Change JSON object from webhook to a python dictionary
        data = json.loads(request.data)

        # Check if safety key is matching
        if data['passphrase'] != config.WEBHOOK_PASSPHRASE:
            send_report("Incorrect Passcode!")
            return {
                "code": "error",
                "message": "Access Denied!"
            }

        side = data['strategy']['order_action'].upper()

        action = data['strategy']['order_comment'].upper()

        leverage = config.LEVERAGE

        # Get the base currency, and check it's length
        base_name = data['base_currency']
        base_len = len(base_name)

        # Get The pair and asset names
        symbol = data['ticker']

        # Get the asset name by removing the base currency name from the pair
        asset_name = symbol[:-base_len]

        # Get information about the pair
        symbol_info = client.get_symbol_info(symbol)

        # Check the minimum precision
        price_precision = 0
        precision = 0
        for rule in symbol_info['filters']:
            if rule['filterType'] == "LOT_SIZE":
                min_quantity = float(rule["minQty"])

                # Check the maximum precision
                max_quantity = float(rule["maxQty"])

                # Calculate the precision from minimum quantity
                i = min_quantity

                # Precision will be positive, if it allows floating numbers
                if i < 1:
                    while i < 1:
                        i *= 10
                        precision += 1

                # Precision will be negative, if it requires rounding up integers
                elif i > 1:
                    while i > 1:
                        i //= 10
                        precision += -1

            # Check the minimum base currency amount requirements
            if rule['filterType'] == "MIN_NOTIONAL":
                min_base_order = float(rule["minNotional"])

            # Check the minimum base currency amount requirements
            if rule['filterType'] == "PRICE_FILTER":
                i = float(rule["tickSize"])

                # Precision will be positive, if it allows floating numbers
                if i < 1:
                    while i < 1:
                        i *= 10
                        price_precision += 1

                # Precision will be negative, if it requires rounding up integers
                elif i > 1:
                    while i > 1:
                        i //= 10
                        price_precision += -1

        # ---------------------------------------------------------------------------------------------
        # MARGIN LONG
        # ---------------------------------------------------------------------------------------------

        assets = client.get_margin_account()['userAssets']

        # If Going Long
        if side == "BUY":

            # Check information about assets
            for asset in assets:

                if asset['asset'] == asset_name:
                    # Borrowed
                    loan_amount = float(asset['borrowed'])
                if asset['asset'] == base_name:
                    # Base currency amount
                    base = float(asset['free'])
                    old_loan = float(asset['borrowed'])

            if old_loan > 10 and action == "BUY":
                return {
                        "code": "error",
                        "message": "Already in a Trade!"
                    }

            # Get asset price
            price = float(client.get_margin_price_index(symbol=symbol)['price'])

            # Calculate the amount you can buy
            if loan_amount > 0:
                base_price = price * loan_amount
                if base_price > min_base_order:
                    order_response = margin_order(side, loan_amount, symbol,
                                                  precision, min_quantity, price_precision)
                    transactions.append(order_to_str(order_response, asset_name))
                    repay_loan(asset_name, loan_amount, symbol)

            if action == "BUY":

                assets = client.get_margin_account()['userAssets']

                # Check information about assets
                for asset in assets:
                    if asset['asset'] == base_name:
                        # Base currency amount
                        base = float(asset['free'])

                loan = base * leverage
                take_loan(base_name, loan)
                base += loan

                price = float(client.get_margin_price_index(symbol=symbol)['price'])
                quantity = base / price
                order_response = margin_order(side, quantity, symbol, precision, min_quantity, price_precision)
                transactions.append(order_to_str(order_response, asset_name))

                if order_response:

                    for transaction in transactions:
                        report_transaction(transaction)

                    return {
                        "code": "success",
                        "message": "order completed"
                    }
                else:
                    # Failed order
                    print("Long failed!")
                    send_report("Long failed!")

                    return {
                        "code": "error",
                        "message": "order failed"
                    }
            else:
                for transaction in transactions:
                    report_transaction(transaction)
                return {
                    "code": "success",
                    "message": "short canceled!"
                }

        # ---------------------------------------------------------------------------------------------
        # MARGIN SHORT
        # ---------------------------------------------------------------------------------------------
        # CLOSING LONG
        # .............................................................................................

        # If going Long
        elif side == "SELL":

            # Check information about assets
            for asset in assets:
                if asset['asset'] == asset_name:
                    # Borrowed
                    asset_free = float(asset['free'])
                    old_loan = float(asset['borrowed'])
                if asset['asset'] == base_name:
                    # Base currency amount
                    loan_amount = float(asset['borrowed'])

            if old_loan > 3 and action == "SELL":
                return {
                        "code": "error",
                        "message": "Already in a Trade!"
                    }

            if asset_free > 0:
                order_response = margin_order(side, asset_free, symbol, precision, min_quantity, price_precision)
                transactions.append(order_to_str(order_response, asset_name))
                if loan_amount > 0:
                    repay_loan(base_name, loan_amount, symbol)

            if action == "SELL":

                assets = client.get_margin_account()['userAssets']
                for asset in assets:
                    if asset['asset'] == base_name:
                        base = float(asset['free'])

                price = float(client.get_margin_price_index(symbol=symbol)['price'])
                quantity = (base + base * leverage) / price

                # Take a loan for the same amount as sold
                take_loan(asset_name, quantity)

                # .............................................................................................
                # SHORTING THE MARKET
                # .............................................................................................

                # Sell the loan to short the market
                # Later it will be bought and repaid for a lower price
                order_response = margin_order(side, quantity, symbol, precision, min_quantity, price_precision)
                transactions.append(order_to_str(order_response, asset_name))

                # Successful trade
                if order_response:
                    for transaction in transactions:
                        report_transaction(transaction)

                    return {
                        "code": "success",
                        "message": "order completed"
                    }

                else:
                    # Error :(
                    print("Short Failed!")
                    send_report("Failed Short")

                    return {
                        "code": "error",
                        "message": "order failed"
                    }
            else:
                for transaction in transactions:
                    report_transaction(transaction)
                return {
                    "code": "success",
                    "message": "long canceled!"
                }
        else:
            # No BUY or SELL found in signal
            print("incorrect order action")

            send_report("Incorect Buy / Sell In Alert")

            return {
                "code": "error",
                "message": "incorrect order action"
                }
    except Exception as e:
        print(e)
        send_report(str(repr(e)) + " Crash!")
        return {
            "code": "error",
            "message": "incorrect order action"
        }
