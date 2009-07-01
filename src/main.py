# -*- coding: iso-8859-1 -*-
# -----------------------------------------------------------------------------
# main.py - Main loop functions
# -----------------------------------------------------------------------------
# $Id$
#
# -----------------------------------------------------------------------------
# kaa.base - The Kaa Application Framework
# Copyright 2005-2009 Dirk Meyer, Jason Tackaberry, et al.
#
# Please see the file AUTHORS for a complete list of authors.
#
# This library is free software; you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License version
# 2.1 as published by the Free Software Foundation.
#
# This library is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA
# 02110-1301 USA
#
# -----------------------------------------------------------------------------

"""
Control the mainloop

This module provides basic functions to control the kaa mainloop.
"""

__all__ = [ 'run', 'stop', 'step', 'select_notifier', 'is_running', 'wakeup',
            'set_as_mainthread', 'is_shutting_down', 'loop', 'signals' ]

# python imports
import sys
import logging
import os
import time
import signal
import threading
import atexit

import nf_wrapper as notifier
# rename as kaasignals; a bit cumbersome, but we're using signals for something else.
import signals as kaasignals
import timer
import thread

# get logging object
log = logging.getLogger('base')

# Running state of the main loop.  Possible values are:
#  True: running
#  False: was running, but is now shutdown
#  None: not yet started
_running = None
# Set if currently in shutdown() (to prevent reentrancy)
_shutting_down = False
# Lock preventing multiple threads from executing loop().
_loop_lock = threading.Lock()

#: mainloop signals to connect to
#:  - exception: emitted when an unhandled async exceptions occurs
#:  - step: emitted on each step of the mainloop
#:  - shutdown: emitted on kaa mainloop termination
#:  - shutdown-after: emitted after shutdown signals.
#:  - exit: emitted when process exits
signals = kaasignals.Signals('exception', 'shutdown', 'shutdown-after', 'step', 'exit')

def select_notifier(module, **options):
    """
    Initialize the specified mainloop.

    :param module: the mainloop implementation to use.
                   ``"generic"``: Python based mainloop, default;
                   ``"gtk"``: pygtk mainloop;
                   ``"threaded"``: Python based mainloop in an extra thread;
                   ``"twisted"``: Twisted mainloop
    :type module: str
    :param options: module-specific keyword arguments
    """
    if module in ('thread', 'twisted'):
        import nf_thread
        return nf_thread.init(module, **options)
    return notifier.init( module, **options )


def loop(condition, timeout=None):
    """
    Executes the main loop until condition is met.

    :param condition: a callable or object that is evaluated after each step
                      of the loop.  If it evaluates False, the loop
                      terminates.  (If condition is therefore ``True``, the
                      loop will run forever.)
    :param timeout: number of seconds after which the loop will terminate
                    (regardless of the condition).

    This function may be called recursively, however two loops may not
    run in parallel threads.

    Generally it is not necessary to call this function.  You probably want
    to use :func:`kaa.main.run` instead.

    .. warning::
       Refer to the warning detailed in :func:`kaa.main.run`.
    """
    _loop_lock.acquire()
    if is_running() and not thread.is_mainthread():
        # race condition. Two threads started a mainloop and the other
        # one is executed right now. Raise a RuntimeError
        _loop_lock.release()
        raise RuntimeError('loop running in a different thread')

    initial_mainloop = False
    if not is_running():
        # no mainloop is running, set this thread as mainloop and
        # set the internal running state.
        initial_mainloop = True
        thread.set_as_mainthread()
        _set_running(True)
    # ok, that was the critical part
    _loop_lock.release()

    if not callable(condition):
        condition = lambda: condition

    abort = []
    if timeout is not None:
        # timeout handling to stop the mainloop after the given timeout
        # even when the condition is still True.
        sec = timeout
        timeout = timer.OneShotTimer(lambda: abort.append(True))
        timeout.start(sec)

    try:
        while condition() and not abort:
            try:
                notifier.step()
                signals['step'].emit()
            except BaseException, e:
                if signals['exception'].emit(*sys.exc_info()) != False:
                    # Either there are no global exception handlers, or none of
                    # them explicitly returned False to abort mainloop
                    # termination.  So abort the main loop.
                    type, value, tb = sys.exc_info()
                    raise type, value, tb
    finally:
        # make sure we set mainloop status
        if timeout is not None:
            timeout.stop()
        if initial_mainloop:
            _set_running(False)


def run(threaded=False):
    """
    Start the main loop.

    The main loop will continue to run until an exception is raised
    (and makes its way back up to the main loop without being caught).
    SystemExit and KeyboardInterrupt exceptions will cause the main loop
    to terminate, and execution will resume after run() was called.

    :param threaded: if True, the Kaa mainloop will start in a new thread.
    :type threaded: bool

    Specifying ``threaded=True`` is useful if the main Python thread has been
    co-opted by another mainloop framework and you want to use Kaa in parallel.
    Another use-case would be using kaa from the interactive Python shell::

        >>> import kaa, sys, time
        >>> kaa.main.run(threaded=True)
        >>> @kaa.threaded()
        ... def foo():
        ...     time.sleep(5)
        ...     return 'Background task finished\\n'
        ...
        >>> foo().connect(sys.stdout.write)
        <Callable for <built-in method write of file object at 0xb7de5068>>
        >>> Background task finished

    .. warning::

       Once the main loop has been started, do not fork using :func:`os.fork`.
       Doing so may cause peculiar interactions when using threads.  To safely
       fork, use :func:`kaa.utils.fork`, which may be called whether the main
       loop has been started or not.
    """
    if is_running():
        raise RuntimeError('Main loop is already running')

    if threaded:
        # start mainloop as thread and wait until it is started
        event = threading.Event()
        timer.OneShotTimer(event.set).start(0)
        t = threading.Thread(target=run, name='kaa mainloop')
        if 'readline' in sys.modules:
            # If the readline module is loaded, this almost certainly means
            # that we're running interactively.  If so, and the main loop is
            # being run in a separate thread, make that thread a daemon thread
            # so when the user exits the interactive interpreter it doesn't
            # block.
            t.setDaemon(True)
        t.start()
        return event.wait()

    global _shutting_down
    _shutting_down = False

    try:
        loop(True)
    except (KeyboardInterrupt, SystemExit):
        try:
            # This looks stupid, I know that. The problem is that if we have
            # a KeyboardInterrupt, that flag is still valid somewhere inside
            # python. The next system call will fail because of that. Since we
            # don't want a join of threads or similar fail, we use a very short
            # sleep here. In most cases we won't sleep at all because this sleep
            # fails. But after that everything is back to normal.
            # XXX: (tack) this sounds like an interpreter bug, does it still do this?
            time.sleep(0.001)
        except:
            pass
    finally:
        # stop might be None if mainloop was run in a thread at interactive prompt
        # (and therefore is a daemon thread).  In that case, we get here when
        # the interpreter is shutting down, and we've shutdown enough that stop
        # no longer exists.  It doesn't matter, because the atexit handler will
        # have called stop already.
        if stop:
            stop()


# Don't use @threaded decorator because this module participates in import cycles.
def stop():
    """
    Stop the main loop and terminate all child processes and thread
    pools started via the Kaa API.

    Any notifier callback can also cause the main loop to terminate
    by raising SystemExit.
    """
    if not thread.is_mainthread():
        # Ensure stop() is called from main thread.
        return thread.MainThreadCallable(stop)()

    global _shutting_down

    if _shutting_down:
        return

    if is_running():
        # loop still running, send system exit
        log.info('Stop notifier loop')
        notifier.shutdown()

    _shutting_down = True
    
    signals["shutdown"].emit()
    signals["shutdown"].disconnect_all()
    signals["step"].disconnect_all()

    # Process.supervisor.stopall() is attached to shutdown-after.  We emit this
    # after 'shutdown' so that callbacks connected to 'shutdown' get a chance
    # to terminate any processes.
    signals['shutdown-after'].emit()

    thread.killall()
    # One final attempt to reap any remaining zombies
    try:
        os.waitpid(-1, os.WNOHANG)
    except OSError:
        pass


def step(*args, **kwargs):
    """
    Performs a single iteration of the main loop.

    This function should almost certainly never be called directly.  Use it
    at your own peril.
    """
    if not thread.is_mainthread():
        # If step is being called from a thread, wake up the mainthread
        # instead of allowing the thread into notifier.step.
        thread.wakeup()
        # Sleep for epsilon to prevent busy loops.
        time.sleep(0.001)
        return
    notifier.step(*args, **kwargs)
    signals['step'].emit()


def is_running():
    """
    Return True if the main loop is currently running.
    """
    return _running == True


def is_shutting_down():
    """
    Return True if the main loop is currently inside stop()
    """
    return _shutting_down


def is_stopped():
    """
    Returns True if the main loop used to be running but is now shutdown.

    This is useful for worker tasks running a thread that need to live for
    the life of the application, but are started before kaa.main.run() is
    called.  These threads can loop until kaa.main.is_stopped()
    """
    return _running == False


# wakeup and set_as_mainthread are thread module functions, but expose them
# in the main namespace.  However, we don't pull them from the thread module
# now because this module participates in import cycles.
def wakeup():
    return thread.wakeup()

def set_as_mainthread():
    return thread.set_as_mainthread()


def _set_running(status):
    """
    Set mainloop running status.
    """
    global _running
    _running = status


def _shutdown_check(*args):
    # Helper function to shutdown kaa on system exit
    # The problem is that pytgtk just exits python and
    # does not simply return from the main loop and kaa
    # can't call the shutdown handler. This is not a perfect
    # solution, e.g. with the generic mainloop you can do
    # stuff after kaa.main.run() which is not possible with gtk
    if is_running():
        # If the kaa mainthread (i.e. thread the mainloop is running in)
        # is not the program's main thread, then is_mainthread() will be False
        # and we don't need to set running=False since shutdown() will raise a
        # SystemExit and things will exit normally.
        if thread.is_mainthread():
            _set_running(False)
        r = stop()
        if r:
            # If stop() returns non-None, it's an InProgress, which means the
            # main loop was running in a separate thread.  Wait for it to
            # finish.
            r.wait()


# catch SIGTERM and SIGINT if possible for a clean shutdown
if threading.enumerate()[0] == threading.currentThread():
    def signal_handler(*args):
        # use the preferred stop function for the mainloop. Most
        # backends only call sys.exit(0). Some, like twisted need
        # specific code.
        notifier.shutdown()
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
else:
    log.info('kaa imported from thread, disable SIGTERM handler')

# check to make sure we really call our shutdown function
atexit.register(_shutdown_check)
atexit.register(signals['exit'].emit)
