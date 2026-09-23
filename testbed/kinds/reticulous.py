"""The reticulous firmware (spangap/reticulous), built for the Linux host.

Spec keys beyond the common ones:

    fixed: <dir>      the build's merged /fixed tree (esp-idf/build.linux/data_merged)

A station of this kind has a text CLI on TCP port 8081 of its own address,
open from boot and needing no login, which is how it is set up and asked
things (setup.py); a web UI on port 80; a store that coalesces writes until
`save`; and `state/boot` once it has booted.
"""

import os

import setup as setup_module

from . import CommandError, Kind

TRANSPORT_KEY = "s.rnsd.transport_enabled"
FLUSH_TIMEOUT_S = 8.0       # how long a station gets to commit its store


class Reticulous(Kind):
    type_name = "reticulous"

    def __init__(self, name, spec, scenario_dir):
        super().__init__(name, spec, scenario_dir)
        self.fixed = self.path(self.spec.get("fixed"))

    def env(self, station):
        env = super().env(station)
        # What this firmware reads: its board (hw-linux) takes its identity,
        # directory, address, ether and /fixed tree from these names.
        env.update(SPANGAP_NODE_ID=str(station.node_id),
                   SPANGAP_NODE_DIR=station.dir,
                   SPANGAP_BIND_ADDR=station.addr,
                   SPANGAP_ETHER=station.ether_addr)
        if self.fixed:
            env["SPANGAP_FIXED_DIR"] = self.fixed
        return env

    async def wait_up(self, station, timeout):
        return await setup_module.wait_until_up(station.node_id, timeout)

    async def run(self, station, line, timeout=setup_module.COMMAND_TIMEOUT):
        try:
            return await setup_module.ask(station.node_id, line, timeout)
        except setup_module.CliError as err:
            raise CommandError(str(err)) from err

    async def setup(self, station, lines):
        """All the lines on one CLI session, then `save`.

        The store coalesces writes for `s.storage.flash_delay` seconds — a
        minute by default — so a station set up and then reset inside that
        window would come back with none of it. `save` is not a setting,
        which is why the testbed sends it and the scenario does not.
        """
        try:
            await setup_module.send(station.node_id, list(lines) + ["save"])
        except setup_module.CliError as err:
            raise CommandError(str(err)) from err

    async def flush(self, station):
        try:
            await setup_module.ask(station.node_id, "save", timeout=FLUSH_TIMEOUT_S)
        except setup_module.CliError:
            pass

    async def transport(self, station):
        reply = await self.run(station, "show %s" % TRANSPORT_KEY)
        value = setup_module.parse_setting(reply, TRANSPORT_KEY)
        if value is None:
            return None
        return value not in ("0", "")

    def web_port(self):
        return 80

    def configured(self, station):
        return os.path.exists(os.path.join(station.dir, "state", "boot"))
