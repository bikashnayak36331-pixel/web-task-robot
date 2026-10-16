"""Small local test pages for things the demo site doesn't show: JavaScript forms and paging, odd widgets,
uploads, confirm boxes, and text that is awkward for spreadsheets."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

SPA_FORM = """<!doctype html><html><body>
<form id="f"><input id="name"><input id="email"><button id="save" type="submit">Save</button></form>
<div id="msg"></div>
<script>
document.getElementById('f').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const r = await fetch('/api/save', {method: 'POST', body: JSON.stringify({name: name.value, email: email.value})});
  const d = await r.json();
  msg.innerHTML = d.ok ? '<div class="alert-success">Saved <b class="ref">' + d.ref + '</b></div>'
                       : '<div class="alert-error">' + d.error + '</div>';
});
</script></body></html>"""

SPA_LIST = """<!doctype html><html><body><ul id="list"></ul><button id="next">Next</button>
<script>
const all = Array.from({length: 25}, (_, i) => 'Item ' + (i + 1));
let page = 0;
function show() {
  document.getElementById('list').innerHTML =
    all.slice(page * 10, page * 10 + 10).map(t => '<li class="it">' + t + '</li>').join('');
  document.getElementById('next').disabled = (page + 1) * 10 >= all.length;
}
document.getElementById('next').onclick = () => { setTimeout(() => { page++; show(); }, 300); };
show();
</script></body></html>"""

WIDGETS = """<!doctype html><html><body><form action="/echo" method="get">
<input id="when" name="when" type="date">
<label><input type="radio" name="size" value="S" id="size-S">S</label>
<label><input type="radio" name="size" value="M" id="size-M">M</label>
<input type="checkbox" name="gift" id="gift" value="yes">
<select id="color" name="color"><option value="r">Red</option><option value="g">Green</option></select>
<input id="qty" name="qty" type="number">
<input id="doc" name="doc" type="file">
<button id="go">Go</button></form>
<button id="ask" onclick="document.getElementById('answer').textContent = confirm('Sure?') ? 'yes' : 'no'">Ask</button>
<div id="answer"></div></body></html>"""

WEIRD = (
    "<div class='it'><span class='t'>=HYPERLINK(\"http://evil.example\",\"click\")</span></div>"
    "<div class='it'><span class='t'>bell\x07and\x0bvt</span></div>"
)


class Lab:
    """`with Lab(delay=0.5) as lab:` serves the pages at lab.url; /api/save waits `delay` seconds."""

    def __init__(self, delay: float = 0.0):
        self.saved: list[dict] = []
        self.delay = delay
        lab = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, body: str, status: int = 200, ctype: str = "text/html; charset=utf-8"):
                data = body.encode()
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):  # noqa: N802
                url = urlparse(self.path)
                pages = {"/spa-form": SPA_FORM, "/spa-list": SPA_LIST, "/widgets": WIDGETS, "/weird": WEIRD}
                if url.path in pages:
                    return self._send(pages[url.path])
                if url.path == "/echo":
                    query = parse_qs(url.query, keep_blank_values=True)
                    return self._send("<pre id='echo'>" + json.dumps(query, sort_keys=True) + "</pre>")
                if url.path == "/empty":
                    return self._send("<h1>No results</h1>")
                self._send("not found", 404)

            def do_POST(self):  # noqa: N802
                data = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                time.sleep(lab.delay)
                email = data.get("email", "")
                if "@" not in email or any(s["email"] == email for s in lab.saved):
                    body = {"ok": False, "error": f"Email invalid or already used: {email}"}
                else:
                    lab.saved.append(data)
                    body = {"ok": True, "ref": f"R-{len(lab.saved)}"}
                self._send(json.dumps(body), ctype="application/json")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def __enter__(self) -> Lab:
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
