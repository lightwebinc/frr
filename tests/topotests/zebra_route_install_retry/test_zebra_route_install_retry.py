#!/usr/bin/env python
# SPDX-License-Identifier: ISC

"""
test_zebra_route_install_retry.py: a route the kernel refused to install
must be retried, a bounded number of times.

A route-map "set src" gives every route of a protocol a preferred source.
The kernel refuses an IPv6 route whose source address is not on the box
("Invalid source address"), and zebra marks the route failed.  Before the
retry, nothing ever asked for that route again: it stayed out of the FIB
(`r` in show ipv6 route) even after the address came back, until its owner
withdrew and re-sent it.  An interface reconfiguration that briefly removes
the address strands every route reinstalled inside that window.

The test opens the window deterministically: delete the source address,
add a static route while it is gone, then restore the address.  The route
must reach the kernel with the source on it, which only a retry can do.  A
second route whose source never returns must be given up on after the
retry budget, and must not be retried after that.
"""

import json
import os
import re
import sys

import pytest

CWD = os.path.dirname(os.path.realpath(__file__))
sys.path.append(os.path.join(CWD, "../"))

# pylint: disable=C0413
from lib import topotest
from lib.common_config import step
from lib.topogen import Topogen, get_topogen

pytestmark = [pytest.mark.staticd]

SRC = "2001:db8:50::1"
SRC_GONE = "2001:db8:51::1"
NEXTHOP = "2001:db8:1::2"
RECOVERS = "2001:db8:99::/64"
STAYS_GONE = "2001:db8:98::/64"

# zebra's budget: 6 retries after 1, 2, 4, 8, 16 and 16 seconds.
RETRY_MAX = 6


def build_topo(tgen):
    tgen.add_router("r1")

    switch = tgen.add_switch("s1")
    switch.add_link(tgen.gears["r1"])


def setup_module(mod):
    tgen = Topogen(build_topo, mod.__name__)
    tgen.start_topology()

    r1 = tgen.gears["r1"]
    # The set src addresses must exist when the route-map is configured.
    r1.run("ip link add src0 type dummy")
    r1.run("ip link set src0 up")
    r1.run("ip -6 addr add {}/128 dev src0 nodad".format(SRC))
    r1.run("ip -6 addr add {}/128 dev src0 nodad".format(SRC_GONE))

    r1.load_frr_config(
        os.path.join(CWD, "r1/frr.conf"), daemons=["zebra", "mgmtd", "staticd"]
    )

    tgen.start_router()


def teardown_module(mod):
    tgen = get_topogen()
    tgen.stop_topology()


def retry_counters(router):
    "The retry rows of show zebra, as a dict."
    out = router.vtysh_cmd("show zebra")
    rows = {
        "Retries": r"Route Install Retries\s+(\d+)",
        "Recovered": r"Route Install Retry Recovered\s+(\d+)",
        "Exhausted": r"Route Install Retry Exhausted\s+(\d+)",
        "Pending": r"Route Install Retry Pending\s+(\d+)",
    }
    counters = {}
    for key, pattern in rows.items():
        m = re.search(pattern, out)
        assert m, "show zebra has no {} row:\n{}".format(key, out)
        counters[key] = int(m.group(1))
    return counters


def soft_counter(router, key):
    "One retry counter, or -1 when show zebra has no such row."
    try:
        return retry_counters(router)[key]
    except AssertionError:
        return -1


def zebra_route(router, prefix):
    out = json.loads(router.vtysh_cmd("show ipv6 route {} json".format(prefix)))
    entries = out.get(prefix, [])
    return entries[0] if entries else None


def kernel_route(router, prefix):
    out = json.loads(router.run("ip -6 -j route show {}".format(prefix)) or "[]")
    return out[0] if out else None


def wait_for_failed(router, prefix):
    def _failed():
        re_ = zebra_route(router, prefix)
        if re_ is None:
            return "{} not in the RIB yet".format(prefix)
        if not re_.get("failed") or re_.get("installed"):
            return "{} is not failed: {}".format(prefix, re_)
        return None

    _, result = topotest.run_and_expect(_failed, None, count=20, wait=0.5)
    assert result is None, result


def test_converged():
    tgen = get_topogen()
    if tgen.routers_have_failure():
        pytest.skip(tgen.errors)

    r1 = tgen.gears["r1"]
    c = retry_counters(r1)
    assert c == {"Retries": 0, "Recovered": 0, "Exhausted": 0, "Pending": 0}, c


def test_failed_install_is_retried_once_the_source_returns():
    tgen = get_topogen()
    if tgen.routers_have_failure():
        pytest.skip(tgen.errors)

    r1 = tgen.gears["r1"]

    step("Remove the set src address and add a route inside the gap")
    r1.run("ip -6 addr del {}/128 dev src0".format(SRC))
    r1.vtysh_cmd("configure terminal\nipv6 route {} {}\n".format(RECOVERS, NEXTHOP))
    wait_for_failed(r1, RECOVERS)
    assert kernel_route(r1, RECOVERS) is None, "kernel took a route with no source"

    step("Let retries fail while the source is still missing")
    # Soft wait: an unfixed zebra has no retry rows and never retries, and
    # must fail on the stranded route below rather than here.  Three failed
    # retries (7s) also outlast staticd's own follow-up update of the new
    # route, which would otherwise reinstall it and hide a missing retry.
    # Pending again after a retry means that retry's install came back
    # failed and the next one is scheduled.
    topotest.run_and_expect(
        lambda: soft_counter(r1, "Retries") >= 3 and soft_counter(r1, "Pending") >= 1,
        True,
        count=15,
        wait=1,
    )
    re_ = zebra_route(r1, RECOVERS)
    assert re_.get("failed") and not re_.get("installed"), re_
    assert kernel_route(r1, RECOVERS) is None

    step("Restore the address; the next retry must install the route")
    r1.run("ip -6 addr add {}/128 dev src0 nodad".format(SRC))

    def _installed():
        re_ = zebra_route(r1, RECOVERS)
        if not re_ or not re_.get("installed") or re_.get("failed"):
            return "{} not installed in zebra: {}".format(RECOVERS, re_)
        kr = kernel_route(r1, RECOVERS)
        if not kr:
            return "{} not in the kernel".format(RECOVERS)
        if kr.get("prefsrc") != SRC:
            return "{} in the kernel without its source: {}".format(RECOVERS, kr)
        return None

    # The longest single backoff is 16s.
    _, result = topotest.run_and_expect(_installed, None, count=40, wait=1)
    assert result is None, result

    # Anything that reprocesses the route (the restored address can) may
    # install it ahead of the scheduled retry, which then fires as a no-op.
    topotest.run_and_expect(lambda: retry_counters(r1)["Pending"], 0, count=20, wait=1)
    c = retry_counters(r1)
    assert c["Retries"] >= 4, c
    assert c["Recovered"] == 1, c
    assert c["Exhausted"] == 0, c
    assert c["Pending"] == 0, c
    assert "installRetries" not in zebra_route(r1, RECOVERS)


def test_permanent_failure_is_bounded():
    tgen = get_topogen()
    if tgen.routers_have_failure():
        pytest.skip(tgen.errors)

    r1 = tgen.gears["r1"]
    before = retry_counters(r1)

    step("Remove the other source address for good and add a route using it")
    r1.run("ip -6 addr del {}/128 dev src0".format(SRC_GONE))
    r1.vtysh_cmd("configure terminal\nipv6 route {} {}\n".format(STAYS_GONE, NEXTHOP))
    wait_for_failed(r1, STAYS_GONE)

    step("zebra spends the budget and gives up")

    def _exhausted():
        c = retry_counters(r1)
        if c["Exhausted"] != before["Exhausted"] + 1 or c["Pending"] != 0:
            return "not given up yet: {}".format(c)
        return None

    # 1 + 2 + 4 + 8 + 16 + 16 = 47s of backoff.
    _, result = topotest.run_and_expect(_exhausted, None, count=75, wait=1)
    assert result is None, result

    after = retry_counters(r1)
    assert after["Retries"] - before["Retries"] == RETRY_MAX, (before, after)
    assert after["Recovered"] == before["Recovered"], (before, after)

    re_ = zebra_route(r1, STAYS_GONE)
    assert re_.get("failed") and not re_.get("installed"), re_
    assert re_.get("installRetries") == RETRY_MAX, re_
    assert kernel_route(r1, STAYS_GONE) is None

    step("No retry follows the last one")
    topotest.sleep(20, "past the longest backoff")
    assert retry_counters(r1) == after

    step("The route that recovered is untouched")
    kr = kernel_route(r1, RECOVERS)
    assert kr and kr.get("prefsrc") == SRC, kr


def test_memory_leak():
    "Run the memory leak test and report results."
    tgen = get_topogen()
    if not tgen.is_memleak_enabled():
        pytest.skip("Memory leak test/report is disabled")

    tgen.report_memory_leaks()


if __name__ == "__main__":
    args = ["-s"] + sys.argv[1:]
    sys.exit(pytest.main(args))
