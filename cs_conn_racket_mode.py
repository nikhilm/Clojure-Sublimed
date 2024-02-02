import os, re, contextvars, subprocess, sublime, sublime_plugin
from . import cs_common, cs_conn, cs_eval, cs_parser, cs_printer

server_process = contextvars.ContextVar("racket_mode_server_process", default=None)


class RacketModeServer:
    def __init__(self):
        # TODO: Not sure if we want to run the subproc
        # in the constructor. since we may want dynamic enable/disable/reset.
        # TODO: Figure out path from prefs.
        # Also this assumes racket is on the path, which also seems bad.
        self.proc = subprocess.Popen(
            [
                "/home/nikhil/racket-8.9/bin/racket",
                "/home/nikhil/racket-projects/racket-mode/racket/main.rkt",
                "--do-not-use-svg",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )
        # TODO: Spin up a thread to monitor responses

    def terminate(self):
        self.proc.kill()


class ConnectionRacketMode(cs_conn.Connection):
    def __init__(self):
        super().__init__()
        self.status = None
        self.disconnecting = False
        self.session_id = None  # TODO: Generate one
        self.window = sublime.active_window()

    def connect_impl(self):
        print("Connect impl called")

        if not server_process.get():
            print("Spawning new racket-mode server")
            server_process.set(RacketModeServer())
        self.set_status(4, "Started server")

    def eval_impl(self, form):
        print("Eval request, sending fake success", form)
        cs_eval.on_success(form.id, "FAKE SUCCESS VALUE")

    def load_file_impl(self, id, file, path):
        print("Load file request", id, file, path)
        cs_eval.on_success(id, "FAKE SUCCESS VALUE")

    def lookup_impl(self, id, symbol, ns):
        print("Lookup request", id, symbol, ns)

    def interrupt_impl(self, batch_id, id):
        print("Interrupt request", batch_id, id)

    def disconnect_impl(self):
        # TODO: Shutdown if we are the last connection.
        print("Disconnect request")


class ClojureSublimedConnectRacketCommand(sublime_plugin.WindowCommand):
    def run(self):
        # When a non-zero number of repls are active
        # we need to start a racket-mode server
        # or use an existing one.
        state = cs_common.get_state(self.window)

        state.last_conn = ("clojure_sublimed_connect_racket", "boo")
        ConnectionRacketMode().connect()

    def is_enabled(self):
        state = cs_common.get_state(self.window)
        return state.conn is None


def plugin_unloaded():
    server = server_process.get()
    if server:
        server.terminate()
        print("Killed server")
