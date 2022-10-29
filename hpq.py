import copy
import datetime
import io
import ijson
import json
import websocket


class WebSocketClient:
    class __Error(Exception):
        def __init__(self, str):
            super().__init__(str)

    class __JsonError(__Error):
        def __init__(self, obj):
            super().__init__(json.dumps(obj))

    __idle = 0
    __request_sent = 1
    __scheduled = 2
    __waiting_for_response = 3
    __after_response = 4

    def __init__(self):
        self.url = "wss://mdx.uat.maystreet.com"
        self.init_opts = {}
        self.socket = None
        self.frame = None
        self.connect_opts = {}
        self.accepted = None
        self.__state = WebSocketClient.__idle

    def connect(self):
        if not self.socket:
            socket = websocket.WebSocket(**self.init_opts)
            socket.connect(self.url, **self.connect_opts)
            self.socket = socket
        
        return self.socket

    def send_request_raw(self, request):
        self.connect().send(request)
        self.__state = WebSocketClient.__request_sent

    def send_request(self, request):
        self.send_request_raw(json.dumps(request))

    def __recv_json(self):
        text = self.socket.recv()
        obj = json.loads(text)
        if "query_status" not in obj.keys():
            raise WebSocketClient.__Error(text)
        
        return obj

    def __recv_and_check(self, expected):
        response = self.__recv_json()
        if response["query_status"] != expected:
            raise WebSocketClient.__JsonError(response)
        self.__state += 1
        
        return response

    def begin_response(self):
        self.__recv_and_check("scheduled")
        self.accepted = self.__recv_and_check("accepted")
        
        return self.accepted

    def next_frame_of_response(self):
        self.frame = self.socket.recv_frame()
        if self.finished_response():
            self.__state = WebSocketClient.__after_response
        
        return self.frame.data

    def next_frame_of_response_as_string(self):
        return self.next_frame_of_response().decode(encoding="utf-8", errors="strict")

    def finished_response(self):
        return self.frame.fin == 1

    def end_response(self):
        self.__recv_and_check("complete")
        self.__state = WebSocketClient.__idle

    def rest_of_response_as_string(self):
        str = ""
        while True:
            str += self.next_frame_of_response_as_string()
            if self.finished_response():
                break
        self.end_response()
        
        return str

    def request(self, request):
        self.send_request(request)
        self.begin_response()
        
        return json.loads(self.rest_of_response_as_string())

    def stream(self, request):
        self.send_request(request)
        self.begin_response()

        class Stream(io.RawIOBase):
            def __init__(self, that):
                self.__that = that
                self.__fin = False
                self.__inner = None

            def readinto(self, b):
                if self.__inner is not None:
                    next = self.__inner.read(len(b))
                    if len(next) != 0:
                        b[: len(next)] = next
                        return len(next)
                    self.__inner = None
                while not self.__fin:
                    next = self.__that.next_frame_of_response()
                    self.__fin = self.__that.finished_response()
                    if len(next) == 0:
                        continue
                    if len(next) <= len(b):
                        b[: len(next)] = next
                        return len(next)
                    self.__inner = io.BytesIO(next)
                    b[: len(b)] = self.__inner.read(len(b))
                    return len(b)
                self.__that.end_response()
                
                return 0

        
        return Stream(self)

    def disconnect(self):
        self.socket = None

    def cancel(self):
        if self.__state == WebSocketClient.__idle:
            return
        if self.__state == WebSocketClient.__after_response:
            #   Will throw if the response ended in
            #   mid-stream error
            self.end_response()
            return
        self.socket.send("cancel\n")

        def maybe_cancel(expected):
            response = self.__recv_json()
            if response["query_status"] == "canceled":
                self.__state = WebSocketClient.__idle
                return True
            if response["query_status"] != expected:
                self.__state = WebSocketClient.__idle
                raise WebSocketClient.__JsonError(response)
            self.__state += 1
            
            return False

        if self.__state == WebSocketClient.__request_sent:
            if maybe_cancel("scheduled"):
                return
        if self.__state == WebSocketClient.__scheduled:
            if maybe_cancel("accepted"):
                return
        while True:
            self.next_frame_of_response()
            if self.finished_response():
                #   If a cancel is received by the router before
                #   it sends the first chunk it doesn't send the
                #   body at all
                obj = None
                try:
                    obj = json.loads(
                        self.frame.data.decode(encoding="utf-8", errors="strict")
                    )
                except:
                    pass
                if (
                    obj is not None
                    and "query_status" in obj.keys()
                    and obj["query_status"] == "canceled"
                ):
                    self.__state = WebSocketClient.__idle
                    return
                break
        if not maybe_cancel("complete"):
            #   Accepting "error" works around the fact
            #   that when there's no outstanding request
            #   (which could be due to a race condition between
            #   client and server) the HPQ API interprets the
            #   next frame as a request and emits an error because
            #   it can't parse it
            maybe_cancel("error")


class Position:
    def __init__(self, cont):
        self.__cont = cont

    def request(self, other={}):
        ts = self.__cont["receipt_timestamp"]
        ns = ts % 1000000000
        dt = datetime.datetime.utcfromtimestamp(ts / 1000000000)
        retr = copy.copy(other)
        if "date" in retr.keys():
            retr["end_date"] = retr["date"]
            del retr["date"]
        #   TODO: What if the input request is not UTC?
        retr["time_zone"] = "UTC"
        retr["start_date"] = dt.strftime("%Y-%m-%d")
        retr["start_time"] = dt.strftime("%H:%M:%S.") + str(ns)
        
        return retr

    def predicate(self, item):
        if item["receipt_timestamp"] > self.__cont["receipt_timestamp"]:
            return True
        if item["sequence_number"] < self.__cont["sequence_number"]:
            return False
        if "message_number" in item.keys():
            if item["message_number"] < self.__cont["message_number"]:
                return False
        
        return True

    def filter(self, iterable):
        filtered = False
        for e in iterable:
            if filtered or self.predicate(e):
                yield e
                filtered = True


class Page:
    def __init__(self, conn, request, per_page, filter=lambda x: True, pos=None):
        self.__conn = conn
        self.__request = request
        self.__pos = pos
        self.__per_page = per_page
        self.__next_pos = None
        self.__filter = filter

    def __iter__(self):
        i = 0
        request = self.__request
        if self.__pos is not None:
            request = self.__pos.request(request)
        items = ijson.items(self.__conn.stream(request), "item")
        if self.__pos is not None:
            items = self.__pos.filter(items)
        for e in filter(self.__filter, items):
            if i > self.__per_page:
                #   TODO: Handle no_follow
                self.__next_pos = Position(e)
                break
            yield e
            i += 1

    def next_page(self, conn):
        if self.__next_pos is None:
            return None
        return Page(
            conn, self.__request, self.__per_page, self.__filter, self.__next_pos
        )


class Pages:
    def __init__(self, conn, request, per_page, filter=lambda x: True):
        self.__conn = conn
        self.__page = Page(conn, request, per_page, filter)

    def __iter__(self):
        while self.__page is not None:
            yield self.__page
            self.__conn.cancel()
            self.__page = self.__page.next_page(self.__conn)


def format_timestamp(ts):
    dt = datetime.datetime.utcfromtimestamp(ts / 1000000000)
    ns = ts % 1000000000
    ns_str = str(ns)
    while len(ns_str) < 9:
        ns_str = "0" + ns_str
    
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + ns_str + "Z"


def format(obj):
    def impl(key):
        if key in obj.keys():
            obj[key] = format_timestamp(obj[key])

    impl("receipt_timestamp")
    impl("exchange_timestamp")
    
    return obj


def create_web_socket_client():
    retr = WebSocketClient()
    secret = "6C753A250093DF2E997C143CC95DC246024C8B6B5F717F8D6B6EE2B4B7399E59"
    retr.connect_opts["header"] = [
        f"Authorization: MayStreet-Data-Lake-Secret {secret}"
    ]
    
    return retr
