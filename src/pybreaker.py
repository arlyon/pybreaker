# -*- coding:utf-8 -*-

"""
Threadsafe pure-Python implementation of the Circuit Breaker pattern, described
by Michael T. Nygard in his book 'Release It!'.

For more information on this and other patterns and best practices, buy the
book at http://pragprog.com/titles/mnee/release-it
"""
import time

import calendar
import inspect
import logging
import types
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from functools import wraps
from threading import RLock
from typing import Callable, Tuple, Optional, Iterable, Coroutine

try:
    from redis.exceptions import RedisError

    HAS_REDIS_SUPPORT = True
except ImportError:
    HAS_REDIS_SUPPORT = False
    RedisError = None

__all__ = (
    'CircuitBreaker', 'CircuitBreakerListener', 'CircuitBreakerError',
    'CircuitMemoryStorage', 'CircuitRedisStorage', 'STATE_OPEN', 'STATE_CLOSED',
    'STATE_HALF_OPEN',)

STATE_OPEN = 'open'
STATE_CLOSED = 'closed'
STATE_HALF_OPEN = 'half-open'


class CircuitBreaker:
    """
    More abstractly, circuit breakers exists to allow one subsystem to fail
    without destroying the entire system.

    This is done by wrapping dangerous operations (typically integration points)
    with a component that can circumvent calls when the system is not healthy.

    This pattern is described by Michael T. Nygard in his book 'Release It!'.
    """

    def __init__(self, fail_max=5, timeout_duration=timedelta(seconds=60),
                 exclude: Optional[Iterable[type]] = None,
                 listeners: Optional[Iterable['CircuitBreakerListener']] = None,
                 state_storage: Optional['CircuitBreakerStorage'] = None,
                 name: Optional[str] = None):
        """
        Creates a new circuit breaker with the given parameters.
        """
        self._lock = RLock()
        self._state_storage = state_storage or CircuitMemoryStorage(STATE_CLOSED)
        self._state = self._create_new_state(self.current_state)

        self._fail_max = fail_max
        self._timeout_duration = timeout_duration

        self._excluded_exception_types = list(exclude or [])
        self._listeners = list(listeners or [])
        self._name = name

    @property
    def fail_counter(self):
        """
        Returns the current number of consecutive failures.
        """
        return self._state_storage.counter

    @property
    def fail_max(self):
        """
        Returns the maximum number of failures tolerated before the circuit is opened.
        """
        return self._fail_max

    @fail_max.setter
    def fail_max(self, number):
        """
        Sets the maximum `number` of failures tolerated before the circuit is opened.
        """
        self._fail_max = number

    @property
    def timeout_duration(self):
        """
        Once this circuit breaker is opened, it should remain opened until the timeout period elapses.
        """
        return self._timeout_duration

    @timeout_duration.setter
    def timeout_duration(self, timeout: datetime):
        """
        Sets the timeout period this circuit breaker should be kept open.
        """
        self._timeout_duration = timeout

    def _create_new_state(self, new_state: str, prev_state=None, notify=False) -> 'CircuitBreakerState':
        """
        Return state object from state string, i.e.,
        'closed' -> <CircuitClosedState>
        """
        state_map = {
            STATE_CLOSED: CircuitClosedState,
            STATE_OPEN: CircuitOpenState,
            STATE_HALF_OPEN: CircuitHalfOpenState,
        }

        try:
            return state_map[new_state](self, prev_state=prev_state, notify=notify)
        except KeyError:
            msg = "Unknown state {!r}, valid states: {}"
            raise ValueError(msg.format(new_state, ', '.join(state_map)))

    @property
    def state(self):
        """
        Update (if needed) and returns the cached state object.
        """
        # Ensure cached state is up-to-date
        if self.current_state != self._state.name:
            # If cached state is out-of-date, that means that it was likely
            # changed elsewhere (e.g. another process instance). We still send
            # out a notification, informing others that this particular circuit
            # breaker instance noticed the changed circuit.
            self.state = self.current_state
        return self._state

    @state.setter
    def state(self, state_str):
        """
        Set cached state and notify listeners of newly cached state.
        """
        with self._lock:
            self._state = self._create_new_state(
                state_str, prev_state=self._state, notify=True)

    @property
    def current_state(self):
        """
        Returns a string that identifies the state of the circuit breaker as
        reported by the _state_storage. i.e., 'closed', 'open', 'half-open'.
        """
        return self._state_storage.state

    @property
    def excluded_exceptions(self) -> Tuple[type]:
        """
        Returns a tuple of the excluded exceptions, e.g., exceptions that should
        not be considered system errors by this circuit breaker.
        """
        return tuple(self._excluded_exception_types)

    def add_excluded_exception(self, exception: type):
        """
        Adds an exception to the list of excluded exceptions.
        """
        with self._lock:
            self._excluded_exception_types.append(exception)

    def add_excluded_exceptions(self, *exceptions):
        """
        Adds exceptions to the list of excluded exceptions.
        """
        for exc in exceptions:
            self.add_excluded_exception(exc)

    def remove_excluded_exception(self, exception: type):
        """
        Removes an exception from the list of excluded exceptions.
        """
        with self._lock:
            self._excluded_exception_types.remove(exception)

    def _inc_counter(self):
        """
        Increments the counter of failed calls.
        """
        self._state_storage.increment_counter()

    def is_system_error(self, exception: Exception):
        """
        Returns whether the exception 'exception' is considered a signal of
        system malfunction. Business exceptions should not cause this circuit
        breaker to open.

        It does this by making sure the given exception is not a subclass
        of the excluded exceptions.
        """
        exception_type = type(exception)
        return not issubclass(exception_type, tuple(self._excluded_exception_types))

    def call(self, func: Callable, *args, **kwargs):
        """
        Calls `func` with the given `args` and `kwargs` according to the rules
        implemented by the current state of this circuit breaker.
        """
        with self._lock:
            return self.state.call(func, *args, **kwargs)

    async def call_async(self, func: Callable[..., Coroutine], *args, **kwargs):
        with self._lock:
            return await self.state.call_async(func, *args, **kwargs)

    def open(self):
        """
        Opens the circuit, e.g., the following calls will immediately fail
        until timeout elapses.
        """
        with self._lock:
            self.state = self._state_storage.state = STATE_OPEN

    def half_open(self):
        """
        Half-opens the circuit, e.g. lets the following call pass through and
        opens the circuit if the call fails (or closes the circuit if the call
        succeeds).
        """
        with self._lock:
            self.state = self._state_storage.state = STATE_HALF_OPEN

    def close(self):
        """
        Closes the circuit, e.g. lets the following calls execute as usual.
        """
        with self._lock:
            self.state = self._state_storage.state = STATE_CLOSED

    def __call__(self, *call_args, **call_kwargs):
        """
        Returns a wrapper that calls the function `func` according to the rules
        implemented by the current state of this circuit breaker.
        """

        def _outer_wrapper(func):

            @wraps(func)
            def _inner_wrapper(*args, **kwargs):
                return self.call(func, *args, **kwargs)

            @wraps(func)
            async def _inner_wrapper_async(*args, **kwargs):
                return await self.call_async(func, *args, **kwargs)

            return _inner_wrapper_async if inspect.iscoroutinefunction(func) else _inner_wrapper

        if call_args:
            return _outer_wrapper(*call_args)
        return _outer_wrapper

    @property
    def listeners(self):
        """
        Returns the registered listeners as a tuple.
        """
        return tuple(self._listeners)

    def add_listener(self, listener):
        """
        Registers a listener for this circuit breaker.
        """
        with self._lock:
            self._listeners.append(listener)

    def add_listeners(self, *listeners):
        """
        Registers listeners for this circuit breaker.
        """
        for listener in listeners:
            self.add_listener(listener)

    def remove_listener(self, listener):
        """
        Unregisters a listener of this circuit breaker.
        """
        with self._lock:
            self._listeners.remove(listener)

    @property
    def name(self):
        """
        Returns the name of this circuit breaker. Useful for logging.
        """
        return self._name

    @name.setter
    def name(self, name):
        """
        Set the name of this circuit breaker.
        """
        self._name = name


class CircuitBreakerState:
    """
    Implements the behavior needed by all circuit breaker states.
    """

    def __init__(self, breaker: CircuitBreaker, name: str):
        """
        Creates a new instance associated with the circuit breaker `cb` and
        identified by `name`.
        """
        self._breaker = breaker
        self._name = name

    @property
    def name(self):
        """
        Returns a human friendly name that identifies this state.
        """
        return self._name

    def _handle_error(self, exception: Exception):
        """
        Handles a failed call to the guarded operation.
        """
        if self._breaker.is_system_error(exception):
            self._breaker._inc_counter()
            for listener in self._breaker.listeners:
                listener.failure(self._breaker, exception)
            self.on_failure(exception)
        else:
            self._handle_success()
        raise exception

    def _handle_success(self):
        """
        Handles a successful call to the guarded operation.
        """
        self._breaker._state_storage.reset_counter()
        self.on_success()
        for listener in self._breaker.listeners:
            listener.success(self._breaker)

    def call(self, func: Callable, *args, **kwargs):
        """
        Calls `func` with the given `args` and `kwargs`, and updates the
        circuit breaker state according to the result.
        """
        ret = None

        self.before_call(func, *args, **kwargs)
        for listener in self._breaker.listeners:
            listener.before_call(self._breaker, func, *args, **kwargs)

        try:
            ret = func(*args, **kwargs)
            if isinstance(ret, types.GeneratorType):
                return self.generator_call(ret)
        except Exception as e:
            self._handle_error(e)
        else:
            self._handle_success()
        return ret

    async def call_async(self, func: Callable[..., Coroutine], *args, **kwargs):

        ret = None
        self.before_call(func, *args, **kwargs)
        for listener in self._breaker.listeners:
            listener.before_call(self._breaker, func, *args, **kwargs)

        try:
            ret = await func(*args, **kwargs)
            if isinstance(ret, types.GeneratorType):
                return self.generator_call(ret)
        except Exception as e:
            self._handle_error(e)
        else:
            self._handle_success()
        return ret

    def generator_call(self, wrapped_generator):
        try:
            value = yield next(wrapped_generator)
            while True:
                value = yield wrapped_generator.send(value)
        except StopIteration:
            self._handle_success()
            return
        except Exception as e:
            self._handle_error(e)

    def before_call(self, func, *args, **kwargs):
        """
        Override this method to be notified before a call to the guarded
        operation is attempted.
        """
        pass

    def on_success(self):
        """
        Override this method to be notified when a call to the guarded
        operation succeeds.
        """
        pass

    def on_failure(self, exception: Exception):
        """
        Override this method to be notified when a call to the guarded
        operation fails.
        """
        pass


class CircuitClosedState(CircuitBreakerState):
    """
    In the normal "closed" state, the circuit breaker executes operations as
    usual. If the call succeeds, nothing happens. If it fails, however, the
    circuit breaker makes a note of the failure.

    Once the number of failures exceeds a threshold, the circuit breaker trips
    and "opens" the circuit.
    """

    def __init__(self, breaker: CircuitBreaker, prev_state: Optional[CircuitBreakerState] = None, notify=False):
        """
        Moves the given circuit breaker `cb` to the "closed" state.
        """
        super().__init__(breaker, STATE_CLOSED)
        if notify:
            # We only reset the counter if notify is True, otherwise the CircuitBreaker
            # will lose it's failure count due to a second CircuitBreaker being created
            # using the same _state_storage object, or if the _state_storage objects
            # share a central source of truth (as would be the case with the redis
            # storage).
            self._breaker._state_storage.reset_counter()
            for listener in self._breaker.listeners:
                listener.state_change(self._breaker, prev_state, self)

    def on_failure(self, exception: Exception):
        """
        Moves the circuit breaker to the "open" state once the failures
        threshold is reached.
        """
        if self._breaker._state_storage.counter >= self._breaker.fail_max:
            self._breaker.open()
            raise CircuitBreakerError('Failures threshold reached, circuit breaker opened.') from exception


class CircuitOpenState(CircuitBreakerState):
    """
    When the circuit is "open", calls to the circuit breaker fail immediately,
    without any attempt to execute the real operation. This is indicated by the
    ``CircuitBreakerError`` exception.

    After a suitable amount of time, the circuit breaker decides that the
    operation has a chance of succeeding, so it goes into the "half-open" state.
    """

    def __init__(self, breaker, prev_state=None, notify=False):
        """
        Moves the given circuit breaker `cb` to the "open" state.
        """
        super().__init__(breaker, STATE_OPEN)
        self._breaker._state_storage.opened_at = datetime.utcnow()
        if notify:
            for listener in self._breaker.listeners:
                listener.state_change(self._breaker, prev_state, self)

    def before_call(self, func, *args, **kwargs):
        """
        After the timeout elapses, move the circuit breaker to the "half-open"
        state; otherwise, raises ``CircuitBreakerError`` without any attempt
        to execute the real operation.
        """
        timeout = self._breaker.timeout_duration
        opened_at = self._breaker._state_storage.opened_at
        if opened_at and datetime.utcnow() < opened_at + timeout:
            error_msg = 'Timeout not elapsed yet, circuit breaker still open'
            raise CircuitBreakerError(error_msg)

    def call(self, func, *args, **kwargs):
        """
        Call before_call to check if the breaker should close and open it if it passes.
        """
        self.before_call(func, *args, **kwargs)
        self._breaker.half_open()
        return self._breaker.call(func, *args, **kwargs)

    async def call_async(self, func: Callable[..., Coroutine], *args, **kwargs):
        """
        Call before_call to check if the breaker should close and open it if it passes.
        """
        self.before_call(func, *args, **kwargs)
        self._breaker.half_open()
        return await self._breaker.call_async(func, *args, **kwargs)


class CircuitHalfOpenState(CircuitBreakerState):
    """
    In the "half-open" state, the next call to the circuit breaker is allowed
    to execute the dangerous operation. Should the call succeed, the circuit
    breaker resets and returns to the "closed" state. If this trial call fails,
    however, the circuit breaker returns to the "open" state until another
    timeout elapses.
    """

    def __init__(self, breaker, prev_state=None, notify=False):
        """
        Moves the given circuit breaker to the "half-open" state.
        """
        super().__init__(breaker, STATE_HALF_OPEN)
        if notify:
            for listener in self._breaker._listeners:
                listener.state_change(self._breaker, prev_state, self)

    def on_failure(self, exception: Exception):
        """
        Opens the circuit breaker.
        """
        self._breaker.open()
        raise CircuitBreakerError('Trial call failed, circuit breaker opened.') from exception

    def on_success(self):
        """
        Closes the circuit breaker.
        """
        self._breaker.close()


class CircuitBreakerListener:
    """
    Listener class used to plug code to a CircuitBreaker instance when certain events happen.

    todo async listener handlers
    """

    def before_call(self, breaker: CircuitBreaker, func: Callable, *args, **kwargs) -> None:
        """
        Called before a function is executed over a breaker.
        :param breaker: The breaker that is used.
        :param func: The function that is called.
        :param args: The args to the function.
        :param kwargs: The kwargs to the function.
        """
        pass

    def failure(self, breaker: CircuitBreaker, exception: Exception) -> None:
        """
        Called when a function executed over the circuit breaker 'breaker' fails.
        """
        pass

    def success(self, breaker: CircuitBreaker) -> None:
        """
        Called when a function executed over the circuit breaker 'breaker' succeeds.
        """
        pass

    def state_change(self, breaker: CircuitBreaker, old: CircuitBreakerState, new: CircuitBreakerState) -> None:
        """
        Called when the state of the circuit breaker 'breaker' changes.
        """
        pass


class CircuitBreakerError(Exception):
    """
    When calls to a service fails because the circuit is open, this error is
    raised to allow the caller to handle this type of exception differently.
    """
    pass
