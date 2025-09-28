# cli.py
import argparse
import asyncio
from core import check   # <-- change run_check -> check

def main():
    parser = argparse.ArgumentParser(description="SherlockX - Improved Username/Email/Phone checker (limited variants)")
    parser.add_argument("base", help="Base username or name")
    parser.add_argument("--domains", "-d", nargs="*", help="Email domains")
    parser.add_argument("--phone", "-p", help="Phone number")
    parser.add_argument("--concurrency", "-c", type=int, default=20, help="Concurrent requests")
    parser.add_argument("--timeout", "-t", type=int, default=10, help="Timeout (seconds)")
    args = parser.parse_args()

    asyncio.run(check(args.base, args.domains, args.phone, args.concurrency, args.timeout))

if __name__ == "__main__":
    main()
