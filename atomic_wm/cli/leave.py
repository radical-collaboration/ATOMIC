"""``atomic-leave NAME`` -- remove a resource from the federation.

The counterpart of ``atomic-join --detach``: it

1. tells the federation to drop the resource (``POST
   /broker/federation/leave/default/<name>``, which cancels the pool's
   tasks and unregisters its dispatcher session),
2. stops the endpoint child recorded in
   ``~/.radical/orbit/atomic/<name>/endpoint.pid``,
3. terminates **surviving pilot children** -- psij's ``local`` executor
   cancels only the wrapper job, so a pilot endpoint can outlive its
   pool.  They are recognised by running an orbit endpoint whose child
   name carries this resource's pool prefix ``fed-<name>_``.

Every step is attempted even if an earlier one failed: a half-torn-down
resource is worse than a noisy teardown.
"""

import argparse
import sys

from typing import Any, Optional, Sequence

from .. import endpoint_proc
from ..client import Client, ClientError, add_connection_args

TOOL = 'atomic-leave'


# ---------------------------------------------------------------------------
def info(msg: str) -> None:
    sys.stdout.write('%s\n' % msg)
    sys.stdout.flush()


def warn(msg: str) -> None:
    sys.stderr.write('%s: warning: %s\n' % (TOOL, msg))


def error(msg: str) -> None:
    sys.stderr.write('%s: error: %s\n' % (TOOL, msg))


# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    """The ``atomic-leave`` command line."""

    parser = argparse.ArgumentParser(
        prog=TOOL,
        description='remove a resource from the ATOMIC federation and stop '
                    'its endpoint (and any surviving pilots)')

    add_connection_args(parser)

    parser.add_argument('name', help='resource name, as given to atomic-join')
    parser.add_argument('--cancel-tasks', action='store_true',
                        help='also cancel this resource\'s tasks which have '
                             'not finished.  Off by default: a capability '
                             'class pool has other members, so a queued '
                             'task can still run elsewhere -- ask for this '
                             'only when the whole federation goes down')
    parser.add_argument('--keep-endpoint', action='store_true',
                        help='leave the federation but keep the endpoint '
                             'process (and its pilots) running')
    parser.add_argument('--timeout', type=float, default=10.0, metavar='SEC',
                        help='how long a process gets after SIGTERM before '
                             'it is killed (default: 10)')

    return parser


# ---------------------------------------------------------------------------
def leave_federation(args: argparse.Namespace) -> int:
    """Step 1 -- tell the federation.  Returns an exit code contribution."""

    try:
        client = Client(broker=args.broker, token=args.token, cert=args.cert)
    except ValueError as e:
        warn('%s -- not leaving the federation, only stopping processes' % e)
        return 1

    try:
        report_left(args.name, client.fed_leave(
                                   args.name,
                                   cancel_tasks=args.cancel_tasks))
        return 0

    except ClientError as e:
        # a 404 from the *route* means the plugin is absent -- a 404 from
        # the plugin means the resource is already gone, which is fine
        if e.status == 404 and 'no route' not in e.detail.lower():
            info('resource %r was not in the federation' % args.name)
            return 0

        warn('leaving the federation failed: %s' % e)
        if e.status in (404, 503):
            warn("the broker does not host the 'federation' plugin")

        return 1


def report_left(name: str, result: Any) -> None:
    """Say what leaving did -- members removed, tasks re-queued or failed.

    Removing a member re-queues the tasks it was running; the class pool's
    other members pick them up, and a task no remaining member can satisfy
    is failed by the dispatcher.  Both numbers matter to whoever is
    watching a demo teardown.
    """

    info('left the federation: %s' % name)

    if not isinstance(result, dict):
        return

    bits = [('%s member(s) removed', result.get('members_removed')),
            ('%s task(s) re-queued', result.get('tasks_requeued')),
            ('%s task(s) failed',    result.get('tasks_failed'))]
    text = ', '.join(label % value for label, value in bits
                     if isinstance(value, int) and value)

    if text:
        info('  %s' % text)


def endpoint_pid(args: argparse.Namespace) -> Optional[int]:
    """The pid of `name`'s endpoint — from the pidfile, else from ``/proc``.

    A pid read from a pidfile is only trusted once ``/proc`` confirms it
    is really this resource's endpoint: pids are reused, and killing an
    unrelated process because a stale file named it would be worse than
    doing nothing.
    """

    endpoint = endpoint_proc.endpoint_name(args.name)
    record   = endpoint_proc.read_pidfile(args.name)

    if record:
        pid     = int(record['pid'])
        cmdline = endpoint_proc.cmdline_of(pid)

        if endpoint_proc.is_endpoint_cmdline(cmdline, endpoint):
            return pid

        if not cmdline:
            info('endpoint pid %d is already gone' % pid)
        else:
            warn('pidfile stale: pid %d is not %s -- ignoring it'
                 % (pid, endpoint))

        endpoint_proc.remove_pidfile(args.name)

    # no (usable) pidfile: the CLI may have been killed before it wrote
    # one.  Look the endpoint up in the process table instead.
    found = endpoint_proc.endpoint_pids(endpoint)

    if found:
        info('found endpoint %s in the process table (pid %s)'
             % (endpoint, ', '.join(str(p) for p in found)))
        return found[0]

    if not record:
        warn('no pidfile for %r (%s) and no running %s'
             % (args.name, endpoint_proc.pid_path(args.name), endpoint))

    return None


def stop_endpoint(args: argparse.Namespace) -> int:
    """Step 2 -- stop this resource's endpoint process."""

    pid = endpoint_pid(args)

    if pid is None:
        return 0

    if endpoint_proc.stop_pid(pid, timeout=args.timeout):
        info('stopped endpoint %s (pid %d)'
             % (endpoint_proc.endpoint_name(args.name), pid))
        endpoint_proc.remove_pidfile(args.name)
        return 0

    error('endpoint pid %d survived SIGKILL' % pid)

    return 1


def stop_pilots(args: argparse.Namespace) -> int:
    """Step 3 -- terminate pilot children which outlived the pool."""

    pids = endpoint_proc.kill_pilots(args.name, timeout=args.timeout)

    if pids:
        info('terminated %d surviving pilot process(es): %s'
             % (len(pids), ', '.join(str(p) for p in pids)))

    return 0


# ---------------------------------------------------------------------------
def run(args: argparse.Namespace) -> int:
    """Tear the resource down; 0 only if every step succeeded."""

    rc = leave_federation(args)

    if args.keep_endpoint:
        info('keeping the endpoint process running (--keep-endpoint)')
        return rc

    rc |= stop_endpoint(args)
    rc |= stop_pilots(args)

    return 1 if rc else 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Console script entry point."""

    args = build_parser().parse_args(argv)

    return run(args)


# ---------------------------------------------------------------------------
if __name__ == '__main__':
    sys.exit(main())
