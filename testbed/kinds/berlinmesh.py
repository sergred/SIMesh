"""Sergeyculum, the Rust Reticulum stack at git.emcomm.cc/berlinmesh/reticulum,
as its Linux station `fw/simesh`. The kind is named after the repository.

Spec keys beyond the common ones:

    tools: { rncfg: <path> }    its configuration tool; `rncfg` on PATH when absent

A station of this kind has no text console and no web UI. Its host door is
RNode KISS on a pty it makes itself and links as `kiss` in its directory; the
console (stdout) carries its log lines. It is configured with `rncfg`, one
invocation per line, the way a person configures a Sergeyculum board over USB.
Its key-value writes are synchronous, so there is nothing to flush.

A setup line is `rncfg` without the program and the port: `<verb> <args…>`,
and the port goes in after the verb, which is where `rncfg` has it
(`rncfg <verb> <PORT> …`). So `set --freq-hz 869525000 --sf 8` runs
`rncfg set <dir>/kiss --freq-hz 869525000 --sf 8`, and `name set {name}`
names the station.

What `rncfg` prints, read from tools/rncfg/src/main.rs:

    detect:           `DETECT   : ok (0x..)` when the station answers
    transport [get]:  `transport: on` | `transport: off`
    a failure:        `error: …` on stderr, exit status 1
"""

import asyncio
import os
import re
import shlex
import shutil

from . import CommandError, Kind, run_tool

DETECT_OK = re.compile(r"^DETECT\s*:\s*ok", re.MULTILINE)
TRANSPORT = re.compile(r"^transport:\s*(on|off)\s*$", re.MULTILINE)

TOOL_TIMEOUT_S = 10.0       # one rncfg invocation, KISS round trips included
POLL_S = 0.5                # how often a booting station is asked whether it is up


class Berlinmesh(Kind):
    type_name = "berlinmesh"

    def __init__(self, name, spec, scenario_dir):
        super().__init__(name, spec, scenario_dir)
        tools = self.spec.get("tools") or {}
        self.rncfg = self.path(tools.get("rncfg")) or shutil.which("rncfg")
        # One rncfg at a time per station: two on one pty interleave their
        # KISS frames and both read garbage.
        self.locks = {}

    def kiss(self, station):
        return os.path.join(station.dir, "kiss")

    def lock_for(self, station):
        return self.locks.setdefault(station.dir, asyncio.Lock())

    async def rncfg_run(self, station, verb, args, timeout=TOOL_TIMEOUT_S):
        """`rncfg <verb> <kiss> <args…>`: its exit status and what it printed."""
        if not self.rncfg:
            raise CommandError("no rncfg: name it under the kind's tools: or put it on PATH")
        async with self.lock_for(station):
            return await run_tool([self.rncfg, verb, self.kiss(station), *args], timeout)

    async def wait_up(self, station, timeout):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if os.path.exists(self.kiss(station)):
                try:
                    _, out = await self.rncfg_run(station, "detect", [])
                    if DETECT_OK.search(out):
                        return True
                except CommandError:
                    pass
            await asyncio.sleep(POLL_S)
        return False

    async def run(self, station, line, timeout=TOOL_TIMEOUT_S):
        try:
            words = shlex.split(line)
        except ValueError as err:
            raise CommandError("%r: %s" % (line, err)) from err
        if not words:
            return ""
        code, out = await self.rncfg_run(station, words[0], words[1:], timeout)
        if code != 0:
            raise CommandError(out.strip() or "rncfg exited %d" % code)
        return out

    async def transport(self, station):
        code, out = await self.rncfg_run(station, "transport", ["get"])
        found = TRANSPORT.search(out) if code == 0 else None
        return None if found is None else found.group(1) == "on"

    def configured(self, station):
        state = os.path.join(station.dir, "state")
        return os.path.isdir(state) and bool(os.listdir(state))
