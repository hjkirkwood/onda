#    This file is part of OnDA.
#
#    OnDA is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    OnDA is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with OnDA.  If not, see <http://www.gnu.org/licenses/>.
'''
An MPI-based parallelization engine.

This module contains an implementation of an MPI-based parallelization
engine which uses a Master-Worker architecture. The engine is implemented
in the ParallelizationEngine class, which can be subclassed to create an
OnDA monitor.
'''

from __future__ import (absolute_import, division, print_function,
                        unicode_literals)

import importlib
import sys

from future.utils import raise_from
from mpi4py import MPI

from onda.utils import exceptions, parameters


# Define some labels for internal MPI communication (just some syntactic
# sugar).
_NOMORE = 998
_DIETAG = 999
_DEADTAG = 1000

# Import the facility layer.
_FACILITY_LAYER = importlib.import_module(
    'onda.facility_layer.{0}'.format(
        parameters.get_param(
            section='Onda',
            parameter='facility_layer',
            type_=str,
            required=True
        )
    )
)

# Import the detector layer.
_DETECTOR_LAYER = importlib.import_module(
    'onda.detector_layer.{0}'.format(
        parameters.get_param(
            section='Onda',
            parameter='detector_layer',
            type_=str,
            required=True
        )
    )
)


class ParallelizationEngine(object):
    '''
    An MPI-based master-worker parallelization engine for OnDA.

    An MPI-based parallelization engine using a master-worker
    architecture. The parallelization engine gets initialized with a data
    source (a python generator) and two functions (map_func and reduced_func)
    to be executed on the worker and the master nodes (respectively)

    Each worker node recovers a data "event" by getting a value from the data
    source. It then executes the map_func function on the recovered data.
    The dictionary returned by the map_func function is then transfered to the
    master node, which executes the reduce_func function every time it
    receives data from a worker node.

        Attributes:

        role (str): 'worker' or 'master', depending the role of the node where
            the attribute is read.

        rank (int): rank (in MPI terms) of the node where the attribute is
            read.

        data (dict): dictionary containing the data extracted from the last
            processed event. Set to None when no event has been processed yet.
    '''

    def __init__(self, map_func, reduce_func, source):
        '''ParallelizationEngine class initialization.

        Initializes the ParallelizationEngine class.

        Args:

            map_func (function): function to be executed on each worker node
               after recovering a data event

            reduce_func (function): function to be executed on the master node
                every time something is received from a worker node

            source (function): a python generator function from which
                worker nodes can recover data (by iterating over it)
        '''

        # Instrospect role of the node, then set the "role", the 'rank'
        # and the "mpi_size" attributes.
        self._mpi_size = MPI.COMM_WORLD.Get_size()
        self.rank = MPI.COMM_WORLD.Get_rank()
        if self.rank == 0:
            self.role = 'master'
        else:
            self.role = 'worker'

        # Store input functions and the data source as private attributes.
        self._map = map_func
        self._reduce = reduce_func
        self._source = source

        # Perform master-node specific initialization
        if self.role == 'master':

            # Initialize counter that keeps track of how many worker nodes
            # have already shut down.
            self._num_nomore = 0

            # Initialize counter that keeps track of how many events have
            # been processed.
            self._num_reduced_events = 0

        # Perform worker-node specific initialization.
        if self.role == 'worker':

            # Import functions from the facility layer.
            self._event_generator = getattr(
                object=_FACILITY_LAYER,
                name='event_generator'
            )

            self._reject_filter = getattr(
                object=_FACILITY_LAYER,
                name='reject_filter'
            )

            self._create_event_data = getattr(
                object=_FACILITY_LAYER,
                name='reject_filter'
            )

            # Import functions from the detector layer.
            self._open_event = getattr(
                object=_DETECTOR_LAYER,
                name='open_event'
            )

            self._close_event = getattr(
                object=_DETECTOR_LAYER,
                name='close_event'
            )

            self._get_num_frames_in_event = getattr(
                object=_DETECTOR_LAYER,
                name='get_num_frames_in_event'
            )

            # Create the attribute that will hold the data extraction
            # functions.
            self._data_extr_funcs = None

            # Create the attribute that will contain the data extracted from
            # each frame
            self.data = None

            # Create the attribute that will hold the data that to be
            # sent to the worker node.
            self._buffer = None

            # Read from the configuration file the number of frames that
            # must be processed from each event.
            self._num_frames_in_event_to_process = parameters.get_param(
                section='General',
                parameter='nun_frames_in_event_to_process',
                type_=str,
                required=True
            )

        return

    def shutdown(self, msg='Reason not provided.'):
        '''
        Shut down the parallelization engine.

        When called on a worker node, communicate to the master node that
        the you s shutting down, then shut down. When called on the master
        node, tell each worker node that it needs to shut down, wait for all
        the workers to confirm that they have indeed shut down, then shut
        yourself down.

        Args:

            msg (:obj:`str`, optional): reason for shutting down the
                parallelization engine. Defaults to 'Reason not provided'.
        '''

        # Print message to inform the user that the engine is shutting down.
        print('Shutting down:', msg)
        sys.stdout.flush()

        # If the function is called on a worker node, tell the master node
        # that you are shutting it down, then shut down.
        if self.role == 'worker':
            self._buffer = MPI.COMM_WORLD.send(
                dest=0,
                tag=_DEADTAG
            )
            MPI.Finalize()
            exit(0)

        if self.role == 'master':

            try:

                # If the function is called on the master node, tell all the
                # worker node to shut down.
                for nod_num in range(1, self._mpi_size()):
                    MPI.COMM_WORLD.isend(
                        0,
                        dest=nod_num,
                        tag=_DIETAG
                    )
                # Wait for all the worker nodes to confirm that they are
                # shutting down.
                num_shutdown_confirm = 0

                # Keep receiving messages until all the worker nodes confirm
                # that they are shutting down.
                while True:

                    # Keep receiving messages to empty the message queue.
                    if MPI.COMM_WORLD.Iprobe(
                            source=MPI.ANY_SOURCE,
                            tag=0
                    ):
                        self._buffer = MPI.COMM_WORLD.recv(
                            source=MPI.ANY_SOURCE,
                            tag=0
                        )

                    # If a worker nodes confirms that it is shutting down,
                    # keep track of how many have already confirmed.
                    if MPI.COMM_WORLD.Iprobe(
                            source=MPI.ANY_SOURCE,
                            tag=_DEADTAG
                    ):
                        num_shutdown_confirm += 1

                    # If all the worker nodes confirmed that they shut down,
                    # stop listening.
                    if num_shutdown_confirm == self._mpi_size() - 1:
                        break

                # Shut down
                MPI.Finalize()
                exit(0)
            except RuntimeError:

                # In case of error in the clean shut down procedure,
                # crash hard!
                MPI.COMM_WORLD.Abort(0)

            exit(0)

    def start(self, verbose=False):
        '''
        Start the parallelization engine.

        Start the parallelization engine. Worker nodes will start recovering
        data and processing it, while the master node will start listening
        for communications from the worker nodes.

        Args:

            verbose (:obj:`bool`, optional): if True, additional debugging
                information is printed to the console. Defaults to False.
        '''

        # Execute the following on each worker node.
        if self.role == 'worker':

            req = None

            # Call the function that prepares the data extraction functions.
            self._prepare_data_extraction_functions()

            # Initialize the event generator.
            events = self._event_generator(
                source=self._source,
                node_rank=self.rank,
                mpi_pool_size=self._mpi_size
            )

            # Start processing events.
            for event in events:

                # Listen for requests to shut down coming from the master
                # node.
                if MPI.COMM_WORLD.Iprobe(
                        source=0,
                        tag=_DIETAG
                ):
                    self.shutdown(
                        'Shutting down RANK: {0}.'.format(self.rank)
                    )

                # Check if the event should be rejected. If it does, skip
                # to next event iteration of the loop.
                if self._reject_filter(event):
                    continue

                # Recover the number of frames in the event.
                n_frames = self._get_num_frames_in_event(event)

                # If the number of frames that should be processed is higher
                # than the number of frames in the event, limit the number
                # of frames that should be processed.
                if n_frames < self._num_frames_in_event_to_process:
                    self._num_frames_in_event_to_process = n_frames

                # Open the event (a file, a data structure).
                opened_event = self._open_event(event)

                # Iterate over the frames that should be processed. Assuming
                # than n is the number of frames to be processed, the last n
                # frames is the event are processed, in the order in which
                # they appear in the event.
                for frame_offset in range(
                        -self._num_frames_in_event_to_process,
                        0
                ):

                    # Create an event_data structure, that joins the event
                    # data with some metadata.
                    event_data = self._create_event_data(
                        event=opened_event,
                        frame_offset=frame_offset
                    )

                    try:
                        # Pass the event_data structure to the extract
                        # function.
                        self.data = self._extract_data(event_data)
                    except exceptions.DataExtractionError as exc:
                        # If the extract_data function fails to extract the
                        # data, drop the event and skip to the next iteration
                        # in the loop.
                        print(
                            'OnDA Warning: Cannot interpret some event data: '
                            '{}. Skipping event....'.format(exc)
                        )
                        continue

                    # Pass the extracted data to the processing function.
                    result = self._map(self.data)

                    # Send the result of the processing to the master node.
                    if req:
                        req.Wait()
                    req = MPI.COMM_WORLD.isend(
                        buf=result,
                        dest=0,
                        tag=0
                    )

                # Wait for messages that might still be in delivery by MPI.
                if req:
                    req.Wait()

                # Close the event.
                self._close_event(event)

            # After finishing iterating over the events to process, tell
            # the master node that you finished processing.
            end_dict = {'end': True}

            # Send the message to the master node.
            if req:
                req.Wait()
            MPI.COMM_WORLD.isend(
                buf=(end_dict, self.rank),
                dest=0,
                tag=0
            )

            # Shut down
            MPI.Finalize()
            exit(0)

        # Execute the following on the master node.
        elif self.role == 'master':

            # Print to the console that you are starting.
            if verbose:
                print('Starting master.')
                sys.stdout.flush()

            # Initialize the counter of processed events.
            self._num_reduced_events = 0

            # Loop continuously waiting for processed data from workers.
            while True:

                try:

                    # Receive a message from a worker node.
                    buffer_data = MPI.COMM_WORLD.recv(
                        source=MPI.ANY_SOURCE,
                        tag=0
                    )

                    # If the message announces that the worker node is
                    # shutting down, keep track of how many worker nodes
                    # have shut down.
                    if 'end' in buffer_data[0].keys():
                        print(
                            'Finalizing {}'.format(buffer_data[1])
                        )
                        self._num_nomore += 1

                        # If all worker nodes have shut down, shut yourself
                        # down too.
                        if self._num_nomore == self._mpi_size - 1:
                            print('All workers have run out of events.')
                            print('Shutting down.')
                            sys.stdout.flush()
                            self.end_processing()
                            MPI.Finalize()
                            exit(0)

                    # Execute the collecting function on the received data
                    self._reduce(buffer_data)

                    # Keep track of how many events have been processed
                    self._num_reduced_events += 1

                # If the use stops the parallelization engine volunterily,
                # clean up and shut down.
                except KeyboardInterrupt as exc:
                    print('Recieved keyboard sigterm...')
                    print(str(exc))
                    print('shutting down MPI.')
                    self.shutdown()
                    print('---> execution finished.')
                    sys.stdout.flush()
                    exit(0)

        return

    def end_processing(self):
        '''
        Execute end-of-processing actions.

        This function is called by the parallelization engine at the end of
        the processing. By default, it prints a message to the console and
        stops. The function can be overridden in a derived class to implement
        custom end-of-processing actions.
        '''
        print(
            'Processing finished. Processed {} events in total.'.format(
                self._num_reduced_events
            )
        )
        sys.stdout.flush()

    def _prepare_data_extraction_functions(self):
        # Recover the required data extraction functions from various layers.
        # Raises a MissingDataExtractionFunction if the extraction function
        # is not found in any layer.

        # Recover list of required data function from the parameter file.
        data_extraction_funcs = [
            x.strip() for x in parameters.get_param(
                section='Onda',
                parameter='required_data',
                type_=list,
                required=True
            )
        ]

        # Create list that will be returned.
        func_list = []

        # Iterate over the required functions and look for them in the detector
        # and facility layer.
        for func in data_extraction_funcs:
            try:
                func_list.append(getattr(object=_DETECTOR_LAYER, name=func))
            except AttributeError:
                try:
                    func_list.append(getattr(object=_DETECTOR_LAYER, name=func))
                except AttributeError:
                    raise_from(
                        exc=exceptions.MissingDataExtractionFunction(
                            'Data extraction function not defined for the'
                            'following data type: {0}'.format(func)
                        ),
                        cause=None
                    )

        # Set the data_extr_funcs attribute after converting the list of
        # recovered functions to a tuple.
        self._data_extr_funcs = tuple(func_list)

    def _extract_data(self, event):
        # Extract data and add it to a dictionary, by calling the
        # data extraction functions one after the other.
        # If any exception is raised in the data extraction functions,
        # catch it and raise a DataExtractionError exception.
        data = {}

        for func_name in self._data_extr_funcs:
            decorated_func_name = '_{}'.format(func_name)
            try:
                data[func_name] = globals()[decorated_func_name](event)
            except Exception as exc:
                raise exceptions.DataExtractionError(
                    'Error extracting {0}: {1}'.format(func_name, exc)
                )

        return data