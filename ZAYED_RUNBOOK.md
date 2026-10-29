# Zayed's Lab Runbook & Demo Guide (CCEN 356 Project 1)

This runbook lists the exact commands to collect every real measurement in the university networking lab and to run Zayed's steps in the live demonstration.

---

## 1. Before You Start (Laptop & Network Setup)

1. **Connect your laptop** to the Laboratory switch (`SW-LAB`) as `AUTO-PC` (`192.168.30.20/24`, default gateway `192.168.30.1`), **or** any port that can reach `192.168.10.1`, `192.168.20.1`, and `192.168.30.1`.
2. **Keep a console cable handy** for `LIB-R1` (required for **T2**) and `TCH-R2` (recommended for **T3**), because shutting the link that carries your SSH session from the Laboratory LAN will drop SSH until RIP converges over the alternate path.
3. **Install dependencies** (from the repository root):

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m pytest
```

4. **Credentials** (defaults match Saif's router configs: `admin` / `Admin@123`, enable secret `class123`). If the lab routers use different credentials, set environment variables in PowerShell instead of editing code:

```powershell
$env:RIP_USER = "admin"
$env:RIP_PASS = "your-lab-password"
$env:RIP_SECRET = "your-enable-secret"
```

---

## 2. One-Time Router Preparation

Run these two commands **once** before the first experiment:

```powershell
# 1. Verify SSH connectivity and hostnames on all 3 routers
python scripts/connect_test.py

# 2. Enable millisecond debug/log timestamps and a 64 KB debugging log buffer on all routers
python scripts/push_config.py configs/push/timestamps.txt all
```

> **Why step 2 matters:** `configs/push/timestamps.txt` adds `service timestamps debug datetime msec`, `service timestamps log datetime msec`, and `logging buffered 64000 debugging`. Without it, `parse_rip_debug.py` has no timestamps to subtract and `T2` / `T3` would lose debug output when the SSH session drops.

---

## 3. Running the Experiments (T0 to T5)

You have **two ways** to run every test:
- **Option A (Recommended — One-Command Guided Runner):** `run_experiment.py` automatically takes the before-snapshot, checks preconditions, clears the router's log buffer, enables `debug ip rip` + `debug ip routing`, runs `monitor_convergence.py`, waits for you to press `Enter` when the failure is applied, reads back `show logging`, runs `undebug all`, takes the after-snapshot, and updates `data/results.csv`.
- **Option B (Manual Individual Scripts):** Run `get_routes.py`, `capture_rip_debug.py`, `monitor_convergence.py`, `parse_rip_debug.py`, and `compare_routes.py` separately.

During each run of **T1–T5**, run a continuous 1-second-timeout ping from a Library PC (`192.168.10.x`) to a Teachers' PC (`192.168.20.x`, for T1/T4) or Laboratory PC (`192.168.30.x`, for T2/T3/T5) in a separate terminal and count the `Request timed out.` lines:

```powershell
ping -t -w 1000 192.168.20.10
```

### T0 — Baseline Routing State (1 run)

```powershell
python scripts/run_experiment.py T0
```

*(Or directly: `python scripts/get_routes.py T0-baseline`)*

### T1 & T4 — Library–Teachers Serial Link Failure and Recovery (3 runs paired)

Because **T4** measures recovery when the **T1** link (`LIB-R1 Serial0/0/0`) comes back up, run `T1` and `T4` back-to-back three times:

```powershell
# --- Pair 1 ---
python scripts/run_experiment.py T1 --run 1
# When prompted: on LIB-R1 run:
#   configure terminal
#   interface Serial0/0/0
#   shutdown
#   end
# Press Enter in the script terminal right as you hit Enter on 'shutdown'.

python scripts/run_experiment.py T4 --run 1
# When prompted: on LIB-R1 run:
#   configure terminal
#   interface Serial0/0/0
#   no shutdown
#   end
# Press Enter in the script terminal right as you hit Enter on 'no shutdown'.

# --- Pair 2 ---
python scripts/run_experiment.py T1 --run 2
python scripts/run_experiment.py T4 --run 2

# --- Pair 3 ---
python scripts/run_experiment.py T1 --run 3
python scripts/run_experiment.py T4 --run 3
```

### T2 — Library–Laboratory Ethernet Link Failure (3 runs, console required)

Shutting `GigabitEthernet0/1` on `LIB-R1` cuts the direct path between `AUTO-PC` and `LIB-R1`. Perform the `shutdown` and `no shutdown` from the **`LIB-R1` console cable**:

```powershell
python scripts/run_experiment.py T2 --run 1
# On LIB-R1 CONSOLE:
#   configure terminal
#   interface GigabitEthernet0/1
#   shutdown
#   end
# After the run finishes, restore on LIB-R1 CONSOLE before Run 2:
#   configure terminal
#   interface GigabitEthernet0/1
#   no shutdown
#   end
# Wait ~45 s for LIB-R1 to return to metric 1 via 10.0.13.2, then run:
python scripts/run_experiment.py T2 --run 2
# Restore and wait ~45 s, then:
python scripts/run_experiment.py T2 --run 3
# Restore GigabitEthernet0/1 after Run 3.
```

### T3 — Teachers–Laboratory Serial Link Failure (3 runs, console recommended)

From `AUTO-PC` (`192.168.30.20`), packets to `TCH-R2` (`192.168.20.1`) travel over `LAB-R3 S0/0/0 -> TCH-R2 S0/0/1`. Shutting `TCH-R2 Serial0/0/1` drops SSH to `TCH-R2` until RIP converges via `LIB-R1`, so use the `TCH-R2` console:

```powershell
python scripts/run_experiment.py T3 --run 1
# On TCH-R2 CONSOLE:
#   configure terminal
#   interface Serial0/0/1
#   shutdown
#   end
# After the run finishes, restore on TCH-R2 CONSOLE:
#   configure terminal
#   interface Serial0/0/1
#   no shutdown
#   end
# Wait ~45 s, then repeat for Run 2 and Run 3:
python scripts/run_experiment.py T3 --run 2
python scripts/run_experiment.py T3 --run 3
```

### T5 — Silent RIP Update Failure (`passive-interface G0/1` on `LAB-R3`, 3 runs)

Each `T5` run records for **330 seconds** (~5.5 minutes): 35 seconds of pre-roll so at least one periodic update from `10.0.13.2` is in the log buffer, followed by the 180 s invalid timer, 240 s flush timer, and backup route installation via `TCH-R2`.

```powershell
python scripts/run_experiment.py T5 --run 1
# Wait ~35 s after monitoring starts so LIB-R1 logs at least one update from 10.0.13.2.
# In a second terminal, push the passive-interface change (without saving):
#   python scripts/push_config.py configs/push/t5-start.txt LAB-R3 --no-save
# Press Enter in the runner terminal when push_config finishes.
#
# After the 330 s run completes, restore LAB-R3:
python scripts/push_config.py configs/push/t5-stop.txt LAB-R3 --no-save
# Wait ~60 s for LIB-R1 to return to metric 1 via 10.0.13.2, then repeat:
python scripts/run_experiment.py T5 --run 2
python scripts/push_config.py configs/push/t5-stop.txt LAB-R3 --no-save
python scripts/run_experiment.py T5 --run 3
python scripts/push_config.py configs/push/t5-stop.txt LAB-R3 --no-save
```

---

## 4. Generating the Final Results Table and Charts

Once all runs are saved in `data/debug_logs/` and `data/monitor_logs/`:

```powershell
# 1. Build data/results.csv and print the Markdown table for Section 6a
#    (Optionally pass your recorded ping-loss counts for each run)
python scripts/collect_results.py --ping `
  T1-run1=2 T1-run2=3 T1-run3=2 `
  T2-run1=3 T2-run2=2 T2-run3=4 `
  T3-run1=2 T3-run2=3 T3-run3=2 `
  T4-run1=0 T4-run2=0 T4-run3=0 `
  T5-run1=1 T5-run2=0 T5-run3=1

# 2. Generate the PNG charts in data/charts/
python scripts/visualize.py
```

This creates:
- `data/results.csv` — full per-run table (debug convergence, monitor first-change, monitor converged, poll interval, session gaps, pings lost)
- `data/charts/convergence_comparison.png` — mean convergence time per test with min–max error bars and individual runs
- `data/charts/timeline_<test>-run1_<router>.png` — route-change step timelines and polling interval plots

---

## 5. Manual Individual Script Reference (Option B)

If you ever need to run a single tool on its own (for example during the live demo):

```powershell
# Record debug ip rip + debug ip routing for 60 s on LIB-R1
python scripts/capture_rip_debug.py LIB-R1 --test T1 --run 1

# Poll LIB-R1 every 1 s and press Enter when the link is shut
python scripts/monitor_convergence.py --test T1 --run 1 --mark-on-enter

# Parse a saved debug log for convergence time
python scripts/parse_rip_debug.py data/debug_logs/T1-run1_LIB-R1.txt --network 192.168.20.0/24 --metric 2
python scripts/parse_rip_debug.py data/debug_logs/T5-run1_LIB-R1.txt --network 192.168.30.0/24 --metric 2 --silent-peer 10.0.13.2

# Compare before/after snapshots
python scripts/compare_routes.py T1-run1-before T1-run1-after
```

---

## 6. Zayed's Steps in the 10-Minute Live Demo

From the Team Plan (v3) demo run-sheet:

1. **Step 4 (Live T1 failure monitoring):**
   Start the live route monitor on `LIB-R1` watching `192.168.20.0/24`:
   ```powershell
   python scripts/monitor_convergence.py LIB-R1 --network 192.168.20.0/24 --expect-metric 2 --duration 60 --mark-on-enter
   ```
   While a teammate shuts `Serial0/0/0` on `LIB-R1`, press `Enter` to mark the failure. Point out the `route_lost` line (immediate removal when `Serial0/0/0` goes down) followed by `route_restored` at metric 2 via `10.0.13.2` (`GigabitEthernet0/1`) when `LAB-R3`'s next update arrives.
2. **Step 7 (Restore link & show charts):**
   Bring `Serial0/0/0` back up (`no shutdown`), then open `data/charts/convergence_comparison.png` and `data/charts/timeline_T5-run1_LIB-R1.png` to show the difference between direct link down events (~0–30 s) and silent neighbour failure (~240–270 s).

---

## 7. Emergency Troubleshooting

- **Debugging left on after a cable pull:** Run `undebug all` and `terminal no monitor` on the router console, or re-run `capture_rip_debug.py <ROUTER> --duration 1`.
- **`parse_rip_debug.py` prints `No convergence found`:** Check that `configs/push/timestamps.txt` was pushed (`service timestamps debug datetime msec`) and that for `T5` you waited at least 30 s before applying `t5-start.txt` so at least one update from `10.0.13.2` appears in the buffer.
