#
# Module for starting a process object using os.fork() or CreateProcess()
#
# multiprocessing/forking.py
#
# Copyright (c) 2006-2008, R Oudkerk
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
#
# 1. Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
# 3. Neither the name of author nor the names of any contributors may be
#    used to endorse or promote products derived from this software
#    without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE AUTHOR AND CONTRIBUTORS "AS IS" AND
# ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED.  IN NO EVENT SHALL THE AUTHOR OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS
# OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
# HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
# LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY
# OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF
# SUCH DAMAGE.
#

import os
import sys
import signal
import errno
import thread
import time
import threading

from _multiprocessing import Connection
from multiprocessing import util, process

__all__ = ['Popen', 'assert_spawning', 'exit', 'duplicate', 'close', 'ForkingPickler']

#
# Check that the current thread is spawning a child process
#

def assert_spawning(self):
    if not Popen.thread_is_spawning():
        raise RuntimeError(
            '%s objects should only be shared between processes'
            ' through inheritance' % type(self).__name__
            )

#
# Try making some callable types picklable
#

from pickle import Pickler
class ForkingPickler(Pickler):
    dispatch = Pickler.dispatch.copy()

    @classmethod
    def register(cls, type, reduce):
        def dispatcher(self, obj):
            rv = reduce(obj)
            self.save_reduce(obj=obj, *rv)
        cls.dispatch[type] = dispatcher

def _reduce_method(m):
    if m.im_self is None:
        return getattr, (m.im_class, m.im_func.func_name)
    else:
        return getattr, (m.im_self, m.im_func.func_name)
ForkingPickler.register(type(ForkingPickler.save), _reduce_method)

def _reduce_method_descriptor(m):
    return getattr, (m.__objclass__, m.__name__)
ForkingPickler.register(type(list.append), _reduce_method_descriptor)
ForkingPickler.register(type(int.__add__), _reduce_method_descriptor)

#def _reduce_builtin_function_or_method(m):
#    return getattr, (m.__self__, m.__name__)
#ForkingPickler.register(type(list().append), _reduce_builtin_function_or_method)
#ForkingPickler.register(type(int().__add__), _reduce_builtin_function_or_method)

try:
    from functools import partial
except ImportError:
    pass
else:
    def _reduce_partial(p):
        return _rebuild_partial, (p.func, p.args, p.keywords or {})
    def _rebuild_partial(func, args, keywords):
        return partial(func, *args, **keywords)
    ForkingPickler.register(partial, _reduce_partial)


#
# _python_exe is the assumed path to the python executable.
# People embedding Python want to modify it.
#

WINEXE = (sys.platform == 'win32' and getattr(sys, 'frozen', False))
WINSERVICE = sys.executable.lower().endswith("pythonservice.exe")
if WINSERVICE:
    _python_exe = os.path.join(sys.exec_prefix, 'python.exe')
else:
    _python_exe = sys.executable


#try:
#    from cPickle import dump, load, HIGHEST_PROTOCOL
#except ImportError:
from pickle import load, HIGHEST_PROTOCOL

def dump(obj, file, protocol=None):
    ForkingPickler(file, protocol).dump(obj)


class PopenBase(object):
    _tls = thread._local()

    #
    # We define a Popen class similar to the one from subprocess, but
    # whose constructor takes a process object as its argument.
    #

    def __init__(self, process_obj):
        self.returncode = None
        self._process_obj = process_obj
        self.rfd, self.wfd = os.pipe()

    def _write_prep_data(self, to_child):
        prep_data = get_preparation_data(self._process_obj._name)
        dump(prep_data, to_child, HIGHEST_PROTOCOL)
        dump(self._process_obj, to_child, HIGHEST_PROTOCOL)

#
# Unix
#

if sys.platform != 'win32':
    exit = os._exit
    duplicate = os.dup
    close = os.close

    def _child_cleanup():
        util._run_after_forkers()
        util._run_finalizers()

    class Popen(PopenBase):
        def __init__(self, process_obj):
            super(Popen, self).__init__(process_obj)

            sys.stdout.flush()
            sys.stderr.flush()

            # create pipe for communication with child
            cmd = get_command_line()
            cmd.append(str(self.rfd))

            try:
                self.pid = os.fork()
                if self.pid == 0:
                    try:
                        close(self.wfd)

                        # risky: we must not call any function that uses an
                        # internal lock because the child has its private copy
                        # of the interpreter state (i.e. non-shared mutexes and
                        # semaphores may be locked), so we do this in a thread.
                        # not perfect, because the risk of deadlock is still
                        # present (i.e. deadlock of zlib's internal lock), but
                        # a bit safer
                        cleaner = threading.Thread(target=_child_cleanup)
                        cleaner.start()
                        cleaner.join(10)

                        os.execv(cmd[0], cmd)
                    finally:
                        os._exit(1)
            except Exception:
                close(self.wfd)
                raise
            finally:
                close(self.rfd)

            # send information to child
            to_child = os.fdopen(self.wfd, 'wb')
            Popen._tls._spawning = True
            try:
                self._write_prep_data(to_child)
            finally:
                del Popen._tls._spawning
                to_child.close()

        def poll(self, flag=os.WNOHANG):
            if self.returncode is None:
                while True:
                    try:
                        pid, sts = os.waitpid(self.pid, flag)
                    except os.error as e:
                        if e.errno == errno.EINTR:
                            continue
                        # Child process not yet created. See #1731717
                        # e.errno == errno.ECHILD == 10
                        return None
                    else:
                        break
                if pid == self.pid:
                    if os.WIFSIGNALED(sts):
                        self.returncode = -os.WTERMSIG(sts)
                    else:
                        assert os.WIFEXITED(sts)
                        self.returncode = os.WEXITSTATUS(sts)
            return self.returncode

        @staticmethod
        def duplicate_for_child(handle):
            return duplicate(handle)

        def wait(self, timeout=None):
            if timeout is None:
                return self.poll(0)
            deadline = time.time() + timeout
            delay = 0.0005
            while 1:
                res = self.poll()
                if res is not None:
                    break
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                delay = min(delay * 2, remaining, 0.05)
                time.sleep(delay)
            return res

        def terminate(self):
            if self.returncode is None:
                try:
                    os.kill(self.pid, signal.SIGTERM)
                except OSError, e:
                    if self.wait(timeout=0.1) is None:
                        raise

        @staticmethod
        def thread_is_spawning():
            return getattr(Popen._tls, '_spawning', False)

#
# Windows
#

else:
    import msvcrt
    import _subprocess

    from _multiprocessing import win32
    from .util import Finalize

    TERMINATE = 0x10000

    exit = win32.ExitProcess
    close = win32.CloseHandle

    def set_executable(exe):
        global _python_exe
        _python_exe = exe

    #
    #
    #

    def duplicate(handle, target_process=None, inheritable=False):
        if target_process is None:
            target_process = _subprocess.GetCurrentProcess()
        return _subprocess.DuplicateHandle(
            _subprocess.GetCurrentProcess(), handle, target_process,
            0, inheritable, _subprocess.DUPLICATE_SAME_ACCESS
            ).Detach()

    #
    # We define a Popen class similar to the one from subprocess, but
    # whose constructor takes a process object as its argument.
    #

    class Popen(PopenBase):
        '''
        Start a subprocess to run the code of a process object
        '''
        def __init__(self, process_obj):
            super(Popen, self).__init__(process_obj)

            # get handle for read end of the pipe and make it inheritable
            rhandle = duplicate(msvcrt.get_osfhandle(self.rfd),
                                inheritable=True)
            os.close(self.rfd)

            # start process
            cmd = get_command_line() + [rhandle]
            cmd = ' '.join('"%s"' % x for x in cmd)
            hp, ht, pid, tid = _subprocess.CreateProcess(
                _python_exe, cmd, None, None, 1, 0, None, None, None
                )
            ht.Close()
            close(rhandle)

            # set attributes of self
            self.pid = pid
            self._handle = hp

            # send information to child
            to_child = os.fdopen(self.wfd, 'wb')
            Popen._tls.process_handle = int(hp)
            try:
                self._write_prep_data(to_child)
            finally:
                del Popen._tls.process_handle
                to_child.close()

        @staticmethod
        def thread_is_spawning():
            return getattr(Popen._tls, 'process_handle', None) is not None

        @staticmethod
        def duplicate_for_child(handle):
            return duplicate(handle, Popen._tls.process_handle)

        def wait(self, timeout=None):
            if self.returncode is None:
                if timeout is None:
                    msecs = _subprocess.INFINITE
                else:
                    msecs = max(0, int(timeout * 1000 + 0.5))

                res = _subprocess.WaitForSingleObject(int(self._handle), msecs)
                if res == _subprocess.WAIT_OBJECT_0:
                    code = _subprocess.GetExitCodeProcess(self._handle)
                    if code == TERMINATE:
                        code = -signal.SIGTERM
                    self.returncode = code

            return self.returncode

        def poll(self):
            return self.wait(timeout=0)

        def terminate(self):
            if self.returncode is None:
                try:
                    _subprocess.TerminateProcess(int(self._handle), TERMINATE)
                except WindowsError:
                    if self.wait(timeout=0.1) is None:
                        raise

#
#
#

def is_forking(argv):
    '''
    Return whether commandline indicates we are forking
    '''
    if len(argv) >= 2 and argv[1] == '--multiprocessing-fork':
        assert len(argv) == 3
        return True
    else:
        return False


def freeze_support():
    '''
    Run code for process object if this in not the main process
    '''
    if is_forking(sys.argv):
        main()
        sys.exit()


def get_command_line():
    '''
    Returns prefix of command line used for spawning a child process
    '''
    if getattr(process.current_process(), '_inheriting', False):
        raise RuntimeError('''
        Attempt to start a new process before the current process
        has finished its bootstrapping phase.

        This probably means that you are on Windows and you have
        forgotten to use the proper idiom in the main module:

            if __name__ == '__main__':
                freeze_support()
                ...

        The "freeze_support()" line can be omitted if the program
        is not going to be frozen to produce a Windows executable.''')

    if getattr(sys, 'frozen', False):
        return [sys.executable, '--multiprocessing-fork']
    else:
        prog = 'from multiprocessing.forking import main; main()'
        opts = util._args_from_interpreter_flags()
        return [_python_exe] + opts + ['-c', prog, '--multiprocessing-fork']


def main():
    '''
    Run code specified by data received over pipe
    '''
    assert is_forking(sys.argv)

    handle = int(sys.argv[-1])
    if sys.platform == 'win32':
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY)
        from_parent = os.fdopen(fd, 'rb')
    else:
        from_parent = os.fdopen(handle, 'rb')

    process.current_process()._inheriting = True
    preparation_data = load(from_parent)
    prepare(preparation_data)
    self = load(from_parent)
    process.current_process()._inheriting = False

    from_parent.close()

    exitcode = self._bootstrap()
    exit(exitcode)


def get_preparation_data(name):
    '''
    Return info about parent needed by child to unpickle process object
    '''
    from .util import _logger, _log_to_stderr

    d = dict(
        name=name,
        sys_path=sys.path,
        sys_argv=sys.argv,
        log_to_stderr=_log_to_stderr,
        orig_dir=process.ORIGINAL_DIR,
        authkey=process.current_process().authkey,
        )

    if _logger is not None:
        d['log_level'] = _logger.getEffectiveLevel()

    if not WINEXE and not WINSERVICE:
        main_path = getattr(sys.modules['__main__'], '__file__', None)
        if not main_path and sys.argv[0] not in ('', '-c'):
            main_path = sys.argv[0]
        if main_path is not None:
            if not os.path.isabs(main_path) and \
                                      process.ORIGINAL_DIR is not None:
                main_path = os.path.join(process.ORIGINAL_DIR, main_path)
            d['main_path'] = os.path.normpath(main_path)

    return d

#
# Make (Pipe)Connection picklable
#

def reduce_connection(conn):
    if not Popen.thread_is_spawning():
        raise RuntimeError(
            'By default %s objects can only be shared between processes\n'
            'using inheritance' % type(conn).__name__
            )
    return type(conn), (Popen.duplicate_for_child(conn.fileno()),
                        conn.readable, conn.writable)

ForkingPickler.register(Connection, reduce_connection)
if sys.platform == 'win32':
    from _multiprocessing import PipeConnection
    ForkingPickler.register(PipeConnection, reduce_connection)

#
# Prepare current process
#

old_main_modules = []

def prepare(data):
    '''
    Try to get current process ready to unpickle process object
    '''
    old_main_modules.append(sys.modules['__main__'])

    if 'name' in data:
        process.current_process().name = data['name']

    if 'authkey' in data:
        process.current_process()._authkey = data['authkey']

    if 'log_to_stderr' in data and data['log_to_stderr']:
        util.log_to_stderr()

    if 'log_level' in data:
        util.get_logger().setLevel(data['log_level'])

    if 'sys_path' in data:
        sys.path = data['sys_path']

    if 'sys_argv' in data:
        sys.argv = data['sys_argv']

    if 'dir' in data:
        os.chdir(data['dir'])

    if 'orig_dir' in data:
        process.ORIGINAL_DIR = data['orig_dir']

    if 'main_path' in data:
        # XXX (ncoghlan): The following code makes several bogus
        # assumptions regarding the relationship between __file__
        # and a module's real name. See PEP 302 and issue #10845
        # The problem is resolved properly in Python 3.4+, as
        # described in issue #19946

        main_path = data['main_path']
        main_name = os.path.splitext(os.path.basename(main_path))[0]
        if main_name == '__init__':
            main_name = os.path.basename(os.path.dirname(main_path))

        if main_name == '__main__':
            # For directory and zipfile execution, we assume an implicit
            # "if __name__ == '__main__':" around the module, and don't
            # rerun the main module code in spawned processes
            main_module = sys.modules['__main__']
            main_module.__file__ = main_path
        elif main_name != 'ipython':
            # Main modules not actually called __main__.py may
            # contain additional code that should still be executed
            import imp

            if main_path is None:
                dirs = None
            elif os.path.basename(main_path).startswith('__init__.py'):
                dirs = [os.path.dirname(os.path.dirname(main_path))]
            else:
                dirs = [os.path.dirname(main_path)]

            assert main_name not in sys.modules, main_name
            file, path_name, etc = imp.find_module(main_name, dirs)
            try:
                # We would like to do "imp.load_module('__main__', ...)"
                # here.  However, that would cause 'if __name__ ==
                # "__main__"' clauses to be executed.
                main_module = imp.load_module(
                    '__parents_main__', file, path_name, etc
                    )
            finally:
                if file:
                    file.close()

            sys.modules['__main__'] = main_module
            main_module.__name__ = '__main__'

            # Try to make the potentially picklable objects in
            # sys.modules['__main__'] realize they are in the main
            # module -- somewhat ugly.
            for obj in main_module.__dict__.values():
                try:
                    if obj.__module__ == '__parents_main__':
                        obj.__module__ = '__main__'
                except Exception:
                    pass
