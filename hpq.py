"""Python utilities for interacting with the HPQ WebSocket API.

Note that while HPQ has a REST API this is deprecated and therefore this module has no support therefor."""

import copy
import datetime
import io
import ijson
import json
import os
import re
import ssl
import websocket


class WebSocketClient:
    """Submits requests to the HPQ API via WebSocket and retrieves responses therefrom.

    Both fully-streaming and full-buffered workflows are supported."""

    class Error(Exception):
        """The base class for exceptions raised interoperating with the HPQ API."""

        def __init__(self, text, obj):
            super().__init__(text)
            self.json = obj
            """The parsed JSON representing the last control message received."""

    class MidStreamError(Error):
        """Indicates that a response failed after being accepted during transmission of the body."""

        def __init__(self, text, obj, accepted):
            super().__init__(text, obj)
            self.accepted = accepted
            """The parsed JSON representing the control message which accepted the response."""

    class RejectError(Error):
        """Indicates that a request was rejected."""

        pass

    class ProtocolError(Error):
        """Indicates that the server did not transmit an expected control message at the expected time."""

        pass

    __idle = 0
    __request_sent = 1
    __scheduled = 2
    __waiting_for_response = 3
    __after_response = 4

    def __init__(self):
        self.url = "wss://mdx.uat.maystreet.com"
        """The url to connect to. Defaults to the HPQ UAT environment."""
        self.init_opts = {}
        """A dictionary of options which will be expanded and forwarded when calling
        websocket.WebSocket.__init__."""
        self.socket = None
        """The managed websocket.WebSocket object (once it has been initialized)."""
        self.frame = None
        """The last WebSocket frame received (once one has been received)."""
        self.connect_opts = {}
        """A dictionary of options which will be expanded and forwarded when calling websocket.WebSocket.connect."""
        self.accepted = None
        """The parsed JSON of the control message which accepted the last request once a request has been accepted."""
        self.__state = WebSocketClient.__idle
        self.__last_text = None
        self.__last_json = None

    def connect(self):
        """Creates a websocket.WebSocket, connects, and returns it.

        If a websocket.WebSocket is already managed this function does nothing except return it.

        It is not usually necessary to call this function directly (it is called internally as needed)."""
        if not self.socket:
            socket = websocket.WebSocket(**self.init_opts)
            socket.connect(self.url, **self.connect_opts)
            self.socket = socket

        return self.socket

    def send_request_raw(self, request):
        """Transmits a raw request.

        It is not usually necessary to call this function directly."""
        self.connect().send(request)
        self.__state = WebSocketClient.__request_sent

    def send_request(self, request):
        """Transmits a JSON request by stringifying the request parameter and sending it."""
        self.send_request_raw(json.dumps(request))

    def __recv_json(self):
        self.__last_text = self.socket.recv()
        self.__last_json = json.loads(self.__last_text)
        if "query_status" not in self.__last_json.keys():
            raise WebSocketClient.ProtocolError(self.__last_text, self.__last_json)

        return self.__last_json

    def __recv_and_check(self, *args):
        response = self.__recv_json()
        if response["query_status"] in args:
            self.__state += 1
            return response
        if response["query_status"] == "error":
            previous = self.__state
            self.__state = WebSocketClient.__idle
            if previous == WebSocketClient.__after_response:
                raise WebSocketClient.MidStreamError(
                    self.__last_text, self.__last_json, self.accepted
                )
            raise WebSocketClient.RejectError(self.__last_text, self.__last_json)

        raise WebSocketClient.ProtocolError(self.__last_text, self.__last_json)

    def begin_response(self):
        """Waits for the server to begin the response.

        Should be called after initiating a response using send_request or send_request_raw.

        Returns the parsed message which accepted the request. Raises an exception on reject."""
        self.__recv_and_check("scheduled")
        self.accepted = self.__recv_and_check("accepted")

        return self.accepted

    def next_frame_of_response(self):
        """Streams the next JSON frame of the response.

        Returns the data associated with the received WebSocket frame."""
        self.frame = self.socket.recv_frame()
        if self.finished_response():
            self.__state = WebSocketClient.__after_response

        return self.frame.data

    def next_frame_of_response_as_string(self):
        """Functions identically to next_frame_of_response except the received data is decoded as UTF-8 into a string."""
        return self.next_frame_of_response().decode(encoding="utf-8", errors="strict")

    def finished_response(self):
        """Checks to see if the last frame received completed the response.

        Returns True if the response is finished, False otherwise."""
        return self.frame.fin == 1

    def end_response(self):
        """Attempts to end the response and prepare the connection for the next request.

        Must be called after finished_response returns True.

        Throws an exception if the response didn't complete successfully (i.e. ended in mid-stream error)."""
        self.__recv_and_check("complete")
        self.__state = WebSocketClient.__idle

    def rest_of_response_as_string(self):
        """Returns the unread portion of the response decoded into a string.

        Note that end_response will be called internally for you (and all that implies,
        i.e. mid-stream errors will result in this function throwing)."""
        str = ""
        while True:
            str += self.next_frame_of_response_as_string()
            if self.finished_response():
                break
        self.end_response()

        return str

    def request(self, request):
        """Performs an entire request cycle in a single call with the assumption that the response is JSON:

        1. Sends the request via send_request
        2. Calls begin_response
        3. JSON decodes the entire response
        4. Calls end_response

        Note that this function should not be used for large responses (as their bodies will be buffered)."""
        self.send_request(request)
        self.begin_response()

        return json.loads(self.rest_of_response_as_string())

    def stream(self, request):
        """Begins a streaming request.

        Blocks until calls to send_request and begin_response return and then returns an object which
        derives from io.RawIOBase from which the body of the response may be read.

        The returned stream will call end_response internally (and raise any exceptions therefrom)."""
        self.send_request(request)
        self.begin_response()

        class Stream(io.RawIOBase):
            def __init__(self, that):
                self.__that = that
                self.__fin = False
                self.__inner = None
                self.__ended = False

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
                if not self.__ended:
                    self.__that.end_response()
                    self.__ended = True

                return 0

            def readable(self):
                return True

        return Stream(self)

    def disconnect(self):
        """Abandons the managed socket causing subsequent calls to connect to actually form a new connection."""
        self.socket = None

    def cancel(self):
        """Requests that the server stop sending the request which is currently being processed.

        Returns once the connection is ready for reuse.

        Any outstanding streams obtained by calling stream should be abandoned."""
        if self.__state == WebSocketClient.__idle:
            return
        if self.__state == WebSocketClient.__after_response:
            #   Will throw if the response ended in
            #   mid-stream error
            self.end_response()
            return
        self.socket.send("cancel\n")

        def maybe_cancel(expected):
            response = self.__recv_and_check("canceled", expected)
            if response["query_status"] == "canceled":
                self.__state = WebSocketClient.__idle
                return True

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
    """Represents a position in an HPQ response.

    Intended to be used when resuming a response immediately after a certain point."""

    def __init__(self, cont):
        """Creates an object which represents the position immediately after the provided object (which is assumed to have come from an HPQ response).

        To continue from immediately after the represented position synthesize a request object using the request function and then ignore all leading entries until predicate returns True."""
        self.__cont = cont

    def request(self, other={}):
        """Returns a dictionary which represents a request which continues from immediately after the position represented by this object.

        Populates start_date and start_time keys and may transform the date key into an end_date key.

        The optional parameter is the request object into which the newly-created keys shall be merged. Note this object will not be updated but will be copied."""
        ts = self.__cont["receipt_timestamp"]
        ns = str(ts % 1000000000)
        while len(ns) != 9:
            ns = "0" + ns
        dt = datetime.datetime.utcfromtimestamp(ts / 1000000000)
        retr = copy.copy(other)
        if "date" in retr.keys():
            retr["end_date"] = retr["date"]
            del retr["date"]
        #   TODO: What if the input request is not UTC?
        retr["time_zone"] = "UTC"
        retr["start_date"] = dt.strftime("%Y-%m-%d")
        retr["start_time"] = dt.strftime("%H:%M:%S.") + ns

        return retr

    def predicate(self, item):
        """Determines whether or not a certain entry should be included in the continued response.

        This function is necessary because the HPQ API accepts requests for a certain time range but multiple entries may occur at the same time (in which case they are ordered based on sequence number et cetera)."""
        if item["receipt_timestamp"] > self.__cont["receipt_timestamp"]:
            return True
        if item["sequence_number"] < self.__cont["sequence_number"]:
            return False
        if item["sequence_number"] > self.__cont["sequence_number"]:
            return True
        if "message_number" in item.keys():
            if item["message_number"] <= self.__cont["message_number"]:
                return False

        return True

    def filter(self, iterable):
        """Obtains a callable which has the same effect as the predicate member function."""
        filtered = False
        for e in iterable:
            if filtered or self.predicate(e):
                yield e
                filtered = True


class Page:
    """Represents a single page in a paginated result set."""

    def __init__(self, conn, request, per_page, filter=lambda x: True, pos=None):
        """Creates a page.

        Parameters:
        - Connection to use to communicate with the HPQ API
        - Request object to use as a template
        - Number of results in each page
        - Callable which will be used to filter the result set (defaults to a callable which always returns True)
        - Position object representing the position to resume from (defaults to None which means to start at the beginning)"""
        self.__conn = conn
        self.__request = request
        self.__pos = pos
        self.__per_page = per_page
        self.__next_pos = None
        self.__filter = filter

    def __iter__(self):
        """Returns an iterator which traverses each entry on the page."""
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
        """Obtains a page object representing the next page in the paginated result set.

        This function is only meaningful to call after traversing this object.

        Returns None if there are no further pages."""
        if self.__next_pos is None:
            return None
        return Page(
            conn, self.__request, self.__per_page, self.__filter, self.__next_pos
        )


class Pages:
    """Represents an entire paginated result set."""

    def __init__(self, conn, request, per_page, filter=lambda x: True):
        """Creates a set of pages.

        Parameters:
        - Connection to use to communicate with the HPQ API
        - Request object to use as a template
        - Number of results in each page
        - Callable which will be used to filter the result set (defaults to a callable which always returns True)"""
        self.__conn = conn
        self.__page = Page(conn, request, per_page, filter)

    def __iter__(self):
        """Traverses all pages."""
        while self.__page is not None:
            yield self.__page
            self.__conn.cancel()
            self.__page = self.__page.next_page(self.__conn)


def format_timestamp(ts):
    """Formats a timestamp from the HPQ API (nanoseconds since epoch) in ISO8601 format with nanoseconds."""
    dt = datetime.datetime.utcfromtimestamp(ts / 1000000000)
    ns = ts % 1000000000
    ns_str = str(ns)
    while len(ns_str) < 9:
        ns_str = "0" + ns_str

    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + ns_str + "Z"


def format(obj):
    """Formats all known keys of the input dictionary and returns that dictionary.

    Note the dictionary will be updated. If this is not desired copy the dictionary first."""

    def impl(key):
        if key in obj.keys():
            obj[key] = format_timestamp(obj[key])

    impl("receipt_timestamp")
    impl("exchange_timestamp")

    return obj


def skip(n, iter):
    """Generator which returns a traversable which skips the first n elements of the provided traversable."""
    skipped = 0
    for i in iter:
        if skipped == n:
            yield i
        else:
            skipped += 1


def take(n, iter):
    """Generator which returns a traversable which takes the first n elements of the provided traversable, or all elements of the provided traversable, whichever is fewer."""
    i = 0
    for obj in iter:
        if i == n:
            break
        yield obj
        i += 1


def is_production():
    """Determines if the environment is production.

    Do not use."""
    if "API_SERVER_BASE_URL" not in os.environ.keys():
        raise Exception("Environment variable API_SERVER_BASE_URL not set")
    if re.search("production", os.environ["API_SERVER_BASE_URL"]):
        return True
    return False


def url():
    """Determines the HPQ URL for this environment.

    Do not use."""
    if is_production():
        return "wss://mdx.uat.maystreet.com"
    return "wss://mdx.stg.maystreet.com"


def jwt_authorization_header():
    """Obtains the JWT authorization header for this environment.

    It will not usually be necessary to use this function directly."""
    if "JWT_FILE" not in os.environ.keys():
        raise Exception("Environment variable JWT_FILE not set")
    with open(os.environ["JWT_FILE"], "r") as file:
        jwt = file.read()
    jwt = jwt.strip()
    return f"Authorization: Bearer {jwt}"


def secret_authorization_header():
    """Obtains the secret authorization header for this environment.

    It will not usually be necessary to use this function directly."""
    return "Authorization: MayStreet-Data-Lake-Secret 6C753A250093DF2E997C143CC95DC246024C8B6B5F717F8D6B6EE2B4B7399E59"


def set_authorization_header(conn, header):
    """Sets a provided authorization header on a provided WebSocketClient."""
    if "header" not in conn.connect_opts.keys():
        conn.connect_opts["header"] = [header]
    else:
        conn.connect_opts["header"].append(header)
    return conn


def set_untrusted(conn):
    """Disables TLS peer verification for the provided WebSocketClient."""
    conn.init_opts["sslopt"] = {"cert_reqs": ssl.CERT_NONE}
    return conn


def create_web_socket_client():
    """Creates and returns a WebSocketClient for the current environment."""
    retr = WebSocketClient()
    retr.url = url()
    if is_production():
        set_authorization_header(retr, secret_authorization_header())
    else:
        set_authorization_header(retr, jwt_authorization_header())
        set_untrusted(retr)
    return retr
