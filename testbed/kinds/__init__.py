"""Station kinds: what the testbed must know about one kind of firmware.

A station is a process that keeps the station contract (STATION.md): it reads
its identity, directory, address and ether from `SIMESH_*` in its
environment, runs in its directory, and treats stdin/stdout as its console.
Everything beyond that differs by firmware — how to tell it is up, how to
type a setup line at it, whether it can be asked if it forwards, whether it
has a web UI — and that is a kind.

A scenario names its kinds under `kinds:`, each with the binary and whatever
that kind takes (`env:`, `setup:`, and keys of its own); every node is one of
them. `type:` picks the class and defaults to the kind's own name, so two
builds of one firmware can sit side by side as two kinds of one type.

Paths in a kind's spec are relative to the directory scenario files live in
(`testbed/scenarios/`), whichever copy of the scenario is being read; an
`env:` value is taken as a path only when it starts with `./` or `../`.
"""

import asyncio
import os


class CommandError(Exception):
    """A station could not be asked, or did not answer in time."""


class Kind:
    """One kind of station. Subclasses say how to talk to it."""

    type_name = None                # the `type:` a spec names this class by

    def __init__(self, name, spec, scenario_dir):
        self.name = name
        self.spec = dict(spec or {})
        self.scenario_dir = scenario_dir
        self.elf = self.path(self.spec.get("elf"))
        self.extra_env = {
            str(key): self.env_value(value)
            for key, value in (self.spec.get("env") or {}).items()}

    # ---- paths -----------------------------------------------------------

    def path(self, value):
        """A spec path made absolute against the scenarios directory."""
        if not value:
            return None
        value = os.path.expanduser(str(value))
        if not os.path.isabs(value):
            value = os.path.join(self.scenario_dir, value)
        return os.path.normpath(value)

    def env_value(self, value):
        value = str(value)
        if value.startswith("./") or value.startswith("../"):
            return self.path(value)
        return value

    # ---- the contract ----------------------------------------------------

    def env(self, station):
        """The station's environment: the contract, then the kind's own."""
        env = {"SIMESH_NODE_ID": str(station.node_id),
               "SIMESH_NODE_DIR": station.dir,
               "SIMESH_BIND_ADDR": station.addr,
               "SIMESH_ETHER": station.ether_addr}
        env.update(self.extra_env)
        return env

    async def wait_up(self, station, timeout):
        """True once the station answers the way this kind answers."""
        raise NotImplementedError

    async def run(self, station, line, timeout=None):
        """One line typed at the station; what it said back."""
        raise NotImplementedError

    async def setup(self, station, lines):
        """The setup lines of a station that has never been set up, then a flush.

        Every line is tried; the ones that could not be asked are reported
        together at the end.
        """
        failed = []
        for line in lines:
            try:
                await self.run(station, line)
            except CommandError as err:
                failed.append("%s: %s" % (line, err))
        await self.flush(station)
        if failed:
            raise CommandError("; ".join(failed))

    async def flush(self, station):
        """Make what the station has been told durable; best effort."""

    async def transport(self, station):
        """Whether the station forwards for others.

        None when this kind cannot say; CommandError when the station could
        not be asked this time, which leaves the last answer standing.
        """
        return None

    def web_port(self):
        """The port its web UI answers on, or None when it has none."""
        return None

    def configured(self, station):
        """True when this station's directory has been set up already."""
        raise NotImplementedError

    def describe(self):
        return "%s (%s) %s" % (self.name, self.type_name, self.elf or "no binary")


def kind_types():
    """Every kind class by its type name."""
    from . import berlinmesh, reticulous
    return {cls.type_name: cls for cls in (reticulous.Reticulous, berlinmesh.Berlinmesh)}


def make_kinds(specs, scenario_dir):
    """The scenario's kinds, in order, by name. The first is the default."""
    types = kind_types()
    kinds = {}
    for name, spec in (specs or {}).items():
        spec = spec or {}
        type_name = spec.get("type", name)
        cls = types.get(type_name)
        if cls is None:
            raise CommandError("kind %r: no such type %r (known: %s)"
                               % (name, type_name, ", ".join(sorted(types))))
        kinds[name] = cls(name, spec, scenario_dir)
    return kinds


async def run_tool(argv, timeout):
    """Run a helper program to completion: its exit code and all it printed."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    except OSError as err:
        raise CommandError("%s: %s" % (argv[0], err)) from err
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError as err:
        proc.kill()
        await proc.wait()
        raise CommandError("%s gave no answer in %.0fs" % (os.path.basename(argv[0]),
                                                          timeout)) from err
    return proc.returncode, out.decode("utf-8", "replace")
