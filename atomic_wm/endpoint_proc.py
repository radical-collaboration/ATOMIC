"""Manage the orbit endpoint child process behind ``atomic-join``.

``atomic-join`` turns a machine into a federated resource, and the first
thing that takes is a running orbit *endpoint*: a child process which
dials the broker over one outbound WebSocket and serves the endpoint-side
plugins (``sysinfo``, ``queue_info``, ``rhapsody``, …).  This module owns
that child — how it is found, which environment it must run with, where
its output goes, and how it (and the pilots it spawned) are torn down
again.

Environment rules for the child (verified in the 2026-09-06 spike, see
``plans/00-overview.md``):

* ``PATH`` must have the venv ``bin`` directory **first** — psij launched
  pilots resolve ``radical-orbit-endpoint-wrapper.sh`` by name.
* ``RADICAL_ORBIT_LOG_LVL`` is set; ``RADICAL_LOG_LVL`` is removed (a
  developer shell exports ``DEBUG_9``, which the endpoint's
  ``--log-level`` rejects) and so is ``RADICAL_ORBIT_LOG_FILE`` (pilots
  would inherit it and all write the same file).
* ``RADICAL_ORBIT_RHAPSODY_BACKEND`` is passed through when set — locally
  it must be ``concurrent``, else rhapsody defaults to ``dragon_v3``.

The endpoint binary has no log-file flag, so we redirect its stdout and
stderr ourselves to ``<state>/endpoint.log``.
"""

import errno
import os
import shutil
import signal
import subprocess
import sys
import time

from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# state (log + pidfile) lives here; the env var exists for tests and for
# running several demos side by side
ENV_STATE_ROOT   = 'ATOMIC_WM_STATE'
DEFAULT_STATE    = '~/.radical/orbit/atomic'

# where to find the endpoint binary, when it is not on $PATH
ENV_ENDPOINT_BIN = 'ATOMIC_ENDPOINT_BIN'
ENDPOINT_SCRIPT  = 'radical-orbit-endpoint.py'

# how the endpoint of a federated resource, and the pilots it spawns, are
# named -- `atomic-leave` matches surviving pilots on the pool prefix
ENDPOINT_PREFIX  = 'ep_'
POOL_PREFIX      = 'fed-'
PILOT_MARKER     = 'radical-orbit-endpoint'


# ---------------------------------------------------------------------------
class EndpointError(RuntimeError):
    """The endpoint child could not be started, found or stopped."""


# ---------------------------------------------------------------------------
# naming and paths
# ---------------------------------------------------------------------------

def endpoint_name(name: str) -> str:
    """Endpoint name serving the resource `name` (``ep_<name>``)."""

    return ENDPOINT_PREFIX + name


def pool_prefix(name: str) -> str:
    """Prefix of the child endpoint names of `name`'s pilots.

    The dispatcher names a pilot's child endpoint ``<pool>_<pid>`` and the
    federation's pool is ``fed-<name>`` — so every pilot of this resource
    carries ``fed-<name>_`` on its command line.
    """

    return '%s%s_' % (POOL_PREFIX, name)


def state_root() -> str:
    """Root of the CLI's own state (``$ATOMIC_WM_STATE`` or the default)."""

    return os.path.expanduser(os.environ.get(ENV_STATE_ROOT) or DEFAULT_STATE)


def state_dir(name: str, create: bool = False) -> str:
    """Per-resource state directory (log + pidfile)."""

    path = os.path.join(state_root(), name)

    if create:
        os.makedirs(path, exist_ok=True)

    return path


def log_path(name: str) -> str:
    """Where the endpoint child's stdout/stderr are redirected."""

    return os.path.join(state_dir(name), 'endpoint.log')


def pid_path(name: str) -> str:
    """Pidfile written by ``atomic-join``, read by ``atomic-leave``."""

    return os.path.join(state_dir(name), 'endpoint.pid')


# ---------------------------------------------------------------------------
# pidfile
# ---------------------------------------------------------------------------

def write_pidfile(name: str, pid: int, endpoint: str,
                  broker: str = '') -> str:
    """Record the endpoint child so ``atomic-leave`` finds it.

    Written on every successful join (not only ``--detach``): should the
    CLI be killed outright, the pidfile is what lets ``atomic-leave``
    still stop the endpoint it left behind.  A clean teardown removes it.

    The format is deliberately trivial (``key=value`` lines, ``pid``
    first) so that a human — or ``kill $(head -1 …)`` — can use it too.
    """

    path = pid_path(name)
    os.makedirs(os.path.dirname(path), exist_ok=True)

    body = ['pid=%d'      % pid,
            'endpoint=%s' % endpoint,
            'resource=%s' % name,
            'broker=%s'   % broker,
            'started=%.3f' % time.time()]

    with open(path, 'w', encoding='utf-8') as fout:
        fout.write('\n'.join(body) + '\n')

    return path


def read_pidfile(name: str) -> Optional[Dict[str, Any]]:
    """Read the pidfile of `name`; None if there is none (or it is junk)."""

    path = pid_path(name)

    try:
        with open(path, 'r', encoding='utf-8') as fin:
            text = fin.read()
    except OSError:
        return None

    info: Dict[str, Any] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if '=' in line:
            key, _, val = line.partition('=')
            info[key.strip()] = val.strip()
        elif line.isdigit() and 'pid' not in info:
            # tolerate a plain pid file written by anything else
            info['pid'] = line

    try:
        info['pid'] = int(info['pid'])
    except (KeyError, TypeError, ValueError):
        return None

    return info


def remove_pidfile(name: str) -> None:
    """Delete the pidfile of `name`, if any."""

    try:
        os.unlink(pid_path(name))
    except OSError:
        pass


# ---------------------------------------------------------------------------
# the child's binary and environment
# ---------------------------------------------------------------------------

def find_endpoint_bin(explicit: Optional[str] = None) -> str:
    """Locate ``radical-orbit-endpoint.py``.

    Order: explicit path (``--endpoint-bin``) > ``$ATOMIC_ENDPOINT_BIN`` >
    ``$PATH`` > next to the running interpreter (the usual case: the CLI
    and the endpoint live in the same venv).
    """

    for cand in [explicit, os.environ.get(ENV_ENDPOINT_BIN)]:
        if cand:
            cand = os.path.expanduser(cand)
            if not os.path.exists(cand):
                raise EndpointError('endpoint binary not found: %s' % cand)
            return os.path.abspath(cand)

    found = shutil.which(ENDPOINT_SCRIPT)
    if found:
        return found

    cand = os.path.join(os.path.dirname(os.path.abspath(sys.executable)),
                        ENDPOINT_SCRIPT)
    if os.path.exists(cand):
        return cand

    raise EndpointError('cannot find %s -- install radical.orbit into this '
                        'environment, put it on $PATH, or pass '
                        '--endpoint-bin' % ENDPOINT_SCRIPT)


def child_env(base: Optional[Dict[str, str]] = None,
              log_level: str = 'INFO') -> Dict[str, str]:
    """Environment for the endpoint child — see the module docstring."""

    env = dict(os.environ if base is None else base)

    # the venv bin directory must come first: psij launched pilots resolve
    # `radical-orbit-endpoint-wrapper.sh` by name
    vbin = os.path.dirname(os.path.abspath(sys.executable))
    path = [p for p in (env.get('PATH') or '').split(os.pathsep) if p]
    if not path or os.path.normpath(path[0]) != os.path.normpath(vbin):
        path = [vbin] + [p for p in path
                         if os.path.normpath(p) != os.path.normpath(vbin)]
    env['PATH'] = os.pathsep.join(path)

    env['RADICAL_ORBIT_LOG_LVL'] = log_level.upper()

    # RADICAL_LOG_LVL is often DEBUG_9 in a developer shell, which the
    # endpoint's --log-level choices reject; RADICAL_ORBIT_LOG_FILE would
    # be inherited by every pilot
    env.pop('RADICAL_LOG_LVL', None)
    env.pop('RADICAL_ORBIT_LOG_FILE', None)

    # RADICAL_ORBIT_RHAPSODY_BACKEND is passed through by simply not
    # touching it (it must reach the endpoint, and through it the pilots)

    return env


def command(endpoint: str, url: str,
            token: Optional[str] = None,
            cert: Optional[str] = None,
            plugins: str = 'default',
            log_level: str = 'INFO',
            binary: Optional[str] = None,
            python: Optional[str] = None) -> List[str]:
    """Argument vector for the endpoint child.

    Run with the *same* interpreter as this process — the endpoint binary
    is a plain script and its shebang may point at a different python.
    """

    argv = [python or sys.executable, find_endpoint_bin(binary),
            '--name',      endpoint,
            '--url',       url,
            '--plugins',   plugins,
            '--log-level', log_level.upper()]

    if token:
        argv += ['--token', token]
    if cert:
        argv += ['--cert', os.path.expanduser(cert)]

    return argv


# ---------------------------------------------------------------------------
# the child process
# ---------------------------------------------------------------------------

class EndpointProcess:
    """The endpoint child of one joined resource."""

    def __init__(self, name: str, url: str,
                 endpoint: Optional[str] = None,
                 token: Optional[str] = None,
                 cert: Optional[str] = None,
                 plugins: str = 'default',
                 log_level: str = 'INFO',
                 binary: Optional[str] = None):

        self.name      = name
        self.endpoint  = endpoint or endpoint_name(name)
        self.url       = url
        self.token     = token
        self.cert      = cert
        self.plugins   = plugins
        self.log_level = log_level
        self.binary    = binary

        self.argv: List[str] = []
        self.log             = log_path(name)
        self._proc: Optional[subprocess.Popen] = None
        self._logfd: Any     = None

    # ----------------------------------------------------------------- run
    @property
    def pid(self) -> Optional[int]:
        return self._proc.pid if self._proc else None

    def start(self) -> int:
        """Spawn the endpoint; return its pid."""

        if self._proc:
            raise EndpointError('endpoint child already started')

        state_dir(self.name, create=True)

        self.argv = command(self.endpoint, self.url,
                            token    =self.token,
                            cert     =self.cert,
                            plugins  =self.plugins,
                            log_level=self.log_level,
                            binary   =self.binary)

        self._logfd = open(self.log, 'a', encoding='utf-8')
        self._logfd.write('\n--- %s: %s\n'
                          % (time.strftime('%Y-%m-%d %H:%M:%S'),
                             ' '.join(self.argv)))
        self._logfd.flush()

        try:
            self._proc = subprocess.Popen(
                self.argv,
                stdin =subprocess.DEVNULL,
                stdout=self._logfd,
                stderr=subprocess.STDOUT,
                env   =child_env(log_level=self.log_level),
                # own session: a Ctrl-C on the CLI's terminal must not
                # race our orderly `leave` + shutdown, and `--detach`
                # leaves the endpoint behind on purpose
                start_new_session=True)
        except OSError as e:
            self._close_log()
            raise EndpointError('cannot start endpoint: %s' % e) from e

        return self._proc.pid

    def is_alive(self) -> bool:

        if not self._proc:
            return False

        return self._proc.poll() is None

    @property
    def returncode(self) -> Optional[int]:

        return self._proc.poll() if self._proc else None

    def stop(self, timeout: float = 10.0) -> None:
        """Terminate the endpoint (SIGTERM, then SIGKILL) and reap it."""

        if not self._proc:
            return

        if self._proc.poll() is None:
            self._signal_and_wait(self._proc.terminate, timeout)

            if self._proc.poll() is None:
                self._signal_and_wait(self._proc.kill, 5.0)

        self._close_log()

    def _signal_and_wait(self, send: Any, timeout: float) -> None:
        """Send a stop signal and give the child `timeout` s to react."""

        try:
            send()
        except OSError:                                  # already reaped
            return

        try:
            self._proc.wait(timeout=timeout)             # type: ignore
        except subprocess.TimeoutExpired:
            pass

    def detach(self) -> None:
        """Give up ownership of the child (``--detach``) — keep it running."""

        self._close_log()
        self._proc = None

    def log_tail(self, lines: int = 15) -> str:
        """Last `lines` lines of the child's output — for error messages."""

        return log_tail(self.log, lines)

    # -------------------------------------------------------------- helper
    def _close_log(self) -> None:

        if self._logfd:
            try:
                self._logfd.close()
            except OSError:
                pass
            self._logfd = None


def log_tail(path: str, lines: int = 15) -> str:
    """Last `lines` lines of `path`, or '' if it cannot be read."""

    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as fin:
            tail = fin.readlines()[-lines:]
    except OSError:
        return ''

    return ''.join(tail).rstrip()


# ---------------------------------------------------------------------------
# process table -- used by `atomic-leave` to catch surviving pilots
# ---------------------------------------------------------------------------

def iter_processes() -> List[Tuple[int, str]]:
    """``[(pid, cmdline), …]`` for the processes we can see.

    Read straight from ``/proc`` — no psutil dependency, and the CLI must
    run on a login node with nothing but python3.
    """

    out: List[Tuple[int, str]] = []

    try:
        entries = os.listdir('/proc')
    except OSError:
        return out

    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open('/proc/%s/cmdline' % entry, 'rb') as fin:
                raw = fin.read()
        except OSError:
            continue
        if not raw:
            continue
        cmdline = raw.replace(b'\0', b' ').decode('utf-8',
                                                  'replace').strip()
        out.append((int(entry), cmdline))

    return out


def pilot_pids(name: str,
               procs: Optional[Iterable[Tuple[int, str]]] = None
               ) -> List[int]:
    """Pids of pilot children still running for the resource `name`.

    psij's ``local`` executor cancels only the wrapper job, so a pilot's
    endpoint can outlive its pool.  A pilot is recognised by running an
    orbit endpoint **and** carrying this resource's pool prefix
    (``fed-<name>_``) — that is the dispatcher's child endpoint name.
    """

    if procs is None:
        procs = iter_processes()

    mine   = os.getpid()
    prefix = pool_prefix(name)
    found  = []

    for pid, cmdline in procs:
        if pid == mine:
            continue
        if PILOT_MARKER in cmdline and prefix in cmdline:
            found.append(pid)

    return found


def kill_pilots(name: str,
                procs: Optional[Iterable[Tuple[int, str]]] = None,
                timeout: float = 5.0,
                killer: Any = None,
                alive: Any = None) -> List[int]:
    """Terminate the surviving pilot children of the resource `name`.

    (`killer`/`alive` are injectable for tests, as in :func:`kill_pids`.) of the resource `name`.

    Returns the pids which were signalled (empty when there were none).
    """

    pids = pilot_pids(name, procs)

    if pids:
        kill_pids(pids, timeout=timeout, killer=killer, alive=alive)

    return pids


def kill_pids(pids: Sequence[int], timeout: float = 5.0,
              killer: Any = None,
              alive: Any = None) -> List[int]:
    """SIGTERM (then SIGKILL) `pids`; return those which are gone.

    `killer` and `alive` are resolved at call time (and injectable) so
    the teardown logic can be tested without spawning processes.
    """

    if killer is None:
        killer = os.kill
    if alive is None:
        alive = pid_alive

    gone: List[int] = []
    left: List[int] = []

    for pid in pids:
        if _signal(pid, signal.SIGTERM, killer):
            left.append(pid)
        else:
            gone.append(pid)

    deadline = time.time() + timeout
    while left and time.time() < deadline:
        left = [pid for pid in left if alive(pid)]
        if left:
            time.sleep(0.2)

    for pid in left:
        _signal(pid, signal.SIGKILL, killer)

    return gone + [pid for pid in pids if pid not in left]


def pid_alive(pid: int) -> bool:
    """Is `pid` still around (and ours to signal)?"""

    try:
        os.kill(pid, 0)
    except OSError as e:
        return e.errno == errno.EPERM

    return True


def stop_pid(pid: int, timeout: float = 10.0, killer: Any = None,
             alive: Any = None) -> bool:
    """Stop a single process by pid; True if it is gone afterwards."""

    if alive is None:
        alive = pid_alive

    if not alive(pid):
        return True

    kill_pids([pid], timeout=timeout, killer=killer, alive=alive)

    return not alive(pid)


def _signal(pid: int, sig: int, killer: Any) -> bool:
    """Send `sig`; False if the process is already gone."""

    try:
        killer(pid, sig)
    except ProcessLookupError:
        return False
    except OSError:
        return False

    return True
