import html, re, sublime, sublime_plugin
from typing import Any, Dict, Tuple
from . import cs_common, cs_parser, cs_progress

class Eval:
    # class
    next_id: int = 10
    colors:  Dict[str, Tuple[str, str]] = {}

    # instance
    id:         int
    view:       sublime.View
    status:     str # "pending" | "interrupt" | "success" | "exception" | "lookup"
    code:       str
    session:    str
    msg:        Dict[str, Any]
    trace:      str
    phantom_id: int
    
    def __init__(self, view, region):
        self.id = Eval.next_id
        self.view = view
        self.code = view.substr(region)
        self.session = None
        self.msg = None
        self.trace = None
        self.phantom_id = None
        self.value = None
        Eval.next_id += 1
        self.update("pending", None, region)

    def value_key(self):
        return f"{cs_common.ns}.eval-{self.id}"

    def scope_color(self):
        if not Eval.colors:
            default = self.view.style_for_scope("source")
            def try_scopes(*scopes):
                for scope in scopes:
                    colors = self.view.style_for_scope(scope)
                    if colors != default:
                        return (scope, colors["foreground"])
            Eval.colors["pending"]   = try_scopes("region.eval.pending",   "region.bluish")
            Eval.colors["interrupt"] = try_scopes("region.eval.interrupt", "region.eval.pending", "region.bluish")
            Eval.colors["success"]   = try_scopes("region.eval.success",   "region.greenish")
            Eval.colors["exception"] = try_scopes("region.eval.exception", "region.redish")
            Eval.colors["lookup"]    = try_scopes("region.eval.lookup",    "region.eval.pending",   "region.bluish")
        return Eval.colors[self.status]

    def region(self):
        regions = self.view.get_regions(self.value_key())
        if regions and len(regions) >= 1:
            return regions[0]

    def escape(self, value):
        return html.escape(value).replace("\t", "&nbsp;&nbsp;").replace(" ", "&nbsp;")

    def update(self, status, value, region = None, time_taken = None):
        self.status = status
        self.value = value
        region = region or self.region()
        if region:
            scope, color = self.scope_color()
            if value:
                if (self.status in {"success", "exception"}) and (time := cs_common.format_time_taken(time_taken)):
                    value = time + " " + value
                self.view.add_regions(self.value_key(), [region], scope, '', sublime.DRAW_NO_FILL + sublime.NO_UNDO, [self.escape(value)], color)
            else:
                self.view.erase_regions(self.value_key())
                self.view.add_regions(self.value_key(), [region], scope, '', sublime.DRAW_NO_FILL + sublime.NO_UNDO)

    def toggle_phantom(self, text, styles):
        if text:
            if self.phantom_id:
                self.view.erase_phantom_by_id(self.phantom_id)
                self.phantom_id = None
            else:
                body = f"""<body id='clojure-sublimed'>
                    { cs_common.basic_styles(self.view) }
                    { styles }
                </style>"""
                for line in self.escape(text).splitlines():
                    body += "<p>" + re.sub(r"(?<!\\)\\n", "<br>", line) + "</p>"
                body += "</body>"
                region = self.region()
                if region:
                    point = self.view.line(region.end()).begin()
                    self.phantom_id = self.view.add_phantom(self.value_key(), sublime.Region(point, point), body, sublime.LAYOUT_BLOCK)

    def toggle_pprint(self):
        self.toggle_phantom(self.value, """
            .light body { background-color: hsl(100, 100%, 90%); }
            .dark body  { background-color: hsl(100, 100%, 10%); }
        """)
        
    def toggle_trace(self):
        self.toggle_phantom(self.trace, """
            .light body { background-color: hsl(0, 100%, 90%); }
            .dark body  { background-color: hsl(0, 100%, 10%); }
        """)

    def erase(self):
        self.view.erase_regions(self.value_key())
        if self.phantom_id:
            self.view.erase_phantom_by_id(self.phantom_id)

class StatusEval(Eval):
    def __init__(self, code):
        self.id = Eval.next_id
        self.view = None
        self.code = code
        self.session = None
        self.msg = None
        self.trace = None
        Eval.next_id += 1
        self.update("pending", None)

    def region(self):
        return None

    def active_view(self):
        if window := sublime.active_window():
            return window.active_view()

    def update(self, status, value, region = None, time_taken = None):
        self.status = status
        self.value = value
        if self.active_view():
            if status in {"pending", "interrupt"}:
                self.active_view().set_status(self.value_key(), "⏳ " + self.code)
            elif "success" == status:
                if time := cs_common.format_time_taken(time_taken):
                    value = time + ' ' + value
                self.active_view().set_status(self.value_key(), "✅ " + value)
            elif "exception" == status:
                if time := cs_common.format_time_taken(time_taken):
                    value = time + ' ' + value
                self.active_view().set_status(self.value_key(), "❌ " + value)

    def erase(self):
        if self.active_view():
            self.active_view().erase_status(self.value_key())

def get_middleware_opts(conn):
    """Returns middleware options to send to nREPL as a dict.
    Currently only Clojure profile supports middleware.
    """
    if conn and conn.profile == cs_common.Profile.CLOJURE:
        return {
            "nrepl.middleware.caught/caught": f"{cs_common.ns}.middleware/print-root-trace",
            "nrepl.middleware.print/print": f"{cs_common.ns}.middleware/pprint",
            "nrepl.middleware.print/quota": 4096
        }
    return {}

def eval_msg(view, region, msg):
    extended_region = view.line(region)
    cs_common.conn.erase_evals(lambda eval: eval.region() and eval.region().intersects(extended_region), view)
    eval = Eval(view, region)
    cs_progress.wake()
    eval.msg = {k: v for k, v in msg.items() if v}
    eval.msg["id"] = eval.id
    eval.msg["session"] = cs_common.conn.session
    eval.msg.update(get_middleware_opts(cs_common.conn))

    cs_common.conn.add_eval(eval)
    cs_common.conn.send(eval.msg)
    eval.update("pending", cs_progress.phase())

def eval(view, region, code=None):
    (line, column) = view.rowcol_utf16(region.begin())
    msg = {"op":     "eval" if (cs_common.conn.profile == cs_common.Profile.SHADOW_CLJS or cs_common.setting("eval_in_session")) else "clone-eval-close",
           "code":   view.substr(region) if code is None else code,
           "ns":     cs_parser.namespace(view, region.begin()) or 'user',
           "line":   line + 1,
           "column": column,
           "file":   view.file_name()}
    eval_msg(view, region, msg)

class ClojureSublimedEval(sublime_plugin.TextCommand):
    def run(self, edit):
        covered = []
        for sel in self.view.sel():
            if all([not sel.intersects(r) for r in covered]):
                if sel.empty():
                    region = cs_parser.topmost_form(self.view, sel.begin())
                    if region:
                        covered.append(region)
                        eval(self.view, region)
                else:
                    covered.append(sel)
                    eval(self.view, sel)

    def is_enabled(self):
        return cs_common.conn.ready()

class ClojureSublimedEvalBufferCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        view = self.view
        region = sublime.Region(0, view.size())
        file_name = view.file_name()
        msg = {"op":        "load-file",
               "file":      view.substr(region),
               "file-path": file_name,
               "file-name": os.path.basename(file_name) if file_name else "NO_SOURCE_FILE.cljc"}
        eval_msg(view, region, msg)
        
    def is_enabled(self):
        return cs_common.conn.ready()

class ClojureSublimedEvalCodeCommand(sublime_plugin.ApplicationCommand):
    def run(self, code, ns = None):
        cs_common.conn.erase_evals(lambda eval: isinstance(eval, StatusEval) and eval.status not in {"pending", "interrupt"})
        eval = StatusEval(code)
        if (not ns) and (view := eval.active_view()):
            ns = cs_parser.namespace(view, view.size())
        eval.msg = {"op":   "eval",
                    "id":   eval.id,
                    "ns":   ns or 'user',
                    "code": code}
        eval.msg.update(get_middleware_opts(cs_common.conn))        
        cs_common.conn.add_eval(eval)
        cs_common.conn.send(eval.msg)
        eval.update("pending", cs_progress.phase())

    def is_enabled(self):
        return cs_common.conn.ready()

class ClojureSublimedCopyCommand(sublime_plugin.TextCommand):
    def eval(self):
        view = self.view
        return cs_common.conn.find_eval(view, view.sel()[0])

    def run(self, edir):
        if cs_common.conn.ready() and len(self.view.sel()) == 1 and self.view.sel()[0].empty() and (eval := self.eval()) and eval.value:
            sublime.set_clipboard(eval.value)
        else:
            self.view.run_command("copy", {})

class ClojureSublimedClearEvalsCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        cs_common.conn.erase_evals(lambda eval: eval.status not in {"pending", "interrupt"}, self.view)
        cs_common.conn.erase_evals(lambda eval: isinstance(eval, StatusEval) and eval.status not in {"pending", "interrupt"})

class ClojureSublimedInterruptEvalCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        for eval in cs_common.conn.evals_by_view[self.view.id()].values():
            if eval.status == "pending":
                cs_common.conn.send({"op":           "interrupt",
                                     "session":      eval.session,
                                     "interrupt-id": eval.id})
                eval.update("interrupt", "Interrupting...")

    def is_enabled(self):
        return cs_common.conn.ready()

class ClojureSublimedRequireNamespaceCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        view = self.view
        for sel in view.sel():
            region = cs_parser.symbol_at_point(view, sel.begin()) if sel.empty() else sel
            # narrow down to the namespace part if present
            if region and (sym := view.substr(region).partition('/')[0]):
                region = sublime.Region(region.a, region.a + len(sym))
            if region:
                eval(view, region, code=f"(require '{sym})")

    def is_enabled(self):
        return cs_common.conn.ready()

def on_settings_change(settings):
    Eval.colors.clear()

def plugin_loaded():
    cs_common.on_settings_change(__name__, on_settings_change)

def plugin_unloaded():
    cs_common.clear_settings_change(__name__)