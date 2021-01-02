import contextlib
import threading
import warnings

from sortedcontainers import SortedKeyList

from . import _core, _threads, _sync
from ._core._windows_cffi import (
    ffi,
    kernel32,
    ErrorCodes,
    WaitFlags,
    raise_winerror,
    _handle,
    _is_signaled,
)

_WAIT_FLAGS = WaitFlags.WT_EXECUTEINWAITTHREAD | WaitFlags.WT_EXECUTEONLYONCE


@ffi.callback("WAITORTIMERCALLBACK")
def _wait_callback(context, timer_or_wait_fired):  # pragma: no cover
    ffi.from_handle(context)()


def UnregisterWait_win32(cancel_token):
    """Python wrapper for kernel32.UnregisterWait.

    Args:
      cancel_token: Whatever was returned by RegisterWaitForSingleObject.

    """
    cancel_token, context_handle = cancel_token
    # have to dereference cancel token i.e. PHANDLE -> HANDLE
    return kernel32.UnregisterWait(cancel_token[0])


def RegisterWaitForSingleObject_win32(handle, callback):
    """Python wrapper for kernel32.RegisterWaitForSingleObject.

    Args:
      handle: A valid Win32 handle. This should be guaranteed by WaitForSingleObject.

      callback: A Python function taking no arguments and definitely not raising
        any errors.

    Returns:
      cancel_token: An opaque object that can be used with UnregisterWait.
        This object must be kept alive until the callback is called or cancelled!

    Callbacks are run with WT_EXECUTEINWAITTHREAD | WT_EXECUTEONLYONCE.

    Callbacks are run in a windows system thread, so they must not raise errors.

    """
    cancel_token = ffi.new("PHANDLE")
    context_handle = ffi.new_handle(callback)
    timeout = 0xFFFFFFFF  # INFINITE
    if not kernel32.RegisterWaitForSingleObject(
        cancel_token,
        handle,
        _wait_callback,
        context_handle,
        timeout,
        _WAIT_FLAGS,
    ):  # pragma: no cover
        raise_winerror()
    # keep context_handle alive by passing it around with cancel_token
    return cancel_token, context_handle


_wait_local = _core.RunVar("_wait_local")
WAIT_POOL = None
MAXIMUM_WAIT_OBJECTS = 64


def _get_wait_pool_local():
    try:
        wait_pool = _wait_local.get()
    except LookupError:
        wait_pool = WaitPool(contextlib.nullcontext())
        _wait_local.set(wait_pool)
    return wait_pool


def _get_wait_pool_global():
    global WAIT_POOL
    if WAIT_POOL is None:
        WAIT_POOL = WaitPool(threading.Lock())
    return WAIT_POOL


def WaitForMultipleObjects_sync(*handles):
    """Wait for any of the given Windows handles to be signaled."""
    # Very important that `handles` length not change, so splat op is mandatory
    n = len(handles)
    assert n <= MAXIMUM_WAIT_OBJECTS
    handle_arr = ffi.new("HANDLE[]", n)
    for i in range(n):
        handle_arr[i] = handles[i]
    timeout = 0xFFFFFFFF  # INFINITE
    retcode = kernel32.WaitForMultipleObjects(n, handle_arr, False, timeout)  # blocking
    if retcode == ErrorCodes.WAIT_FAILED:
        raise_winerror()
    elif retcode >= ErrorCodes.WAIT_ABANDONED:  # pragma: no cover
        # We should never abandon handles but who knows
        retcode -= ErrorCodes.WAIT_ABANDONED
        warnings.warn(RuntimeWarning("Abandoned Mutex: {}".format(handles[retcode])))
    return handles[retcode]


class WaitPool:
    def __init__(self, lock):
        self._handle_map = {}
        self._size_sorted_wait_groups = SortedKeyList(key=len)
        self.lock = lock

    def add(self, handle, callback):
        # Shortcut if we are already waiting on this handle
        if handle in self._handle_map:
            self._handle_map[handle][0].add(callback)
            return

        try:
            wait_group = self._size_sorted_wait_groups.pop()
        except IndexError:
            # pool is empty or every group is full
            wait_group = WaitGroup()
        else:
            wait_group.wake()

        self._handle_map[handle] = ({callback}, wait_group)

        with self.mutating(wait_group):
            wait_group.add(handle)

    def remove(self, handle, callback):
        callbacks, wait_group = self._handle_map[handle]

        callbacks.remove(callback)

        if callbacks:
            # no cleanup or thread interaction needed
            return

        del self._handle_map[handle]

        wait_group.wake()

        with self.mutating(wait_group):
            wait_group.remove(handle)

    def remove_and_execute_callbacks(self, handle):
        for callback in self._handle_map.pop(handle)[0]:
            callback()

    @contextlib.contextmanager
    def mutating(self, wait_group):
        # if SortedKeyList has many equal values, remove() performance degrades
        # from O(log(n)) to O(n), so we don't keep full groups inside
        self._size_sorted_wait_groups.discard(wait_group)
        try:
            yield
        finally:
            if 1 < len(wait_group) < MAXIMUM_WAIT_OBJECTS:
                self._size_sorted_wait_groups.add(wait_group)


class WaitGroup:
    def __init__(self):
        self._wait_handles = [kernel32.CreateEventA(ffi.NULL, True, False, ffi.NULL)]
        # TODO: inline this method
        self.wait_soon()

    def __len__(self):
        return len(self._wait_handles)

    def add(self, handle):
        return self._wait_handles.append(handle)

    def remove(self, handle):
        return self._wait_handles.remove(handle)

    def wait_soon_thread(self):
        trio_token = _core.current_trio_token()

        def fn():
            try:
                self.drain_as_completed_sync()
            finally:
                kernel32.CloseHandle(self._wait_handles[0])

        def deliver(outcome):
            # blow up trio if the thread raises so we get a traceback
            try:
                trio_token.run_sync_soon(outcome.unwrap)
            except _core.RunFinishedError:  # pragma: no cover
                # if trio is already gone, here is better than nowhere
                outcome.unwrap()

        _core.start_thread_soon(fn, deliver)

    def wait_soon_task(self):
        async def async_fn():
            try:
                await self.drain_as_completed()
            finally:
                kernel32.CloseHandle(self._wait_handles[0])

        _core.spawn_system_task(async_fn)

    def wake(self):
        kernel32.SetEvent(self._wait_handles[0])

    def drain_as_completed_sync(self):
        wait_pool = _get_wait_pool()
        # This with block prevents a race with the spawning thread on init
        with wait_pool.lock:
            handles_left = len(self._wait_handles) - 1
        while handles_left:
            signaled_handle = WaitForMultipleObjects_sync(*self._wait_handles)
            with wait_pool.lock:
                # This assignment must be locked, not just the method call
                handles_left = self.drain_one(wait_pool, signaled_handle)

    async def drain_as_completed(self):
        wait_pool = _get_wait_pool()
        handles_left = len(self._wait_handles) - 1
        while handles_left:
            signaled_handle = await _threads.to_thread_run_sync(
                WaitForMultipleObjects_sync,
                *self._wait_handles,
                limiter=_sync.CapacityLimiter(1),
            )
            handles_left = self.drain_one(wait_pool, signaled_handle)

    def drain_one(self, wait_pool, signaled_handle):
        if signaled_handle is self._wait_handles[0]:
            kernel32.CloseHandle(self._wait_handles[0])
            self._wait_handles[0] = kernel32.CreateEventA(
                ffi.NULL, True, False, ffi.NULL
            )
        else:
            with wait_pool.mutating(self):
                self._wait_handles.remove(signaled_handle)
            wait_pool.remove_and_execute_callbacks(signaled_handle)

        return len(self._wait_handles) - 1


def UnregisterWait_trio(cancel_token):
    """Trio thread cache variant of UnregisterWait.

    Args:
      cancel_token: Whatever was returned by RegisterWaitForSingleObject.

    """

    handle, callback = cancel_token
    wait_pool = _get_wait_pool()

    with wait_pool.lock:
        try:
            wait_pool.remove(handle, callback)
        except KeyError:
            return False
        else:
            return True


def RegisterWaitForSingleObject_trio(handle, callback):
    """Trio thread cache variant of RegisterWaitForSingleObject.

    Args:
      handle: A valid Win32 handle. This should be guaranteed by WaitForSingleObject.

      callback: A Python callable, to be called with no arguments.

    Returns:
      cancel_token: An opaque Python object that can be used with UnregisterWait.

    Callbacks run with semantics equivalent to
    WT_EXECUTEINWAITTHREAD | WT_EXECUTEONLYONCE

    Callbacks are run in a trio system thread, so they must not raise errors.

    """
    wait_pool = _get_wait_pool()

    with wait_pool.lock:
        wait_pool.add(handle, callback)

    return handle, callback


async def WaitForSingleObject_pool(obj):
    """Async and cancellable variant of WaitForSingleObject. Windows only.

    Args:
      obj: A Win32 handle, as a Python integer.

    Raises:
      OSError: If the handle is invalid, e.g. when it is already closed.

    """
    await _core.checkpoint_if_cancelled()
    # Allow ints or whatever we can convert to a win handle
    handle = _handle(obj)

    # Quick check; we might not even need to register the handle.  We also exit here
    # if the handle is already closed for some reason.
    if _is_signaled(handle):
        await _core.cancel_shielded_checkpoint()
        return

    task = _core.current_task()
    trio_token = _core.current_trio_token()
    # This register transforms the _core.Abort.FAILED case from pulsed (on while
    # the callback is running) to level triggered
    reschedule_in_flight = [False]

    def wakeup():  # pragma: no cover  # sometimes run in non-python thread
        reschedule_in_flight[0] = True
        try:
            trio_token.run_sync_soon(_core.reschedule, task, idempotent=True)
        except _core.RunFinishedError:  # pragma: no cover
            # No need to throw a fit here, the task can't be rescheduled anyway
            pass

    cancel_token = RegisterWaitForSingleObject(handle, wakeup)

    def abort(raise_cancel):
        retcode = UnregisterWait(cancel_token)
        if (
            retcode == ErrorCodes.ERROR_IO_PENDING or reschedule_in_flight[0]
        ):  # pragma: no cover
            # The callback is about to wake up our task
            return _core.Abort.FAILED
        elif retcode:
            return _core.Abort.SUCCEEDED
        else:  # pragma: no cover
            raise RuntimeError(f"Unexpected retcode: {retcode}")

    await _core.wait_task_rescheduled(abort)
    # Unconditional unregister if not cancelled. Resource cleanup? MSDN says,
    # "Even wait operations that use WT_EXECUTEONLYONCE must be canceled."
    UnregisterWait(cancel_token)


async def WaitForSingleObject_pair(obj):
    """Async and cancellable variant of WaitForSingleObject. Windows only.

    Args:
      obj: A Win32 handle, as a Python integer.

    Raises:
      OSError: If the handle is invalid, e.g. when it is already closed.

    """
    await _core.checkpoint_if_cancelled()
    # Allow ints or whatever we can convert to a win handle
    handle = _handle(obj)

    # Quick check; we might not even need to spawn a thread.  We also exit here
    # if the handle is already closed for some reason.
    if _is_signaled(handle):
        await _core.cancel_shielded_checkpoint()
        return

    # Wait for a thread that waits for two handles: the handle plus a handle
    # that we can use to cancel the thread.
    cancel_handle = kernel32.CreateEventA(ffi.NULL, True, False, ffi.NULL)
    try:
        await _threads.to_thread_run_sync(
            WaitForMultipleObjects_sync,
            handle,
            cancel_handle,
            cancellable=True,
            limiter=_sync.CapacityLimiter(1),
        )
    finally:
        # Clean up our cancel handle. In case we get here because this task was
        # cancelled, we also want to set the cancel_handle to stop the thread.
        kernel32.SetEvent(cancel_handle)
        kernel32.CloseHandle(cancel_handle)


def _pool_per_run():
    global WaitForSingleObject, UnregisterWait, RegisterWaitForSingleObject, _get_wait_pool
    WaitForSingleObject = WaitForSingleObject_pool
    UnregisterWait = UnregisterWait_trio
    RegisterWaitForSingleObject = RegisterWaitForSingleObject_trio
    WaitGroup.wait_soon = WaitGroup.wait_soon_task
    _get_wait_pool = _get_wait_pool_local


def _pool_per_process():
    global WaitForSingleObject, UnregisterWait, RegisterWaitForSingleObject, _get_wait_pool
    WaitForSingleObject = WaitForSingleObject_pool
    UnregisterWait = UnregisterWait_trio
    RegisterWaitForSingleObject = RegisterWaitForSingleObject_trio
    WaitGroup.wait_soon = WaitGroup.wait_soon_thread
    _get_wait_pool = _get_wait_pool_global


def _win32_pool():
    global WaitForSingleObject, UnregisterWait, RegisterWaitForSingleObject
    WaitForSingleObject = WaitForSingleObject_pool
    UnregisterWait = UnregisterWait_win32
    RegisterWaitForSingleObject = RegisterWaitForSingleObject_win32


def _no_pool():
    global WaitForSingleObject
    WaitForSingleObject = WaitForSingleObject_pair


_pool_per_process()
