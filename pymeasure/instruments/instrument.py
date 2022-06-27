#
# This file is part of the PyMeasure package.
#
# Copyright (c) 2013-2022 PyMeasure Developers
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.
#


import abc
import logging
import threading
from socket import error as socket_error
from time import sleep, time

import numpy as np

from pymeasure.adapters.visa import VISAAdapter

log = logging.getLogger(__name__)
log.addHandler(logging.NullHandler())


class DynamicProperty(property):
    """Class that allows managing python property behaviour in a "dynamic" fashion

    The class allows passing, in addition to regular property parameters, a list of
    runtime configurable parameters.
    The effect is that the behaviour of fget/fset not only depends on the obj parameter, but
    also on a set of keyword parameters with a default value.
    These extra parameters are read from instance, if available, or left with the default value.
    Dynamic behaviour is achieved by changing class or instance variables with special names
    defined as `<prefix> + <property name> + <param name>`.

    Code has been based on Python equivalent implementation of properties provided in the
    python documentation `here <https://docs.python.org/3/howto/descriptor.html#properties>`_.

    :param fget: class property fget parameter whose signature is expanded with a
                 set of keyword arguments as in fget_params_list
    :param fset: class property fget parameter whose signature is expanded with a
                 set of keyword arguments as in fset_params_list
    :param fdel: class property fdel parameter
    :param doc: class property doc parameter
    :param fget_params_list: List of parameter names that are dynamically configurable
    :param fset_params_list: List of parameter names that are dynamically configurable
    :param prefix: String to be prefixed to get dynamically configurable
                   parameters.
    """

    def __init__(
        self,
        fget=None,
        fset=None,
        fdel=None,
        doc=None,
        fget_params_list=None,
        fset_params_list=None,
        prefix="",
    ):
        super().__init__(fget, fset, fdel, doc)
        self.fget_params_list = () if fget_params_list is None else fget_params_list
        self.fset_params_list = () if fset_params_list is None else fset_params_list
        self.name = ""
        self.prefix = prefix

    def __get__(self, obj, objtype=None):
        if obj is None:
            # Property return itself when invoked from a class
            return self
        if self.fget is None:
            raise AttributeError(f"Unreadable attribute {self.name}")

        kwargs = {}
        for attr in self.fget_params_list:
            attr_instance_name = self.prefix + "_".join([self.name, attr])
            if hasattr(obj, attr_instance_name):
                kwargs[attr] = getattr(obj, attr_instance_name)
        return self.fget(obj, **kwargs)

    def __set__(self, obj, value):
        if self.fset is None:
            raise AttributeError(f"Can't set attribute {self.name}")
        kwargs = {}
        for attr in self.fset_params_list:
            attr_instance_name = self.prefix + "_".join([self.name, attr])
            if hasattr(obj, attr_instance_name):
                kwargs[attr] = getattr(obj, attr_instance_name)
        self.fset(obj, value, **kwargs)

    def __set_name__(self, owner, name):
        self.name = name


class Instrument:
    """The base class for all Instrument definitions.

    It makes use of one of the :py:class:`~pymeasure.adapters.Adapter` classes for communication
    with the connected hardware device. This decouples the instrument/command definition from the
    specific communication interface used.

    When ``adapter`` is a string, this is taken as an appropriate resource name. Depending on your
    installed VISA library, this can be something simple like ``COM1`` or ``ASRL2``, or a more
    complicated
    `VISA resource name <https://pyvisa.readthedocs.io/en/latest/introduction/names.html>`__
    defining the target of your connection.

    When ``adapter`` is an integer, a GPIB resource name is created based on that.
    In either case a :py:class:`~pymeasure.adapters.VISAAdapter` is constructed based on that
    resource name.
    Keyword arguments can be used to further configure the connection.

    Otherwise, the passed :py:class:`~pymeasure.adapters.Adapter` object is used and any keyword
    arguments are discarded.

    This class defines basic SCPI commands by default. This can be disabled with
    :code:`includeSCPI` for instruments not compatible with the standard SCPI commands.

    :param adapter: A string, integer, or :py:class:`~pymeasure.adapters.Adapter` subclass object
    :param string name: The name of the instrument. Often the model designation by default.
    :param includeSCPI: A boolean, which toggles the inclusion of standard SCPI commands
    :param \\**kwargs: In case ``adapter`` is a string or integer, additional arguments passed on
        to :py:class:`~pymeasure.adapters.VISAAdapter` (check there for details).
        Discarded otherwise.
    """

    # Variable holding the list of DynamicProperty parameters that are configurable
    # by users
    _fget_params_list = (
        "get_command",
        "values",
        "map_values",
        "get_process",
        "command_process",
    )

    _fset_params_list = (
        "set_command",
        "validator",
        "values",
        "map_values",
        "set_process",
        "command_process",
    )

    # Prefix used to store reserved variables
    __reserved_prefix = "___"

    @property
    @abc.abstractmethod
    def id_starts_with(self):
        ...

    def connect(self, temp_adapter, kwargs):
        return VISAAdapter(temp_adapter, **kwargs)

    # noinspection PyPep8Naming
    def __init__(self, adapter, name, includeSCPI=True, **kwargs):
        try:
            if isinstance(adapter, (int, str)):
                adapter = self.connect(adapter, **kwargs)
        except ImportError:
            raise Exception(
                "Invalid Adapter provided for Instrument since " "PyVISA is not present"
            )

        self.name = name
        self.SCPI = includeSCPI
        self.adapter = adapter
        self._lock = threading.Lock()
        self._flush_errors()

        idn = self.get_id()
        if not idn.startswith(self.id_starts_with):
            log.warning(f"Could not retrieve ID for {self.adapter}")
            self.communication_success = False
        else:
            log.info(f"IDN: {idn}")
            log.info(f"Connected to {self.name}")
            self.communication_success = True

        self.isShutdown = False
        self._special_names = self._setup_special_names()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.shutdown()

    def _setup_special_names(self):
        """Return list of class/instance special names

        Compute the list of special names based on the list of
        class attributes that are a DynamicProperty. Check also for class variables
        with special name and copy them at instance level
        Internal method, not intended to be accessed at user level."""
        special_names = []
        dynamic_params = tuple(set(self._fget_params_list + self._fset_params_list))
        # Check whether class variables of DynamicProperty type are present
        for obj in (self,) + self.__class__.__mro__:
            for attr_name, attr in obj.__dict__.items():
                if isinstance(attr, DynamicProperty):
                    special_names += [attr_name + "_" + key for key in dynamic_params]
        # Check if special variables are defined at class level
        for obj in (self,) + self.__class__.__mro__:
            for attr in obj.__dict__:
                if attr in special_names:
                    # Copy class special variable at instance level, prefixing reserved_prefix
                    setattr(self, self.__reserved_prefix + attr, obj.__dict__[attr])
        return special_names

    def __setattr__(self, name, value):
        """Add reserved_prefix in front of special variables"""
        if hasattr(self, "_special_names"):
            if name in self._special_names:
                name = self.__reserved_prefix + name
        super().__setattr__(name, value)

    def __getattribute__(self, name):
        """Prevent read access to variables with special names used to
        support dynamic property behaviour"""
        if name in ("_special_names", "__dict__"):
            return super().__getattribute__(name)
        if hasattr(self, "_special_names"):
            if name in self._special_names:
                raise AttributeError(
                    f"{name} is a reserved variable name and it cannot be read"
                )
        return super().__getattribute__(name)

    @property
    def complete(self):
        """This property allows synchronization between a controller and a device. The Operation Complete
        query places an ASCII character 1 into the device's Output Queue when all pending
        selected device operations have been finished.
        """
        if self.SCPI:
            ready = self.ask_no_lock("*OPC?").strip()
            if ready == "1":
                return True
            elif ready == "0":
                return False
            else:
                return None
        else:
            raise NotImplementedError(
                "Non SCPI instruments require implementation in subclasses"
            )

    @property
    def status(self):
        """Checks the status of the system.

        Returns:
            "ok: busy" if the threading lock cannot be acquired, "ok: ON"
            if the system is on and the lock was acquired. If communication
            was not initially successfull, the system is queried again and
            the ID is checked. If the system does not return an expected response,
            status is set to warning: Cannot communicate with device".
        """
        if not self.communication_success:
            id = self.get_id(check_for_errors=False)

            if id.startswith(self.id_starts_with):
                curr_status = "ok: ON"
                self.communication_success = True
            else:
                curr_status = "warning: Cannot communicate with device"
        else:
            if self._lock.locked():
                curr_status = "ok: busy"
            else:
                curr_status = "ok: ON"

        return curr_status

    @property
    def options(self):
        """Requests and returns the device options installed."""
        if self.SCPI:
            return self.ask("*OPT?").strip()
        else:
            raise NotImplementedError(
                "Non SCPI instruments require implementation in subclasses"
            )

    def get_id(self, check_errs=True):
        """Requests and returns the identification of the instrument."""
        if self.SCPI:
            return self.ask("*IDN?", check_errs).strip()
        else:
            raise NotImplementedError(
                "Non SCPI instruments require implementation in subclasses"
            )

    def _wait_until_ready(self, timeout=10):
        """Polls the system busy parameter until the device is ready to execute a new
        operation. This method must be called after _lock has been acquired by the
        respective process.

        Args:
            timeout: The number of seconds to wait before raising a TimeoutError.

        """
        end = time() + timeout
        while not self.complete:
            if time() > end:
                raise TimeoutError(
                    f"The operation did not complete in the timeout specified ({timeout} s)"
                )
            sleep(0.1)

    # Wrapper functions for the Adapter object
    def ask(self, command, check_for_errors=True):
        """Writes the command to the instrument through the adapter
        and returns the read response.

        :param command: command string to be sent to the instrument
        :param check_for_errors Flag indicating if error checking should be performed
        """
        with self._lock:
            if check_for_errors:
                self._wait_until_ready()

            response = self.adapter.ask(command)

            if check_for_errors:
                self.check_errors()
        return response

    def ask_no_lock(self, command):
        return self.adapter.ask(command)

    def write(self, command):
        """Writes the command to the instrument through the adapter.

        :param command: command string to be sent to the instrument
        """
        with self._lock:
            self._wait_until_ready()
            self.adapter.write(command)
            self.check_errors()

    def read(self):
        """Reads from the instrument through the adapter and returns the
        response.
        """
        with self._lock:
            self._wait_until_ready()
            response = self.adapter.read()
            self.check_errors()
        return response

    def values(self, command, **kwargs):
        """Reads a set of values from the instrument through the adapter,
        passing on any key-word arguments.
        """
        with self._lock:
            self._wait_until_ready()
            response = self.adapter.values(command, **kwargs)
            self.check_errors()
        return response

    def binary_values(self, command, header_bytes=0, dtype=np.float32):
        with self._lock:
            self._wait_until_ready()
            response = self.adapter.binary_values(command, header_bytes, dtype)
            self.check_errors()
        return response

    def check_errors(self):

        errors = self._flush_errors()

        # Check if the errors list is empty
        if errors:
            raise RuntimeError(
                f"Error read from error queue. First error read from the error queue is: {errors[0]}\nSee logs for more details"
            )

    def _flush_errors(self):
        """Flushs the system's error queue and logs all errors. This method must be called
        after _lock has been acquired by the respective process.

        Returns:
            List of strings giving all the errors read from the error queue - each element
            contains an error code with the respective error description. If no errors are
            read from the error queue, an empty list is returned.
        """
        if self.SCPI:
            self._wait_until_ready()
            errors = []
            while True:
                current_err = self.ask_no_lock("SYST:ERR?")
                if not (current_err.startswith("+0")):
                    log.error(f"Error read from error queue: {current_err}")
                    errors.append(current_err)
                else:
                    break
            return errors
        else:
            raise NotImplementedError(
                "Non SCPI instruments require implementation in subclasses"
            )

    # flake8: noqa: C901
    @staticmethod
    def control(
        get_command,  # noqa: C901 accept that this is a complex method
        set_command,
        docs,
        validator=lambda v, vs: v,
        values=(),
        map_values=False,
        get_process=lambda v: v,
        set_process=lambda v: v,
        command_process=lambda c: c,
        dynamic=False,
        **kwargs,
    ):
        """Returns a property for the class based on the supplied
        commands. This property may be set and read from the
        instrument. See also :meth:`measurement` and :meth:`setting`.

        :param get_command: A string command that asks for the value, set to `None`
            if get is not supported (see also :meth:`setting`).
        :param set_command: A string command that writes the value, set to `None`
            if set is not supported (see also :meth:`measurement`).
        :param docs: A docstring that will be included in the documentation
        :param validator: A function that takes both a value and a group of valid values
            and returns a valid value, while it otherwise raises an exception
        :param values: A list, tuple, range, or dictionary of valid values, that can be used
            as to map values if :code:`map_values` is True.
        :param map_values: A boolean flag that determines if the values should be
            interpreted as a map
        :param get_process: A function that take a value and allows processing
            before value mapping, returning the processed value
        :param set_process: A function that takes a value and allows processing
            before value mapping, returning the processed value
        :param command_process: A function that takes a command and allows processing
            before executing the command
        :param dynamic: Specify whether the property parameters are meant to be changed in
            instances or subclasses.

        Example of usage of dynamic parameter is as follows:

        .. code-block:: python

            class GenericInstrument(Instrument):
                center_frequency = Instrument.control(
                    ":SENS:FREQ:CENT?;", ":SENS:FREQ:CENT %e GHz;",
                    " A floating point property that represents the frequency ... ",
                    validator=strict_range,
                    # Redefine this in subclasses to reflect actual instrument value:
                    values=(1, 20),
                    dynamic=True  # enable changing property parameters on-the-fly
                )

            class SpecificInstrument(GenericInstrument):
                # Identical to GenericInstrument, except for frequency range
                # Override the "values" parameter of the "center_frequency" property
                center_frequency_values = (1, 10) # Redefined at subclass level

            instrument = SpecificInstrument()
            instrument.center_frequency_values = (1, 6e9) # Redefined at instance level

        .. warning:: Unexpected side effects when using dynamic properties

        Users must pay attention when using dynamic properties, since definition of class and/or
        instance attributes matching specific patterns could have unwanted side effect.
        The attribute name pattern `property_param`, where `property` is the name of the dynamic
        property (e.g. `center_frequency` in the example) and `param` is any of this method
        parameters name except `dynamic` and `docs` (e.g. `values` in the example) has to be
        considered reserved for dynamic property control.
        """

        def fget(
            self,
            get_command=get_command,
            values=values,
            map_values=map_values,
            get_process=get_process,
            command_process=command_process,
        ):
            if get_command is None:
                raise LookupError("Instrument property can not be read.")
            vals = self.values(command_process(get_command), **kwargs)
            if len(vals) == 1:
                value = get_process(vals[0])
                if not map_values:
                    return value
                elif isinstance(values, (list, tuple, range)):
                    return values[int(value)]
                elif isinstance(values, dict):
                    for k, v in values.items():
                        if v == value:
                            return k
                    raise KeyError(f"Value {value} not found in mapped values")
                else:
                    raise ValueError(
                        "Values of type `{}` are not allowed "
                        "for Instrument.control".format(type(values))
                    )
            else:
                vals = get_process(vals)
                return vals

        def fset(
            self,
            value,
            set_command=set_command,
            validator=validator,
            values=values,
            map_values=map_values,
            set_process=set_process,
            command_process=command_process,
        ):

            if set_command is None:
                raise LookupError("Instrument property can not be set.")

            value = set_process(validator(value, values))
            if not map_values:
                pass
            elif isinstance(values, (list, tuple, range)):
                value = values.index(value)
            elif isinstance(values, dict):
                value = values[value]
            else:
                raise ValueError(
                    "Values of type `{}` are not allowed "
                    "for Instrument.control".format(type(values))
                )
            self.write(command_process(set_command) % value)

        # Add the specified document string to the getter
        fget.__doc__ = docs

        if dynamic:
            fget.__doc__ += "(dynamic)"
            return DynamicProperty(
                fget=fget,
                fset=fset,
                fget_params_list=Instrument._fget_params_list,
                fset_params_list=Instrument._fset_params_list,
                prefix=Instrument.__reserved_prefix,
            )
        else:
            return property(fget, fset)

    @staticmethod
    def measurement(
        get_command,
        docs,
        values=(),
        map_values=None,
        get_process=lambda v: v,
        command_process=lambda c: c,
        dynamic=False,
        **kwargs,
    ):
        """Returns a property for the class based on the supplied
        commands. This is a measurement quantity that may only be
        read from the instrument, not set.

        :param get_command: A string command that asks for the value
        :param docs: A docstring that will be included in the documentation
        :param values: A list, tuple, range, or dictionary of valid values, that can be used
            as to map values if :code:`map_values` is True.
        :param map_values: A boolean flag that determines if the values should be
            interpreted as a map
        :param get_process: A function that take a value and allows processing
            before value mapping, returning the processed value
        :param command_process: A function that take a command and allows processing
            before executing the command, for getting
        :param dynamic: Specify whether the property parameters are meant to be changed in
            instances or subclasses. See :meth:`control` for an usage example.
        """

        return Instrument.control(
            get_command=get_command,
            set_command=None,
            docs=docs,
            values=values,
            map_values=map_values,
            get_process=get_process,
            command_process=command_process,
            dynamic=dynamic,
            **kwargs,
        )

    @staticmethod
    def setting(
        set_command,
        docs,
        validator=lambda x, y: x,
        values=(),
        map_values=False,
        set_process=lambda v: v,
        dynamic=False,
        **kwargs,
    ):
        """Returns a property for the class based on the supplied
        commands. This property may be set, but raises an exception
        when being read from the instrument.

        :param set_command: A string command that writes the value
        :param docs: A docstring that will be included in the documentation
        :param validator: A function that takes both a value and a group of valid values
            and returns a valid value, while it otherwise raises an exception
        :param values: A list, tuple, range, or dictionary of valid values, that can be used
            as to map values if :code:`map_values` is True.
        :param map_values: A boolean flag that determines if the values should be
            interpreted as a map
        :param set_process: A function that takes a value and allows processing
            before value mapping, returning the processed value
        :param dynamic: Specify whether the property parameters are meant to be changed in
            instances or subclasses. See :meth:`control` for an usage example.
        """

        return Instrument.control(
            get_command=None,
            set_command=set_command,
            docs=docs,
            validator=validator,
            values=values,
            map_values=map_values,
            set_process=set_process,
            dynamic=dynamic,
            **kwargs,
        )

    def clear(self):
        """Clears the instrument status byte"""
        if self.SCPI:
            self.write("*CLS")
        else:
            raise NotImplementedError(
                "Non SCPI instruments require implementation in subclasses"
            )

    def reset(self):
        """Resets the instrument."""
        if self.SCPI:
            self.write("*RST")
        else:
            raise NotImplementedError(
                "Non SCPI instruments require implementation in subclasses"
            )

    def shutdown(self):
        """Brings the instrument to a safe and stable state"""
        self.isShutdown = True
        log.info("Shutting down %s" % self.name)
