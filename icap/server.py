import socket
import uuid

from types import ClassType, TypeType, GeneratorType
from collections import defaultdict

from .models import ICAPRequest, ICAPResponse, Session, ChunkedMessage
from .errors import abort, ICAPAbort, MalformedRequestError


class Hooks(dict):
    """Dispatch class for providing hooks at certain parts of the ICAP
    transaction.

    Used on a server instance like so:

    >>> from icap.server import Server
    >>> server = Server()
    >>> @server.hooks('options_headers')
    >>> def extra_headers():
    ...     return {'new': 'headers'}


    Available hooks:
        options_headers:
            Return dictionary of additional headers to add to the OPTIONS
            response.

            arguments: None.

        is_tag:
            Return a string to be used for a custom ISTag header on the
            response. String will be sliced to maximum of 32 bytes.

            arguments: request object, may be None.

    """
    def __getitem__(self, name):
        """Return the callable hook matching *name*.

        Always returns a callable that won't raise an exception.

        """

        if name in self:
            func, default = dict.__getitem__(self, name)
        else:
            func = lambda *args: None
            default = None

        def safe_callable(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except Exception:
                return default
        return safe_callable

    def __call__(self, name, default=None, override=False):
        """Register a hook function with *name*, and *default* return value.

        Unless *override* is True, then *default* will only be saved the for
        the first time. This is to ensure sane defaults are used in the event
        that an error occurs in the registered hook.

        """
        # we want to keep the original default, as it will be used if the new
        # one fails, e.g. for the ISTag header.
        if name in self and not override:
            _oldfunc, default = dict.__getitem__(self, name)

        def wrapped(func):
            self[name] = func, default
        return wrapped


class Server(object):
    """Server, for handling requests on a given address."""

    def __init__(self, reqmod='/reqmod', respmod='/respmod', services=None):
        self.services = services or []
        self.reqmod = reqmod
        self.respmod = respmod

        self.handlers = defaultdict(list)
        self.hooks = Hooks()

        fallback_is_tag = uuid.uuid4().hex

        @self.hooks('is_tag', default=fallback_is_tag)
        def is_tag(request):
            return fallback_is_tag

    def is_tag(self, request):
        return '"%s"' % self.hooks['is_tag'](request)[:32]

    def start(self):
        for key, value in self.handlers.iteritems():
            self.handlers[key] = sorted(value, key=lambda item: item[0])

    def handle_conn(self, connection, addr):
        """Handle a single connection. May handle many requests."""

        f = connection.makefile()

        def respond_with_error(error):
            # Clients are required to be aware of early returns, so sending an
            # error back without reading everything up should be fine. If not,
            # then, it's their fault for sending us invalid requests.
            response = ICAPResponse.from_error(error)
            response.serialize_to_stream(f, self.is_tag(None))

        while True:
            try:
                request = ICAPRequest.from_stream(f)
            except socket.error:
                # probably ECONNRESET. FIXME: logging
                request = None
            except MalformedRequestError as e:
                respond_with_error(400)
                return
            except ICAPAbort as e:
                respond_with_error(e)
                return

            # connection was closed or some such.
            if request is None:
                f.close()
                connection.close()
                return

            # Squid doesn't send Connection: close headers for OPTIONS requests.
            should_close = request.is_options or (request.headers.get('Connection') == 'close')

            valid_request = (request.is_request and
                             request.request_line.version.startswith('ICAP/'))
            if not valid_request:
                respond_with_error(400)
                return

            if not request.request_line.version.endswith('/1.0'):
                respond_with_error(505)
                return

            if request.is_options:
                self.handle_options(request)
            else:
                request.session = Session.from_request(request)
                try:
                    response = self.handle_request(request)
                except ICAPAbort as e:
                    if e.status_code == 204 and not request.allow_204:
                        response = ICAPResponse.from_request(request)
                    else:
                        response = ICAPResponse.from_error(e)
                else:
                    response = ICAPResponse(http=response)

                if not request.complete():
                    transfer_chunks = ((response.http is not request.http)
                                       and not response.http.chunks)
                    if transfer_chunks:
                        response.http.chunks.extend(list(request))
                    else:
                        for _ignored in request.http:
                            pass

                if isinstance(response.http, ChunkedMessage):
                    response.http.complete(True)

                http = response.http

                if len(http.chunks) == 1:
                    content_length = sum((len(c.content) for c in http.chunks))
                    http.headers.replace('Content-Length', str(content_length))
                elif 'content-length' in http.headers:
                    assert False
                    del http.headers['content-length']

                response.serialize_to_stream(f, self.is_tag(request))

                # FIXME: if this service doesn't handle respmods, then this
                # would be a memory leak.
                if request.is_respmod:
                    request.session.finished()

            if should_close:
                f.close()
                connection.close()
                return

    def handle_options(self, request):
        """Handle an OPTIONS request."""
        response = ICAPResponse(is_options=True)

        response.headers['Methods'] = 'RESPMOD' if request.sline.uri.endswith(self.respmod) else 'REQMOD'
        response.headers['Allow'] = '204'

        extra_headers = self.hooks['options_headers']()

        if extra_headers:
            response.headers.update(extra_headers)

        response.serialize_to_stream(request.stream, self.is_tag(request))

    def get_handler(self, request):
        import urlparse
        uri = urlparse.urlparse(request.request_line.uri)
        path = uri.path
        services = self.handlers.get(path)

        if not services:
            if request.is_reqmod:
                key = '/reqmod'
            else:
                key = '/respmod'
            services = self.handlers.get(key, [])

        handler = None
        for criteria, handler, raw in services:
            if criteria(request):
                return handler, raw
        return None, False

    def handle_request(self, request):
        """Handle a REQMOD or RESPMOD request."""
        handler, raw = self.get_handler(request)
        if handler is None:
            abort(204)

        try:
            if raw:
                response = handler(request)
            else:
                response = handler(request.http)

        except (SystemExit, KeyboardInterrupt) as e:
            raise  # pragma: no cover
        except BaseException as e:
            # FIXME: communicating this exception in some way would be nice.
            abort(500)

        if response is None:
            return request.http
        elif isinstance(response, (basestring, list, GeneratorType)):
            request.http.set_payload(response)
            return request.http
        elif request.is_respmod and response.is_request:
            abort(500)
        else:
            return response

    def handler(self, criteria, name='', raw=False):
        def inner(handler):
            if isinstance(handler, (ClassType, TypeType)):
                handler = handler()
                reqmod = getattr(handler, 'reqmod', None)
                respmod = getattr(handler, 'respmod', None)
            else:
                reqmod = handler if handler.__name__ == 'reqmod' else None
                respmod = handler if handler.__name__ == 'respmod' else None

            if reqmod:
                key = '/'.join([name, 'reqmod'])
                key = key if key.startswith('/') else '/%s' % key
                self.handlers[key].append((criteria, reqmod, raw))

            if respmod:
                key = '/'.join([name, 'respmod'])
                key = key if key.startswith('/') else '/%s' % key
                self.handlers[key].append((criteria, respmod, raw))
            return handler

        return inner
