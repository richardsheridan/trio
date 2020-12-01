import threading
import warnings
from collections import defaultdict

import attr
from sortedcontainers import SortedKeyList

from . import _core
from ._core._windows_cffi import (
    ffi,
    kernel32,
    ErrorCodes,
    raise_winerror,
    _handle,
)


def WaitForMultipleObjects_sync(*handles):
    """Wait for any of the given Windows handles to be signaled."""
    n = len(handles)
    handle_arr = ffi.new("HANDLE[]", n)
    for i in range(n):
        handle_arr[i] = handles[i]
    timeout = 0xFFFFFFFF  # INFINITE
    retcode = kernel32.WaitForMultipleObjects(n, handle_arr, False, timeout)  # blocking
    if retcode == ErrorCodes.WAIT_FAILED:
        raise_winerror()
    elif (
        retcode >= ErrorCodes.WAIT_ABANDONED
    ):  # We should never abandon handles but who knows
        retcode -= ErrorCodes.WAIT_ABANDONED
        warnings.warn(RuntimeWarning("Abandoned Mutex: {}".format(handles[retcode])))
    return handles[retcode]


@attr.s(slots=True, frozen=True, eq=False)
class WaitJob:
    handle = attr.ib()
    callback = attr.ib()


def _is_signaled(handle):
    # The zero means a zero timeout; this call never blocks.
    retcode = kernel32.WaitForSingleObject(handle, 0)
    if retcode == ErrorCodes.WAIT_FAILED:
        raise_winerror()
    return retcode != ErrorCodes.WAIT_TIMEOUT


class WaitPool:
    def __init__(self):
        self.wait_jobs_by_handle = defaultdict(list)
        self.wait_groups = SortedKeyList(key=len)

    def pop_by_cancel_handle(self, cancel_handle):
        for i, wait_group in enumerate(self.wait_groups):
            if wait_group.cancel_handle == cancel_handle:
                del self.wait_groups[i]
                return wait_group

    def pop_by_wait_handle(self, wait_handle):
        for i, wait_group in enumerate(self.wait_groups):
            if wait_handle in wait_group.wait_handles:
                del self.wait_groups[i]
                return wait_group


WAIT_POOL = WaitPool()


class WaitGroup:
    def __init__(self, cancel_handle):
        self.wait_handles = set()
        self.cancel_handle = cancel_handle
        self.lock = threading.Lock()
        self.lock.acquire()

    def __len__(self):
        return len(self.wait_handles) + 1  # include cancel_handle

    def drain_as_completed(self):
        while self.wait_handles:
            # need a local name in case someone changes it while in thread
            cancel_handle = self.cancel_handle
            self.lock.release()
            woken_handle = WaitForMultipleObjects_sync(cancel_handle, *self.wait_handles)
            assert self.lock.acquire(timeout=1)

            # Race condition: cancel_handle may have been signalled after a wakeup
            # on another handle. Treat it as a legitimate cancel.
            if _is_signaled(cancel_handle):
                kernel32.CloseHandle(cancel_handle)
                return

            # a handle other than the cancel_handle fired
            WAIT_POOL.pop_by_cancel_handle(cancel_handle)
            self.wait_handles.discard(woken_handle)
            WAIT_POOL.wait_groups.add(self)
            for wait_job in WAIT_POOL.wait_jobs_by_handle.pop(woken_handle):
                wait_job.callback()
            del wait_job, woken_handle
        # only the cancel handle remains, clean up and return
        WAIT_POOL.pop_by_cancel_handle(cancel_handle)
        kernel32.CloseHandle(cancel_handle)
        self.lock.release()


def UnregisterWait(cancel_token):
    """Trio thread cache variant of UnregisterWait.

    Args:
      cancel_token: Whatever was returned by RegisterWaitForSingleObject.

    """

    handle = cancel_token.handle
    if handle not in WAIT_POOL.wait_jobs_by_handle:
        # if all is well, this case should be handled by reschedule_in_flight
        # so return False to cause a crash if that is not the case
        return False

    # give up if handle been triggered
    if _is_signaled(handle):
        return ErrorCodes.ERROR_IO_PENDING
    wait_jobs = WAIT_POOL.wait_jobs_by_handle[handle]

    if len(wait_jobs) > 1:
        # simply discard the data associated with this cancel_token
        for i, waiter in enumerate(wait_jobs):
            if cancel_token == waiter[2]:
                del wait_jobs[i]
                return True
        else:
            raise RuntimeError(
                "Nothing found to cancel with cancel_token {}".format(cancel_token)
            )

    assert wait_jobs
    # This cancel_token points to a handle that only has one waiter
    del WAIT_POOL.wait_jobs_by_handle[handle]

    # extract it from its wait_group
    # remove it from WAIT_POOL as well as it will change size
    wait_group = WAIT_POOL.pop_by_wait_handle(handle)
    assert wait_group is not None
    # free any thread waiting on this group
    kernel32.SetEvent(wait_group.cancel_handle)
    wait_group.wait_handles.remove(handle)

    if len(wait_group) == 1:
        # Just the cancel handle left, we're done
        return True

    # make a new cancel_handle
    wait_group.cancel_handle = kernel32.CreateEventA(
        ffi.NULL, True, False, ffi.NULL
    )
    WAIT_POOL.wait_groups.add(wait_group)

    trio_token = _core.current_trio_token()

    def deliver(outcome):
        # blow up trio if the thread raises so we get a traceback
        trio_token.run_sync_soon(outcome.unwrap)

    _core.start_thread_soon(wait_group.drain_as_completed, deliver)
    return True


def RegisterWaitForSingleObject(handle, callback):
    """Trio thread cache variant of RegisterWaitForSingleObject.

    Args:
      handle: A valid Win32 handle. This should be guaranteed by WaitForSingleObject.

      callback: A Python function.

    Returns:
      cancel_token: An opaque Python object that can be used with UnregisterWait.

    Callbacks run with semantics equivalent to
    WT_EXECUTEINWAITTHREAD | WT_EXECUTEONLYONCE

    Callbacks are run in a trio system thread, so they must not raise errors.

    """
    cancel_token = WaitJob(handle, callback)

    # Shortcut if we are already waiting on this handle
    if handle in WAIT_POOL.wait_jobs_by_handle:
        WAIT_POOL.wait_jobs_by_handle[handle].append(cancel_token)
        return cancel_token

    cancel_handle = kernel32.CreateEventA(ffi.NULL, True, False, ffi.NULL)
    wait_group_index = WAIT_POOL.wait_groups.bisect_key_left(64) - 1
    if wait_group_index == -1:
        # wait_groups is empty or every group is full with 64
        wait_group = WaitGroup(cancel_handle)
    else:
        wait_group = WAIT_POOL.wait_groups.pop(wait_group_index)
        assert wait_group.lock.acquire(timeout=1)
        # wake this particular group
        kernel32.SetEvent(wait_group.cancel_handle)
        # overwrite with fresh cancel_handle since PulseEvent/ResetEvent could be flaky
        wait_group.cancel_handle = cancel_handle
        # update each waiter with the new cancel_handle
        for waiter in WAIT_POOL.wait_jobs_by_handle[handle]:
            waiter.cancel_handle = cancel_handle
    WAIT_POOL.wait_jobs_by_handle[handle].append(cancel_token)
    wait_group.wait_handles.add(handle)
    WAIT_POOL.wait_groups.add(wait_group)

    trio_token = _core.current_trio_token()

    def deliver(outcome):
        # blow up trio if the thread raises so we get a traceback
        trio_token.run_sync_soon(outcome.unwrap)

    _core.start_thread_soon(wait_group.drain_as_completed, deliver)

    return cancel_token


async def WaitForSingleObject(obj):
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
        return

    task = _core.current_task()
    trio_token = _core.current_trio_token()
    # This register transforms the _core.Abort.FAILED case from pulsed (on while
    # the cffi callback is running) to level triggered
    reschedule_in_flight = [False]

    def wakeup():
        reschedule_in_flight[0] = True
        try:
            trio_token.run_sync_soon(_core.reschedule, task, idempotent=True)
        except _core.RunFinishedError:
            # No need to throw a fit here, the task can't be rescheduled anyway
            pass

    cancel_token = RegisterWaitForSingleObject(handle, wakeup)

    def abort(raise_cancel):
        retcode = UnregisterWait(cancel_token)
        if retcode == ErrorCodes.ERROR_IO_PENDING or reschedule_in_flight[0]:
            # The callback is about to wake up our task
            return _core.Abort.FAILED
        elif retcode:
            return _core.Abort.SUCCEEDED
        else:
            raise RuntimeError(f"Unexpected retcode: {retcode}")

    await _core.wait_task_rescheduled(abort)
    # Unconditional unregister if not cancelled. Resource cleanup? MSDN says,
    # "Even wait operations that use WT_EXECUTEONLYONCE must be canceled."
    UnregisterWait(cancel_token)
