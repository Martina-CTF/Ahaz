from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Hello World!")

server = HTTPServer(("0.0.0.0", 8080), Handler)

print("Serving on :8080")
server.serve_forever()
