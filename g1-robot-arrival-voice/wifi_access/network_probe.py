"""Temporary /32 routes and proxy ARP for a stationary G1 Wi-Fi check.

No motion APIs, default-route changes or persistent services.
Temporary firewall exceptions are limited to this host and two forwarded peers.
Only the exact resources added by this invocation are removed on rollback.
"""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
import urllib.request

HOST_IF, ROBOT_WIFI, ROBOT_ETH = "wlp131s0f0", "wlan0", "eth0"
HOST_WIFI_IP, ROBOT_WIFI_IP, ALIAS = "192.0.2.5", "192.0.2.18", "192.0.2.6"
DESTINATIONS = ("192.0.2.14", "192.0.2.10", "192.0.2.8")


def run(arguments, check=True):
    return subprocess.run(arguments, check=check, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, universal_newlines=True, timeout=8)


def ip_json(*arguments):
    return json.loads(run(["ip", "-j"] + list(arguments)).stdout)


def inventory():
    return {"addresses": ip_json("-4", "address", "show"),
            "routes": ip_json("-4", "route", "show", "table", "main"),
            "proxy": ip_json("neigh", "show", "proxy"),
            "forwarding": Path("/proc/sys/net/ipv4/ip_forward").read_text().strip(),
            "bridges": bridge_processes()}


def process_identity(pid):
    root = Path('/proc') / str(pid)
    try:
        fields = (root / 'stat').read_text().rsplit(')', 1)[1].split()
        args = (root / 'cmdline').read_bytes().split(b'\0')
        if not args or Path(os.fsdecode(args[0])).name != 'g1_ros1_sdk':
            return None
        return {'pid': int(pid), 'start_ticks': fields[19], 'state': fields[0]}
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return None


def bridge_processes():
    return [entry for p in Path('/proc').iterdir() if p.name.isdigit()
            for entry in [process_identity(p.name)] if entry]


def signal_bridge(action, value):
    # Linux pidfds keep the signal bound to the original process across PID reuse.
    if not hasattr(os, 'pidfd_open') or not hasattr(signal, 'pidfd_send_signal'):
        raise ValueError('Host Python/kernel must support pidfd signalling; no PID-only fallback')
    descriptor = os.pidfd_open(action['pid'])
    try:
        current = process_identity(action['pid'])
        if not current or current['start_ticks'] != action['start_ticks']:
            raise ValueError('Bridge identity changed; refusing signal')
        signal.pidfd_send_signal(descriptor, value)
    finally:
        os.close(descriptor)


def addresses(snapshot, interface):
    return [a["local"] for entry in snapshot["addresses"] if entry["ifname"] == interface
            for a in entry.get("addr_info", [])]


def route_exists(snapshot, destination):
    return any(r.get("dst") in (destination, destination + "/32") for r in snapshot["routes"])


def plan(role, snapshot):
    if role == "host":
        if HOST_WIFI_IP not in addresses(snapshot, HOST_IF):
            raise ValueError("Unexpected navigation-host Wi-Fi address; refusing changes")
        if addresses(snapshot, "enp132s0"):
            raise ValueError("Wired IPv4 address is still present; check is for Wi-Fi only")
        if any(ALIAS in addresses(snapshot, e["ifname"]) for e in snapshot["addresses"]):
            raise ValueError("Navigation alias already exists; refusing to take ownership")
        bridges = snapshot.get('bridges', [])
        if len(bridges) > 1 or any(p['state'] in ('T', 't', 'Z') for p in bridges):
            raise ValueError('Bridge identity is ambiguous or already paused; refusing changes')
        actions = [dict(p, kind='process') for p in bridges]
        actions.append({"kind": "address", "interface": HOST_IF, "address": ALIAS + "/32"})
        for destination in DESTINATIONS:
            if route_exists(snapshot, destination):
                raise ValueError("A host route already exists for " + destination)
            actions.append({"kind": "route", "destination": destination + "/32",
                            "gateway": ROBOT_WIFI_IP, "interface": HOST_IF, "source": ALIAS})
        return actions
    if ROBOT_WIFI_IP not in addresses(snapshot, ROBOT_WIFI) or "192.0.2.8" not in addresses(snapshot, ROBOT_ETH):
        raise ValueError("Unexpected robot addresses; refusing changes")
    if snapshot["forwarding"] != "1":
        raise ValueError("Robot IP forwarding is disabled; it will not be enabled by this probe")
    if route_exists(snapshot, ALIAS):
        raise ValueError("Robot return host route already exists; refusing to replace it")
    if any(p.get("dst") == ALIAS and p.get("dev") == ROBOT_ETH for p in snapshot["proxy"]):
        raise ValueError("Proxy ARP entry already exists; refusing to take ownership")
    return [{"kind": "route", "destination": ALIAS + "/32", "gateway": HOST_WIFI_IP,
             "interface": ROBOT_WIFI}, {"kind": "proxy", "address": ALIAS, "interface": ROBOT_ETH}]


def firewall_plan(token):
    private_state(token)
    actions = []
    for peer in DESTINATIONS:
        if peer == "192.0.2.8":
            continue  # Robot-local traffic uses INPUT/OUTPUT, not FORWARD.
        for protocol in ('udp', 'icmp'):
            for source, destination, incoming, outgoing in (
                    (ALIAS, peer, ROBOT_WIFI, ROBOT_ETH),
                    (peer, ALIAS, ROBOT_ETH, ROBOT_WIFI)):
                actions.append({'kind': 'firewall', 'token': token,
                                'source': source, 'destination': destination,
                                'incoming': incoming, 'outgoing': outgoing,
                                'protocol': protocol})
    return actions


def host_nat_plan(token):
    private_state(token)
    return [{'kind': 'firewall', 'token': token, 'table': 'nat', 'chain': 'POSTROUTING',
             'source': ALIAS, 'destination': peer, 'outgoing': HOST_IF, 'protocol': protocol}
            for peer in DESTINATIONS for protocol in ('udp', 'icmp')]


def firewall_command(action, operation):
    arguments = ['iptables', '-w', '2', '-t', action.get('table', 'filter'),
                 operation, action.get('chain', 'FORWARD')]
    if operation == '-I':
        arguments.append('1')
    if action.get('incoming'):
        arguments += ['-i', action['incoming']]
    return arguments + ['-o', action['outgoing'],
                        '-s', action['source'] + '/32', '-d', action['destination'] + '/32',
                        '-p', action['protocol'], '-m', 'comment', '--comment',
                        'g1-wifi-probe-' + action['token'], '-j', 'ACCEPT']


def command(action, operation):
    if action['kind'] == 'firewall':
        return firewall_command(action, '-I' if operation == 'add' else '-D')
    if action["kind"] == "address":
        return ["ip", "address", operation, action["address"], "dev", action["interface"]]
    if action["kind"] == "proxy":
        return ["ip", "neigh", operation, "proxy", action["address"], "dev", action["interface"]]
    arguments = ["ip", "route", operation, action["destination"], "via", action["gateway"],
                 "dev", action["interface"]]
    if action.get("source"):
        arguments += ["src", action["source"]]
    return arguments


def present(action, snapshot):
    if action['kind'] == 'firewall':
        outcome = run(firewall_command(action, '-C'), check=False)
        if outcome.returncode not in (0, 1):
            raise ValueError('Cannot determine owned firewall rule state: ' + outcome.stderr)
        return outcome.returncode == 0
    if action['kind'] == 'process':
        current = process_identity(action['pid'])
        return bool(current and current['start_ticks'] == action['start_ticks'] and current['state'] in ('T', 't'))
    if action["kind"] == "address":
        return action["address"].split("/")[0] in addresses(snapshot, action["interface"])
    if action["kind"] == "proxy":
        return any(p.get("dst") == action["address"] and p.get("dev") == action["interface"] for p in snapshot["proxy"])
    return any(r.get("dst") in (action["destination"], action["destination"].split("/")[0])
               and r.get("gateway") == action["gateway"] and r.get("dev") == action["interface"]
               and (not action.get("source") or r.get("prefsrc") == action["source"])
               for r in snapshot["routes"])


def save(path, state):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(str(temporary), str(path))


def private_state(token):
    if not re.fullmatch(r"[a-f0-9]{32}", token):
        raise ValueError("Invalid run identity")
    return Path("/run") / ("g1-wifi-probe-" + token)


def heartbeat_alive(path, uid, now):
    try:
        info = path.lstat()
        return (stat.S_ISREG(info.st_mode) and info.st_uid == uid and info.st_nlink == 1
                and stat.S_IMODE(info.st_mode) == 0o600 and 0 <= now - info.st_mtime <= 30)
    except OSError:
        return False


class LeaseWindow:
    """An initial setup grace, then 30s without heartbeat; hard cap one hour."""
    def __init__(self, now, grace):
        self.started = now
        self.deadline = now + grace

    def active(self, now, heartbeat):
        if now >= self.started + 3600:
            return False
        # The full setup grace permits interactive sudo authentication. After
        # that grace heartbeat_alive already bounds file age to 30 seconds.
        return now < self.deadline or heartbeat


def packet_header(data):
    """Return only relevant Ethernet/ARP/IP header fields, never payload."""
    import socket
    import struct
    if len(data) < 14:
        return None
    mac = lambda value: ':'.join('%02x' % part for part in value)
    result = {'source_mac': mac(data[6:12]), 'destination_mac': mac(data[:6])}
    ethertype = struct.unpack('!H', data[12:14])[0]
    relevant = {ALIAS, *DESTINATIONS}
    if ethertype == 0x0806 and len(data) >= 42:
        if data[14:20] != b'\x00\x01\x08\x00\x06\x04':
            return None
        source, destination = socket.inet_ntoa(data[28:32]), socket.inet_ntoa(data[38:42])
        if not ({source, destination} & relevant): return None
        result.update(kind='arp', operation=struct.unpack('!H', data[20:22])[0],
                      source=source, destination=destination)
    elif ethertype == 0x0800 and len(data) >= 34:
        ihl = (data[14] & 15) * 4
        if data[14] >> 4 != 4 or ihl < 20 or len(data) < 14 + ihl:
            return None
        source, destination = socket.inet_ntoa(data[26:30]), socket.inet_ntoa(data[30:34])
        if not ({source, destination} & relevant): return None
        protocol = data[23]
        result.update(kind='ipv4', source=source, destination=destination,
                      protocol=protocol, ttl=data[22])
        # Later fragments do not contain a transport header.
        fragment_offset = struct.unpack('!H', data[20:22])[0] & 0x1fff
        start = 14 + ihl
        if fragment_offset == 0 and protocol == 17 and len(data) >= start + 8:
            result['source_port'], result['destination_port'] = struct.unpack('!HH', data[start:start+4])
        elif fragment_offset == 0 and protocol == 1 and len(data) > start:
            result['icmp_type'] = data[start]
    else:
        return None
    return result


def diagnose(directory):
    """Read active routing and counters; never changes configuration."""
    state = json.loads((directory / 'state.json').read_text())
    role = state.get('role')
    if role not in ('robot', 'host') or state.get('phase') != 'temporary_active':
        raise ValueError('Diagnostic requires this run to be active')
    commands = [
        ['ip', '-4', 'route', 'get', '192.0.2.10', 'from', ALIAS, 'iif', ROBOT_WIFI],
        ['ip', '-4', 'route', 'get', ALIAS, 'from', '192.0.2.10', 'iif', ROBOT_ETH],
        ['ip', 'neigh', 'show', 'dev', ROBOT_ETH], ['ip', 'neigh', 'show', 'proxy'],
        ['iptables', '-w', '2', '-t', 'filter', '-n', '-v', '-x', '-L', 'FORWARD'],
        ['iptables', '-w', '2', '-t', 'raw', '-S'],
        ['iptables', '-w', '2', '-t', 'mangle', '-S'],
        ['iptables', '-w', '2', '-t', 'nat', '-S']]
    if role == 'host':
        commands = [
            ['ip', '-4', 'route', 'get', '192.0.2.10', 'from', ALIAS],
            ['iptables', '-w', '2', '-t', 'nat', '-S'],
            ['iptables', '-w', '2', '-n', '-v', '-x', '-L', 'INPUT'],
            ['ip', 'neigh', 'show', 'dev', HOST_IF]]
    result = []
    for arguments in commands:
        outcome = run(arguments, check=False)
        result.append({'command': arguments, 'code': outcome.returncode,
                       'output': outcome.stdout[-12000:], 'error': outcome.stderr[-1000:]})
    return {role + '_readonly_diagnostic': result}


def udp_counters():
    """Read kernel receive counters and existing Livox socket queues."""
    result = {}
    path = Path('/proc/net/snmp')
    if path.exists():
        result['udp_snmp'] = [line for line in path.read_text().splitlines() if line.startswith('Udp:')]
    path = Path('/proc/net/udp')
    if path.exists():
        ports = {'%04X' % port for port in (56101, 56201, 56301, 56401, 56501)}
        result['livox_udp_socket_rows'] = [line for line in path.read_text().splitlines()[1:]
                                         if line.split()[1].split(':')[1].upper() in ports]
    for name in ('/proc/net/nf_conntrack', '/proc/net/ip_conntrack'):
        path = Path(name)
        if path.exists():
            try:
                with path.open() as stream:
                    result['existing_alias_udp_conntrack'] = [line.strip() for line in stream
                        if 'udp' in line and ('src=' + ALIAS + ' ') in line][:24]
            except OSError as error: result['conntrack_read_error'] = str(error)
            break
    return result


def observe(directory, seconds):
    """Bounded passive header observation; no packet sends."""
    import socket
    import select
    token = directory.name.rsplit('-', 1)[1]
    state = json.loads((directory / 'state.json').read_text())
    role = state.get('role')
    if os.geteuid() != 0 or role not in ('robot', 'host') or state.get('phase') != 'temporary_active':
        raise ValueError('Passive observer requires an active root-owned run')
    sockets, counts, port_pairs, ignored = [], {}, {}, 0
    try:
        for interface in ((ROBOT_WIFI, ROBOT_ETH) if role == 'robot' else (HOST_IF,)):
            sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(3))
            sockets.append(sock)
            sock.bind((interface, 0))
            sock.setblocking(False)
        (directory / 'observer_ready').write_text('ready')
        deadline = time.monotonic() + seconds
        initial = diagnose(directory)
        initial_udp = udp_counters() if role == 'host' else {}
        last_counters, next_counters = {}, 0
        while time.monotonic() < deadline and not Path('/tmp/g1-wifi-stop-' + token).exists():
            if json.loads((directory / 'state.json').read_text()).get('phase') != 'temporary_active':
                break
            if time.monotonic() >= next_counters:
                counter = run(['iptables', '-w', '2', '-t', 'filter' if role == 'robot' else 'nat',
                               '-n', '-v', '-x', '-L', 'FORWARD' if role == 'robot' else 'POSTROUTING'], check=False)
                last_counters = {'code': counter.returncode,
                                 'owned_rule_counters': [line for line in counter.stdout.splitlines()
                                                         if 'g1-wifi-probe-' + token in line],
                                 'error': counter.stderr[-1000:]}
                next_counters = time.monotonic() + 5
            ready, _, _ = select.select(sockets, [], [], 0.3)
            for sock in ready:
                data, address = sock.recvfrom(128)
                header = packet_header(data)
                if header is None: continue
                header.update(interface=address[0], direction='outgoing' if address[2] == 4 else 'incoming')
                ports = (header.pop('source_port', None), header.pop('destination_port', None))
                key = json.dumps(header, sort_keys=True)
                if key in counts or len(counts) < 32:
                    counts[key] = counts.get(key, 0) + 1
                    if ports[0] is not None:
                        pairs = port_pairs.setdefault(key, [])
                        if ports not in pairs and len(pairs) < 8: pairs.append(ports)
                else: ignored += 1
    finally:
        for sock in sockets: sock.close()
    report = {'passive_headers': [dict(json.loads(k), count=v, udp_port_pairs=port_pairs.get(k, []))
                                 for k, v in counts.items()],
              'additional_flow_packets_omitted': ignored, 'packet_sends': 0,
              'role': role, 'initial_diagnostic': initial,
              'latest_forward_counters' if role == 'robot' else 'latest_nat_counters': last_counters,
              'initial_udp_counters': initial_udp,
              'final_udp_counters': udp_counters() if role == 'host' else {}}
    destination = '/tmp/g1-wifi-packets-' + token + '.json'
    temporary = destination + '.part'
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(report, stream)
        stream.flush()
        os.fchmod(stream.fileno(), 0o644)
    try:
        os.link(temporary, destination)  # Atomic publication, without replacing an existing path.
    finally:
        os.unlink(temporary)


def ensure_idle():
    def get(endpoint):
        with urllib.request.urlopen("http://127.0.0.1:9000/api/" + endpoint, timeout=4) as response:
            return json.load(response)
    nav, voice = get("extra/get_status"), get("task/voice_status")
    navigation = str(nav.get("control_status", ""))
    admitted_states = {'导航模式 空闲': 'idle',
                       '导航模式 请重新设置位姿（重定位）': 'localization_required'}
    if navigation not in admitted_states:
        raise ValueError("Navigation state is not admitted; cancel navigation first: " + navigation)
    task = str(nav.get("task_status", ""))
    if task != "无任务" and not any(word in task for word in ("已完成", "取消")):
        raise ValueError("Task is not confirmed idle: " + task)
    if voice.get("managed") or voice.get("paused"):
        raise ValueError("A speech task is active or paused; terminate it first")
    return {"navigation": navigation, "task": task, 'state_kind': admitted_states[navigation]}


def rollback(directory):
    import fcntl
    if directory.is_symlink() or directory.stat().st_uid != 0:
        raise ValueError("Rollback state must be owned by root")
    with (directory / "lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = directory / "state.json"
        state = json.loads(path.read_text())
        results = []
        for action in reversed(state["actions"]):
            try:
                if present(action, inventory()):
                    if action['kind'] == 'process':
                        if any(present(a, inventory()) for a in state['actions'] if a['kind'] != 'process'):
                            results.append({'action': action, 'error': 'Network rollback incomplete; bridge remains paused'})
                            continue
                        signal_bridge(action, signal.SIGCONT)
                        time.sleep(0.05)
                        results.append({'action': action, 'code': 0})
                    else:
                        outcome = run(command(action, "del"), check=False)
                        results.append({"action": action, "code": outcome.returncode, "error": outcome.stderr})
            except Exception as error:
                # A failed firewall inspection must not skip independent route cleanup.
                results.append({'action': action, 'error': str(error)})
        state["rollback"] = results
        state["remaining"] = []
        for action in state['actions']:
            try:
                if present(action, inventory()): state['remaining'].append(action)
            except Exception as error:
                state['remaining'].append(dict(action, verification_error=str(error)))
        state["phase"] = "rolled_back" if not state["remaining"] else "rollback_incomplete"
        save(path, state)
        report = {"phase": state["phase"], "remaining": state["remaining"]}
        token = directory.name.rsplit("-", 1)[1]
        result_path = "/tmp/g1-wifi-result-" + token + ".json"
        try:
            fd = os.open(result_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
            os.fchmod(fd, 0o644)  # Public summary only; the full snapshot remains root-private.
            with os.fdopen(fd, "w") as stream:
                stream.write(json.dumps(report))
        except FileExistsError:
            pass
        print(json.dumps(report), flush=True)
        return not state["remaining"]


def apply(role, directory, seconds, passive_observer=False, preserve_source=False, session_lease=False):
    import fcntl
    if os.geteuid() != 0:
        raise ValueError("Temporary network changes require sudo")
    before = inventory()
    lease_uid = None
    if session_lease:
        lease_uid = int(os.environ.get('SUDO_UID', '-1'))
        heartbeat = Path('/tmp/g1-network-heartbeat-' + directory.name.rsplit('-', 1)[1])
        if lease_uid <= 0 or not heartbeat_alive(heartbeat, lease_uid, time.time()):
            raise ValueError('Session requires a fresh private heartbeat owned by the sudo caller')
        if before.get('bridges'):
            raise ValueError('Session handover requires the legacy bridge to be stopped; it will not be paused/restarted')
    actions = plan(role, before)
    if preserve_source:
        if role != 'host': raise ValueError('NAT bypass is only for the navigation host')
        # Insert before any route restoration; the existing bridge stays paused throughout.
        insertion = next(i for i, action in enumerate(actions) if action['kind'] == 'route')
        actions[insertion:insertion] = host_nat_plan(directory.name.rsplit('-', 1)[1])
    idle = ensure_idle() if role == "host" else None
    if any(a['kind'] == 'process' for a in actions) and (
            not hasattr(os, 'pidfd_open') or not hasattr(signal, 'pidfd_send_signal')):
        raise ValueError('Host Python must support pidfds before the bridge can be paused')
    if role == 'robot' or preserve_source:
        if not shutil.which('iptables'):
            raise ValueError('iptables is required for bounded firewall exceptions')
        if role == 'robot': actions += firewall_plan(directory.name.rsplit('-', 1)[1])
        if any(present(a, before) for a in actions if a['kind'] == 'firewall'):
            raise ValueError('A firewall rule for this run already exists; refusing ownership')
    if role == "robot":
        # A responsive device using .10 would conflict with the navigation alias.
        if run(["ping", "-I", ROBOT_ETH, "-c", "1", "-W", "1", ALIAS], check=False).returncode == 0:
            raise ValueError("A device already answers for 192.0.2.6 on eth0; refusing proxy ARP")
    os.umask(0o077)
    directory.mkdir(mode=0o700)
    state = {"role": role, "before": before, "idle": idle, "actions": [], "phase": "prepared"}
    if session_lease:
        state['session_lease'] = {'uid': lease_uid, 'hard_limit_seconds': 3600, 'heartbeat_timeout_seconds': 30}
    if role == 'robot':
        state['forward_filter'] = {}
        for chain in ('FORWARD', 'DOCKER-USER'):
            rules = run(['iptables', '-w', '2', '-S', chain], check=False)
            state['forward_filter'][chain] = {'code': rules.returncode, 'output': rules.stdout[-4000:], 'error': rules.stderr[-1000:]}
        print(json.dumps({'forward_filter_readonly': state['forward_filter']}), flush=True)
    save(directory / "state.json", state)
    trusted = directory / "rollback.py"
    shutil.copyfile(__file__, str(trusted))
    with (directory / "lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with (directory / "timer.log").open("a") as log:
            timer = [sys.executable, str(trusted), "rollback", "--token", directory.name.rsplit("-", 1)[1],
                     "--delay", str(seconds)]
            if session_lease: timer += ['--heartbeat-uid', str(lease_uid)]
            subprocess.Popen(timer, stdin=subprocess.DEVNULL, stdout=log,
                             stderr=subprocess.STDOUT, start_new_session=True)
        try:
            for action in actions:
                # Journal intent before mutation, retaining uncertain results for the watchdog.
                state["actions"].append(action)
                save(directory / "state.json", state)
                try:
                    if action['kind'] == 'process':
                        current = process_identity(action['pid'])
                        if not current or current['start_ticks'] != action['start_ticks']:
                            raise ValueError('Bridge changed after preflight; refusing to signal it')
                        signal_bridge(action, signal.SIGSTOP)
                        time.sleep(0.1)
                        if not present(action, inventory()):
                            raise ValueError('Could not confirm bridge pause; refusing network setup')
                    else:
                        run(command(action, "add"))
                except subprocess.CalledProcessError:
                    # A rejected atomic netlink add does not grant ownership of an existing resource.
                    state["actions"].pop()
                    save(directory / "state.json", state)
                    raise
            state["phase"] = "temporary_active"
            save(directory / "state.json", state)
            if passive_observer:
                with (directory / 'observer.log').open('a') as log:
                    observer = subprocess.Popen([sys.executable, str(trusted), 'observe',
                                                 '--token', directory.name.rsplit('-', 1)[1],
                                                 '--seconds', str(seconds)], stdin=subprocess.DEVNULL,
                                                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                for _ in range(20):
                    if (directory / 'observer_ready').exists(): break
                    if observer.poll() is not None: raise ValueError('Passive observer failed; see root-private observer.log')
                    time.sleep(0.1)
                else: raise ValueError('Passive observer did not become ready')
            print(json.dumps({"phase": state["phase"], "role": role, "auto_rollback_seconds": seconds,
                              "token": directory.name.rsplit("-", 1)[1]}), flush=True)
        except BaseException:
            fcntl.flock(lock, fcntl.LOCK_UN)
            rollback(directory)
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("role", choices=("host", "robot", "rollback", "observe", "diagnose"))
    parser.add_argument("--token")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--check-state", action="store_true")
    parser.add_argument("--passive-observer", action="store_true")
    parser.add_argument("--preserve-source", action="store_true")
    parser.add_argument("--stationary-confirmed", action="store_true")
    parser.add_argument("--seconds", type=int, default=180)
    parser.add_argument("--delay", type=int, default=0)
    parser.add_argument("--session-lease", action="store_true")
    parser.add_argument("--heartbeat-uid", type=int)
    args = parser.parse_args()
    if args.preserve_source and args.role != 'host':
        raise ValueError('--preserve-source is only supported on the host')
    if args.role in ('observe', 'diagnose'):
        directory = private_state(args.token)
        if os.geteuid() != 0 or directory.is_symlink() or directory.stat().st_uid != 0:
            raise ValueError('Diagnostic state must be owned by root')
        if args.role == 'observe': observe(directory, max(1, min(args.seconds, 180)))
        else: print(json.dumps(diagnose(directory)), flush=True)
        return 0
    if args.role == "rollback":
        directory = private_state(args.token)
        stop_path = Path("/tmp/g1-wifi-stop-" + args.token)
        window = LeaseWindow(time.monotonic(), max(0, min(args.delay, 300)))
        heartbeat = Path('/tmp/g1-network-heartbeat-' + args.token)
        while not stop_path.exists() and window.active(time.monotonic(),
                args.heartbeat_uid is not None and heartbeat_alive(heartbeat, args.heartbeat_uid, time.time())):
            time.sleep(0.5)
        return 0 if rollback(directory) else 1
    if not args.apply:
        snapshot = inventory()
        proposed = plan(args.role, snapshot)
        if args.role == 'robot':
            proposed += firewall_plan('0' * 32)
        elif args.preserve_source:
            insertion = next(i for i, action in enumerate(proposed) if action['kind'] == 'route')
            proposed[insertion:insertion] = host_nat_plan('0' * 32)
        print(json.dumps({"role": args.role, "proposed": proposed,
                          'firewall_token_note': 'preview uses zero token; apply generates unique run token'}))
        if args.check_state:
            if args.role != 'host':
                raise ValueError('--check-state is only supported on the navigation host')
            print(json.dumps({'navigation_preflight': ensure_idle()}), flush=True)
        return 0
    if not args.stationary_confirmed:
        raise ValueError("Confirm the robot is stationary, navigation cancelled, supervised and remote stop available")
    if not 60 <= args.seconds <= 300:
        raise ValueError("Temporary probe must roll back within 60–300 seconds")
    apply(args.role, private_state(args.token), args.seconds, args.passive_observer, args.preserve_source, args.session_lease)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr, flush=True)
        sys.exit(1)
