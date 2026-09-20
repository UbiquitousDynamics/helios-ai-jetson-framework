# Fast connectivity and network-quality routing

The Codex subscription profile makes network availability the first routing
gate. A voice request never waits for a new Internet test:

```text
request
  -> passive kernel gate (default route + carrier + usable IP)
  -> fresh background HTTPS quality result
  -> remote model tier, or immediate local Ollama fallback
```

An interface name or assigned IP alone is insufficient. A Wi-Fi interface can
exist while disconnected, and a local address can remain assigned without a
working gateway, DNS, Internet path, or valid TLS route. ICMP ping is not used
because it can be filtered even when HTTPS works.

## Decision algorithm

On every request, the synchronous passive gate performs only local kernel and
sysfs reads plus an address ioctl:

1. Find the lowest-metric IPv4 or IPv6 default route.
2. Apply `interface_allowlist` and `require_wifi`, when configured.
3. Require interface `operstate` to be `up` or `unknown`.
4. Reject an explicit `carrier=0`.
5. Require a non-loopback, non-link-local, non-multicast IPv4 or IPv6 address.

Any failure yields `Connectivity.OFFLINE` immediately, so the route planner
excludes Codex before starting its process or sending a prompt. Linux route,
link, and address changes are observed through `NETLINK_ROUTE`; they wake the
background monitor without waiting for its periodic timer.

When the passive gate passes, a bounded background HTTPS probe validates the
actual ChatGPT path. It measures DNS, TCP connect, TLS handshake, time to first
byte, and received application payload rate. The rolling estimator uses:

- RFC 6298's SRTT/RTTVAR update constants (alpha 1/8, beta 1/4) on endpoint
  TTFB samples;
- a bounded rolling success ratio as path-loss/failure evidence;
- EWMA application payload rate;
- Wi-Fi RSSI when the kernel exposes it;
- a weighted score dominated by latency, then loss, variation, useful payload
  rate, and signal.

The score is intentionally explainable, not a claimed universal learned
optimum. Hysteresis requires a stronger score to enter remote mode than to
remain there, preventing oscillation near the threshold. An active-probe
failure, changed interface, stale result, captive TLS interception, or score
below threshold fails closed to local. The first successful validation is
required before remote routing is allowed.

The received payload rate is an endpoint-observed application-rate estimate.
True goodput excludes retransmitted bytes and requires transport sequence
visibility, so this probe does not pretend to provide packet-level goodput.
For conversational LLM use, TTFB and path failures receive more weight than
bulk bandwidth.

## Configuration

The committed profile contains:

```toml
[network]
enabled = true
probe_url = "https://chatgpt.com/"
probe_interval_seconds = 3.0
result_max_age_seconds = 6.0
probe_timeout_seconds = 1.2
probe_bytes = 32768
goodput_probe_interval_seconds = 60.0
minimum_quality_score = 0.50
quality_hysteresis = 0.05
target_ttfb_ms = 1200.0
target_jitter_ms = 300.0
minimum_goodput_kbps = 128.0
history_size = 8
require_wifi = false
interface_allowlist = []
```

`require_wifi=false` is deliberate: Ethernet, Wi-Fi, and cellular default
routes can all reach ChatGPT. To force the Jetson's Wi-Fi path:

```toml
require_wifi = true
interface_allowlist = ["wlan0"]
```

Use the actual predictable interface name reported by the Jetson; do not
assume it is always `wlan0`.

Do not place Ethernet and Wi-Fi in the same IPv4 subnet unless policy routing
has been validated. Linux can choose the directly connected Ethernet route for
the gateway while the usable default route is Wi-Fi, producing intermittent
local gateway failures that an HTTPS-only probe cannot fully explain.

The initial/recovery probe measures the bounded payload immediately. Subsequent
three-second checks use HTTPS `HEAD`; the larger payload sample is repeated
only every `goodput_probe_interval_seconds`. This preserves fast failure
detection without downloading 32 KiB every three seconds.

Lowering `probe_interval_seconds` detects degraded paths sooner but consumes
more TLS handshakes and wakeups. `result_max_age_seconds` must be at least the
probe interval, and the goodput interval cannot be shorter. A lower quality
threshold uses remote on poorer paths; a lower TTFB target makes the score
stricter. These values must be calibrated against p50 and p95 measurements on
the deployed modem, carrier, and route.

## Jetson diagnostics

Load the same routing profile used by Helios, then run one passive inspection
and one bounded active probe:

```bash
export HELIOS_LLM_CONFIG="$PWD/examples/llm-routing.codex-subscription.toml"
export HELIOS_LLM_REMOTE_ENABLED=true
python scripts/network_diagnostics.py
```

Exit status is zero only when remote routing is currently admitted. The JSON
contains no IP address, URL, prompt, token, or credential. Important reasons
include `no_default_route`, `interface_down`, `no_carrier`, `no_usable_ip`,
`probe_unreachable`, `quality_below_threshold`, and `quality_validated`.

At INFO level, transitions are logged without content:

```text
Network gate state=online reason=quality_validated interface=wlan0 ...
Network gate state=offline reason=no_carrier interface=None ...
```

During normal operation, the request planning line remains authoritative:

```text
Planning talk request with eligible routes in fallback order: codex-talk-luna,local-talk
Planning talk request with eligible routes in fallback order: local-talk
```

The first line means the fresh network gate admitted remote; the second means
remote was excluded by connectivity or another eligibility gate.

## Deployment-owned limitations

- Probe frequency and data allowance must be approved for the vehicle SIM.
- Captive-portal behavior must be certified on the real NetworkManager setup.
- Wi-Fi RSSI thresholds vary by radio, antenna, driver, and installation.
- A validated HTTPS path cannot guarantee future Codex service latency, quota,
  account availability, or response quality.
- Multi-WAN policy routing and VPN-specific route selection should be tested on
  the exact Jetson image.

Primary references:

- Linux `rt-route` netlink specification:
  <https://www.kernel.org/doc/html/next/networking/netlink_spec/rt-route.html>
- NetworkManager connectivity checking and captive-portal states:
  <https://www.networkmanager.dev/docs/api/latest/NetworkManager.conf.html>
- RFC 6298 SRTT/RTTVAR estimator:
  <https://www.rfc-editor.org/rfc/rfc6298.html>
- RFC 9065 on throughput, goodput, latency, loss, and measurement visibility:
  <https://www.rfc-editor.org/rfc/rfc9065.html>
