# TtB Miner Farm — V5.2 settings-editor release candidate

TtB Miner Farm monitors multiple headless ESP32 miners with the HenrysCat NerdMiner-Modded V1.5.0+ HTTP status API. It uses the Python standard library and a SQLite database, with no USB ports or serial monitoring.

**Status: test release candidate, not yet a GitHub stable release.** V5 and V5.1 should remain available as rollback options until this build has been tested on the Pi and phone.

## New in V5.2

- **Working Settings editor** on the dashboard: edit farm title, wallet, polling intervals, individual miner display names, IP/hostnames, expected hashrates, and allowed upper-range factors. Add, temporarily disable, or remove miners from the monitored list.
- The miner's **stable ID is never changed when you rename the display name**, preserving association with earlier historical samples.
- Monitoring and viewing the dashboard remain accessible without a password. **Saving settings requires a long, private admin key**, configured via `--admin-key-file`. If no key file is configured, editing is disabled.
- Saves are validated server-side, backed up to `farm_config.json.backup-*`, and atomically written. If another editor or program changed the config since you opened Settings, Save is rejected until you reload.
- All changes take effect automatically without restarting the server. Configuration backups are private files.
- Existing V5.1 outlier filtering, farm + Public Pool graph, individual miner status, SQLite history, and activity events remain.

**IMPORTANT:** The editor modifies **only the monitor's JSON configuration**. It does **not** change an ESP32's Wi-Fi password/SSID, firmware, Bitcoin pool credentials, or mining configuration. Those must still be configured on each miner's own firmware web page.

## A: New installation

Run this on a Pi or Linux host that can reach the miners:

```bash
cp config.example.json farm_config.json
python3 setup_admin_key.py
python3 ttb_miner_farm_v5.py --config farm_config.json --db miner_farm_v52.sqlite3 --port 8082 --admin-key-file admin_key.txt
```

Open `http://<pi-address>:8082`. Click **Settings**. To save, read the key on the Pi with `cat admin_key.txt` and paste it into the **Admin key to save** box. Don't share the key or include it in screenshots. Browser sessions do **not** save it in localStorage or cookies.

If you want a view-only dashboard without the ability to edit, start the application without `--admin-key-file`.

## B: Safe upgrade for the existing two-dashboard Pi setup

The Pi has a production V5 monitor on **8081** and a manually started V5.1 test monitor on **8082**. To upgrade the test version while **preserving its history**, copy `farm_config.json` from V5.1 into this V5.2 directory, **stop V5.1 with Ctrl+C**, and start V5.2 on **the same port 8082**, pointing `--db` at V5.1's existing test database.

From inside `~/miner-farm/ttb-miner-farm-v5.2`:

```bash
cp ../ttb-miner-farm-v5.1/farm_config.json ./farm_config.json
python3 setup_admin_key.py
python3 ttb_miner_farm_v5.py \
  --config farm_config.json \
  --db ../ttb-miner-farm-v5.1/miner_farm_v51_test.sqlite3 \
  --port 8082 --admin-key-file admin_key.txt
```

**Never run two monitor processes concurrently against the same SQLite database**, and do not change the existing production V5 service or database during this test. To keep separate V5.2 history instead, use `--db miner_farm_v52_test.sqlite3`.

## Using Settings

- Farm name appears as the dashboard heading.
- Miner **ID** should be permanent; change the **Name** field to rename a miner in the table and popup.
- IP/hostname changes point the monitor at a different device; they do not reconfigure an ESP32.
- Expected speed (kH/s) and Max factor set the implausible-hashrate safeguard. `990` and `3.0` are reasonable defaults for this particular ESP32 farm.
- Disable a miner to take it out of the current online count without deleting its configuration; Remove takes it out of the configured miner list but does not delete historical rows.
- Changes are applied to the file only after **Save changes** returns success.
- Backups are made at each successful save. They contain the wallet, miner IPs and monitoring configuration; keep private.

## Security limitations

- **Do not port-forward the dashboard to the public internet.** Keep it on a trusted LAN or private VPN such as Tailscale.
- The default site is **plain HTTP**. Do **not** enter the admin key from a hostile/untrusted Wi-Fi network without Tailscale or an authenticated HTTPS proxy. Tailscale protects traffic between the devices, but accessing the Pi through its ordinary `192.168.x.x` address is plain HTTP on the LAN.
- An editor admin key is required for config changes. Use the long random key from `setup_admin_key.py`; permissions should be `600`.
- The monitor does not proxy incoming commands to ESP32s. It only fetches their status endpoints and reads Public Pool's API.
- The monitoring URL itself is not access-controlled. If the network or server is shared with untrusted users, use access controls around the dashboard, not just the settings editor.
- An admin can configure new miner IPs, which causes the monitor to make HTTP requests to those addresses. Share the key only with trusted administrators.

## Configuration and history

The JSON file stores all the editable fields, and `config.example.json` is a privacy-safe template with imaginary device IPs. The program preserves unrecognized extension fields when saving an existing miner under the same stable ID. A stale editor will not overwrite external file changes; reload the editor and try again.

The 24-hour farm average requires 23+ hours of successful readings. The green graph line is a sum of measured miner hashrates, blue dashed is Public Pool's independent one-hour share-based estimate, and yellow marks partial miner results. Implausibly high readings are excluded but not erased from historical data. Accepted-share totals shown on miner cards are firmware session counters; wallet totals come separately from Public Pool.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

Tests cover outlier rejection, stable miner naming, config backup and atomic writes, edit conflicts, rejecting invalid config, and the protected settings API.

## GitHub release checklist

- **Keep out of GitHub:** `farm_config.json`, `admin_key.txt`, backup settings files, database files, logs. The included `.gitignore` covers these.
- Use only the sample configuration and screenshots that do not show identifying home-network details.
- Verify project attribution and determine the license before public publication.
- Test actual browsers on desktop and mobile, including invalid admin keys and changes to miner names.
- Avoid claiming ESP32 firmware changes: this software is a monitor and a monitor-settings editor only.
