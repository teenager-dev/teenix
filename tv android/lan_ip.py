import ipaddress
import socket


def main() -> int:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))
        address = probe.getsockname()[0]
    except OSError:
        return 1
    finally:
        probe.close()

    parsed = ipaddress.ip_address(address)
    if not parsed.is_private or parsed.is_loopback or parsed.is_link_local:
        return 1
    print(address)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
