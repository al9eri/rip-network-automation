# Section 6a — Link-Failure Tests and Results

> **Author:** Zayed (Measurement, Testing and Visualisation)  
> **Status:** Draft ready for report integration. Placeholders marked `[LAB_VALUE: ...]` are filled automatically by running `python scripts/collect_results.py` and `python scripts/visualize.py` after the lab session.

---

## 6a.1 Purpose of the Experiments

The three-router triangle between the Library (`LIB-R1`), the Teachers' room (`TCH-R2`), and the Laboratory (`LAB-R3`) gives every LAN (`192.168.10.0/24`, `192.168.20.0/24`, and `192.168.30.0/24`) two physical paths to every other LAN: a direct 1-hop path and a 2-hop backup path through the third router. Under normal operation (`T0`), RIPv2 installs the 1-hop path for each remote room LAN and two equal-cost 1-hop paths for the opposite WAN `/30` link.

The goal of the link-failure experiments (`T1` to `T5`) is to measure how quickly RIPv2 detects a failure, removes the broken route, and installs the 2-hop backup path across two very different failure modes:

1. **Direct interface state changes (`T1`–`T4`):** When a local serial or GigabitEthernet interface goes down (or comes back up in `T4`), Cisco IOS immediately notifies the routing table (`%LINK` / `%LINEPROTO`), deletes the affected 1-hop route, and sends a triggered ("flash") poisoned update (`metric 16`). However, because RIP does not keep a backup topology table in memory like OSPF or EIGRP, the router must still wait for its surviving neighbour's next periodic RIP update (sent every 30 s) before it can install the 2-hop alternative route.
2. **Silent neighbour failure (`T5`):** When a neighbour stops sending RIP updates without the physical link going down (simulated by applying `passive-interface GigabitEthernet0/1` on `LAB-R3`), `LIB-R1` receives no link-down interrupt. Instead, it has to wait for RIP's timers to expire: the 180 s invalid timer (when the route enters hold-down as `is possibly down` at metric 16) and the 240 s flush timer (when the stale route is deleted so the 2-hop route from `TCH-R2` can be installed).

---

## 6a.2 Test Methodology and Measurement Techniques

To make sure random alignment with the 30-second periodic update timer does not skew our findings, each failure test (`T1`–`T5`) is executed **three independent times** (`Run 1`, `Run 2`, `Run 3`), and we record the individual values, the mean, and the min–max range.

Before running the first test, we push [`configs/push/timestamps.txt`](../configs/push/timestamps.txt) to all three routers using [`scripts/push_config.py`](../scripts/push_config.py):

```text
service timestamps debug datetime msec
service timestamps log datetime msec
logging buffered 64000 debugging
```

This configuration puts millisecond timestamps on every debug and syslog line and stores up to 64 KB of debugging output in router RAM so captures survive even if an SSH session drops during a test.

We measure convergence using **three complementary methods**:

1. **Router-clock timestamped debug logs (Primary — Millisecond Precision):**  
   Using [`scripts/capture_rip_debug.py`](../scripts/capture_rip_debug.py) (or [`scripts/run_experiment.py`](../scripts/run_experiment.py)), we clear the router's log buffer (`clear logging`) and enable both `debug ip rip` and `debug ip routing` on the affected router. After the run, the script sends `undebug all` and saves `show logging` to `data/debug_logs/<test>-run<n>_<router>.txt`.  
   [`scripts/parse_rip_debug.py`](../scripts/parse_rip_debug.py) then calculates convergence entirely on **that single router's internal clock**:
   - **Start time ($t_{\text{start}}$):** For `T1`–`T4`, the timestamp of the first `%LINK` / `%LINEPROTO` interface state-change line. For `T5` (where no interface goes down), the timestamp of the last `RIP: received v2 update` heard from the silent neighbour (`10.0.13.2`), which is the exact moment RIP's 180 s invalid timer starts counting.
   - **End time ($t_{\text{end}}$):** The timestamp of the `RT: add <network> via <next_hop>, rip metric [120/<m>]` line emitted by `debug ip routing` when the new route is actually installed in the forwarding table (not merely when an update packet arrives, since backup advertisements arriving while a 1-hop route is still valid are ignored).
   - **Convergence time:** $\Delta t = t_{\text{end}} - t_{\text{start}}$. Because both timestamps come from the same router, clock drift between routers and the laptop does not affect the measurement.

2. **Python routing-table polling (`monitor_convergence.py` — ~1 s Granularity):**  
   [`scripts/monitor_convergence.py`](../scripts/monitor_convergence.py) keeps an SSH session open to the measured router, runs `show ip route rip` in a loop (targeting 1.0 s intervals), and parses each response with `get_routes.parse_routes()`. Whenever a route disappears (`route_lost`), enters hold-down (`possibly_down`, metric 16), changes metric or next hop (`route_changed`), or reappears (`route_restored`), the script writes an event row to `data/monitor_logs/<test>-run<n>_<router>.csv` and logs every poll's actual send/receive duration to `<test>-run<n>_<router>_polls.csv`. It distinguishes the operator's keypress mark (`mark`), the first observed routing change (`first_change`), and the moment the target route reaches its expected metric/next hop and stays stable for at least one full 30 s update period (`converged`).

3. **Continuous ICMP ping loss (`ping -t -w 1000`):**  
   In parallel with the scripts, we run a continuous ping with a 1-second timeout (`ping -t -w 1000 <dest-ip>`) between rooms (replacing Windows' default 4-second timeout so each `Request timed out.` corresponds to roughly 1 second of data-plane outage).

---

## 6a.3 Description of Each Test Scenario

| Test | Measured Router | Target Route | How the Event Is Triggered | Expected Routing Behaviour |
|---|---|---|---|---|
| **T0** Baseline | All (`LIB-R1`, `TCH-R2`, `LAB-R3`) | All remote `/24` and `/30` | All interfaces up; run `get_routes.py T0-baseline` | Every router reaches the other two room LANs at metric 1 and has 2 equal-cost paths to the opposite WAN `/30`. |
| **T1** Library–Teachers serial down | `LIB-R1` | `192.168.20.0/24` | On `LIB-R1`: `interface Serial0/0/0` $\rightarrow$ `shutdown` | `LIB-R1` immediately deletes the 1-hop route via `10.0.12.2` and installs `192.168.20.0/24` via `LAB-R3` (`10.0.13.2`, `Gi0/1`) at **metric 2** on `LAB-R3`'s next update (~0–30 s). |
| **T2** Library–Lab Ethernet down | `LIB-R1` | `192.168.30.0/24` | From `LIB-R1` **console**: `interface GigabitEthernet0/1` $\rightarrow$ `shutdown` | `LIB-R1` switches `192.168.30.0/24` to `TCH-R2` (`10.0.12.2`, `Se0/0/0`) at **metric 2** (~0–30 s). Because `AUTO-PC`'s SSH session to `LIB-R1` uses `Gi0/1`, SSH drops until RIP converges via `TCH-R2`; the capture is recovered from `LIB-R1`'s 64 KB log buffer. |
| **T3** Teachers–Lab serial down | `TCH-R2` | `192.168.30.0/24` | From `TCH-R2` **console**: `interface Serial0/0/1` $\rightarrow$ `shutdown` | `TCH-R2` switches `192.168.30.0/24` to `LIB-R1` (`10.0.12.1`, `Se0/0/0`) at **metric 2** (~0–30 s). Like `T2`, `AUTO-PC` reaches `TCH-R2` over `Se0/0/1`, so the monitor logs the SSH gap and reconnects once the path via `LIB-R1` is up. |
| **T4** Link recovery | `LIB-R1` | `192.168.20.0/24` | With `T1` link down and converged, on `LIB-R1`: `interface Serial0/0/0` $\rightarrow$ `no shutdown` | Once `Serial0/0/0` comes up and `TCH-R2` exchanges updates over it, `LIB-R1` replaces the metric-2 route with the direct **metric 1** route via `10.0.12.2`. |
| **T5** Silent RIP update failure | `LIB-R1` | `192.168.30.0/24` | Push [`configs/push/t5-start.txt`](../configs/push/t5-start.txt) (`passive-interface Gi0/1`) to `LAB-R3` with `--no-save` | `Gi0/1` stays physically `up/up`, so `LIB-R1` keeps the stale metric-1 route until the **180 s invalid timer** marks it `possibly down` (metric 16), and replaces it with `TCH-R2`'s **metric 2** path (`10.0.12.2`) after the **240 s flush timer** removes the stale entry (~240–270 s total). Leaves asymmetric routing (`LIB-R1` $\rightarrow$ `TCH-R2` $\rightarrow$ `LAB-R3`, while `LAB-R3` $\rightarrow$ `LIB-R1` stays direct). |

---

## 6a.4 Experimental Results

### Table 6a.1 — Actual Lab Measurements (Template to Fill After Lab Session)

*Run `python scripts/collect_results.py` after completing the lab runs to populate this table from `data/results.csv`:*

| Test | Run 1 (s) | Run 2 (s) | Run 3 (s) | Average (s) | Min–Max (s) | Pings lost (avg) |
|---|---:|---:|---:|---:|---:|---:|
| **T1** (Library–Teachers serial down) | `[LAB_VALUE: T1-run1]` | `[LAB_VALUE: T1-run2]` | `[LAB_VALUE: T1-run3]` | `[LAB_VALUE: T1-avg]` | `[LAB_VALUE: T1-minmax]` | `[LAB_VALUE: T1-pings]` |
| **T2** (Library–Lab Ethernet down) | `[LAB_VALUE: T2-run1]` | `[LAB_VALUE: T2-run2]` | `[LAB_VALUE: T2-run3]` | `[LAB_VALUE: T2-avg]` | `[LAB_VALUE: T2-minmax]` | `[LAB_VALUE: T2-pings]` |
| **T3** (Teachers–Lab serial down) | `[LAB_VALUE: T3-run1]` | `[LAB_VALUE: T3-run2]` | `[LAB_VALUE: T3-run3]` | `[LAB_VALUE: T3-avg]` | `[LAB_VALUE: T3-minmax]` | `[LAB_VALUE: T3-pings]` |
| **T4** (Recovery — T1 link back up) | `[LAB_VALUE: T4-run1]` | `[LAB_VALUE: T4-run2]` | `[LAB_VALUE: T4-run3]` | `[LAB_VALUE: T4-avg]` | `[LAB_VALUE: T4-minmax]` | `[LAB_VALUE: T4-pings]` |
| **T5** (Silent failure — passive Gi0/1) | `[LAB_VALUE: T5-run1]` | `[LAB_VALUE: T5-run2]` | `[LAB_VALUE: T5-run3]` | `[LAB_VALUE: T5-avg]` | `[LAB_VALUE: T5-minmax]` | `[LAB_VALUE: T5-pings]` |

### Table 6a.2 — Simulated Reference Output (Offline Verification Only)

> **Important Note:** The values in Table 6a.2 were generated offline by [`scripts/simulate_data.py`](../scripts/simulate_data.py) and [`scripts/sim_router.py`](../scripts/sim_router.py) to verify the parsing, aggregation, and plotting pipeline before accessing the university lab routers. **They are simulated sample values, not physical router measurements, and must be replaced by Table 6a.1 once lab testing is complete.**

| Test | Run 1 (s) | Run 2 (s) | Run 3 (s) | Average (s) | Min–Max (s) | Pings lost (avg) |
|---|---:|---:|---:|---:|---:|---:|
| **T1** (Library–Teachers serial down) | 7.42 | 18.16 | 11.69 | 12.42 | 7.42–18.16 | 13.0 |
| **T2** (Library–Lab Ethernet down) | 14.21 | 6.86 | 22.41 | 14.49 | 6.86–22.41 | 15.0 |
| **T3** (Teachers–Lab serial down) | 9.61 | 21.11 | 13.76 | 14.82 | 9.61–21.11 | 15.3 |
| **T4** (Recovery — T1 link back up) | 4.30 | 12.90 | 8.15 | 8.46 | 4.30–12.90 | 0.0 |
| **T5** (Silent failure — passive Gi0/1) | 252.41 | 263.81 | 246.91 | 254.37 | 246.91–263.81 | 14.3 |

---

## 6a.5 Generated Charts and Figure Captions

[`scripts/visualize.py`](../scripts/visualize.py) generates four charts from the collected CSV logs. When run against the offline sample dataset (`python scripts/visualize.py --simulated`), each chart is stamped with a red **`SIMULATED SAMPLE DATA — NOT FROM PHYSICAL ROUTERS`** banner and watermark; running `python scripts/visualize.py` after the lab session produces the clean report figures in `data/charts/`.

1. **Figure 6a.1 — Convergence Time Comparison (`convergence_comparison.png` / `SIMULATED_convergence_comparison.png`):**  
   *Caption:* **Figure 6a.1:** Mean RIPv2 convergence time across three runs per experiment (`T1`–`T5`), with vertical error bars indicating the minimum and maximum run durations and white markers showing individual runs. The left panel compares all five experiments against the 180 s RIP invalid timer and 240 s flush timer, while the right panel zooms into the direct interface-change tests (`T1`–`T4`) relative to the 30 s periodic RIP update interval.

2. **Figure 6a.2 — T1 Route-Change Timeline (`timeline_T1-run1_LIB-R1.png` / `SIMULATED_timeline_T1-run1_LIB-R1.png`):**  
   *Caption:* **Figure 6a.2:** Route-state step timeline on `LIB-R1` for destination `192.168.20.0/24` during `T1` (`Serial0/0/0` shutdown). The top panel shows the route dropping from Metric 1 (`via 10.0.12.2`) to Missing immediately after the link goes down, then recovering at Metric 2 (`via 10.0.13.2` on `Gi0/1`) when `LAB-R3`'s next periodic update arrives. The bottom panel shows the actual SSH command duration and polling interval for each `show ip route rip` query.

3. **Figure 6a.3 — T2 Route-Change Timeline with SSH Reconnection (`timeline_T2-run1_LIB-R1.png` / `SIMULATED_timeline_T2-run1_LIB-R1.png`):**  
   *Caption:* **Figure 6a.3:** Route-state timeline on `LIB-R1` for `192.168.30.0/24` during `T2` (`GigabitEthernet0/1` shutdown from console). The shaded red region marks the window in which the monitoring laptop's SSH session to `LIB-R1` was severed by the link failure before automatically reconnecting over the newly converged 2-hop path through `TCH-R2` (`via 10.0.12.2`, Metric 2).

4. **Figure 6a.4 — T5 Silent-Failure Timeline (`timeline_T5-run1_LIB-R1.png` / `SIMULATED_timeline_T5-run1_LIB-R1.png`):**  
   *Caption:* **Figure 6a.4:** Route-state timeline on `LIB-R1` for `192.168.30.0/24` during `T5` (`passive-interface Gi0/1` on `LAB-R3`). Because `Gi0/1` remains physically up, the direct Metric 1 route (`via 10.0.13.2`) persists for 180 s after the last update, transitions to Metric 16 hold-down (`is possibly down`) between 180 s and 240 s, is flushed at 240 s, and finally converges to Metric 2 (`via 10.0.12.2`) on `TCH-R2`'s next periodic advertisement.

---

## 6a.6 Evidence Collection Procedure

For each run of `T1`–`T5` (and the single baseline run `T0`), the scripts record and archive five pieces of verifiable evidence in the repository:

1. **Before/after routing-table snapshots (`data/snapshots/<timestamp>_<label>-before.json` and `-after.json` plus `data/routes.csv`):** Captured across all three routers by [`scripts/get_routes.py`](../scripts/get_routes.py) and diffed per network by [`scripts/compare_routes.py`](../scripts/compare_routes.py).
2. **Router buffered debug logs (`data/debug_logs/<test>-run<n>_<router>.txt` and `.meta.json`):** Saved from the router's 64 KB debugging buffer by [`scripts/capture_rip_debug.py`](../scripts/capture_rip_debug.py), together with pre/post `show clock` readings.
3. **Parsed debug event tables (`data/debug_logs/<test>-run<n>_<router>.csv`):** Generated by [`scripts/parse_rip_debug.py`](../scripts/parse_rip_debug.py), listing every sent/received RIP update, `%LINK`/`%LINEPROTO` transition, and `RT: add` / `RT: delete` event with millisecond timestamps.
4. **Polling event and timing logs (`data/monitor_logs/<test>-run<n>_<router>.csv`, `_polls.csv`, and `_summary.json`):** Recorded by [`scripts/monitor_convergence.py`](../scripts/monitor_convergence.py).
5. **Aggregated summary table (`data/results.csv`):** Built automatically by [`scripts/collect_results.py`](../scripts/collect_results.py).

---

## 6a.7 Measurement Limitations and Practical Considerations

1. **Observation time vs. actual network event time in polling:**  
   `monitor_convergence.py` records the laptop clock (`obs_time`) when an SSH reply finishes returning. A change observed at poll $k$ actually occurred somewhere inside the interval $[t_{\text{sent}}(k-1),\, t_{\text{obs}}(k)]$, which is reported as `window_s` in the CSV. Moreover, executing `show ip route rip` over a 64 kbps serial link takes approximately 0.3–1.2 s per command, so real polling intervals can exceed the nominal 1.0 s target. For sub-second accuracy, the router-internal debug log (`parse_rip_debug.py`) is used as the primary convergence figure, and polling serves as an independent cross-check.
2. **In-band SSH disconnection during `T2` and `T3`:**  
   Because `AUTO-PC` sits on the Laboratory LAN (`192.168.30.20`), its normal SSH path to `LIB-R1` traverses `Gi0/1` (`T2`), and its normal SSH path to `TCH-R2` traverses `Se0/0/1` (`T3`). Shutting either interface cuts the live SSH connection until RIP converges around the triangle. Live `monitor_convergence.py` polling is therefore blind during the outage window (logging `session_lost` and `session_restored`), whereas the router's internal 64 KB log buffer (`logging buffered 64000 debugging`) continues recording every `debug ip rip` and `debug ip routing` line uninterrupted and is read back after reconnection.
3. **Unsynchronised clocks across devices:**  
   Lab routers without NTP often boot with clocks starting in 1993 or with several seconds of offset from the laptop. Measuring $t_{\text{end}} - t_{\text{start}}$ strictly from a single router's buffered log eliminates cross-device clock skew, while `capture_rip_debug.py` also records `show clock` before and after each capture to document the offset relative to laptop time.
4. **Cisco IOS hold-down vs. flush timer behaviour in `T5`:**  
   Standard RIP theory states that the 180 s hold-down timer (starting when the 180 s invalid timer expires) ignores worse-metric updates until 360 s, unless the 240 s flush timer deletes the route first. On Cisco IOS 15, the flush timer (240 s) removes the `possibly down` entry 60 s into hold-down, allowing the next periodic update from `TCH-R2` to install the metric-2 route at ~240–270 s. Some IOS releases or emulator images may also clear or replace a route slightly earlier depending on split-horizon/poison-reverse interactions; our scripts record both the `possibly_down` transition and the actual `RT: add` installation time so the report reflects the exact behaviour of the lab routers.
