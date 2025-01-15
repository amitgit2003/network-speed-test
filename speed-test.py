

import socket
import struct
import sys
import threading
import time
from datetime import datetime

# ----------------------------------------------------------------------------------
# CONSTANTS
# ----------------------------------------------------------------------------------

MAGIC_COOKIE = 0xabcddcba

MSG_TYPE_OFFER   = 0x2  # Server -> Client
MSG_TYPE_REQUEST = 0x3  # Client -> Server
MSG_TYPE_PAYLOAD = 0x4  # Server -> Client

UDP_BROADCAST_IP    = '<broadcast>'
UDP_BROADCAST_PORT  = 13117
OFFER_INTERVAL      = 1.0        # seconds
UDP_TIMEOUT         = 1.0        # to detect end of UDP stream
TCP_RECV_BUFFER     = 4096

# ----------------------------------------------------------------------------------
# SERVER CODE
# ----------------------------------------------------------------------------------

class SpeedTestServer:
    def __init__(self):
        # TCP socket
        self.tcp_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.tcp_socket.bind(('0.0.0.0', 0))  # any free port
        self.tcp_socket.listen(5)
        self.tcp_port = self.tcp_socket.getsockname()[1]

        # UDP socket (for receiving requests and broadcasting offers)
        self.udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp_socket.bind(('0.0.0.0', 0))
        self.udp_port = self.udp_socket.getsockname()[1]

        print(f"Server started, listening on IP address {self.get_local_ip()}")
        print(f"TCP port: {self.tcp_port}, UDP port: {self.udp_port}")

        self.stop_broadcast = False

        # Start broadcasting offers
        self.broadcast_thread = threading.Thread(target=self._broadcast_offers_loop, daemon=True)
        self.broadcast_thread.start()

        # Start listening for UDP requests
        self.udp_thread = threading.Thread(target=self._listen_udp_requests, daemon=True)
        self.udp_thread.start()

    def get_local_ip(self):
        """Retrieve local IP address (best effort)."""
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(('8.8.8.8', 80))  # doesn't need to succeed
            ip = s.getsockname()[0]
        except Exception:
            ip = '127.0.0.1'
        finally:
            s.close()
        return ip

    def _broadcast_offers_loop(self):
        """Broadcast 'offer' packets every second on UDP_BROADCAST_PORT."""
        while not self.stop_broadcast:
            # Construct offer message: cookie (4 bytes), type (1 byte), UDP port (2 bytes), TCP port (2 bytes)
            msg = struct.pack('>IBHH', MAGIC_COOKIE, MSG_TYPE_OFFER, self.udp_port, self.tcp_port)
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                s.sendto(msg, (UDP_BROADCAST_IP, UDP_BROADCAST_PORT))
            time.sleep(OFFER_INTERVAL)

    def _listen_udp_requests(self):
        """Listen for UDP 'request' messages (type=0x3)."""
        while True:
            data, addr = self.udp_socket.recvfrom(1024)
            if len(data) < 13:
                continue

            # Parse request
            cookie, msg_type, file_size = struct.unpack('>IBQ', data[:13])
            if cookie != MAGIC_COOKIE or msg_type != MSG_TYPE_REQUEST:
                continue

            # Handle request in a new thread
            t = threading.Thread(target=self._handle_udp_request, args=(addr, file_size))
            t.start()

    def _handle_udp_request(self, client_addr, file_size):
        """
        Respond with multiple payload packets.
        Each payload packet includes:
          - cookie (4 bytes)
          - msg_type (1 byte) = 0x4
          - total_segment_count (8 bytes)
          - current_segment (8 bytes)
          - actual payload (some chunk)
        """
        # Decide an arbitrary packet size. We'll break the file_size into chunks.
        CHUNK_SIZE = 1024
        total_segments = (file_size + CHUNK_SIZE - 1) // CHUNK_SIZE  # ceiling division

        bytes_sent = 0
        for seg_index in range(total_segments):
            # Build header
            segment_header = struct.pack('>IBQQ', MAGIC_COOKIE, MSG_TYPE_PAYLOAD,
                                         total_segments, seg_index + 1)

            # Construct the data chunk (filler bytes)
            bytes_to_send = min(CHUNK_SIZE, file_size - bytes_sent)
            payload_data = b'\xab' * bytes_to_send
            bytes_sent += bytes_to_send

            packet = segment_header + payload_data
            self.udp_socket.sendto(packet, client_addr)

    def accept_tcp_connections(self):
        """Accept incoming TCP connections, handle in new thread."""
        while True:
            client_sock, client_addr = self.tcp_socket.accept()
            t = threading.Thread(target=self._handle_tcp_connection, args=(client_sock,))
            t.start()

    def _handle_tcp_connection(self, client_sock):
        """
        Read the requested file size (as ASCII + newline).
        Send that many bytes as the file content.
        """
        try:
            data = b""
            while True:
                chunk = client_sock.recv(TCP_RECV_BUFFER)
                if not chunk:
                    return
                data += chunk
                if b'\n' in data:
                    break

            # Parse the file size
            line, _ = data.split(b'\n', 1)
            try:
                file_size = int(line.decode().strip())
            except ValueError:
                client_sock.close()
                return

            # Send file_size bytes
            bytes_sent = 0
            CHUNK_SIZE = 4096
            while bytes_sent < file_size:
                remaining = file_size - bytes_sent
                send_size = min(CHUNK_SIZE, remaining)
                client_sock.sendall(b'\xff' * send_size)
                bytes_sent += send_size

        finally:
            client_sock.close()

    def stop(self):
        """Stop the server (not strictly required for assignment)."""
        self.stop_broadcast = True
        self.tcp_socket.close()
        self.udp_socket.close()

# ----------------------------------------------------------------------------------
# CLIENT CODE
# ----------------------------------------------------------------------------------

class SpeedTestClient:
    def __init__(self):
        # Ask user for parameters
        self.file_size = self._get_file_size()
        self.tcp_count = self._get_connection_count("TCP")
        self.udp_count = self._get_connection_count("UDP")

        # Create a socket to listen for server offers
        self.offer_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.offer_socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            self.offer_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except AttributeError:
            pass

        self.offer_socket.bind(('', UDP_BROADCAST_PORT))
        print("Client started, listening for offers...")

        # Listen for offers
        self._listen_for_offers()

    def _get_file_size(self):
        while True:
            val = input("Enter desired file size in bytes (e.g. 1000000): ")
            try:
                return int(val)
            except ValueError:
                print("Invalid input, try again.")

    def _get_connection_count(self, protocol):
        while True:
            val = input(f"Enter number of {protocol} connections: ")
            try:
                return int(val)
            except ValueError:
                print("Invalid input, try again.")

    def _listen_for_offers(self):
        while True:
            data, addr = self.offer_socket.recvfrom(1024)
            if len(data) < 9:
                continue

            cookie = struct.unpack('>I', data[:4])[0]
            msg_type = data[4]
            udp_port = struct.unpack('>H', data[5:7])[0]
            tcp_port = struct.unpack('>H', data[7:9])[0]

            if cookie != MAGIC_COOKIE or msg_type != MSG_TYPE_OFFER:
                continue

            print(f"Received offer from {addr[0]}")
            self._run_speed_test(addr[0], tcp_port, udp_port)

            print("All transfers complete, listening for new offers")

    def _run_speed_test(self, server_ip, server_tcp_port, server_udp_port):
        threads = []

        # Launch TCP threads
        for i in range(self.tcp_count):
            t = threading.Thread(target=self._tcp_transfer, args=(server_ip, server_tcp_port, i+1))
            t.start()
            threads.append(t)

        # Launch UDP threads
        for i in range(self.udp_count):
            t = threading.Thread(target=self._udp_transfer, args=(server_ip, server_udp_port, i+1))
            t.start()
            threads.append(t)

        for t in threads:
            t.join()

    def _tcp_transfer(self, server_ip, tcp_port, transfer_id):
        start_time = time.time()
        bytes_received = 0

        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(5)
        try:
            s.connect((server_ip, tcp_port))
            # Send file size
            s.sendall(str(self.file_size).encode() + b'\n')

            # Receive until we get file_size bytes
            while bytes_received < self.file_size:
                chunk = s.recv(4096)
                if not chunk:
                    break
                bytes_received += len(chunk)

        except Exception as e:
            print(f"[TCP #{transfer_id}] Error: {e}")
        finally:
            s.close()

        duration = time.time() - start_time
        speed_bits_sec = 0.0
        if duration > 0:
            speed_bits_sec = (bytes_received * 8) / duration

        print(f"TCP transfer #{transfer_id} finished, total time: {duration:.2f}s, "
              f"speed: {speed_bits_sec:.2f} bits/s")

    def _udp_transfer(self, server_ip, udp_port, transfer_id):
        # Build request packet: magic cookie (4 bytes), msg_type (1 byte), file_size (8 bytes)
        request_packet = struct.pack('>IBQ', MAGIC_COOKIE, MSG_TYPE_REQUEST, self.file_size)

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(UDP_TIMEOUT)

        try:
            # Send request packet
            sock.sendto(request_packet, (server_ip, udp_port))
        except Exception as e:
            print(f"[UDP #{transfer_id}] Error sending request: {e}")
            sock.close()
            return

        start = datetime.now()
        last_packet_time = time.time()

        expected_segments = None
        received_segments = 0

        while True:
            try:
                data, addr = sock.recvfrom(65535)
            except socket.timeout:
                # No data for UDP_TIMEOUT
                break

            if data:
                last_packet_time = time.time()
                if len(data) < 21:
                    continue

                # parse header
                cookie, msg_type, total_seg_count, current_seg = struct.unpack('>IBQQ', data[:21])
                if cookie != MAGIC_COOKIE or msg_type != MSG_TYPE_PAYLOAD:
                    continue

                if expected_segments is None:
                    expected_segments = total_seg_count

                received_segments += 1

            # If no data for too long, break
            if (time.time() - last_packet_time) > UDP_TIMEOUT:
                break

        duration = (datetime.now() - start).total_seconds()
        speed_bits_sec = 0.0
        if duration > 0:
            speed_bits_sec = (received_segments * 1024 * 8) / duration  # rough guess if each segment was 1 KB
            # Adjust above if you're changing the chunk logic

        percent_received = 100.0
        if expected_segments and expected_segments > 0:
            percent_received = (received_segments / expected_segments) * 100

        print(f"UDP transfer #{transfer_id} finished, total time: {duration:.2f}s, "
              f"speed: {speed_bits_sec:.2f} bits/s, packets received: {percent_received:.2f}%")

        sock.close()

# ----------------------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        print("Usage: python speedtest.py [server|client]")
        sys.exit(1)

    mode = sys.argv[1].lower()
    if mode == 'server':
        server = SpeedTestServer()
        server.accept_tcp_connections()
    elif mode == 'client':
        SpeedTestClient()
    else:
        print("Invalid mode. Use 'server' or 'client'.")


if __name__ == "__main__":
    main()
