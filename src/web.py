import os
from threading import Thread
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Quick Zip Bot is running!")

    def log_message(self, format, *args):
        pass


def run():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), Handler)
    server.serve_forever()


def start():
    thread = Thread(target=run, daemon=True)
    thread.start()
