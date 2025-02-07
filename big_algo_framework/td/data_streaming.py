import tda
from tda.streaming import StreamClient
import asyncio
from datetime import datetime, timedelta
import pytz
import pandas as pd
from sqlalchemy import text
import time
import multiprocessing
import threading

class tdTimeSaleDataStreaming:
    def __init__(self, db, tickers, streaming_data_table, api_key, account_id, redirect_uri, queue_size=0, credentials_path='./ameritrade-credentials.json'):
        self.db = db
        self.api_key = api_key
        self.account_id = account_id
        self.redirect_uri = redirect_uri
        self.credentials_path = credentials_path
        self.tda_client = None
        self.stream_client = None
        self.tickers = tickers
        self.streaming_data_table = streaming_data_table

        # Create a queue so we can queue up work gathered from the client
        self.queue = asyncio.Queue(queue_size)

    def initialize(self):
        """
        Create the clients and log in. Using easy_client, we can get new creds
        from the user via the web browser if necessary
        """
        try:
            self.tda_client = tda.auth.client_from_token_file(self.credentials_path, self.api_key)

        except FileNotFoundError:
            from selenium import webdriver

            with webdriver.Chrome() as driver:
                self.tda_client = tda.auth.client_from_login_flow(driver, self.api_key, self.redirect_uri, self.credentials_path)

        self.stream_client = StreamClient(
            self.tda_client, account_id=self.account_id)

        # The streaming client wants you to add a handler for every service type
        self.stream_client.add_timesale_equity_handler(
            self.handle_timesale_equity)

    async def stream(self):
        await self.stream_client.login()  # Log into the streaming service
        await self.stream_client.quality_of_service(StreamClient.QOSLevel.EXPRESS)
        await self.stream_client.timesale_equity_subs(self.tickers)

        # Kick off our handle_queue function as an independent coroutine
        asyncio.ensure_future(self.handle_queue())

        # Continuously handle inbound messages
        while True:
            await self.stream_client.handle_message()

    async def handle_timesale_equity(self, msg):
        """
        This is where we take msgs from the streaming client and put them on a
        queue for later consumption. We use a queue to prevent us from wasting
        resources processing old data, and falling behind.
        """
        # if the queue is full, make room
        if self.queue.full():  # This won't happen if the queue doesn't have a max size
            print('Handler queue is full. Awaiting to make room... Some messages might be dropped')
            await self.queue.get()
        await self.queue.put(msg)

    def write_db(self, msg):
        now = datetime.now()
        day = now.day
        month = now.month
        year = now.year

        mkt_start = datetime(year, month, day, 9, 30, 00)
        mkt_end = datetime(year, month, day, 16, 00, 00)

        df = pd.DataFrame()
        streaming_list = []

        if msg.get('content'):
            for content in msg['content']:
                date_time = datetime.fromtimestamp(int(content['TRADE_TIME'] / 1000))
                if mkt_start <= date_time <= mkt_end:
                    d = {'ticker': content['key'],
                         'date_time': content['TRADE_TIME'],
                         'price': content['LAST_PRICE'],
                         'volume': content['LAST_SIZE']}

                    streaming_list.append(d)

        if streaming_list:
            df = pd.DataFrame(data=streaming_list)
            df.to_sql(self.streaming_data_table, self.db, if_exists='append', index=False, method='multi')

            query = text("CREATE INDEX IF NOT EXISTS {} ON {} (ticker, date_time);".format(
                self.streaming_data_table + "date_time", self.streaming_data_table))
            with self.db.connect() as conn:
                conn.execute(query)

    async def handle_queue(self):
        """
        Here we pull messages off the queue and process them.
        """
        while True:
            msg = await self.queue.get()
            con_thread = threading.Thread(target=self.write_db(msg), daemon=True)
            con_thread.start()

            # con_thread = multiprocessing.Process(target=self.write_db(msg))
            # con_thread.start()
