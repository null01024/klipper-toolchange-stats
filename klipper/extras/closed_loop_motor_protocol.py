# Transport-neutral contracts for closed-loop motor protocol adapters.


class ProtocolError(Exception):
    pass


class ProtocolRequest:
    def __init__(self, address, command, payload=b'', response_length=None,
                 timeout=None, metadata=None):
        self.address = int(address)
        self.command = int(command)
        self.payload = bytes(payload)
        self.response_length = response_length
        self.timeout = timeout
        self.metadata = dict(metadata or {})


class ProtocolFrame:
    def __init__(self, address, command, payload=b'', metadata=None):
        self.address = int(address)
        self.command = int(command)
        self.payload = bytes(payload)
        self.metadata = dict(metadata or {})


class LinkCodec:
    """Maps logical protocol requests to physical transport units."""

    profile = 'unknown'

    def encode(self, request):
        raise NotImplementedError

    def feed(self, unit):
        raise NotImplementedError

    def reset(self):
        pass


class DeviceProtocolAdapter:
    """Pure device semantics; implementations must not perform I/O."""

    profile = 'unknown'
    settings_profile = 'unknown'
    settings_profile_version = 1

    def poll_requests(self):
        raise NotImplementedError

    def decode(self, frame):
        raise NotImplementedError


class SerialEndpointSpec:
    """Configuration signature for a future shared USB serial endpoint."""

    VALID_TRANSPORTS = ('rs485', 'rs232')
    VALID_PARITY = ('none', 'even', 'odd')
    VALID_TOPOLOGIES = ('multidrop', 'point_to_point', 'daisy_chain')

    def __init__(self, transport, interface, baud, data_bits=8,
                 parity='none', stop_bits=1, topology=None):
        transport = str(transport).strip().lower()
        if transport not in self.VALID_TRANSPORTS:
            raise ProtocolError('serial transport must be rs485 or rs232')
        interface = str(interface).strip()
        if not interface:
            raise ProtocolError('serial interface path is required')
        baud = int(baud)
        if baud <= 0:
            raise ProtocolError('serial baud must be positive')
        data_bits = int(data_bits)
        if data_bits not in (5, 6, 7, 8):
            raise ProtocolError('serial data_bits must be 5, 6, 7 or 8')
        parity = str(parity).strip().lower()
        if parity not in self.VALID_PARITY:
            raise ProtocolError('serial parity must be none, even or odd')
        stop_bits = int(stop_bits)
        if stop_bits not in (1, 2):
            raise ProtocolError('serial stop_bits must be 1 or 2')
        if topology is None:
            topology = 'multidrop' if transport == 'rs485' else 'point_to_point'
        topology = str(topology).strip().lower()
        if topology not in self.VALID_TOPOLOGIES:
            raise ProtocolError('unsupported serial topology')
        if transport == 'rs485' and topology == 'point_to_point':
            pass
        elif transport == 'rs485' and topology != 'multidrop':
            raise ProtocolError('rs485 topology must be multidrop or point_to_point')
        elif transport == 'rs232' and topology == 'multidrop':
            raise ProtocolError('rs232 does not provide a generic multidrop topology')

        self.transport = transport
        self.interface = interface
        self.baud = baud
        self.data_bits = data_bits
        self.parity = parity
        self.stop_bits = stop_bits
        self.topology = topology

    @property
    def endpoint_key(self):
        return (self.transport, self.interface)

    @property
    def signature(self):
        return (
            self.baud, self.data_bits, self.parity, self.stop_bits,
            self.topology)
