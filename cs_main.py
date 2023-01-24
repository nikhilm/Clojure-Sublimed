import collections, html, os, re, socket, sublime, sublime_plugin, threading
from typing import Dict
from . import cs_bencode, cs_common, cs_eval, cs_parser, cs_progress

class Connection:
    host: str
    port: str
    status: str
    evals: Dict[int, cs_eval.Eval]
    evals_by_view: Dict[int, Dict[int, cs_eval.Eval]]
    last_view: sublime.View
    session: str
    profile: cs_common.Profile
    cljs_build: str

    def __init__(self):
        self.host = 'localhost'
        self.port = None
        self.evals = {}
        self.evals_by_view = collections.defaultdict(dict)
        self.reset()
        self.last_view = window.active_view() if (window := sublime.active_window()) else None
        self.session = None
        self.profile = None
        self.cljs_build = None

    def set_status(self, status):
        self.status = status
        self.refresh_status()

    def refresh_status(self):
        if window := sublime.active_window():
            if view := window.active_view():
                if self.status:
                    view.set_status(cs_common.ns, self.status)
                else:
                    view.erase_status(cs_common.ns)
                for eval in self.evals.values():
                    if isinstance(eval, cs_eval.StatusEval):
                        if self.last_view and view != self.last_view:
                            self.last_view.erase_status(eval.value_key())
                        eval.update(eval.status, eval.value)
            self.last_view = view

    def send(self, msg):
        cs_common.debug("SND {}", msg)
        self.socket.sendall(cs_bencode.encode(msg).encode())

    def reset(self):
        self.socket = None
        self.reader = None
        self.session = None
        self.set_status(None)
        for id, eval in self.evals.items():
            eval.erase()
        self.evals.clear()
        self.evals_by_view.clear()

    def add_eval(self, eval):
        self.evals[eval.id] = eval
        if view := eval.view:
            self.evals_by_view[view.id()][eval.id] = eval

    def erase_eval(self, eval):
        eval.erase()
        del self.evals[eval.id]
        if view := eval.view:
            del self.evals_by_view[view.id()][eval.id]
        if eval.status == "pending" and eval.session:
            cs_common.conn.send({"op": "interrupt", "interrupt-id": eval.id, "session": eval.session})

    def find_eval(self, view, region):
        for eval in self.evals_by_view[view.id()].values():
            if cs_common.regions_touch(eval.region(), region):
                return eval

    def erase_evals(self, predicate, view = None):
        evals = list(self.evals.items()) if view is None else list(self.evals_by_view[view.id()].items())
        for id, eval in evals:
            if predicate(eval):
                self.erase_eval(eval)

    def disconnect(self):
        if self.socket:
            self.socket.close()
            self.reset()

    def ready(self):
        return bool(self.socket and self.session)

def handle_new_session(msg):
    if "new-session" in msg and "id" in msg and msg["id"] in cs_common.conn.evals:
        eval = cs_common.conn.evals[msg["id"]]
        eval.session = msg["new-session"]
        return True

def handle_value(msg):
    if "value" in msg and "id" in msg and msg["id"] in cs_common.conn.evals:
        eval = cs_common.conn.evals[msg["id"]]
        eval.update("success", msg.get("value"), time_taken = msg.get(f'{cs_common.ns}.middleware/time-taken'))
        return True

def set_selection(view, region):
    sel = view.sel()
    sel.clear()
    sel.add(region)
    view.show(region.a, show_surrounds = True, keep_to_left = True, animate = True)

def handle_exception(msg):
    if "id" in msg and msg["id"] in cs_common.conn.evals:
        eval = cs_common.conn.evals[msg["id"]]
        present = lambda key: (cs_common.ns + ".middleware/" + key) in msg
        get = lambda key: msg.get(cs_common.ns + ".middleware/" + key)
        if get("root-ex-class") and get("root-ex-msg"):
            text = get("root-ex-class") + ": " + get("root-ex-msg")
            region = None
            if get("root-ex-data"):
                text += " " + get("root-ex-data")
            if present("line") and present("column") and eval.view:
                line = get("line") - 1
                column = get("column")
                point = eval.view.text_point_utf16(line, column, clamp_column = True)
                region = sublime.Region(eval.view.line(point).begin(), eval.view.line(point).end())
                set_selection(eval.view, sublime.Region(point, point))
            elif present("line") and present("column") and get("source"):
                text += " ({}:{}:{})".format(get("source"), get("line"), get("column"))
            eval.trace = get("trace")
            eval.update("exception", text, region)
            return True
        elif "root-ex" in msg:
            eval.update("exception", msg["root-ex"])
            return True
        elif "ex" in msg:
            eval.update("exception", msg["ex"])
            return True        
        elif "status" in msg and "namespace-not-found" in msg["status"]:
            eval.update("exception", f'Namespace not found: {msg["ns"]}')

class ClojureSublimedToggleTraceCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        view = self.view
        point = view.sel()[0].begin()
        for eval in cs_common.conn.evals_by_view[view.id()].values():
            if eval.view == view:
                region = eval.region()
                if region and region.contains(point):
                    eval.toggle_trace()
                    break
        
    def is_enabled(self):
        return cs_common.conn.ready() and len(self.view.sel()) == 1

def format_lookup(view, info):
    settings = view.settings()
    top = settings.get('line_padding_top', 0)
    bottom = settings.get('line_padding_bottom', 0)
    body = f"""<body id='clojure-sublimed'>
        {cs_common.basic_styles(view)}
        .dark body  {{ background-color: color(var(--background) blend(#FFF 90%)); }}
        .light body {{ background-color: color(var(--background) blend(#000 95%)); }}
        a           {{ text-decoration: none; }}
        .arglists   {{ color: color(var(--foreground) alpha(0.5)); }}
    </style>"""

    if not info:
        body += "<p>Not found</p>"
    else:
        ns = info.get('ns')
        name = info['name']
        file = info.get('file')
        arglists = info.get('arglists')
        forms = info.get('forms')
        doc = info.get('doc')

        body += "<p>"
        if file:
            body += f"<a href='{file}'>"
        if ns:
            body += html.escape(ns) + "/"
        body += html.escape(name)
        if file:
            body += f"</a>"
        body += "</p>"

        if arglists:
            body += f'<p class="arglists">{html.escape(arglists.strip("()"))}</p>'

        if forms:
            def format_form(form):
                if isinstance(form, str):
                    return form
                else:
                    return "(" + " ".join([format_form(x) for x in form]) + ")"
            body += '<p class="arglists">'
            body += html.escape(" ".join([format_form(form) for form in forms]))
            body += "</p>"

        if doc:
            body += "<p>" + "</p><p>".join(html.escape(doc).split("\n")) + "</p>"
    body += "</div>"
    return body

def handle_lookup(msg):
    if "info" in msg and "id" in msg and msg["id"] in cs_common.conn.evals:
        eval = cs_common.conn.evals[msg["id"]]
        eval.update("lookup", None)
        view = eval.view
        body = format_lookup(view, msg["info"])
        point = view.line(eval.region().end()).begin()
        eval.phantom_id = view.add_phantom(eval.value_key(), sublime.Region(point, point), body, sublime.LAYOUT_BLOCK)
        return True

class ClojureSublimedToggleSymbolCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        view = self.view
        for sel in view.sel():
            eval = cs_common.conn.find_eval(view, sel)
            if eval and eval.phantom_id:
                cs_common.conn.erase_eval(eval)
            else:
                if region := cs_parser.symbol_at_point(view, sel.begin()) if sel.empty() else sel:
                    line = view.line(region)
                    cs_common.conn.erase_evals(lambda eval: eval.region() and eval.region().intersects(line), view)
                    eval = cs_eval.Eval(view, region)
                    cs_progress.wake()
                    cs_common.conn.add_eval(eval)
                    cs_common.conn.send({"op":      "lookup",
                               "sym":     view.substr(region),
                               "session": cs_common.conn.session,
                               "id":      eval.id,
                               "ns":      cs_parser.namespace(view, region.begin()) or 'user'})

    def is_enabled(self):
        return cs_common.conn.ready()

class ClojureSublimedToggleInfoCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        view = self.view
        for sel in view.sel():
            eval = cs_common.conn.find_eval(view, sel)
            if eval and eval.status == "exception":
                view.run_command("clojure_sublimed_toggle_trace", {})
            elif eval and eval.status == "success":
                if eval := cs_common.conn.find_eval(view, sel):
                    eval.toggle_pprint()
                    break
            else:
                view.run_command("clojure_sublimed_toggle_symbol", {})

    def is_enabled(self):
        return cs_common.conn.ready()

def get_shadow_repl_init_cmd(build):
    """Returns the command to initialise shadow-repl."""
    if build == "node-repl":
        return "(shadow.cljs.devtools.api/node-repl)"
    elif build == "browser-repl":
        return "(shadow.cljs.devtools.api/browser-repl)"
    else:
        return f"(shadow.cljs.devtools.api/repl {build})"

def _status_connected(conn):
    return "🌕 " + (conn.host + ":" if conn.host else "") + str(conn.port)

def handle_connect(msg):
    if cs_common.conn.profile == cs_common.Profile.SHADOW_CLJS:
        if 1 == msg.get("id") and "new-session" in msg:
            # Once we have the connnection to shadow's nrepl, we will 
            # tell shadow to watch the cljs build provided by the user.
            cs_common.conn.session = msg["new-session"]
            cs_common.conn.send({"op": "eval",
                       "session": cs_common.conn.session,
                       "code": get_shadow_repl_init_cmd(cs_common.conn.cljs_build),
                       "id": 2})
            return True

        elif 2 == msg.get("id") and msg.get("status") == ["done"]:
            cs_common.conn.set_status(_status_connected(cs_common.conn))
            return True

    if 1 == msg.get("id") and "new-session" in msg:
        global package
        cs_common.conn.session = msg["new-session"]
        cs_common.conn.send({"op": "load-file",
                             "session": cs_common.conn.session,
                             "file": sublime.load_resource(f"Packages/{package}/cs_middleware.clj"),
                             "id": 2})
        cs_common.conn.set_status("🌓 Uploading middlewares")
        return True

    elif 2 == msg.get("id") and msg.get("status") == ["done"]:
        id = 3 if cs_common.setting("eval_shared") else 4
        cs_common.conn.send({"op":               "add-middleware",
                   "middleware":       [f"{cs_common.ns}.middleware/clone-and-eval",
                                        f"{cs_common.ns}.middleware/time-eval",
                                        f"{cs_common.ns}.middleware/wrap-errors",
                                        f"{cs_common.ns}.middleware/wrap-output"],
                   "extra-namespaces": [f"{cs_common.ns}.middleware"],
                   "session":          cs_common.conn.session,
                   "id":               id})
        cs_common.conn.set_status("🌔 Adding middlewares")
        return True

    elif 3 == msg.get("id") and msg.get("status") == ["done"]:
        cs_common.conn.send({"op":      "eval",
                   "code":    cs_common.setting("eval_shared"), 
                   "session": cs_common.conn.session,
                   "id":      4})

    elif 4 == msg.get("id") and msg.get("status") == ["done"]:
        cs_common.conn.set_status(_status_connected(cs_common.conn))
        return True

def handle_done(msg):
    if "id" in msg and msg["id"] in cs_common.conn.evals and "status" in msg and "done" in msg["status"]:
        eval = cs_common.conn.evals[msg["id"]]
        if eval.status not in {"success", "exception"}:
            cs_common.conn.erase_eval(eval)

def handle_msg(msg):
    cs_common.debug("RCV {}", msg)

    for key in msg.get('nrepl.middleware.print/truncated-keys', []):
        msg[key] += '...'

    handle_connect(msg) \
    or handle_new_session(msg) \
    or handle_value(msg) \
    or handle_exception(msg) \
    or handle_lookup(msg) \
    or handle_done(msg)

def read_loop():
    try:
        cs_common.conn.pending_id = 1
        cs_common.conn.send({"op": "clone", "id": cs_common.conn.pending_id})
        cs_common.conn.set_status(f"🌒 Cloning session")
        for msg in cs_bencode.decode_file(cs_common.SocketIO(cs_common.conn.socket)):
            handle_msg(msg)
    except OSError:
        pass
    cs_common.conn.disconnect()

def connect(host, port, profile=cs_common.Profile.CLOJURE, cljs_build=None):
    cs_common.conn.host = host
    cs_common.conn.port = port
    cs_common.conn.profile = profile
    cs_common.conn.cljs_build = cljs_build
    try:
        if _is_unix_domain_sock(cs_common.conn):
            cs_common.conn.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            cs_common.conn.socket.connect(port)
        else:
            cs_common.conn.socket = socket.create_connection((host, port))
        cs_common.conn.reader = threading.Thread(daemon=True, target=read_loop)
        cs_common.conn.reader.start()
    except Exception as e:
        cs_common.error("Failed to connect to {}:{}", host, port)
        cs_common.conn.socket = None
        cs_common.conn.set_status(None)
        if window := sublime.active_window():
            window.status_message(f"Failed to connect to {host}:{port}")

def _is_unix_domain_sock(conn):
    return conn.host is None

class ClojureSublimedHostPortInputHandler(sublime_plugin.TextInputHandler):
    def placeholder(self):
        return "host:port or /path/to/nrepl.sock"

    def initial_text(self):
        host = ''
        port = ''
        if cs_common.conn.host:
            host = cs_common.conn.host
        if cs_common.conn.port:
            port = str(cs_common.conn.port)
        if window := sublime.active_window():
            for folder in window.folders():
                if os.path.exists(folder + "/.nrepl-port"):
                    with open(folder + "/.nrepl-port", "rt") as f:
                        content = f.read(10).strip()
                        if re.fullmatch(r'[1-9][0-9]*', content):
                            host = 'localhost'
                            port = content
                            break
        if host:
            return host + ":" + port

        return port

    def initial_selection(self):
        if cs_common.conn.host:
            return [(len(cs_common.conn.host + ":"), len(self.initial_text()))]

        return [(cs_common.conn.port.rfind('/') + 1, len(self.initial_text()))]

    def preview(self, text):
        if not self.validate(text):
            return "Expected <host>:<port> or <path>"

    def validate(self, text):
        text = text.strip()
        if "auto" == text:
            return True
        if re.fullmatch(r'[a-zA-Z0-9\.]+:\d{1,5}', text):
            host, port = text.split(':')
            port = int(port)

            return port in range(1, 65536)
        else:
            return bool(os.stat(text))

class ClojureSublimedShadowCljsBuildInputHandler(sublime_plugin.TextInputHandler):
    def initial_text(self):
        return ':app'

    def preview(self, text):
        return sublime.Html("""
        <html>
            <body>
            Provide the cljs build for shadow to watch. 
            <br>
            Valid options are <b>node-repl</b>, <b>browser-repl</b> or the build defined in shadow-cljs.edn / project.clj
            For more info check <a href="https://shadow-cljs.github.io/docs/UsersGuide.html#_repl_2"> Shadow Documentation </a>
            </body>
        </html>
        """)

    def next_input(self, args):
        return ClojureSublimedHostPortInputHandler()

class ClojureSublimedConnectShadowCljsCommand(sublime_plugin.ApplicationCommand):

    def run(self, clojure_sublimed_shadow_cljs_build, clojure_sublimed_host_port=''):
        host, port = clojure_sublimed_host_port.strip().split(':')
        port = int(port)
        connect(host, port, cs_common.Profile.SHADOW_CLJS, clojure_sublimed_shadow_cljs_build)

    def input(self, args):
        if 'clojure_sublimed_shadow_cljs_build' not in args:
            return ClojureSublimedShadowCljsBuildInputHandler()

    def is_enabled(self):
        return cs_common.conn.socket == None

class ClojureSublimedConnectCommand(sublime_plugin.ApplicationCommand):
    def run(self, clojure_sublimed_host_port):
        try:
            if clojure_sublimed_host_port == "auto":
                clojure_sublimed_host_port = ClojureSublimedHostPortInputHandler().initial_text()
            host, port = clojure_sublimed_host_port.strip().split(':', 1)
            port = int(port)
        except ValueError:
            host, port = None, clojure_sublimed_host_port
        connect(host, port)

    def input(self, args):
        return ClojureSublimedHostPortInputHandler()

    def is_enabled(self):
        return cs_common.conn.socket == None

class ClojureSublimedDisconnectCommand(sublime_plugin.ApplicationCommand):
    def run(self):
        cs_common.conn.disconnect()

    def is_enabled(self):
        return cs_common.conn.socket != None

class ClojureSublimedReconnectCommand(sublime_plugin.ApplicationCommand):
    def run(self):
        cs_common.conn.disconnect()
        connect(cs_common.conn.host, cs_common.conn.port, cs_common.conn.profile, cs_common.conn.cljs_build)

    def is_enabled(self):
        return cs_common.conn.socket != None

class EventListener(sublime_plugin.EventListener):
    def on_activated_async(self, view):
        cs_common.conn.refresh_status()
        cs_progress.wake()

    def on_close(self, view):
        cs_common.conn.erase_evals(lambda eval: True, view)

class TextChangeListener(sublime_plugin.TextChangeListener):
    def on_text_changed_async(self, changes):
        view = self.buffer.primary_view()
        changed = [sublime.Region(x.a.pt, x.b.pt) for x in changes]
        def should_erase(eval):
            return not (reg := eval.region()) or any(reg.intersects(r) for r in changed) and view.substr(reg) != eval.code
        cs_common.conn.erase_evals(should_erase, view)

def plugin_loaded():
    global package
    package_path = os.path.dirname(os.path.abspath(__file__))
    if os.path.isfile(package_path):
        # Package is a .sublime-package so get its filename
        package, _ = os.path.splitext(os.path.basename(package_path))
    elif os.path.isdir(package_path):
        # Package is a directory, so get its basename
        package = os.path.basename(package_path)
    cs_common.conn = Connection()

def plugin_unloaded():
    cs_common.conn.disconnect()