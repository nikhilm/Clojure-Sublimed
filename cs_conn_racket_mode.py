import os, re, contextvars, subprocess, sublime, sublime_plugin, uuid, threading
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
        self.shutdown = False
        self.reader = threading.Thread(target=self.read_loop, daemon=True)
        self.reader.start()
        self.nonce = 0
        print("The plugin is running in pid", os.getpid())

    def next_nonce(self):
        self.nonce += 1
        return self.nonce

    def read_loop(self):
        # TODO: What to do about being blocked on a read?
        # Use timeouts!
        # How should reading work?
        # Let's try out some to see what happens.
        print("Running reader")
        while True:
            if self.shutdown:
                break
            # self.proc.communicate() doesn't work since it waits for the proc to exit
            # Grr this can block
            # in which case we can't shut down this thread
            # perhaps we don't need to because of daemon?
            print("Got stdout", self.proc.stdout.readline())
            # Need an s-exp parser here.
            # TODO: Also read stderr?
            # How long to sleep for?

    def send(self, session_id, msg):
        self.proc.stdin.write(
            # TODO: Open with encoding so we don't need this here.
            f"({self.next_nonce()} {session_id} {msg})".encode("utf-8")
        )
        self.proc.stdin.flush()

    def terminate(self):
        self.shutdown = True
        # self.reader.join()
        # TODO: Stop reader thread
        self.proc.kill()


class ConnectionRacketMode(cs_conn.Connection):
    def __init__(self):
        super().__init__()
        self.status = None
        self.disconnecting = False
        self.session_id = str(uuid.uuid4())
        self.window = sublime.active_window()

    def connect_impl(self):
        print("Connect impl called")

        if not server_process.get():
            print("Spawning new racket-mode server")
            server_process.set(RacketModeServer())
        print("initiating a new repl with sesion id", self.session_id)
        self.set_status(1, "Waiting for response")
        server_process.get().send("#f", f"repl-start {self.session_id}")
        self.set_status(4, "Started server")

    def eval_impl(self, form):
        print("Eval request, sending fake success", form)
        cs_eval.on_success(form.id, "FAKE SUCCESS VALUE")

    def load_file_impl(self, id, file, path):
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
