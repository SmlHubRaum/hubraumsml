## @file ensclient.py ENS Client Runtime Library Python API
#
# Project Edge
# Copyright (C) 2016-17  Deutsche Telekom Capital Partners Strategic Advisory LLC
#

import logging
import asyncore, socket
import requests
import struct
import time
import threading
from queue import Queue
import re
import uuid
import os
import sys


# Constants

# URIs
# 'DISCOVERY_ENDPOINT/api/v1.0/discover/<DEVELOPER_ID>/<APP_ID>?sdkversion=<SDK_VERSION>'
DISCOVERY_URL_PATTERN = '{}/api/v1.0/discover/{}/{}?sdkversion={}'
# 'APP_CLOUD_ENDPOINT/api/v1.0/app_cloud/<DEVELOPER_ID>/<APP_ID>/<CLOUDLET_ID>/<CLIENT_ID>'
APP_CREATE_URL_PATTERN = '{}/api/v1.0/app_cloud/{}/{}/{}/{}'
# 'APP_CLOUD_ENDPOINT/api/v1.0/app_cloud/<DEVELOPER_ID>/<APP_ID>/<CLOUDLET_ID>/<CLIENT_ID>/<DEPLOYMENT_ID>'
APP_DELETE_URL_PATTERN = '{}/api/v1.0/app_cloud/{}/{}/{}/{}/{}'

# Counts
PROBE_MAX_COUNT = 10

# Probe max try period
PROBE_MAX_PERIOD = 1

# Global params
NO_RTT_DATA = -1
MSG_ENCODE_UTF8 = 'utf-8'

#JSON header
JSON_HEADER = {'content-type': 'application/json'}

#SDK Configuration file
SDK_CONFIG_FILENAME = 'mecsdk.conf'
#Mandatory parameters in configuration file
CONF_MANDATORY_PARAMS = ['DiscoveryURL', 'SdkVersion', 'ApiKey']

#Logger level
LOGGING_LEVEL = logging.DEBUG
#Logger format
logging.basicConfig(level=LOGGING_LEVEL,
                    format='%(asctime)-15s %(levelname)-8s %(filename)-16s %(lineno)4d %(message)s')


#Simulated enumeration function
def enum(**named_values):
    return type('Enum', (), named_values)

HTTP_METHOD = enum(GET = 'GET', POST = 'POST', DELETE = 'DELETE',
    PUT = 'PUT', OPTIONS = 'OPTIONS', PATCH = 'PATCH')

TASK_STATE = enum(STARTED = 'STARTED', STOPPED = 'STOPPTED')

SDK_CONFIG_FILE = os.path.join(os.path.dirname(os.path.realpath(__file__)), SDK_CONFIG_FILENAME)


class ENSClientError(Exception):
    ## Exception thrown for ENS specific errors.
    def __init__(self, reason):
        self.reason = reason

    def __str__(self):
        return 'ENSClientError: {}'.format(self.reason)


class ENSEndpoint(object):
    def __init__(self, endpoint):
        m = re.match(r'^(tcp|udp|http|https)://\[?([0-9]+(?:\.[0-9]+){3}|[0-9a-fA-F]{4}(?:[\:]+[0-9a-fA-F]{4}){0,7}[\:]*|[a-zA-Z0-9\-\.]+)\]?:([0-9]+)$', endpoint)
        if not m:
            raise ENSClientError('Invalid endpoint {}'.format(endpoint))
        self.endpoint = m.group(0)
        self.protocol = m.group(1)
        self.host = m.group(2)
        self.port = int(m.group(3))
        if self.protocol == 'udp':
            self.sa = [r[4] for r in socket.getaddrinfo(self.host, self.port, 0, socket.SOCK_DGRAM) if r[0] == socket.AF_INET or r[0] == socket.AF_INET6]
        else:
            self.sa = [r[4] for r in socket.getaddrinfo(self.host, self.port, 0, socket.SOCK_STREAM) if r[0] == socket.AF_INET or r[0] == socket.AF_INET6]


## Class representing a FaaS session with a microservice instance hosted on the ENS platform.
#
#  This class should not be instantiated directly by client application.  Instead use the ENSClient.connect method to create a session.
#
class ENSSession(threading.Thread):

    # Data transfer message identifiers
    REQUEST  = 0
    NOTIFY   = 1
    RESPONSE = 2

    # Session lifecycle message identifiers
    START        = 10
    STARTED      = 11
    STOP         = 20
    DISCONNECTED = 21

    header = struct.Struct('>I I I')

    def __init__(self, app, cloudlet, interface, binding):
        threading.Thread.__init__(self)
        logging.info('Create ENSSession to interface {} on application {}'.format(interface, app))
        self.app = app
        self.cloudlet = cloudlet
        self.interface = interface
        self.binding = binding
        self.conn = None
        self.req_sqn = 0
        self.pending_req = {}
        self.notify_q = Queue()

    def run(self):
        while True:
            # Receive the next header and any associated data.
            data = self.conn.recv(ENSSession.header.size)
            logging.debug('Received header, length {}'.format(len(data)))
            if len(data) < ENSSession.header.size:
                break;

            length, msg_id, sqn = ENSSession.header.unpack(data)
            logging.debug('Received length {}, msg_id {}, sqn {}'.format(length, msg_id, sqn))

            if length > 0:
                logging.debug('Waiting for data, length = {}'.format(length))
                s = self.conn.recv(length)
                logging.debug('Received data, length = {}'.format(len(s)))
                if len(s) < length:
                    break;

            if msg_id == ENSSession.RESPONSE:
                # Response, so correlate to the pending request.
                logging.debug('Received response')
                if sqn in self.pending_req:
                    logging.debug('Correlated response, so unblock request sender')
                    waiter = self.pending_req[sqn]
                    waiter[1] = s
                    waiter[0].set()
                else:
                    logging.warn('Received unknown response (sqn={})'.format(sqn))
            elif msg_id == ENSSession.NOTIFY:
                # Notify, so add to the lists of pending
                logging.debug('Add Notify to queue')
                self.notify_q.put((sqn, s))
            else:
                logging.warn('Unknown message {}'.format(msg_id))

        logging.info('Receive loop terminated')
        self.conn.close()
        self.conn = None

    ## Connect to port in the interface binding.
    #
    def connect(self):
        logging.info('Connecting to ENS interface {} at {}'.format(self.interface, self.binding))
        try:
            eventEndpoint = ENSEndpoint(self.binding['endpoint'])
            sa = eventEndpoint.sa[0]
            self.conn = socket.create_connection(sa)
            self.conn.send(
                ENSSession.header.pack(
                    len(self.interface),
                    ENSSession.START, 0) +
                self.interface.encode(MSG_ENCODE_UTF8))
            rsp = ENSSession.header.unpack(self.conn.recv(ENSSession.header.size))

            logging.info('Session connected')
            self.start()
            return True
        except ENSClientError as e:
            logging.error('Invalid interface binding {}'.format(self.binding))
            return False
        except socket.error as e:
            logging.error('Failed to connect session: {}'.format(e))
            return False

    ## Sends a Request over the session and return the Response as a string.
    #
    #  @param  s           A string containing the request data.
    #  @return             A string containing the response data (or None if the request fails).
    def request(self, s):
        if self.conn:
            req_sqn = ++self.req_sqn
            waiter = [threading.Event(), None]
            self.pending_req[req_sqn] = waiter
            self.conn.sendall(ENSSession.header.pack(len(s), ENSSession.REQUEST, req_sqn) + s.encode(MSG_ENCODE_UTF8))
            waiter[0].wait()
            rsp = waiter[1]
            #Decode bytestream
            rsp = rsp.decode(MSG_ENCODE_UTF8)

            del self.pending_req[req_sqn]
            return rsp;
        else:
            return None

    ## Sends a Notify over the session.
    #
    #  @param  sqn         A sequence number.  This does not have to be increasing or even unique,
    #                      but can be used by the application to correlate or sequence notifys sent
    #                      in each direction.
    #  @param  s           A string containing the request data.
    #
    def notify(self, sqn, s):
        if self.conn:
            self.conn.sendall(ENSSession.header.pack(len(s), ENSSession.NOTIFY, sqn) + s.encode(MSG_ENCODE_UTF8))

    ## Gets Notifys received over the session.
    #
    #  @param  block       Specifies whether the call should block if no Notifys are available in the
    #                      queue (defaults to True).
    #  @param  timeout     If block is True, specifies an optional period (in seconds) to wait for a Notify to arrive.
    #  @return             A tuple containing the sequence number of the Notify and a string
    #                      containing the data.  If no Notify is available, returns None.
    #
    def get_notify(self, block=True, timeout=None):
        try:
            return self.notify_q.get(block, timeout)
        except Queue.empty:
            return None

    ## Terminates the session.
    #
    def close(self):
        logging.info('Closing session')
        if self.conn:
            self.conn.send(ENSSession.header.pack(0, ENSSession.STOP, 0))
            self.conn.shutdown(socket.SHUT_RDWR)
            #self.conn.close()
            #self.conn = None

    ## Destructor.
    #
    def __del__(self):
        self.close()


## Class representing a HTTP session with a microservice instance hosted on the ENS platform.
#
#  This class should not be instantiated directly by client application.  Instead use the ENSClient.connect method to create a session.
#
class ENSHttpSession(object):
    def __init__(self, app, cloudlet, interface, binding):
        logging.info('Create ENSHttpSession to interface {} on application {}'.format(interface, app))
        self.app = app
        self.cloudlet = cloudlet
        self.interface = interface
        self.binding = binding

    ## Connect to port in the interface binding.
    #
    def connect(self):
        # Connect to port in the interface binding.
        logging.info('Connecting to HTTP interface {} at {}'.format(self.interface, self.binding))
        return True

    ## Sends a Request over the session and return the Response as a string.
    #
    #  @param  api             Mandatory;Default:None;Description:A string containing the api to be invoked.
    #  @param  method          Optional;Default:GET;Description:A string containing the HTTP_METHOD to be used. Either of 'GET, POST, PUT, DELETE'.
    #  @param  url_params      Optional;Default:None;Description:A string containing HTTP URL/Query parameters.
    #  @param  request_data    Optional;Default:None;Description:Payload data, remote end interprets based on accompanying headers
    #  @param  header          Optional;Default:None;Description:A dictionary containing standard or custom header values.
    #  @return                 A requests.Response object (refer to http://docs.python-requests.org/en/master/api/#requests.Response)
    #
    #  Note:
    #  1. A consumer of this API must not use a custom header with
    #     name 'API-KEY'. It is reserved for platform use.
    def request(self,
        api,
        method = HTTP_METHOD.GET,
        url_params = None,
        request_data = None,
        header = None):

        #Add API Key to header
        headers = {'API-KEY': self.binding['accessToken']}
        if header:
            headers.update(header)

        response = None

        request_url = self.binding['endpoint'] + api
	#print request_url.replace("http","https");
	#request_url = request_url.replace("http","https");
	
        if method == HTTP_METHOD.GET:
            logging.debug('Request Method:{}, URL: {}'.format(method, request_url))
	    print "--------------------"
	    print headers
	    print request_url
	    print "--------------------"
            #response = requests.get(request_url, headers = headers, verify=False)
            response = requests.get(request_url, headers = headers)
            logging.info('Response Status:{}'.format(response.status_code))
	    logging.info('mge: http request: {}'.format(request_url))
	    logging.info('mge: http request header: {}'.format(headers))
        elif method == HTTP_METHOD.POST:
            logging.debug('Request Method:{}, URL: {}, URL Params: {}, Data: {}'.format(method, request_url, url_params, request_data))
            response = requests.post(request_url, headers = headers, data = request_data, params = url_params)
            logging.info('Response Status:{}'.format(response.status_code))
        elif method == HTTP_METHOD.PUT:
            logging.debug('Request Method:{}, URL: {}, URL Params: {}, Data: {}'.format(method, request_url, url_params, request_data))
            response = requests.put(request_url, headers = headers, data = request_data, params = url_params)
            logging.info('Response Status:{}'.format(response.status_code))
        elif method == HTTP_METHOD.DELETE:
            logging.debug('Request Method:{}, URL: {}'.format(method, request_url))
            response = requests.delete(request_url, headers = headers)
            logging.info('Response Status:{}'.format(response.status_code))

	    logging.info('mge: http delete: {}'.format(request_url))
	    logging.info('mge: http delete header: {}'.format(headers))
        else:
            logging.error('Unsupported HTTP Method')

        return response

    ## Terminates the session.
    #
    def close(self):
        return

    ## Destructor.
    #
    def __del__(self):
        return


## Class representing a TCP/UDP session with a microservice instance hosted on the ENS platform.
#
#  This class should not be instantiated directly by client application.  Instead use the ENSClient.connect method to create a session.
#
class ENSNetworkSession(object):
    def __init__(self, app, cloudlet, interface, binding):
        logging.info('Create ENSNetworkSession to interface {} on application {}'.format(interface, app))
        self.app = app
        self.cloudlet = cloudlet
        self.interface = interface
        self.binding = binding
        self.conn = None

    ## Connect to port in the interface binding.
    #
    def connect(self):
        logging.info('Connecting to Network interface {} at {}'.format(self.interface, self.binding))

        try:
            self.nwEndpoint = ENSEndpoint(self.binding['endpoint'])
            if self.nwEndpoint.protocol == 'udp':
                # Create a UDP/IP socket
                self.conn = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            else:
                # Create a TCP/IP socket
                self.conn = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

                sa = self.nwEndpoint.sa[0]

                # Connect the socket to the port where the server is listening
                logging.debug('Connecting cloudlet at {}:{}'.format(sa[0], sa[1]))
                self.conn.connect((sa[0], sa[1]))
        except ENSClientError:
            logging.error('Invalid endpoint {} for {}'.format(self.binding['endpoint'], self.interface))
            return False
        except socket.error:
            logging.error('Failed to connect to endpoint {} for {}'.format(self.binding['endpoint'], self.interface))
            return False

        return True

    ## Sends a Request over the session and return the Response as a string.
    #
    #  @param  s            A string containing the request data.
    #  @param  buff_size    The buffer size for the response data.
    #  @return              A string containing the response data (or None if the request fails).
    #
    def request(self, data, buff_size = 1024):
        response_stream = None

        if self.conn:
            if self.nwEndpoint.protocol == 'udp':
                if buff_size <= 0:
                    return None

                sa = self.nwEndpoint.sa[0]
                sent = self.conn.sendto(data.encode(MSG_ENCODE_UTF8), (sa[0], sa[1]))
                response_stream = self.conn.recvfrom(buff_size)
                if len(response_stream) >= 2:
                    #Decode bytestream
                    response_stream = response_stream[0].decode(MSG_ENCODE_UTF8)
            else:
                self.conn.sendall(data.encode(MSG_ENCODE_UTF8))
                response_stream = self.conn.recv(buff_size)
                #Decode bytestream
                response_stream = response_stream.decode(MSG_ENCODE_UTF8)

        return response_stream

    ## Terminates the session.
    #
    def close(self):
        if self.conn:
            self.conn.close()
            self.conn = None

    ## Destructor.
    #
    def __del__(self):
        self.close()


## Class representing client application.
#
#  An application should create an instance of this class then call the init() method to
#  authenticate with the ENS platform, select an appropriate cloudlet and instantiate the
#  hosted application and microservice components on that cloudlet.
#
class ENSClient(object):
    class Probe(asyncore.dispatcher):
        def __init__(self, cloudlet, config, app):
            asyncore.dispatcher.__init__(self)

            self.app = app
            self.cloudlet = cloudlet
            self.sampling = False
            self.samples = []
            self.app_id = app.split('.')[1]
            self._status = TASK_STATE.STOPPED

            logging.info('[Cloudlet:{}][App:{}]: Probe START'.format(cloudlet, self.app_id))

            if 'endpoints' in config and 'probe' in config['endpoints']:
                try:
                    probe = config['endpoints']['probe']
                    probeEndpoint = ENSEndpoint(probe)
            	
                    sa = probeEndpoint.sa[0]

                    logging.debug('[Cloudlet:{}][App:{}]: Probing at {}:{}'\
                        .format(cloudlet, self.app_id, sa[0], sa[1]))

                    self.create_socket(socket.AF_INET, socket.SOCK_STREAM)
                    self.connect(sa)
                    self._status = TASK_STATE.STARTED
                    self.buffer = 'ENS-PROBE {}\r\n'.format(self.app)
	            logging.info('mge: socket connect: {}'.format(sa))
		    logging.info('mge: socket command: ENS-PROBE {}'.format(self.app))
                except ENSClientError:
                    logging.error('[Cloudlet:{}][App:{}]: Invalid probe endpoint:{}'\
                        .format(cloudlet, self.app_id, probe))
                    self.terminate_probe()
            else:
                logging.error('[Cloudlet:{}][App:{}]: Missing probe endpoint'\
                    .format(cloudlet, self.app_id))
                self.terminate_probe()

        def terminate_probe(self):
            logging.info('[Cloudlet:{}][App:{}]: Probe STOP'.format(self.cloudlet, self.app_id))
            self.close()
            self._status = TASK_STATE.STOPPED

        def get_status(self):
            return self._status

        def handle_connect(self):
            pass

        def handle_error(self):
            exception_class, exception_value = sys.exc_info()[:2]
            logging.error('[Cloudlet:{}][App:{}]: Probe failed with error:{}-{}'.format(self.cloudlet, self.app_id, exception_class, exception_value))
            self.terminate_probe()

        def handle_close(self):
            self.terminate_probe()

        def handle_read(self):
            self.end_time = time.time()
            rsp = self.recv(8192)
	    logging.info('mge: socket recv: buffer len 8192')
            rsp = rsp.decode(MSG_ENCODE_UTF8)
            logging.debug('[Cloudlet:{}][App:{}]: Rx {}'.format(self.cloudlet, self.app_id, rsp))

            if not self.sampling:
                # Check that the microservice is supported
                params = rsp.splitlines()[0].split(' ')
                if params[0] == 'ENS-PROBE-OK':
                    # Microservice is supported, so save microservice data
                    self.buffer = 'ENS-RTT {}\r\n'.format(self.app)
                    self.sampling = True
                else:
                    # Microservice is not supported, so just close the socket
                    # and wait for other probes to finish.
                    self.terminate_probe()
            else:
                # Must be doing RTT estimation
                rtt = self.end_time - self.start_time
                self.samples.append(rtt)

                logging.debug('[Cloudlet:{}][App:{}][Probe {}]: RTT = {}'\
                    .format(self.cloudlet, self.app_id,
                    len(self.samples), rtt))

                if len(self.samples) < PROBE_MAX_COUNT:
                    self.buffer = 'ENS-RTT {}\r\n'.format(self.app)
                else:
                    logging.info('[Cloudlet:{}][App:{}]: Completed {} RTT probes. Mean RTT:{}'\
                        .format(self.cloudlet, self.app_id,
                        PROBE_MAX_COUNT, self.mean_rtt()))

        def writable(self):
            return (len(self.buffer) > 0)

        def handle_write(self):
            self.start_time = time.time()
            sent = self.send(self.buffer.encode(MSG_ENCODE_UTF8))
            logging.debug('[{}][{}]: Tx {}'.format(self.cloudlet, self.app_id, self.buffer))
            self.buffer = self.buffer[sent:]

        def mean_rtt(self):
            if len(self.samples):
                return sum(self.samples) / float(len(self.samples))
            else:
                return NO_RTT_DATA

    # Microservice class; helper calls to keeps the application endpoints
    # (http, event, network) information.
    class Microservice(object):
        def __init__(self, ms_data):
            self._ms_name = ms_data['name']
            self._ms_data = ms_data

            self._faas_bindings = {}
            for binding in self._ms_data['eventGateway']:
                binding_name = self._ms_name + '.' + binding['eventId']
                self._faas_bindings[binding_name] = binding

            self._http_bindings = {}
            for binding in self._ms_data['httpGateway']:
                binding_name = self._ms_name + '.' + binding['httpApiId']
                self._http_bindings[binding_name] = binding

            self._network_bindings = {}
            for binding in self._ms_data['networkBinding']:
                binding_name = self._ms_name + '.' + binding['networkId']
                self._network_bindings[binding_name] = binding

        def name(self):
            return self._ms_name

        def faas_binding(self, interface):
            return self._faas_bindings[interface]

        def faas_bindings(self):
            return self._faas_bindings

        def http_binding(self, interface):
            return self._http_bindings[interface]

        def http_bindings(self):
            return self._http_bindings

        def network_binding(self, interface):
            return self._network_bindings[interface]

        def network_bindings(self):
            return self._network_bindings


    ## Constructor for ENSClient instance.
    #
    #  @param  app         Application identifier in the form <developer-id>.<app-id>.
    #
    def __init__(self, app):
        # Open the configuration file to get the Discovery Server URL, API key
        # and SDK version.
        self.sdkconfig = {}
        try:
            with open(SDK_CONFIG_FILE) as sdkfile:
                logging.info('Loading MEC SDK settings')
                for line in sdkfile:
                    name, var = line.partition('=')[::2]
                    self.sdkconfig[name.strip()] = var.strip()

                err_string = None
                for param in CONF_MANDATORY_PARAMS:
                    if param not in self.sdkconfig:
                        err_string = 'Missing {} in {}'.format(param, SDK_CONFIG_FILE)
                        break

                if err_string:
                    logging.error(err_string)
                    raise ENSClientError(err_string)
        except IOError as io_error:
            err_msg = 'I/O error while loading config file: {}'.format(io_error)
            logging.error(err_msg)
            raise ENSClientError(err_msg)


        logging.info('{} {}'.format(str(uuid.uuid4()),str(uuid.uuid4()).replace('-', '')))

        self.client_id = str(uuid.uuid4()).replace('-', '')


        self.app = app
        self.cloudlet = ''
        self.app_at_cloud = None
        self.deployment_id = None

        self.event_bindings = {}
        self.network_bindings = {}
        self.probed_rtt = 0.0

    ## Requests initialization of the hosted application on the ENS platform.
    #
    #  @return           True or False indicating success of operation.
    #
    def init(self):
        """Requests initialization of the hosted application on the ENS platform.
           Return True or False indicating success of operation.
        """
        # Contact the Discovery Server to get a candidate list of cloudlets for the app
        # and the contact details for the app@cloud instance.
        disc_resp_data = {}
        discovery_response = None

        developer_id, app_id = self.app.split('.')
        try:
            logging.info('mge: discovery_url_pattern: {}' .format(DISCOVERY_URL_PATTERN.format(
                self.sdkconfig['DiscoveryURL'],
                developer_id, app_id,
                self.sdkconfig['SdkVersion'])))
	    logging.info('mge: headers: {}'.format(self.sdkconfig['ApiKey']))

            discovery_response = requests.get(DISCOVERY_URL_PATTERN.format(
                self.sdkconfig['DiscoveryURL'],
                developer_id, app_id,
                self.sdkconfig['SdkVersion']),
                headers = {
                    'Authorization': 'Bearer {}'.format(self.sdkconfig['ApiKey'])
                })

            if discovery_response.status_code == requests.codes.ok:
                disc_resp_data = discovery_response.json()
            else:
                discovery_response.raise_for_status()
        except requests.exceptions.HTTPError as http_error:
            err_msg = 'HTTP Error received during Cloudlet Discovery. Code: {}, Message: {}'\
                .format(http_error.response.status_code,
                    discovery_response)
            logging.error(err_msg)
            return False
        except requests.exceptions.ConnectionError as conn_error:
            err_msg = 'Connection Error during Cloudlet Discovery: {}'\
                .format(conn_error)
            logging.error(err_msg)
            return False
        except requests.exceptions.Timeout as timeout_error:
            err_msg = 'Request timed out for Cloudlet Discovery: {}'\
                .format(timeout_error)
            logging.error(err_msg)
            return False
        except ValueError as val_error:
            err_msg = 'Invalid JSON data received as Discovery Response: {}'\
                .format(val_error)
            logging.error(err_msg)
            return False

        if not disc_resp_data:
            logging.error('Empty Discovery response received')
            return False
        else:
            #Response received, validate it
            logging.debug('Discovery server response:\n{}'.format(disc_resp_data))

        if 'cloudlets' not in disc_resp_data:
            logging.error('No Cloudlets found for App: {}'.format(app_id))
            return False

        if 'cloud' not in disc_resp_data or \
            'endpoints' not in disc_resp_data['cloud'] or \
            'app@cloud' not in disc_resp_data['cloud']['endpoints']:
            logging.error('No App@cloud found for App {} in Discovery Response'.format(app_id))
            return False

        cloudlets = disc_resp_data['cloudlets']

        self.app_at_cloud = str(
            disc_resp_data['cloud']['endpoints']['app@cloud'])

        if len(cloudlets) == 0:
            logging.error('No Cloudlets to probe found for App: {}'.format(app_id))
            return False
        else:
            logging.info('{} probe candidate cloudlets found by discovery'\
                .format(len(cloudlets)))

        #Create probes handles for each cloudlet
        probes = [ENSClient.Probe(c, v, self.app) for c,v in cloudlets.items()]

        # Run the probes for PROBE_MAX_PERIOD
        probe_start = time.time()
        while (time.time() - probe_start) < PROBE_MAX_PERIOD:
            asyncore.loop(timeout=1, count=1, use_poll=True)

        rtt_list = []
        for probe in probes:
            if probe.mean_rtt() == NO_RTT_DATA:
                logging.warn('[Cloudlet:{}][App:{}]: No RTT data received within {} seconds'.format(probe.cloudlet, probe.app_id, PROBE_MAX_PERIOD))
            else:
                #Collect all probe results
                rtt_list.append((probe.cloudlet, probe.mean_rtt()))

            if probe.get_status() != TASK_STATE.STOPPED:
                logging.info('[Cloudlet:{}][App:{}]: Probe STOP'.format(probe.cloudlet, probe.app_id))
                probe.close()

        logging.info('Probes completed for all Cloudlet candidates');

        if not rtt_list:
            logging.error('No valid probe result found')
            return False

        #Sort probe results by mean RTT value
        rtt_list = sorted(rtt_list, key=lambda rtt_entry: rtt_entry[1])

        logging.info('Sorted Probe Report')
        logging.info('===================')
        for rtt_entry in rtt_list:
            logging.info('Cloudlet: {} RTT: {}'\
                .format(rtt_entry[0], rtt_entry[1]))

        logging.info('Selected cloudlet for provision: {}'.format(rtt_list[0][0]))
        self.cloudlet = rtt_list[0][0]
        self.probed_rtt = rtt_list[0][1]

        # Send a service request to platform app@cloud to instantiate the application and microservices.
        url = APP_CREATE_URL_PATTERN\
            .format(self.app_at_cloud,
            developer_id,
            app_id,
            self.cloudlet,
            self.client_id)

        provision_response = None
        prov_resp_data = None
        try:
 
            logging.info('Requesting provisioning of {}'.format(app_id))
            logging.info('mge: url {} headers {}'.format(url,JSON_HEADER))
            provision_response = requests.post(url, headers = JSON_HEADER)
            logging.debug('Provision response: {}'.format(provision_response.text))

	    #file = open('provisining.json','w')
	    #file.write(provision_response.json())
	    #file.close()

	    import json
	    with open('provisining.json', 'w') as outfile:
		json.dump(provision_response.json(), outfile)

            if provision_response.status_code == requests.codes.ok:
                prov_resp_data = provision_response.json()
                logging.info('Provision completed for {}'.format(app_id))
            else:
                provision_response.raise_for_status()
        except requests.exceptions.HTTPError as http_error:
            err_msg = 'HTTP Error received during App provision. Code: {}, Message: {}'\
                .format(http_error.response.status_code,
                    provision_response)
            logging.error(err_msg)
            return False
        except requests.exceptions.ConnectionError as conn_error:
            err_msg = 'Connection Error during App provision: {}'\
                .format(conn_error)
            logging.error(err_msg)
            return False
        except requests.exceptions.Timeout as timeout_error:
            err_msg = 'Request timed out for App provision: {}'\
                .format(timeout_error)
            logging.error(err_msg)
            return False
        except ValueError as val_error:
            err_msg = 'Invalid JSON data received as App provision response: {}'\
                .format(val_error)
            logging.error(err_msg)
            return False

        #App provision response received, validate it
        if not prov_resp_data:
            logging.error('Empty response received for App provision request')
            return False

        if 'deploymentId' not in prov_resp_data or 'microservices' not in prov_resp_data:
            logging.error('App provision response "{}" missing mandatory params'.format(prov_resp_data))
            return False

        self.deployment_id = prov_resp_data['deploymentId']
        self.microservices = {}
        for ms_data in prov_resp_data['microservices']:
            self.microservices[ms_data['name']] = ENSClient.Microservice(ms_data)

        return True


    ## Requests connection of a session to the specified interface provided by the hosted application.
    #
    #  @param interface    Interface name in the form &lt;microservice&gt;.&lt;interface&gt;.
    #  @return             An ENSSession/ENSHttpSession/ENSNetworkSession object (or None if the session cannot be connected).
    #
    def connect(self, interface):
        ms_name = interface.split('.')[0]
        if ms_name not in self.microservices:
            return None

        ms = self.microservices[ms_name]

        if interface in ms.faas_bindings():
            # Create an ENSSession object for the connection
            session = ENSSession(self.app, self.cloudlet, interface, ms.faas_binding(interface))
            if session.connect():
                return session
            else:
                return None

        if interface in ms.http_bindings():
            # Create an ENSHttpSession object for the connection
            session = ENSHttpSession(self.app, self.cloudlet, interface, ms.http_binding(interface))
            if session.connect():
                return session
            else:
                return None

        if interface in ms.network_bindings():
            # Create an ENSNetworkSession object for the connection
            session = ENSNetworkSession(self.app, self.cloudlet, interface, ms.network_binding(interface))
            if session.connect():
                return session
            else:
                return None

        logging.error('Cannot connect to unknown interface {}'.format(interface))
        return None

    def close(self):
        if self.deployment_id:
            developer_id, app_id = self.app.split('.')
            deployment_id = self.deployment_id['uuid']

            app_delete_url = APP_DELETE_URL_PATTERN.format(self.app_at_cloud,
                developer_id, app_id,
                self.cloudlet, self.client_id,
                deployment_id)


            try:
                app_delete_response = requests.delete(app_delete_url,
                    headers = JSON_HEADER)
	    
		logging.info('mge: http delete: {}'.format(app_delete_url))
	    	logging.info('mge: http delete header: {}'.format(JSON_HEADER))

                if app_delete_response.status_code == requests.codes.ok:
                    logging.info('App: [{}], Deployment Id: [{}] deleted.'\
                        .format(app_id, deployment_id))
		
		    file = open('provisining.json','w')
		    file.write(provision_response.text)
		    file.close()


                else:
                    app_delete_response.raise_for_status()
            except requests.exceptions.HTTPError as http_error:
                err_msg = 'App: [{}], Deployment Id: [{}] HTTP Error received during App termination. Code: {}, Message: {}'\
                    .format(app_id, deployment_id,
                    http_error.response.status_code,
                    app_delete_response)
                logging.error(err_msg)
            except requests.exceptions.ConnectionError as conn_error:
                err_msg = 'App: [{}], Deployment Id: [{}] Connection Error during App termination: {}'\
                    .format(app_id, deployment_id, conn_error)
                logging.error(err_msg)
            except requests.exceptions.Timeout as timeout_error:
                err_msg = 'App: [{}], Deployment Id: [{}] Request timed out for App termination: {}'\
                    .format(app_id, deployment_id, timeout_error)
                logging.error(err_msg)

    def __del__(self):
        self.close()
