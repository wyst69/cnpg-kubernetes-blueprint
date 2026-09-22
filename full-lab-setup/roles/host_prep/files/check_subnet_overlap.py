#!/usr/bin/env python3
"""Exit 1 and print any of argv[2:] that overlaps the CIDR in argv[1]."""
import ipaddress
import sys

candidate = ipaddress.ip_network(sys.argv[1])
overlaps = [s for s in sys.argv[2:] if s and ipaddress.ip_network(s).overlaps(candidate)]
if overlaps:
    print("\n".join(overlaps))
    sys.exit(1)
sys.exit(0)
